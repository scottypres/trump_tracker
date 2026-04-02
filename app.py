"""
Trump Tracker - VIP NOTAM Location Tracker

Determines the likely location of the President based on:
1. FAA TFRs with VIP designations / FAR 91.141 (~30 NM diameter)
2. White House public schedule (daily events, travel, location cues)

Data flow:
- Fetch TFR list from tfr.faa.gov, parse XML detail files for airspace shapes
- Fetch White House public schedule for event/travel context
- Combine signals to determine likely presidential location
"""

import json
import re
import time
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Cache TFR data for 5 minutes, schedule for 30 minutes
_cache = {"data": None, "timestamp": 0}
_schedule_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 300
SCHEDULE_CACHE_TTL = 1800

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)"}

# Known Trump-associated locations for labeling
KNOWN_LOCATIONS = {
    "Mar-a-Lago": (26.677, -80.037),
    "White House": (38.8977, -77.0365),
    "Camp David": (39.648, -77.463),
    "Bedminster": (40.654, -74.631),
    "Trump Tower NYC": (40.7625, -73.9742),
    "Joint Base Andrews": (38.811, -76.867),
    "Trump National Doral": (25.812, -80.339),
    "Trump International Las Vegas": (36.128, -115.169),
}


# ---------------------------------------------------------------------------
# FAA TFR XML feed (primary source - structured data)
# ---------------------------------------------------------------------------

def _fetch_tfr_list_page():
    """Fetch the FAA TFR list page and extract NOTAM IDs."""
    # The FAA TFR list page contains a table with links to detail pages
    # Each detail page has a corresponding XML file
    url = "https://tfr.faa.gov/tfr2/list.html"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    notam_ids = []

    # Extract NOTAM IDs from the table - links like "detail_X_YYYY.html"
    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.search(r'detail_(\d+_\d+)', href)
        if match:
            notam_ids.append(match.group(1))

    # Also try the saved list page which sometimes has more entries
    try:
        resp2 = requests.get(
            "https://tfr.faa.gov/save_pages/detail_6_SavedList.html",
            headers=HEADERS, timeout=15,
        )
        for link in BeautifulSoup(resp2.text, "html.parser").find_all("a", href=True):
            match = re.search(r'detail_(\d+_\d+)', link["href"])
            if match and match.group(1) not in notam_ids:
                notam_ids.append(match.group(1))
    except Exception:
        pass

    return notam_ids


def _fetch_tfr_xml(notam_id):
    """Fetch and parse the XML detail for a single TFR.

    The FAA stores TFR details as XML files at predictable URLs:
    https://tfr.faa.gov/save_pages/detail_{notam_id}.xml
    """
    url = f"https://tfr.faa.gov/save_pages/detail_{notam_id}.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return None

    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError:
        return None

    return root


