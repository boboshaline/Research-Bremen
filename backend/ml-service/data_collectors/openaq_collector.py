"""
data_collectors/openaq_collector.py
=====================================
Triangulation Source 2: OpenAQ v3 API
Ground-level physical sensor measurements.

TRIANGULATION ROLE:
    Open-Meteo  = satellite/atmospheric model data
    OpenAQ      = ground station measurements  ← THIS FILE
    UBA Germany = reference instruments (Bremen only)
    AirQo       = African city sensors

WHY OPENAQ AS GROUND TRUTH?
    Open-Meteo derives PM values from atmospheric models.
    OpenAQ collects actual physical air samples from
    government monitoring stations on the ground.
    Comparing these two sources quantifies model-vs-reality
    discrepancy — a key finding in our paper.

    Example finding we can report:
    "Open-Meteo overestimated PM2.5 in Lagos by 18%
    compared to OpenAQ ground stations, consistent with
    known limitations of atmospheric models in tropical
    coastal urban environments."

API VERSION: v3 (v1 and v2 retired January 2025)
KEY: Free from explore.openaq.org
COVERAGE:
    Bremen  → good coverage (German monitoring network)
    Nairobi → limited (2-3 stations)
    Lagos   → limited (1-2 stations)
    NOTE: Limited African coverage is itself a research
    finding — it validates our sparse-data research question.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import requests
import logging
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

# ── Path setup ─────────────────────────────────────────────────
sys.path.append(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
from utils.db_connection import get_connection

# ── Logging ────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/openaq_collector.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────
# Get your free key at: explore.openaq.org
# Takes 2 minutes — use your university email
OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY", "YOUR_KEY_HERE")

BASE_URL = "https://api.openaq.org/v3"

HEADERS = {
    "X-API-Key": OPENAQ_API_KEY,
    "Accept":    "application/json"
}

# ── Three research cities ──────────────────────────────────────
# Bounding box = rectangle drawn around each city
# Format: "min_longitude,min_latitude,max_longitude,max_latitude"
# This is more reliable than city name search in OpenAQ v3

TARGET_CITIES = [
    {
        "name":      "Bremen",
        "country":   "DE",
        "device_id": "openaq-bremen-de",
        "bbox":      "8.48,52.98,9.12,53.22",
        "role":      "developed_benchmark"
    },
    {
        "name":      "Nairobi",
        "country":   "KE",
        "device_id": "openaq-nairobi-ke",
        "bbox":      "36.65,-1.45,37.10,-1.16",
        "role":      "developing_sparse"
    },
    {
        "name":      "Lagos",
        "country":   "NG",
        "device_id": "openaq-lagos-ng",
        "bbox":      "3.10,6.35,3.55,6.70",
        "role":      "developing_sparse"
    },
    {
        "name":      "Kampala",
        "country":   "UG",
        "device_id": "openaq-kampala-ug",
        "bbox":      "32.52,0.25,32.68,0.38",
        "role":      "developing_sparse"
    },
    {
        "name":      "Accra",
        "country":   "GH",
        "device_id": "openaq-accra-gh",
        "bbox":      "-0.30,5.52,0.00,5.72",
        "role":      "developing_sparse"
    }
]


# ══════════════════════════════════════════════════════════════
# STEP 1 — FIND MONITORING STATIONS IN EACH CITY
# ══════════════════════════════════════════════════════════════

def get_locations_in_city(city):
    """
    OpenAQ v3 uses coordinates + radius, not bbox.
    bbox was removed in v3 — using point+radius instead.
    """
    # Calculate centre point from bbox
    parts = city["bbox"].split(",")
    min_lon, min_lat, max_lon, max_lat = [float(x) for x in parts]
    centre_lat = (min_lat + max_lat) / 2
    centre_lon = (min_lon + max_lon) / 2

    url    = f"{BASE_URL}/locations"
    params = {
        "coordinates": f"{centre_lat},{centre_lon}",
        "radius":      25000,
        "limit":       20
    }

    try:
        response = requests.get(
            url,
            headers = HEADERS,
            params  = params,
            timeout = 15
        )
        response.raise_for_status()
        data      = response.json()
        locations = data.get("results", [])

        # Filter only locations that have pm25 or pm10
        locations = [
            loc for loc in locations
            if any(
                s.get("parameter", {}).get("name") in ["pm25", "pm10"]
                for s in loc.get("sensors", [])
            )
        ]

        logger.info(
            f"[{city['name']}] OpenAQ stations found: {len(locations)}"
        )

        if not locations:
            logger.warning(
                f"[{city['name']}] SPARSE NETWORK — "
                f"zero PM stations within 25km"
            )

        return locations

    except requests.exceptions.Timeout:
        logger.error(f"[{city['name']}] Request timed out")
        return []
    except requests.exceptions.HTTPError as e:
        logger.error(f"[{city['name']}] HTTP error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"[{city['name']}] Request failed: {e}")
        return []

# ══════════════════════════════════════════════════════════════
# STEP 2 — GET LATEST MEASUREMENTS FROM A STATION
# ══════════════════════════════════════════════════════════════

def get_sensor_ids_from_location(location_id, city_name):
    """
    Gets PM2.5 and PM10 sensor IDs from a location.
    Per OpenAQ v3 docs, sensors are listed inside the location object.
    Each sensor measures exactly one parameter.
    """
    url = f"{BASE_URL}/locations/{location_id}"

    try:
        response = requests.get(
            url,
            headers = HEADERS,
            timeout = 15
        )
        response.raise_for_status()
        data     = response.json()
        results  = data.get("results", [])

        if not results:
            return None, None

        location = results[0]
        sensors  = location.get("sensors", [])

        pm25_sensor_id = None
        pm10_sensor_id = None

        for sensor in sensors:
            param = sensor.get("parameter", {})
            name  = (param.get("name") or "").lower()
            sid   = sensor.get("id")

            if "pm25" in name or "pm2.5" in name or "pm2_5" in name:
                pm25_sensor_id = sid
            elif "pm10" in name or "pm 10" in name:
                pm10_sensor_id = sid

        logger.info(
            f"[{city_name}] Sensor IDs — "
            f"PM2.5={pm25_sensor_id}  PM10={pm10_sensor_id}"
        )
        return pm25_sensor_id, pm10_sensor_id

    except requests.exceptions.RequestException as e:
        logger.error(f"[{city_name}] Sensor lookup failed: {e}")
        return None, None


def get_sensor_latest_value(sensor_id, city_name, param_name):
    """
    Gets the most recent measurement from one sensor.
    Uses GET /v3/sensors/{sensor_id}/measurements
    with a 1-hour window — most reliable per docs.
    """
    from datetime import timedelta

    url      = f"{BASE_URL}/sensors/{sensor_id}/measurements"
    now      = datetime.now(timezone.utc)
    date_from = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_to   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "date_from": date_from,
        "date_to":   date_to,
        "limit":     10
    }

    try:
        response = requests.get(
            url,
            headers = HEADERS,
            params  = params,
            timeout = 15
        )
        response.raise_for_status()
        data     = response.json()
        results  = data.get("results", [])

        if not results:
            logger.warning(
                f"[{city_name}] No recent {param_name} data "
                f"from sensor {sensor_id}"
            )
            return None

        # Take the most recent value
        latest = results[-1]
        value  = latest.get("value")

        if value is not None and value >= 0:
            logger.info(
                f"[{city_name}] {param_name} = {value} µg/m³"
            )
            return round(float(value), 2)

        return None

    except requests.exceptions.RequestException as e:
        logger.error(
            f"[{city_name}] Sensor {sensor_id} fetch failed: {e}"
        )
        return None


def get_latest_measurements(location_id, city_name):
    """
    Master function — gets PM2.5 and PM10 for a location.
    Uses sensor-level measurement endpoint per OpenAQ v3 docs.
    """
    # Step 1: get sensor IDs
    pm25_sid, pm10_sid = get_sensor_ids_from_location(
        location_id, city_name
    )

    pm25 = None
    pm10 = None

    # Step 2: get values from each sensor
    if pm25_sid:
        pm25 = get_sensor_latest_value(pm25_sid, city_name, "PM2.5")

    if pm10_sid:
        pm10 = get_sensor_latest_value(pm10_sid, city_name, "PM10")

    logger.info(
        f"[{city_name}] Final: PM2.5={pm25}  PM10={pm10}"
    )
    return pm25, pm10

# ══════════════════════════════════════════════════════════════
# STEP 3 — SAVE TO TIMESCALE CLOUD DATABASE
# ══════════════════════════════════════════════════════════════

def insert_reading(device_id, pm25, pm10, station_count=0):
    """
    Inserts one OpenAQ ground reading into the database.

    SOURCE TAG: 'openaq-v3'
    This tag is critical for triangulation analysis.
    Later, your validation script queries like:
        WHERE source = 'openaq-v3'     → ground truth
        WHERE source = 'open-meteo'    → model data
        WHERE source = 'uba-reference' → gold standard
    And compares them to calculate agreement scores.

    Parameters:
        device_id     : identifies city and source
        pm25          : PM2.5 in µg/m³ (None if unavailable)
        pm10          : PM10 in µg/m³ (None if unavailable)
        station_count : how many stations found (for paper)
    """
    conn   = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO sensor_data
            (time, device_id, pm25, pm10,
             temperature, humidity)
        VALUES
            (NOW(), %s, %s, %s, %s, %s)
    """

    try:
        cursor.execute(sql, (
            device_id,
            pm25,
            pm10,
            None,         # temperature — from weather API later
            None,         # humidity    — from weather API later
            # "openaq-v3"
        ))
        conn.commit()

        status = "✓ real data" if pm25 or pm10 else "✗ NULL (gap recorded)"
        logger.info(f"[DB] Inserted {device_id}: {status}")

    except Exception as e:
        conn.rollback()
        logger.error(f"[DB] Insert failed for {device_id}: {e}")
    finally:
        cursor.close()
        conn.close()


