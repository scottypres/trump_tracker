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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Cache TFR data for 5 minutes, schedule for 30 minutes,
# news/social for 10 minutes
_cache = {"data": None, "timestamp": 0}
_schedule_cache = {"data": None, "timestamp": 0}
_news_cache = {"data": None, "timestamp": 0}
_social_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 300
SCHEDULE_CACHE_TTL = 1800
NEWS_CACHE_TTL = 600
SOCIAL_CACHE_TTL = 600

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)"}
REQUEST_TIMEOUT = 6  # seconds - fail fast on unreachable hosts

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
# FAA TFR feed (primary source - structured data)
# List comes from https://tfr.faa.gov/tfr3/export/{json,xml};
# per-NOTAM details are fetched from https://tfr.faa.gov/save_pages/detail_*.xml
# ---------------------------------------------------------------------------

_NOTAM_ID_FIELDS = (
    "notam_id", "notamId", "NotamId", "NOTAM_ID",
    "save_page_id", "savePageId", "id",
)
_NOTAM_NUMBER_FIELDS = (
    "notam", "notamNumber", "NotamNumber", "notam_number",
)


def _normalize_notam_id(raw):
    """Normalize a NOTAM identifier to the ``N_NNNN`` form used in detail URLs."""
    if not raw:
        return None
    s = str(raw).strip()
    # Accept values like "FDC 5/1234", "5/1234", "5_1234", "5-1234"
    m = re.search(r'(\d+)[\s_/\-]+(\d+)', s)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    m = re.fullmatch(r'\d+_\d+', s)
    if m:
        return s
    return None