def _parse_tfr_xml(root, notam_id):
    """Parse a TFR XML document into structured data.

    Returns a list of TFR entries (one TFR can have multiple airspace groups).
    Only returns entries that match VIP/91.141 criteria.
    """
    results = []

    # The XML uses a namespace - find it
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # Check if this is a 91.141 VIP TFR
    full_text = ElementTree.tostring(root, encoding="unicode", method="text")
    is_vip = _is_vip_text(full_text)

    if not is_vip:
        return []

    # Extract NOTAM number
    notam_number = ""
    for elem in root.iter():
        if "txtDescrTraditional" in elem.tag or "txtDescrUSNS" in elem.tag:
            if elem.text:
                id_match = re.search(r'(FDC\s*\d+/\d+|\d+/\d+)', elem.text)
                if id_match:
                    notam_number = id_match.group(1)
                    break

    # Extract effective dates
    effective_start = _find_xml_text(root, ns, [
        "dateEffective", "codeTimeZone", "txtDescrModifiedArea",
    ])
    effective_end = ""
    for elem in root.iter():
        tag = elem.tag.replace(ns, "")
        if tag == "dateEffective" and elem.text:
            effective_start = elem.text
        elif tag == "dateExpire" and elem.text:
            effective_end = elem.text

    # Extract airspace shapes (circles with coordinates)
    for elem in root.iter():
        tag = elem.tag.replace(ns, "")

        if tag == "TFRAreaGroup" or tag == "abdMergedArea" or tag == "Group":
            shapes = _extract_shapes_from_group(elem, ns)
            for shape in shapes:
                if shape["type"] == "circle":
                    radius_nm = shape["radius_nm"]
                    lat = shape["lat"]
                    lon = shape["lon"]

                    nearest = _find_nearest_location(lat, lon)

                    results.append({
                        "notam_id": notam_number or f"FDC {notam_id.replace('_', '/')}",
                        "lat": lat,
                        "lon": lon,
                        "radius_nm": radius_nm,
                        "radius_km": round(radius_nm * 1.852, 1),
                        "effective_start": effective_start,
                        "effective_end": effective_end,
                        "nearest_known_location": nearest,
                        "source_url": f"https://tfr.faa.gov/save_pages/detail_{notam_id}.html",
                        "raw_type": "FAA TFR XML",
                        "is_inner_ring": radius_nm <= 12,
                    })

    # If no shapes found via XML structure, fall back to text parsing
    if not results:
        tfr = _parse_tfr_from_text(full_text, notam_id, notam_number)
        if tfr:
            results.append(tfr)

    return results


def _extract_shapes_from_group(group_elem, ns):
    """Extract circle shapes from a TFR area group XML element."""
    shapes = []

    for elem in group_elem.iter():
        tag = elem.tag.replace(ns, "")

        # Look for circle definitions
        if tag in ("txtNameCircle", "abdCircle", "Circle"):
            lat = lon = radius = None

            for child in elem.iter():
                ctag = child.tag.replace(ns, "")
                text = (child.text or "").strip()

                if ctag in ("geoLat", "latitude", "lat") and text:
                    lat = _parse_coordinate(text, is_lat=True)
                elif ctag in ("geoLong", "longitude", "long", "lon") and text:
                    lon = _parse_coordinate(text, is_lat=False)
                elif ctag in ("valRadiusArc", "radius", "Radius") and text:
                    try:
                        radius = float(text)
                    except ValueError:
                        pass
                elif ctag in ("codeDistVerUpper",):
                    pass  # altitude info

            if lat is not None and lon is not None and radius is not None:
                shapes.append({
                    "type": "circle",
                    "lat": lat,
                    "lon": lon,
                    "radius_nm": radius,
                })

    # Also try to find coordinates at the group level
    if not shapes:
        lat = lon = radius = None
        for elem in group_elem.iter():
            tag = elem.tag.replace(ns, "")
            text = (elem.text or "").strip()

            if tag in ("geoLat", "latitude") and text and lat is None:
                lat = _parse_coordinate(text, is_lat=True)
            elif tag in ("geoLong", "longitude") and text and lon is None:
                lon = _parse_coordinate(text, is_lat=False)
            elif tag in ("valRadiusArc", "radius") and text and radius is None:
                try:
                    radius = float(text)
                except ValueError:
                    pass

        if lat is not None and lon is not None and radius is not None:
            shapes.append({
                "type": "circle",
                "lat": lat,
                "lon": lon,
                "radius_nm": radius,
            })

    return shapes


