"""
data_collectors/airqo_collector.py
=====================================
Triangulation Source 4: AirQo API
African city air quality — Nairobi and Lagos.

STRATEGY (based on official AirQo docs):
    Step 1: GET /devices/grids/summary → find grid IDs
    Step 2: GET /devices/measurements/grids/{GRID_ID}/recent
            → get real PM2.5 and PM10 readings

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import requests
import logging
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
from utils.db_connection import get_connection

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(message)s",
    handlers = [
        logging.FileHandler("logs/airqo_collector.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

AIRQO_TOKEN = os.getenv("AIRQO_API_KEY")
AIRQO_BASE  = "https://api.airqo.net/api/v2"

# ── Target cities ──────────────────────────────────────────────
# grid_id: fill in after first run from the log output
# Leave None to auto-discover
TARGET_CITIES = [
    {
        "name":      "Kenya-Malindi",
        "grid_name": "malindi_municipality",
        "grid_id":   "6943b73309357b00130b6711",
        "device_id": "airqo-nairobi-ke"
    },
    {
        "name":      "Lagos",
        "grid_name": "lagos",
        "grid_id":   "64d635f18f492b0013406c46",
        "device_id": "airqo-lagos-ng"
    },
    {
        "name":       "Uganda-Kampala-Central",
        "grid_name":  "kampala_central",
        "grid_id":    "66e4bc6598c2a9001332991d",
        "device_id":  "airqo-uganda-ug" 

    },
    {
        "name":       "Accra",
        "grid_name":  "accra",
        "grid_id":    "654229088f9ef500139a24d8",
        "device_id":  "airqo-accra-gh" 
    }
]


def get_all_grids():
    """
    GET /devices/grids/summary
    Returns list of all public grids with their IDs and names.
    """
    url = f"{AIRQO_BASE}/devices/grids/summary"
    try:
        r = requests.get(
            url,
            params  = {"token": AIRQO_TOKEN},
            timeout = 20
        )
        r.raise_for_status()
        grids = r.json().get("grids", [])
        logger.info(f"[AirQo] Found {len(grids)} public grids")
        return grids
    except requests.exceptions.RequestException as e:
        logger.error(f"[AirQo] Grid summary failed: {e}")
        return []


def find_grid_id(city_grid_name, all_grids):
    """
    Searches grid list for a matching city name.
    Logs all available names if no match found.
    """
    for grid in all_grids:
        name = (grid.get("name") or "").lower()
        if city_grid_name.lower() in name:
            grid_id = grid.get("_id") or grid.get("id")
            logger.info(
                f"[AirQo] '{city_grid_name}' → "
                f"grid '{name}' id={grid_id}"
            )
            return grid_id

    # Help debug — show what is available
    available = [g.get("name", "?") for g in all_grids]
    logger.warning(
        f"[AirQo] No grid matched '{city_grid_name}'.\n"
        f"Available grids: {available}"
    )
    return None


def get_grid_measurements(grid_id, city_name):
    """
    GET /devices/measurements/grids/{GRID_ID}/recent
    Averages PM2.5 and PM10 across all sensors in the grid.
    Returns (pm25, pm10) or (None, None).
    """
    url = f"{AIRQO_BASE}/devices/measurements/grids/{grid_id}/recent"
    try:
        r = requests.get(
            url,
            params  = {"token": AIRQO_TOKEN},
            timeout = 20
        )
        r.raise_for_status()
        readings = r.json().get("measurements", [])

        if not readings:
            logger.warning(
                f"[AirQo {city_name}] No measurements in grid"
            )
            return None, None

        pm25_vals, pm10_vals = [], []

        for reading in readings:
            # Official response shape:
            # reading["pm2_5"]["value"] or ["calibratedValue"]
            pm2_5_obj = reading.get("pm2_5") or {}
            pm10_obj  = reading.get("pm10")  or {}

            v25 = (
                pm2_5_obj.get("calibratedValue") or
                pm2_5_obj.get("value")
            )
            v10 = (
                pm10_obj.get("calibratedValue") or
                pm10_obj.get("value")
            )

            if v25 is not None:
                pm25_vals.append(float(v25))
            if v10 is not None:
                pm10_vals.append(float(v10))

        pm25 = round(sum(pm25_vals)/len(pm25_vals), 2) if pm25_vals else None
        pm10 = round(sum(pm10_vals)/len(pm10_vals), 2) if pm10_vals else None

        logger.info(
            f"[AirQo {city_name}] {len(readings)} sensors → "
            f"PM2.5={pm25}  PM10={pm10}"
        )
        return pm25, pm10

    except requests.exceptions.RequestException as e:
        logger.error(f"[AirQo {city_name}] Measurements failed: {e}")
        return None, None


def insert_reading(device_id, pm25, pm10):
    """Inserts one reading into sensor_data table."""
    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO sensor_data
                (time, device_id, pm25, pm10, temperature, humidity)
            VALUES (NOW(), %s, %s, %s, %s, %s)
            """,
            (device_id, pm25, pm10, None, None)
        )
        conn.commit()
        status = "✓ real data" if (pm25 or pm10) else "✗ NULL gap"
        logger.info(f"[DB] {device_id}: {status}")
    except Exception as e:
        conn.rollback()
        logger.error(f"[DB] Insert failed: {e}")
    finally:
        cursor.close()
        conn.close()


def collect_all_cities():
    if not AIRQO_TOKEN:
        logger.error("AIRQO_API_KEY not set in .env")
        return

    logger.info("=" * 55)
    logger.info("AirQo collection started")

    # Fetch all grids once — reuse for both cities
    all_grids = get_all_grids()

    for city in TARGET_CITIES:
        logger.info(f"Processing: {city['name']}")

        # Resolve grid ID
        grid_id = city["grid_id"] or find_grid_id(
            city["grid_name"], all_grids
        )

        if not grid_id:
            logger.warning(
                f"[AirQo {city['name']}] "
                f"Grid not found — inserting NULL"
            )
            insert_reading(city["device_id"], None, None)
            continue

        # Get measurements
        pm25, pm10 = get_grid_measurements(grid_id, city["name"])
        insert_reading(city["device_id"], pm25, pm10)

    logger.info("AirQo collection complete")
    logger.info("=" * 55)


if __name__ == "__main__":
    collect_all_cities()