# ══════════════════════════════════════════════════════════════
# STEP 4 — MAIN COLLECTION FUNCTION
# ══════════════════════════════════════════════════════════════

def collect_all_cities():
    """
    Runs the full OpenAQ collection for all five cities.

    COLLECTION STRATEGY:
    1. Find all stations in city bounding box
    2. Try up to 5 stations until one returns real data
       (first station may be offline — common in Africa)
    3. Insert real data or NULL gap record

    TRIANGULATION NOTE:
    After running alongside openmeteo_collector.py and
    airqo_collector.py, your database has multiple rows
    per city per collection cycle:
        openaq-*      → ground station measurements
        openmeteo-*   → satellite/model data
        airqo-*       → low-cost African sensors
    Comparing these sources is your triangulation analysis.

    NULL rows are DATA POINTS not errors — they document
    infrastructure gaps which is a key research finding.
    """

    if OPENAQ_API_KEY == "YOUR_KEY_HERE":
        logger.error(
            "\n" + "="*50 +
            "\nOpenAQ API key not configured." +
            "\nGet your FREE key at: explore.openaq.org" +
            "\nThen add to your .env file:" +
            "\n  OPENAQ_API_KEY=your_key_here" +
            "\n" + "="*50
        )
        return

    logger.info("=" * 55)
    logger.info(
        f"OpenAQ v3 collection started: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    results_summary = []

    for city in TARGET_CITIES:
        logger.info(
            f"\nProcessing: {city['name']} ({city['country']}) "
            f"— role: {city['role']}"
        )

        # ── Step 1: find stations ──────────────────────────────
        locations     = get_locations_in_city(city)
        station_count = len(locations)

        # ── Step 2: no stations found ──────────────────────────
        if not locations:
            logger.warning(
                f"[{city['name']}] Zero stations found — "
                f"recording NULL gap (sparse network confirmed)"
            )
            insert_reading(city["device_id"], None, None)
            results_summary.append({
                "city":     city["name"],
                "stations": 0,
                "pm25":     None,
                "pm10":     None
            })
            continue

        # ── Step 3: try up to 5 stations ──────────────────────
        # First station may be offline — common in African cities
        # Try each one until we get real data
        pm25 = None
        pm10 = None

        for location in locations[:5]:
            location_id = location.get("id")
            loc_name    = location.get("name", "unknown")

            logger.info(
                f"[{city['name']}] Trying: "
                f"'{loc_name}' (id={location_id})"
            )

            pm25, pm10 = get_latest_measurements(
                location_id,
                city["name"]
            )

            if pm25 is not None or pm10 is not None:
                # Got real data — stop trying
                logger.info(
                    f"[{city['name']}] ✓ Data from '{loc_name}'"
                )
                break
            else:
                logger.warning(
                    f"[{city['name']}] '{loc_name}' offline "
                    f"— trying next station"
                )

        # ── Step 4: insert whatever we got ────────────────────
        insert_reading(
            city["device_id"],
            pm25,
            pm10,
            station_count=station_count
        )

        results_summary.append({
            "city":     city["name"],
            "stations": station_count,
            "pm25":     pm25,
            "pm10":     pm10
        })

    # ── Print collection summary ───────────────────────────────
    logger.info("\n" + "="*55)
    logger.info("COLLECTION SUMMARY — OpenAQ v3")
    logger.info("="*55)
    logger.info(
        f"{'City':<12} {'Stations':>10} "
        f"{'PM2.5':>8} {'PM10':>8}"
    )
    logger.info("-"*42)
    for r in results_summary:
        pm25_str = f"{r['pm25']:.1f}" if r['pm25'] else "NULL"
        pm10_str = f"{r['pm10']:.1f}" if r['pm10'] else "NULL"
        logger.info(
            f"{r['city']:<12} {r['stations']:>10} "
            f"{pm25_str:>8} {pm10_str:>8}"
        )
    logger.info("="*55)

    return results_summary

# ── Run directly ───────────────────────────────────────────────
if __name__ == "__main__":
    collect_all_cities()