def _parse_coordinate(text, is_lat=True):
    """Parse a coordinate string in various formats to decimal degrees.

    Handles:
    - Decimal: "26.677"
    - DMS with direction: "264024N" or "0800212W"
    - DM with direction: "2640N" or "08002W"
    - Degrees minutes: "26-40-24N"
    """
    text = text.strip()

    # Try simple decimal
    try:
        val = float(text)
        return val
    except ValueError:
        pass

    # Try DMS format: 264024N or 0800212W
    if is_lat:
        match = re.match(r'(\d{2})(\d{2})(\d{2}(?:\.\d+)?)\s*([NS])', text)
    else:
        match = re.match(r'(\d{3})(\d{2})(\d{2}(?:\.\d+)?)\s*([EW])', text)

    if match:
        deg = int(match.group(1))
        mins = int(match.group(2))
        secs = float(match.group(3))
        direction = match.group(4)
        val = deg + mins / 60.0 + secs / 3600.0
        if direction in ("S", "W"):
            val = -val
        return val

    # Try DM format: 2640N or 08002W
    if is_lat:
        match = re.match(r'(\d{2})(\d{2})\s*([NS])', text)
    else:
        match = re.match(r'(\d{3})(\d{2})\s*([EW])', text)

    if match:
        deg = int(match.group(1))
        mins = int(match.group(2))
        direction = match.group(3)
        val = deg + mins / 60.0
        if direction in ("S", "W"):
            val = -val
        return val

    # Try dash-separated: 26-40-24N
    match = re.match(r'(\d+)-(\d+)-(\d+(?:\.\d+)?)\s*([NSEW])', text)
    if match:
        deg = int(match.group(1))
        mins = int(match.group(2))
        secs = float(match.group(3))
        direction = match.group(4)
        val = deg + mins / 60.0 + secs / 3600.0
        if direction in ("S", "W"):
            val = -val
        return val

    return None


def _is_vip_text(text):
    """Check if text content indicates a VIP/presidential TFR."""
    text_upper = text.upper()
    # FAR 91.141 is the definitive marker for presidential TFRs
    if "91.141" in text_upper:
        return True
    # Also check for explicit VIP + TFR combination
    if "VIP" in text_upper and ("TFR" in text_upper or "FLIGHT RESTRICTION" in text_upper):
        return True
    return False


def _find_xml_text(root, ns, tag_names):
    """Find the first matching text content in XML by tag name."""
    for elem in root.iter():
        tag = elem.tag.replace(ns, "")
        if tag in tag_names and elem.text:
            return elem.text.strip()
    return ""


def _parse_tfr_from_text(text, notam_id, notam_number):
    """Fallback: parse TFR coordinates from raw text content."""
    # Try NOTAM coordinate format: 264024N0800212W
    coord_match = re.search(r'(\d{6})([NS])\s*(\d{7})([EW])', text)
    if coord_match:
        lat = _parse_coordinate(coord_match.group(1) + coord_match.group(2), is_lat=True)
        lon = _parse_coordinate(coord_match.group(3) + coord_match.group(4), is_lat=False)
    else:
        # Try shorter format: 2640N08002W
        coord_match = re.search(r'(\d{4})([NS])\s*(\d{5})([EW])', text)
        if coord_match:
            lat = _parse_coordinate(coord_match.group(1) + coord_match.group(2), is_lat=True)
            lon = _parse_coordinate(coord_match.group(3) + coord_match.group(4), is_lat=False)
        else:
            return None

    if lat is None or lon is None:
        return None

    # Extract radius
    radius_nm = 30  # Default for presidential TFR outer ring
    radius_match = re.search(r'(\d+)\s*(?:NAUTICAL\s*MILE|NM)\s*RADIUS', text, re.IGNORECASE)
    if radius_match:
        radius_nm = int(radius_match.group(1))

    nearest = _find_nearest_location(lat, lon)

    return {
        "notam_id": notam_number or f"FDC {notam_id.replace('_', '/')}",
        "lat": lat,
        "lon": lon,
        "radius_nm": radius_nm,
        "radius_km": round(radius_nm * 1.852, 1),
        "effective_start": "",
        "effective_end": "",
        "nearest_known_location": nearest,
        "source_url": f"https://tfr.faa.gov/save_pages/detail_{notam_id}.html",
        "raw_type": "FAA TFR text fallback",
        "is_inner_ring": radius_nm <= 12,
    }


# ---------------------------------------------------------------------------
# FAA NOTAM search API (secondary source)
# ---------------------------------------------------------------------------

