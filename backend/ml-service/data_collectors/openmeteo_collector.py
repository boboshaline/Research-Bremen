"""
data_collector/openmeteo_collector.py
---------------------------------------
Collects real PM2.5 and PM10 data from Open-Meteo Air Quality API.
Free, no API key, covers all three research cities.

Three cities represent our core research question:
  Bremen  → developed world, dense monitoring (benchmark)
  Nairobi → developing world, sparse monitoring
  Lagos   → developing world, sparse monitoring

Data source: Copernicus Atmosphere Monitoring Service (CAMS)
via Open-Meteo — citable in academic papers.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import requests
import logging
import sys
import os
from datetime import datetime, timezone

# Add parent folder so we can import utils
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.db_connection import get_connection

# ── Logging ────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/collector.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ── The three research cities ──────────────────────────────────
# Coordinates verified from your terminal test above
TARGET_CITIES = [
    {
        "name":      "Bremen",
        "country":   "DE",
        "device_id": "openmeteo-bremen-de",
        "latitude":  53.0793,
        "longitude": 8.8017,
        "timezone":  "Europe/Berlin"
    },
    {
        "name":      "Nairobi",
        "country":   "KE",
        "device_id": "openmeteo-nairobi-ke",
        "latitude":  -1.2921,
        "longitude": 36.8219,
        "timezone":  "Africa/Nairobi"
    },
    {
        "name":      "Lagos",
        "country":   "NG",
        "device_id": "openmeteo-lagos-ng",
        "latitude":  6.5244,
        "longitude": 3.3792,
        "timezone":  "Africa/Lagos"
    },
    {
        "name":      "Kampala",
        "country":   "UG",
        "device_id": "openmeteo-kampala-ug",
        "latitude":  0.3476,
        "longitude": 32.5825,
        "timezone":  "Africa/Kampala"
    },
    {
        "name":      "Accra",
        "country":   "GH",
        "device_id": "openmeteo-accra-gh",
        "latitude":  5.6037,
        "longitude": -0.1870,
        "timezone":  "Africa/Accra"
    }
]

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


def fetch_city_data(city):
    """
    Calls Open-Meteo API for one city.
    Returns raw JSON or None if request fails.
    
    past_days=1 gives us yesterday + today = ~48 hours of data.
    We use ON CONFLICT DO NOTHING in the INSERT so duplicate
    rows are safely ignored if we collect overlapping windows.
    """
    params = {
        "latitude":  city["latitude"],
        "longitude": city["longitude"],
        "hourly":    "pm2_5,pm10",
        "timezone":  city["timezone"],
        "past_days": 1
    }

    try:
        response = requests.get(API_URL, params=params, timeout=15)
        response.raise_for_status()
        logger.info(f"[{city['name']}] API call successful")
        return response.json()

    except requests.exceptions.Timeout:
        logger.error(f"[{city['name']}] Request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"[{city['name']}] Request failed: {e}")
        return None


def insert_readings(city, raw_data):
    """
    Parses API response and inserts each hourly reading
    into the sensor_readings table.

    Each hour = one row in the database.
    This is correct for TimescaleDB time-series indexing.

    ON CONFLICT DO NOTHING = safe to run multiple times.
    No duplicate rows will be created.
    """
    if not raw_data or "hourly" not in raw_data:
        logger.warning(f"[{city['name']}] Empty or invalid response")
        return 0

    times = raw_data["hourly"].get("time",  [])
    pm25s = raw_data["hourly"].get("pm2_5", [])
    pm10s = raw_data["hourly"].get("pm10",  [])

    if not times:
        logger.warning(f"[{city['name']}] No time data returned")
        return 0

    conn     = get_connection()
    cursor   = conn.cursor()
    inserted = 0
    skipped  = 0

    sql = """
        INSERT INTO sensor_data
            (time, device_id, pm25, pm10,
             temperature, humidity)
        VALUES
            (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """

    for i, time_str in enumerate(times):
        try:
            # Parse time
            time = datetime.fromisoformat(time_str)

            # Get values safely
            pm25 = pm25s[i] if i < len(pm25s) else None
            pm10 = pm10s[i] if i < len(pm10s) else None

            # ── Data validation ────────────────────────────
            # Reject physically impossible values.
            # This is sensor-level quality control.
            # We set to None (NULL) rather than skip the row
            # so we can track data gaps in our research.

            if pm25 is not None:
                if pm25 < 0 or pm25 > 1000:
                    logger.warning(
                        f"[{city['name']}] Invalid PM2.5={pm25} "
                        f"at {time_str} — set to NULL"
                    )
                    pm25 = None

            if pm10 is not None:
                if pm10 < 0 or pm10 > 2000:
                    logger.warning(
                        f"[{city['name']}] Invalid PM10={pm10} "
                        f"at {time_str} — set to NULL"
                    )
                    pm10 = None

            cursor.execute(sql, (
                time,
                city["device_id"],
                pm25,
                pm10,
                None,         # temperature — added later
                None,         # humidity    — added later
                # "open-meteo"
            ))
            inserted += 1

        except Exception as e:
            logger.error(
                f"[{city['name']}] Failed to insert row "
                f"at index {i}: {e}"
            )
            skipped += 1
            continue

    conn.commit()
    cursor.close()
    conn.close()

    logger.info(
        f"[{city['name']}] Inserted: {inserted} rows | "
        f"Skipped: {skipped} rows"
    )
    return inserted


def collect_all_cities():
    """
    Main function — runs for all three cities.
    Called directly or by the scheduler every hour.
    """
    logger.info("=" * 55)
    logger.info(
        f"Collection started: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    total = 0

    for city in TARGET_CITIES:
        logger.info(
            f"Collecting: {city['name']} ({city['country']}) "
            f"@ {city['latitude']}, {city['longitude']}"
        )

        raw_data = fetch_city_data(city)

        if raw_data:
            count  = insert_readings(city, raw_data)
            total += count
        else:
            logger.error(
                f"[{city['name']}] FAILED — no data this cycle"
            )

    logger.info(f"Collection complete — total rows inserted: {total}")
    logger.info("=" * 55)
    return total


# ── Run directly ───────────────────────────────────────────────
if __name__ == "__main__":
    collect_all_cities()