def _fetch_tfr_list_page():
    """Fetch the FAA TFR list and extract NOTAM IDs.

    Uses the FAA TFR3 export endpoints (tfr2 list page is no longer
    maintained). Tries JSON first and falls back to XML.
    """
    notam_ids = []
    seen = set()

    def _add(nid):
        norm = _normalize_notam_id(nid)
        if norm and norm not in seen:
            seen.add(norm)
            notam_ids.append(norm)

    # Primary: JSON export
    try:
        resp = requests.get(
            "https://tfr.faa.gov/tfr3/export/json",
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()

        # The payload may be a list, or a dict wrapping a list
        if isinstance(payload, dict):
            for key in ("tfrs", "TFRs", "data", "items", "results"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break

        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                found = None
                for field in _NOTAM_ID_FIELDS:
                    if entry.get(field):
                        found = entry[field]
                        break
                if not found:
                    for field in _NOTAM_NUMBER_FIELDS:
                        if entry.get(field):
                            found = entry[field]
                            break
                _add(found)
    except Exception as e:
        print(f"TFR JSON export failed, falling back to XML: {e}")

    # Fallback: XML export
    if not notam_ids:
        try:
            resp = requests.get(
                "https://tfr.faa.gov/tfr3/export/xml",
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            try:
                root = ElementTree.fromstring(resp.content)
            except ElementTree.ParseError:
                root = None
            if root is not None:
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1].lower()
                    if tag in {f.lower() for f in _NOTAM_ID_FIELDS + _NOTAM_NUMBER_FIELDS}:
                        _add(elem.text)
        except Exception as e:
            print(f"TFR XML export failed: {e}")

    return notam_ids


def _fetch_tfr_xml(notam_id):
    """Fetch and parse the XML detail for a single TFR.

    The FAA stores TFR details as XML files at predictable URLs:
    https://tfr.faa.gov/save_pages/detail_{notam_id}.xml
    """
    url = f"https://tfr.faa.gov/save_pages/detail_{notam_id}.xml"
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
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

    resp = requests.post(url, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
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
    """Fetch recent White House activity from whitehouse.gov.

    Note: whitehouse.gov has no public /schedule/ page and its WP REST API
    requires authentication. We scrape the public listing pages instead:
    - /briefing-room/statements-releases/ (travel announcements, pool reports)
    - /presidential-actions/ (executive actions with location context)
    - /news/ (general news with travel/location cues)

    The site has bot detection, so we use browser-like headers. Fetches may
    fail from server environments - that's why we have fallback sources.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
    }

    # Pages most likely to contain travel/location info
    urls_to_try = [
        ("https://www.whitehouse.gov/briefing-room/statements-releases/",
         "whitehouse.gov statements"),
        ("https://www.whitehouse.gov/news/",
         "whitehouse.gov news"),
        ("https://www.whitehouse.gov/presidential-actions/",
         "whitehouse.gov actions"),
    ]

    for url, source_name in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            events = _parse_wh_listing_page(soup)
            if events:
                return {"events": events, "source": source_name}

        except Exception:
            continue

    return None


def _parse_wh_listing_page(soup):
    """Parse listing items from whitehouse.gov WordPress pages.

    The site uses Gutenberg block editor with these known selectors:
    - div.wp-block-query contains the post list
    - li elements within that container hold individual items
    - h2/h3/a elements hold titles
    - time elements hold dates
    """
    events = []

    # Strategy 1: WordPress block query structure (verified)
    container = soup.select_one("div.wp-block-query")
    if container:
        for li in container.find_all("li")[:15]:
            link = li.find("a")
            if not link:
                continue
            title = link.get_text(strip=True)
            if not title:
                continue

            time_el = li.find("time")
            event_date = ""
            event_time = ""
            if time_el:
                event_date = time_el.get("datetime", "")
                event_time = time_el.get_text(strip=True)

            events.append({
                "title": title,
                "description": "",
                "date": event_date,
                "time": event_time,
                "location": "",
            })

    # Strategy 2: Generic article/entry patterns (fallback)
    if not events:
        for selector in [
            "article", "li.listing-item", ".news-item",
            ".briefing-statement", ".entry",
        ]:
            items = soup.select(selector)
            if not items:
                continue

            for item in items[:15]:
                title_el = item.select_one(
                    "h1.wp-block-whitehouse-topper__headline, "
                    "h2, h3, .title, .entry-title, a"
                )
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue

                time_el = item.select_one("time, .date")
                event_date = ""
                event_time = ""
                if time_el:
                    event_date = time_el.get("datetime", "")
                    event_time = time_el.get_text(strip=True)

                desc_el = item.select_one("p, .excerpt, .description")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

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
    """Fetch from White House RSS feeds.

    Note: Current whitehouse.gov may not expose RSS feeds, but we try
    common WordPress feed URLs as they sometimes work.
    """
    feed_urls = [
        "https://www.whitehouse.gov/feed/",
        "https://www.whitehouse.gov/briefing-room/feed/",
        "https://www.whitehouse.gov/briefing-room/statements-releases/feed/",
        "https://www.whitehouse.gov/news/feed/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    for url in feed_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            # Verify it's actually XML before parsing
            content_type = resp.headers.get("Content-Type", "")
            if "html" in content_type and "xml" not in content_type:
                continue

            root = ElementTree.fromstring(resp.content)

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
    """Fetch schedule from Factba.se (third-party presidential tracker).

    Factba.se tracks presidential schedules, travel, and public appearances.
    They have both a calendar page and JSON API endpoints.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/html, */*",
    }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Try Factba.se JSON API endpoints
    api_urls = [
        f"https://factba.se/json/calendar?date={today}",
        "https://factba.se/json/calendar/today",
    ]

    for api_url in api_urls:
        try:
            resp = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", [])
            events = []
            for item in items:
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
            continue

    # Try calendar HTML page
    calendar_urls = [
        "https://factba.se/trump/calendar",
        "https://factba.se/biden/calendar",
    ]
    for url in calendar_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            events = []
            for item in soup.select(
                ".calendar-item, .schedule-entry, .event-item, "
                ".datatable tbody tr, .event"
            ):
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
            continue

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
# Google News / media scraping (real-time travel reporting)
# ---------------------------------------------------------------------------

def fetch_news():
    """Fetch recent news about presidential travel/location.

    Scrapes Google News RSS and other news aggregators for headlines
    containing presidential travel information. News outlets report
    travel almost in real-time, making this a strong signal.
    """
    now = time.time()
    if _news_cache["data"] is not None and (now - _news_cache["timestamp"]) < NEWS_CACHE_TTL:
        return _news_cache["data"]

    result = {
        "articles": [],
        "inferred_location": None,
        "location_confidence": None,
        "travel_detected": False,
        "source": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    for fetcher in [_fetch_google_news_rss, _fetch_bing_news, _fetch_news_fallback]:
        try:
            articles = fetcher()
            if articles:
                result["articles"] = articles
                result["source"] = articles[0].get("source_name", "news")
                break
        except Exception as e:
            print(f"News fetch error ({fetcher.__name__}): {e}")

    if result["articles"]:
        _analyze_news_location(result)

    _news_cache["data"] = result
    _news_cache["timestamp"] = now
    return result


def _fetch_google_news_rss():
    """Fetch presidential travel news from Google News RSS.

    Google News provides RSS feeds for search queries. These don't
    require an API key and return recent headlines with publication dates.
    """
    articles = []
    queries = [
        "Trump+travels+OR+departs+OR+arrives+OR+%22Air+Force+One%22",
        "Trump+%22Mar-a-Lago%22+OR+%22Camp+David%22+OR+%22White+House%22+travel",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    for query in queries:
        try:
            url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                desc = item.findtext("description", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                link = item.findtext("link", "").strip()
                source = item.findtext("source", "").strip()

                if not title:
                    continue

                # Clean HTML from description
                clean_desc = re.sub(r'<[^>]+>', ' ', desc).strip()

                articles.append({
                    "title": title,
                    "description": clean_desc[:300],
                    "published": pub_date,
                    "url": link,
                    "source_name": source or "Google News",
                })

            if articles:
                break
        except Exception:
            continue

    # Deduplicate by title similarity
    seen_titles = set()
    unique = []
    for a in articles:
        key = a["title"][:50].lower()
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(a)

    return unique[:15]


def _fetch_bing_news():
    """Fetch news from Bing News RSS as a fallback."""
    articles = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    queries = [
        "Trump+travel+Air+Force+One",
        "Trump+departs+arrives+location",
    ]

    for query in queries:
        try:
            url = f"https://www.bing.com/news/search?q={query}&format=rss"
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                desc = item.findtext("description", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                link = item.findtext("link", "").strip()

                if not title:
                    continue

                clean_desc = re.sub(r'<[^>]+>', ' ', desc).strip()

                articles.append({
                    "title": title,
                    "description": clean_desc[:300],
                    "published": pub_date,
                    "url": link,
                    "source_name": "Bing News",
                })

            if articles:
                break
        except Exception:
            continue

    return articles[:15]


def _fetch_news_fallback():
    """Fallback: scrape a news aggregator for presidential location headlines."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,*/*",
    }

    # Try a few general news RSS feeds that cover politics
    feed_urls = [
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://feeds.washingtonpost.com/rss/politics",
        "https://feeds.reuters.com/Reuters/PoliticsNews",
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    ]

    articles = []
    for url in feed_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue

            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                desc = item.findtext("description", "").strip()
                pub_date = item.findtext("pubDate", "").strip()

                if not title:
                    continue

                # Only keep articles mentioning Trump + location/travel
                combined = f"{title} {desc}".lower()
                if "trump" not in combined:
                    continue
                has_location_signal = any(
                    kw in combined
                    for kw in _TRAVEL_KEYWORDS + list(_LOCATION_HINTS.keys())
                )
                if not has_location_signal:
                    continue

                clean_desc = re.sub(r'<[^>]+>', ' ', desc).strip()
                articles.append({
                    "title": title,
                    "description": clean_desc[:300],
                    "published": pub_date,
                    "url": "",
                    "source_name": "News RSS",
                })

            if len(articles) >= 3:
                break
        except Exception:
            continue

    return articles[:10]


def _analyze_news_location(news_result):
    """Analyze news articles for presidential location signals."""
    all_text = " ".join(
        f"{a.get('title', '')} {a.get('description', '')}"
        for a in news_result["articles"]
    ).lower()

    # Check for travel
    news_result["travel_detected"] = any(kw in all_text for kw in _TRAVEL_KEYWORDS)

    # Check for location hints (prioritize non-WH locations)
    best_location = None
    best_priority = -1

    for keyword, location_name in _LOCATION_HINTS.items():
        if keyword in all_text:
            priority = 2 if location_name != "White House" else 1
            if priority > best_priority:
                best_priority = priority
                best_location = location_name

    if best_location:
        news_result["inferred_location"] = best_location
        news_result["location_confidence"] = "medium"
    elif news_result["travel_detected"]:
        news_result["inferred_location"] = "Traveling"
        news_result["location_confidence"] = "medium"


# ---------------------------------------------------------------------------
# Social media / X press pool signals
# ---------------------------------------------------------------------------

def fetch_social():
    """Fetch social media signals about presidential location.

    The White House press pool posts real-time location updates on X/Twitter.
    We scrape aggregators and public feeds since the X API requires paid access.
    Nitter instances and RSS bridges provide free access to public tweets.
    """
    now = time.time()
    if _social_cache["data"] is not None and (now - _social_cache["timestamp"]) < SOCIAL_CACHE_TTL:
        return _social_cache["data"]

    result = {
        "posts": [],
        "inferred_location": None,
        "location_confidence": None,
        "travel_detected": False,
        "source": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    for fetcher in [_fetch_nitter_pool, _fetch_rss_bridge_social, _fetch_social_fallback]:
        try:
            posts = fetcher()
            if posts:
                result["posts"] = posts
                result["source"] = posts[0].get("source_name", "social")
                break
        except Exception as e:
            print(f"Social fetch error ({fetcher.__name__}): {e}")

    if result["posts"]:
        _analyze_social_location(result)

    _social_cache["data"] = result
    _social_cache["timestamp"] = now
    return result


# Key X/Twitter accounts that post presidential location info
_POOL_ACCOUNTS = [
    "WHPoolReport",       # White House pool reports
    "PoolReporters",      # Press pool
    "WHPublicPool",       # Public pool feed
    "ABORAF1Tracking",    # AF1 tracking
    "AirForceOnePhoto",   # AF1 photo ops with locations
    "realDonaldTrump",    # Presidential account
    "ABORAF1",            # AF1 flight tracking
    "potaboraf1",         # Presidential airlift group
]


def _fetch_nitter_pool():
    """Fetch press pool posts from Nitter instances (public X/Twitter proxy).

    Nitter is an open-source Twitter frontend that doesn't require API keys.
    Multiple public instances exist; we try several in case some are down.
    """
    posts = []

    # Nitter instances (these rotate availability)
    nitter_instances = [
        "nitter.net",
        "nitter.privacydev.net",
        "nitter.poast.org",
        "nitter.woodland.cafe",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, text/xml, */*",
    }

    # Try RSS feeds from Nitter for pool accounts
    for instance in nitter_instances:
        for account in _POOL_ACCOUNTS[:4]:  # Limit requests per instance
            try:
                url = f"https://{instance}/{account}/rss"
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue

                root = ElementTree.fromstring(resp.content)
                for item in root.iter("item"):
                    title = item.findtext("title", "").strip()
                    desc = item.findtext("description", "").strip()
                    pub_date = item.findtext("pubDate", "").strip()
                    link = item.findtext("link", "").strip()

                    if not title and not desc:
                        continue

                    text = title or re.sub(r'<[^>]+>', ' ', desc).strip()

                    # Only keep posts with location/travel signals
                    text_lower = text.lower()
                    has_signal = (
                        any(kw in text_lower for kw in _TRAVEL_KEYWORDS)
                        or any(kw in text_lower for kw in _LOCATION_HINTS)
                        or "motorcade" in text_lower
                        or "marine one" in text_lower
                        or "pool report" in text_lower
                        or "lid" in text_lower  # "lid called" = no more events
                    )
                    if not has_signal:
                        continue

                    posts.append({
                        "text": text[:280],
                        "author": f"@{account}",
                        "published": pub_date,
                        "url": link,
                        "source_name": f"X via {instance}",
                    })

                if posts:
                    return posts[:15]

            except Exception:
                continue

        if posts:
            break

    return posts[:15]


def _fetch_rss_bridge_social():
    """Fetch tweets via RSS-Bridge instances (another public Twitter proxy)."""
    posts = []

    bridges = [
        "https://rss-bridge.org/bridge01",
        "https://rss-bridge.bb8.fun",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, text/xml, */*",
    }

    for bridge in bridges:
        for account in _POOL_ACCOUNTS[:3]:
            try:
                url = (
                    f"{bridge}/?action=display&bridge=TwitterBridge"
                    f"&context=By+username&u={account}&norep=on&noretweet=on"
                    f"&format=Atom"
                )
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue

                root = ElementTree.fromstring(resp.content)
                # Atom namespace
                ns = {"atom": "http://www.w3.org/2005/Atom"}

                for entry in root.findall("atom:entry", ns):
                    title = entry.findtext("atom:title", "", ns).strip()
                    content = entry.findtext("atom:content", "", ns).strip()
                    updated = entry.findtext("atom:updated", "", ns).strip()
                    link_el = entry.find("atom:link", ns)
                    link = link_el.get("href", "") if link_el is not None else ""

                    text = title or re.sub(r'<[^>]+>', ' ', content).strip()
                    text_lower = text.lower()

                    has_signal = (
                        any(kw in text_lower for kw in _TRAVEL_KEYWORDS)
                        or any(kw in text_lower for kw in _LOCATION_HINTS)
                        or "motorcade" in text_lower
                        or "pool report" in text_lower
                    )
                    if not has_signal:
                        continue

                    posts.append({
                        "text": text[:280],
                        "author": f"@{account}",
                        "published": updated,
                        "url": link,
                        "source_name": "X via RSS-Bridge",
                    })

                if posts:
                    return posts[:15]
            except Exception:
                continue

        if posts:
            break

    return posts[:15]


def _fetch_social_fallback():
    """Fallback: search Google News for pool report summaries.

    Many news sites aggregate pool reports. We search for recent
    pool reports which contain real-time presidential location updates.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
        "Accept": "application/rss+xml, text/xml, */*",
    }

    try:
        query = "White+House+pool+report+Trump+today"
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []

        root = ElementTree.fromstring(resp.content)
        posts = []

        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            desc = item.findtext("description", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            source = item.findtext("source", "").strip()

            if not title:
                continue

            clean_desc = re.sub(r'<[^>]+>', ' ', desc).strip()

            posts.append({
                "text": f"{title} - {clean_desc[:150]}",
                "author": source or "Pool report",
                "published": pub_date,
                "url": "",
                "source_name": "Pool report (via Google News)",
            })

        return posts[:10]
    except Exception:
        return []


def _analyze_social_location(social_result):
    """Analyze social media posts for presidential location signals."""
    all_text = " ".join(
        p.get("text", "") for p in social_result["posts"]
    ).lower()

    social_result["travel_detected"] = any(kw in all_text for kw in _TRAVEL_KEYWORDS)

    # Check for location hints
    best_location = None
    best_priority = -1

    for keyword, location_name in _LOCATION_HINTS.items():
        if keyword in all_text:
            priority = 2 if location_name != "White House" else 1
            if priority > best_priority:
                best_priority = priority
                best_location = location_name

    # Social media is often very current - "motorcade" implies active travel
    if "motorcade" in all_text or "marine one" in all_text:
        social_result["travel_detected"] = True

    if best_location:
        social_result["inferred_location"] = best_location
        social_result["location_confidence"] = "medium"
    elif social_result["travel_detected"]:
        social_result["inferred_location"] = "Traveling"
        social_result["location_confidence"] = "medium"


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


@app.route("/api/news")
def api_news():
    """API endpoint returning recent news about presidential location."""
    try:
        news = fetch_news()
        return jsonify({"status": "ok", **news})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/social")
def api_social():
    """API endpoint returning social media signals about presidential location."""
    try:
        social = fetch_social()
        return jsonify({"status": "ok", **social})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/location")
def api_location():
    """Combined endpoint: best guess at presidential location using all signals.

    Signal priority (highest to lowest):
    1. VIP TFR (FAR 91.141) - definitive, FAA-sourced
    2. Multiple sources agree on same non-WH location - high confidence
    3. News reports of travel - near real-time
    4. Social media / pool reports - real-time
    5. White House schedule - official but may be outdated
    6. Default: White House (P-56 permanent TFR)
    """
    try:
        # Fetch all sources concurrently to avoid sequential timeout stacking
        with ThreadPoolExecutor(max_workers=4) as pool:
            fut_tfrs = pool.submit(get_vip_tfrs)
            fut_schedule = pool.submit(fetch_schedule)
            fut_news = pool.submit(fetch_news)
            fut_social = pool.submit(fetch_social)

        tfrs = fut_tfrs.result(timeout=30)
        schedule = fut_schedule.result(timeout=30)
        news = fut_news.result(timeout=30)
        social = fut_social.result(timeout=30)

        # Collect all location signals with weights
        signals = []  # list of (location_name, confidence_weight, source_label, detail)

        # TFRs: weight 10 (definitive)
        if tfrs:
            primary = next((t for t in tfrs if not t.get("is_inner_ring")), tfrs[0])
            loc_name = (primary.get("nearest_known_location") or {}).get("name", "Unknown")
            signals.append((
                loc_name, 10, "VIP TFR (FAR 91.141)",
                f"Active TFR: {primary.get('notam_id', 'N/A')}",
                primary.get("lat"), primary.get("lon"),
            ))

        # News: weight 5
        if news.get("inferred_location") and news["inferred_location"] != "Traveling":
            signals.append((
                news["inferred_location"], 5, f"News ({news.get('source', 'unknown')})",
                news["articles"][0]["title"] if news.get("articles") else "",
                None, None,
            ))
        elif news.get("travel_detected"):
            signals.append((
                "Traveling", 3, f"News ({news.get('source', 'unknown')})",
                "Travel activity detected in news headlines",
                None, None,
            ))

        # Social: weight 4 (very current but less authoritative)
        if social.get("inferred_location") and social["inferred_location"] != "Traveling":
            signals.append((
                social["inferred_location"], 4, f"Social media ({social.get('source', 'unknown')})",
                social["posts"][0]["text"][:100] if social.get("posts") else "",
                None, None,
            ))
        elif social.get("travel_detected"):
            signals.append((
                "Traveling", 2, f"Social media ({social.get('source', 'unknown')})",
                "Travel activity detected in social posts",
                None, None,
            ))

        # Schedule: weight 3
        if (schedule.get("inferred_location")
                and schedule["inferred_location"] not in ("White House", "Traveling")):
            signals.append((
                schedule["inferred_location"], 3,
                f"WH schedule ({schedule.get('source', 'unknown')})",
                schedule["events"][0]["title"] if schedule.get("events") else "",
                None, None,
            ))
        elif schedule.get("travel_detected"):
            signals.append((
                "Traveling", 1, f"WH schedule ({schedule.get('source', 'unknown')})",
                "Travel activity in schedule",
                None, None,
            ))

        # Score locations: aggregate weights per location
        location_scores = {}
        for loc_name, weight, source, detail, lat, lon in signals:
            if loc_name not in location_scores:
                location_scores[loc_name] = {
                    "total_weight": 0, "sources": [], "details": [],
                    "lat": lat, "lon": lon,
                }
            location_scores[loc_name]["total_weight"] += weight
            location_scores[loc_name]["sources"].append(source)
            if detail:
                location_scores[loc_name]["details"].append(detail)
            # Prefer coordinates from highest-weight source
            if lat is not None and lon is not None:
                location_scores[loc_name]["lat"] = lat
                location_scores[loc_name]["lon"] = lon

        if location_scores:
            # Pick highest-scoring non-"Traveling" location, or "Traveling" if that's all
            ranked = sorted(
                location_scores.items(),
                key=lambda x: (x[0] != "Traveling", x[1]["total_weight"]),
                reverse=True,
            )
            best_name, best_data = ranked[0]

            # Determine confidence level
            w = best_data["total_weight"]
            num_sources = len(best_data["sources"])
            if w >= 10:
                confidence = "high"
            elif w >= 5 or num_sources >= 2:
                confidence = "medium"
            else:
                confidence = "low"

            # Get coordinates
            lat = best_data.get("lat")
            lon = best_data.get("lon")
            if lat is None or lon is None:
                coords = KNOWN_LOCATIONS.get(best_name, (38.8977, -77.0365))
                lat, lon = coords

            location = {
                "lat": lat,
                "lon": lon,
                "name": best_name,
                "confidence": confidence,
                "source": " + ".join(best_data["sources"]),
                "details": best_data["details"][0] if best_data["details"] else "",
                "all_signals": [
                    {"location": name, "weight": d["total_weight"],
                     "sources": d["sources"]}
                    for name, d in ranked
                ],
            }
        else:
            # No signals at all - default to White House
            location = {
                "lat": 38.8977,
                "lon": -77.0365,
                "name": "White House",
                "confidence": "default",
                "source": "No active travel signals detected",
                "details": "No VIP TFRs, news, social, or schedule signals found. "
                           "Defaulting to White House (P-56 permanent TFR).",
                "all_signals": [],
            }

        return jsonify({
            "status": "ok",
            "location": location,
            "sources_checked": {
                "tfrs": len(tfrs),
                "news_articles": len(news.get("articles", [])),
                "social_posts": len(social.get("posts", [])),
                "schedule_events": len(schedule.get("events", [])),
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