def _fetch_notam_search():
    """Fetch VIP NOTAMs from the FAA NOTAM search API."""
    tfrs = []

    url = "https://notams.aim.faa.gov/notamSearch/search"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        **HEADERS,
    }

    data = {
        "searchType": 0,
        "designatorsForLocationIdentifier": "",
        "notamType": "N",
        "operationsType": "",
        "quickSearch": "91.141",
        "radiusSearchNm": "",
        "radiusSearchLatitudeDirection": "N",
        "radiusSearchLongitudeDirection": "W",
        "latDegrees": "",
        "latMinutes": "",
        "latSeconds": "",
        "longDegrees": "",
        "longMinutes": "",
        "longSeconds": "",
        "formatType": "DOMESTIC",
        "archiveSearch": "false",
        "archiveDate": "",
    }

    resp = requests.post(url, data=data, headers=headers, timeout=15)
    if resp.status_code != 200:
        return []

    try:
        results = resp.json()
    except (json.JSONDecodeError, ValueError):
        return []

    for notam in results.get("notamList", []):
        text = notam.get("icaoMessage", "") or notam.get("traditionalMessage", "")
        if not _is_vip_text(text):
            continue

        # Extract coordinates
        lat = lon = None
        for pattern, is_long_form in [
            (r'(\d{6})([NS])\s*(\d{7})([EW])', True),
            (r'(\d{4})([NS])\s*(\d{5})([EW])', False),
        ]:
            coord_match = re.search(pattern, text)
            if coord_match:
                lat = _parse_coordinate(
                    coord_match.group(1) + coord_match.group(2), is_lat=True
                )
                lon = _parse_coordinate(
                    coord_match.group(3) + coord_match.group(4), is_lat=False
                )
                break

        if lat is None or lon is None:
            continue

        radius_nm = 30
        radius_match = re.search(r'(\d+)\s*(?:NAUTICAL\s*MILE|NM)\s*RADIUS', text, re.IGNORECASE)
        if radius_match:
            radius_nm = int(radius_match.group(1))

        nearest = _find_nearest_location(lat, lon)
        notam_id = str(notam.get("notamNumber", notam.get("id", "")))

        tfrs.append({
            "notam_id": notam_id,
            "lat": lat,
            "lon": lon,
            "radius_nm": radius_nm,
            "radius_km": round(radius_nm * 1.852, 1),
            "effective_start": notam.get("startDate", ""),
            "effective_end": notam.get("endDate", ""),
            "nearest_known_location": nearest,
            "source_url": "https://notams.aim.faa.gov/notamSearch/",
            "raw_type": "NOTAM Search API",
            "raw_text": text[:500],
            "is_inner_ring": radius_nm <= 12,
        })

    return tfrs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_nearest_location(lat, lon):
    """Find the nearest known Trump-associated location."""
    best_name = None
    best_dist = float("inf")

    for name, (klat, klon) in KNOWN_LOCATIONS.items():
        R = 6371  # Earth radius in km
        dlat = radians(klat - lat)
        dlon = radians(klon - lon)
        a = (sin(dlat / 2) ** 2
             + cos(radians(lat)) * cos(radians(klat)) * sin(dlon / 2) ** 2)
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        d = R * c

        if d < best_dist:
            best_dist = d
            best_name = name

    if best_dist < 100:  # Within 100 km
        return {"name": best_name, "distance_km": round(best_dist, 1)}
    return None


# ---------------------------------------------------------------------------
# Main fetch + filter logic
# ---------------------------------------------------------------------------

