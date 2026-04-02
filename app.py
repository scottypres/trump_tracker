"""
Trump Tracker - VIP NOTAM Location Tracker

Determines the likely location of the President based on FAA
Temporary Flight Restrictions (TFRs) with VIP designations
and ~30 nautical mile diameter restrictions.
"""

import json
import re
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Cache TFR data for 5 minutes to avoid hammering FAA servers
_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 300  # seconds

# Known locations to help label TFRs
KNOWN_LOCATIONS = {
    "Mar-a-Lago": (26.677, -80.037),
    "White House": (38.8977, -77.0365),
    "Camp David": (39.648, -77.463),
    "Bedminster": (40.654, -74.631),
    "Trump Tower NYC": (40.7625, -73.9742),
    "Joint Base Andrews": (38.811, -76.867),
}


def fetch_tfr_data():
    """Fetch current TFR data from FAA sources."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    tfrs = []

    # Source 1: FAA TFR API (XML feed parsed into usable data)
    try:
        tfrs.extend(_fetch_faa_tfr_list())
    except Exception as e:
        print(f"Error fetching FAA TFR list: {e}")

    # Source 2: FAA NOTAM search for FDC VIP NOTAMs
    try:
        tfrs.extend(_fetch_notam_search())
    except Exception as e:
        print(f"Error fetching NOTAM search: {e}")

    # Deduplicate by NOTAM ID
    seen = set()
    unique = []
    for tfr in tfrs:
        key = tfr.get("notam_id", id(tfr))
        if key not in seen:
            seen.add(key)
            unique.append(tfr)

    _cache["data"] = unique
    _cache["timestamp"] = now
    return unique


def _fetch_faa_tfr_list():
    """Fetch TFRs from the FAA TFR feed."""
    tfrs = []

    # The FAA provides TFR data via their API
    url = "https://tfr.faa.gov/tfr2/list.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)"
    }

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Also try the XML/JSON feed
    try:
        save_url = "https://tfr.faa.gov/save_pages/detail_6_SavedList.html"
        resp2 = requests.get(save_url, headers=headers, timeout=15)
        html += resp2.text
    except Exception:
        pass

    # Parse TFR entries - look for VIP-related TFRs
    # TFR pages contain links to individual TFR details
    tfr_links = re.findall(r'href="([^"]*detail_[^"]*\.html)"', html)

    for link in tfr_links[:20]:  # Limit to avoid too many requests
        try:
            if not link.startswith("http"):
                link = f"https://tfr.faa.gov/save_pages/{link}"
            detail_resp = requests.get(link, headers=headers, timeout=10)
            detail = detail_resp.text

            # Check if this is a VIP TFR
            if not _is_vip_tfr(detail):
                continue

            tfr = _parse_tfr_detail(detail, link)
            if tfr:
                tfrs.append(tfr)
        except Exception as e:
            print(f"Error fetching TFR detail {link}: {e}")
            continue

    return tfrs


def _fetch_notam_search():
    """Fetch VIP NOTAMs from the FAA NOTAM search API."""
    tfrs = []

    url = "https://notams.aim.faa.gov/notamSearch/search"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (compatible; TFR-Tracker/1.0)",
    }

    # Search for FDC NOTAMs which include TFRs
    data = {
        "searchType": 0,
        "designatorsForLocationIdentifier": "",
        "notamType": "N",
        "operationsType": "",
        "quickSearch": "VIP TFR",
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

    try:
        resp = requests.post(url, data=data, headers=headers, timeout=15)
        if resp.status_code == 200:
            try:
                results = resp.json()
                notam_list = results.get("notamList", [])
                for notam in notam_list:
                    text = notam.get("icaoMessage", "") or notam.get("traditionalMessage", "")
                    if _is_vip_notam_text(text):
                        tfr = _parse_notam_entry(notam)
                        if tfr:
                            tfrs.append(tfr)
            except (json.JSONDecodeError, KeyError):
                pass
    except Exception as e:
        print(f"NOTAM search error: {e}")

    return tfrs


def _is_vip_tfr(html_text):
    """Check if a TFR detail page is a VIP (presidential) TFR."""
    text_lower = html_text.lower()
    # VIP TFRs use specific language
    vip_indicators = [
        "vip",
        "91.141",       # FAR 91.141 - Presidential TFRs
        "special security",
        "temporary flight restriction",
    ]
    # Must match VIP indicator AND have a reasonable radius
    has_vip = any(ind in text_lower for ind in vip_indicators)
    # 30 NM diameter = 15 NM radius, but also check for 30
    has_radius = bool(re.search(r'(?:15|30)\s*(?:nm|nautical|naut)', text_lower))

    return has_vip and has_radius


def _is_vip_notam_text(text):
    """Check if NOTAM text indicates a VIP TFR."""
    text_upper = text.upper()
    return (
        ("91.141" in text_upper or "VIP" in text_upper)
        and ("TFR" in text_upper or "FLIGHT RESTRICTION" in text_upper)
    )


def _parse_tfr_detail(html, url):
    """Parse a TFR detail page to extract coordinates and info."""
    # Extract coordinates - TFR pages typically have lat/long
    coord_pattern = r'(\d{2,3})[°\s]+(\d{1,2})[\'′\s]+(\d{1,2}(?:\.\d+)?)[\"″\s]*([NS])\s*[/,\s]+\s*(\d{2,3})[°\s]+(\d{1,2})[\'′\s]+(\d{1,2}(?:\.\d+)?)[\"″\s]*([EW])'
    match = re.search(coord_pattern, html)

    if not match:
        # Try decimal format
        dec_pattern = r'(\d{2,3}\.\d+)\s*([NS])\s*[/,\s]+\s*(\d{2,3}\.\d+)\s*([EW])'
        match = re.search(dec_pattern, html)
        if match:
            lat = float(match.group(1))
            if match.group(2) == "S":
                lat = -lat
            lon = float(match.group(3))
            if match.group(4) == "W":
                lon = -lon
        else:
            # Try NOTAM coordinate format: 2640N08002W
            notam_coord = re.search(r'(\d{4})([NS])(\d{5})([EW])', html)
            if notam_coord:
                lat_raw = notam_coord.group(1)
                lat = int(lat_raw[:2]) + int(lat_raw[2:]) / 60.0
                if notam_coord.group(2) == "S":
                    lat = -lat
                lon_raw = notam_coord.group(3)
                lon = int(lon_raw[:3]) + int(lon_raw[3:]) / 60.0
                if notam_coord.group(4) == "W":
                    lon = -lon
            else:
                return None
    else:
        lat = int(match.group(1)) + int(match.group(2)) / 60.0 + float(match.group(3)) / 3600.0
        if match.group(4) == "S":
            lat = -lat
        lon = int(match.group(5)) + int(match.group(6)) / 60.0 + float(match.group(7)) / 3600.0
        if match.group(8) == "W":
            lon = -lon

    # Extract effective times
    effective = _extract_times(html)

    # Extract NOTAM number
    notam_id = ""
    id_match = re.search(r'(FDC\s*\d+/\d+|\d+/\d+)', html)
    if id_match:
        notam_id = id_match.group(1)

    # Extract radius
    radius_nm = 15  # default for VIP
    radius_match = re.search(r'(\d+)\s*(?:nm|nautical)', html, re.IGNORECASE)
    if radius_match:
        r = int(radius_match.group(1))
        if r == 30:
            radius_nm = 15  # 30 NM diameter = 15 NM radius
        elif r <= 30:
            radius_nm = r

    nearest = _find_nearest_location(lat, lon)

    return {
        "notam_id": notam_id,
        "lat": lat,
        "lon": lon,
        "radius_nm": radius_nm,
        "radius_km": radius_nm * 1.852,
        "effective_start": effective.get("start", ""),
        "effective_end": effective.get("end", ""),
        "nearest_known_location": nearest,
        "source_url": url,
        "raw_type": "TFR Detail",
    }


def _parse_notam_entry(notam):
    """Parse a NOTAM entry from the search API."""
    text = notam.get("icaoMessage", "") or notam.get("traditionalMessage", "")

    # Extract coordinates from NOTAM text
    # Format: 2640N08002W (DDMMN/DDDMMW)
    coord_match = re.search(r'(\d{4})([NS])(\d{5})([EW])', text)
    if not coord_match:
        # Try longer format with seconds: 264024N0800212W
        coord_match = re.search(r'(\d{6})([NS])(\d{7})([EW])', text)
        if coord_match:
            lat_raw = coord_match.group(1)
            lat = int(lat_raw[:2]) + int(lat_raw[2:4]) / 60.0 + int(lat_raw[4:6]) / 3600.0
            if coord_match.group(2) == "S":
                lat = -lat
            lon_raw = coord_match.group(3)
            lon = int(lon_raw[:3]) + int(lon_raw[3:5]) / 60.0 + int(lon_raw[5:7]) / 3600.0
            if coord_match.group(4) == "W":
                lon = -lon
        else:
            return None
    else:
        lat_raw = coord_match.group(1)
        lat = int(lat_raw[:2]) + int(lat_raw[2:]) / 60.0
        if coord_match.group(2) == "S":
            lat = -lat
        lon_raw = coord_match.group(3)
        lon = int(lon_raw[:3]) + int(lon_raw[3:]) / 60.0
        if coord_match.group(4) == "W":
            lon = -lon

    # Extract radius
    radius_nm = 15
    radius_match = re.search(r'(\d+)\s*NM\s*(?:RADIUS|DIAMETER)', text, re.IGNORECASE)
    if radius_match:
        r = int(radius_match.group(1))
        if "DIAMETER" in text.upper():
            radius_nm = r // 2
        else:
            radius_nm = r

    # Extract times
    start_match = re.search(r'(\d{10,12})', text)
    effective_start = start_match.group(1) if start_match else ""

    notam_id = notam.get("notamNumber", notam.get("id", ""))
    nearest = _find_nearest_location(lat, lon)

    return {
        "notam_id": str(notam_id),
        "lat": lat,
        "lon": lon,
        "radius_nm": radius_nm,
        "radius_km": radius_nm * 1.852,
        "effective_start": effective_start,
        "effective_end": "",
        "nearest_known_location": nearest,
        "source_url": "https://notams.aim.faa.gov/notamSearch/",
        "raw_type": "NOTAM Search",
        "raw_text": text[:500],
    }


def _extract_times(html):
    """Extract effective start/end times from TFR HTML."""
    times = {}
    # Common format: "Effective: 2024/01/15 1200 UTC"
    start_match = re.search(
        r'(?:effective|begin|start)[:\s]*(\d{4}/\d{2}/\d{2}\s+\d{4})\s*(?:UTC|Z)?',
        html, re.IGNORECASE
    )
    if start_match:
        times["start"] = start_match.group(1) + " UTC"

    end_match = re.search(
        r'(?:expire|end|until)[:\s]*(\d{4}/\d{2}/\d{2}\s+\d{4})\s*(?:UTC|Z)?',
        html, re.IGNORECASE
    )
    if end_match:
        times["end"] = end_match.group(1) + " UTC"

    return times


def _find_nearest_location(lat, lon):
    """Find the nearest known Trump-associated location."""
    from math import radians, sin, cos, sqrt, atan2

    best_name = None
    best_dist = float("inf")

    for name, (klat, klon) in KNOWN_LOCATIONS.items():
        # Haversine distance in km
        R = 6371
        dlat = radians(klat - lat)
        dlon = radians(klon - lon)
        a = sin(dlat / 2) ** 2 + cos(radians(lat)) * cos(radians(klat)) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        d = R * c

        if d < best_dist:
            best_dist = d
            best_name = name

    if best_dist < 50:  # Within 50 km
        return {"name": best_name, "distance_km": round(best_dist, 1)}
    return None


def get_vip_tfrs():
    """Get only VIP TFRs with ~30 NM diameter (presidential movement indicators)."""
    all_tfrs = fetch_tfr_data()
    # Filter for 30 NM diameter TFRs (radius ~15 NM)
    vip_tfrs = [
        tfr for tfr in all_tfrs
        if 10 <= tfr.get("radius_nm", 0) <= 20  # Allow some tolerance
    ]
    return vip_tfrs


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
                "VIP TFRs with ~30 NM diameter indicate likely presidential location. "
                "Data sourced from FAA. Results may be delayed."
            ),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/tfrs/all")
def api_all_tfrs():
    """API endpoint returning all fetched TFRs (not just VIP-filtered)."""
    try:
        tfrs = fetch_tfr_data()
        return jsonify({
            "status": "ok",
            "count": len(tfrs),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "tfrs": tfrs,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