def fetch_tfr_data():
    """Fetch current TFR data from all FAA sources."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    tfrs = []

    # Source 1: FAA TFR XML feed (structured, reliable)
    try:
        notam_ids = _fetch_tfr_list_page()
        for nid in notam_ids:
            try:
                root = _fetch_tfr_xml(nid)
                if root is not None:
                    parsed = _parse_tfr_xml(root, nid)
                    tfrs.extend(parsed)
            except Exception as e:
                print(f"Error parsing TFR {nid}: {e}")
    except Exception as e:
        print(f"Error fetching TFR list: {e}")

    # Source 2: FAA NOTAM search API
    try:
        tfrs.extend(_fetch_notam_search())
    except Exception as e:
        print(f"Error fetching NOTAM search: {e}")

    # Deduplicate by coordinates (within ~1km)
    unique = []
    seen_coords = []
    for tfr in tfrs:
        is_dup = False
        for slat, slon, sr in seen_coords:
            if (abs(tfr["lat"] - slat) < 0.01
                    and abs(tfr["lon"] - slon) < 0.01
                    and abs(tfr["radius_nm"] - sr) < 2):
                is_dup = True
                break
        if not is_dup:
            unique.append(tfr)
            seen_coords.append((tfr["lat"], tfr["lon"], tfr["radius_nm"]))

    _cache["data"] = unique
    _cache["timestamp"] = now
    return unique


def get_vip_tfrs():
    """Get VIP TFRs indicating presidential location.

    Presidential TFRs have two rings:
    - Outer: 30 NM radius (no-fly for most GA)
    - Inner: ~10 NM radius (total exclusion)

    We return the outer ring (30 NM) as the primary indicator,
    but also include inner rings marked as such.
    """
    all_tfrs = fetch_tfr_data()
    # All fetched TFRs are already filtered for 91.141/VIP
    return all_tfrs


# ---------------------------------------------------------------------------
# White House public schedule (secondary location signal)
# ---------------------------------------------------------------------------

# Travel keywords that suggest the President is NOT at the White House
_TRAVEL_KEYWORDS = [
    "departs", "arrives", "travel", "mar-a-lago", "palm beach",
    "camp david", "bedminster", "new york", "air force one",
    "joint base andrews", "marine one", "aboard", "en route",
    "international trip", "state visit", "foreign travel",
]

# Location keywords mapped to known locations
_LOCATION_HINTS = {
    "mar-a-lago": "Mar-a-Lago",
    "palm beach": "Mar-a-Lago",
    "camp david": "Camp David",
    "bedminster": "Bedminster",
    "trump tower": "Trump Tower NYC",
    "new york": "Trump Tower NYC",
    "doral": "Trump National Doral",
    "las vegas": "Trump International Las Vegas",
    "joint base andrews": "Joint Base Andrews",
    "white house": "White House",
    "oval office": "White House",
    "rose garden": "White House",
    "east room": "White House",
    "state dining": "White House",
    "south lawn": "White House",
    "blair house": "White House",
}


def fetch_schedule():
    """Fetch the presidential public schedule from multiple sources."""
    now = time.time()
    if (_schedule_cache["data"] is not None
            and (now - _schedule_cache["timestamp"]) < SCHEDULE_CACHE_TTL):
        return _schedule_cache["data"]

    schedule = {
        "events": [],
        "inferred_location": "White House",
        "location_confidence": "low",
        "travel_detected": False,
        "source": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Try multiple sources in order of preference
    for fetcher in [_fetch_wh_schedule, _fetch_wh_rss, _fetch_factbase_schedule]:
        try:
            result = fetcher()
            if result and result.get("events"):
                schedule["events"] = result["events"]
                schedule["source"] = result["source"]
                break
        except Exception as e:
            print(f"Schedule fetch error ({fetcher.__name__}): {e}")

    # Analyze events for location signals
    if schedule["events"]:
        _analyze_schedule_location(schedule)

    _schedule_cache["data"] = schedule
    _schedule_cache["timestamp"] = now
    return schedule


def _fetch_wh_schedule():
    """Fetch schedule from whitehouse.gov."""
    # The WH schedule page is a WordPress site; try the REST API
    # WordPress exposes posts at /wp-json/wp/v2/posts with category filtering
    urls_to_try = [
        # WP REST API for schedule/briefing posts
        "https://www.whitehouse.gov/wp-json/wp/v2/pages?slug=presidential-actions",
        "https://www.whitehouse.gov/wp-json/wp/v2/posts?per_page=10&categories=6",  # daily schedule category
        "https://www.whitehouse.gov/wp-json/wp/v2/posts?per_page=10&search=schedule",
        # Direct schedule page
        "https://www.whitehouse.gov/presidential-actions/",
        "https://www.whitehouse.gov/briefing-room/statements-releases/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/json;q=0.8,*/*;q=0.7",
    }

    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue

            # Try JSON (WP REST API)
            if "wp-json" in url:
                posts = resp.json()
                if isinstance(posts, list) and posts:
                    events = []
                    for post in posts[:10]:
                        title = post.get("title", {}).get("rendered", "")
                        content = post.get("content", {}).get("rendered", "")
                        date = post.get("date", "")
                        # Strip HTML tags for text analysis
                        clean = re.sub(r'<[^>]+>', ' ', content)
                        events.append({
                            "title": re.sub(r'<[^>]+>', '', title),
                            "description": clean[:500].strip(),
                            "date": date,
                            "time": "",
                            "location": "",
                        })
                    if events:
                        return {"events": events, "source": "whitehouse.gov API"}

            # Try HTML
            soup = BeautifulSoup(resp.text, "html.parser")
            events = _parse_wh_html_schedule(soup)
            if events:
                return {"events": events, "source": "whitehouse.gov"}

        except Exception:
            continue

    return None


def _parse_wh_html_schedule(soup):
    """Parse schedule events from White House HTML page."""
    events = []

    # Look for article/entry elements (WordPress theme patterns)
    for selector in [
        "article", ".briefing-statement", ".presidential-action",
        ".news-item", ".entry-content", ".schedule-item",
        ".daily-schedule-item", "li.listing-item",
    ]:
        items = soup.select(selector)
        if not items:
            continue

        for item in items[:15]:
            title_el = item.select_one(
                "h2, h3, .title, .entry-title, a"
            )
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            desc_el = item.select_one(
                ".entry-content, .description, .excerpt, p"
            )
            desc = desc_el.get_text(strip=True)[:500] if desc_el else ""

            time_el = item.select_one(
                "time, .date, .time, .schedule-time"
            )
            event_time = time_el.get_text(strip=True) if time_el else ""
            event_date = ""
            if time_el and time_el.get("datetime"):
                event_date = time_el["datetime"]

            events.append({
                "title": title,
                "description": desc,
                "date": event_date,
                "time": event_time,
                "location": "",
            })

        if events:
            break

    return events


def _fetch_wh_rss():
    """Fetch schedule from White House RSS/Atom feeds."""
    feed_urls = [
        "https://www.whitehouse.gov/feed/",
        "https://www.whitehouse.gov/briefing-room/feed/",
        "https://www.whitehouse.gov/briefing-room/statements-releases/feed/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml",
    }

    for url in feed_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue

            root = ElementTree.fromstring(resp.content)

            # Handle RSS 2.0
            events = []
            for item in root.iter("item"):
                title = item.findtext("title", "")
                desc = item.findtext("description", "")
                pub_date = item.findtext("pubDate", "")
                clean_desc = re.sub(r'<[^>]+>', ' ', desc)

                events.append({
                    "title": title.strip(),
                    "description": clean_desc[:500].strip(),
                    "date": pub_date.strip(),
                    "time": "",
                    "location": "",
                })

            if events:
                return {"events": events[:10], "source": "whitehouse.gov RSS"}

        except Exception:
            continue

    return None


def _fetch_factbase_schedule():
    """Fetch schedule from Factba.se (third-party tracker)."""
    url = "https://factba.se/biden/calendar"  # They track current president
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        # Try their API endpoint
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        api_url = f"https://factba.se/json/calendar?date={today}"
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            events = []
            for item in (data if isinstance(data, list) else data.get("data", [])):
                events.append({
                    "title": item.get("title", item.get("details", "")),
                    "description": item.get("details", ""),
                    "date": item.get("date", today),
                    "time": item.get("time", ""),
                    "location": item.get("location", ""),
                })
            if events:
                return {"events": events, "source": "factba.se"}
    except Exception:
        pass

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select(".calendar-item, .schedule-entry, tr, .event"):
                text = item.get_text(" ", strip=True)
                if len(text) > 10:
                    events.append({
                        "title": text[:200],
                        "description": "",
                        "date": "",
                        "time": "",
                        "location": "",
                    })
            if events:
                return {"events": events[:10], "source": "factba.se"}
    except Exception:
        pass

    return None


def _analyze_schedule_location(schedule):
    """Analyze schedule events to infer presidential location."""
    all_text = " ".join(
        f"{e.get('title', '')} {e.get('description', '')} {e.get('location', '')}"
        for e in schedule["events"]
    ).lower()

    # Check for travel indicators
    travel_detected = any(kw in all_text for kw in _TRAVEL_KEYWORDS)
    schedule["travel_detected"] = travel_detected

    # Try to identify specific location
    best_location = None
    best_priority = -1

    # Priority: explicit location mentions > travel keywords > default
    for keyword, location_name in _LOCATION_HINTS.items():
        if keyword in all_text:
            # Non-WH locations get higher priority (more interesting signal)
            priority = 2 if location_name != "White House" else 1
            if priority > best_priority:
                best_priority = priority
                best_location = location_name

    if best_location:
        schedule["inferred_location"] = best_location
        schedule["location_confidence"] = "medium" if best_priority >= 2 else "low"
    elif travel_detected:
        schedule["inferred_location"] = "Traveling"
        schedule["location_confidence"] = "medium"
    else:
        schedule["inferred_location"] = "White House"
        schedule["location_confidence"] = "low"

    # If we have explicit location in event data, boost confidence
    for event in schedule["events"]:
        if event.get("location"):
            loc_lower = event["location"].lower()
            for keyword, location_name in _LOCATION_HINTS.items():
                if keyword in loc_lower:
                    schedule["inferred_location"] = location_name
                    schedule["location_confidence"] = "high"
                    return


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tfrs")
def api_tfrs():
    """API endpoint returning current VIP TFRs."""
    try:
        tfrs = get_vip_tfrs()
        return jsonify({
            "status": "ok",
            "count": len(tfrs),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "tfrs": tfrs,
            "note": (
                "VIP TFRs under FAR 91.141 indicate likely presidential location. "
                "Presidential TFRs have a 30 NM radius outer ring and ~10 NM inner ring. "
                "Data sourced from FAA. Results may be delayed by a few minutes."
            ),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/schedule")
def api_schedule():
    """API endpoint returning the White House public schedule."""
    try:
        schedule = fetch_schedule()
        return jsonify({"status": "ok", **schedule})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/location")
def api_location():
    """Combined endpoint: best guess at presidential location using all signals."""
    try:
        tfrs = get_vip_tfrs()
        schedule = fetch_schedule()

        # TFRs are the strongest signal
        if tfrs:
            # Use the first outer-ring TFR as primary
            primary = next((t for t in tfrs if not t.get("is_inner_ring")), tfrs[0])
            location = {
                "lat": primary["lat"],
                "lon": primary["lon"],
                "name": (primary.get("nearest_known_location") or {}).get("name", "Unknown"),
                "confidence": "high",
                "source": "VIP TFR (FAR 91.141)",
                "details": f"Active TFR: {primary.get('notam_id', 'N/A')}",
            }
        elif schedule.get("inferred_location") and schedule["inferred_location"] != "Traveling":
            loc_name = schedule["inferred_location"]
            coords = KNOWN_LOCATIONS.get(loc_name, (38.8977, -77.0365))
            location = {
                "lat": coords[0],
                "lon": coords[1],
                "name": loc_name,
                "confidence": schedule.get("location_confidence", "low"),
                "source": f"White House schedule ({schedule.get('source', 'unknown')})",
                "details": schedule["events"][0]["title"] if schedule.get("events") else "",
            }
        else:
            # Default: White House
            location = {
                "lat": 38.8977,
                "lon": -77.0365,
                "name": "White House",
                "confidence": "default",
                "source": "No active travel signals detected",
                "details": "No VIP TFRs or travel schedule entries found. "
                           "Defaulting to White House (P-56 permanent TFR).",
            }

        return jsonify({
            "status": "ok",
            "location": location,
            "tfr_count": len(tfrs),
            "schedule_available": bool(schedule.get("events")),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
