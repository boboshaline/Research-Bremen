"""
data_collectors/uba_collector.py
==================================
Triangulation Source 3: German Environment Agency (UBA)
Reference-grade air quality instruments — Bremen only.

TRIANGULATION ROLE:
    Open-Meteo  = satellite/atmospheric model data
    OpenAQ      = ground station measurements
    UBA Germany = gold standard reference  ← THIS FILE
    AirQo       = African city sensors

WHY UBA IS THE GOLD STANDARD:
    UBA instruments cost €15,000–50,000 each.
    Calibrated to EU and WHO reference standards.
    Over 400 stations across Germany.
    When we say our system is accurate, we mean
    accurate compared to UBA. This is the benchmark
    that makes our paper credible.

BREMEN STATIONS (confirmed from API):
    616  Bremen-Mitte         urban background ← PRIMARY
    617  Bremen-Ost           urban background
    619  Bremen-Nord          urban background
    621  Bremen-Dobben        urban traffic
    627  Bremen-Oslebshausen  urban background
    628  Bremen-Hasenbüren    rural industry

UBA PARAMETER CODES:
    component=1  → PM10
    component=5  → PM2.5

UBA SCOPE CODES (hourly vs daily):
    scope=1  → Tagesmittelwert (daily mean)
    scope=2  → Stundenmittelwert (hourly mean)

    PM10 at Bremen stations publishes daily means (scope=1).
    PM2.5 at Bremen stations publishes hourly means (scope=2).
    We try scope=2 first, then scope=1 as fallback, so the
    collector is robust to whichever aggregation is available.

API: luftdaten.umweltbundesamt.de/api/air-data/v4
Free, no key needed.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import requests
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
from utils.db_connection import get_connection

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/uba_collector.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── UBA Configuration ──────────────────────────────────────────
UBA_BASE = "https://luftdaten.umweltbundesamt.de/api/air-data/v4"

BREMEN_STATIONS = [616, 617, 619, 627, 621, 628]

PARAM_PM10 = 1
PARAM_PM25 = 5

# Scope codes: 2=hourly, 1=daily mean
# PM2.5 is typically published as hourly (scope=2) at German stations.
# PM10 is typically published as daily mean (scope=1).
# We try multiple scopes so the collector doesn't break if this varies.
PM25_SCOPES = [2, 1, 3]   # hourly first, then daily, then any other
PM10_SCOPES = [1, 2, 3]   # daily first, then hourly


def fetch_uba_value(station_id, parameter_id, scopes, param_name):
    """
    Fetches a PM value from one UBA station, trying each scope in order.

    UBA v4 response structure:
    {
      "data": {
        "{station_id}": {
          "YYYY-MM-DD HH:MM:SS": [component_id, scope_id, value, ...]
        }
      }
    }

    Queries the last 3 days to account for UBA's 1-2 hour publish delay.
    Returns (float value, scope_used) or (None, None).
    """
    now       = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    date_to   = now.strftime("%Y-%m-%d")

    url = f"{UBA_BASE}/measures/json"

    for scope in scopes:
        params = {
            "date_from": date_from,
            "date_to":   date_to,
            "station":   station_id,
            "component": parameter_id,
            "scope":     scope,
        }

        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            station_data = (
                data.get("data", {}).get(str(station_id), {})
            )

            if not station_data:
                logger.debug(
                    f"[UBA] Station {station_id} {param_name} "
                    f"scope={scope}: no data"
                )
                continue

            latest_key   = max(station_data.keys())
            latest_entry = station_data[latest_key]

            if not isinstance(latest_entry, list) or len(latest_entry) < 3:
                logger.warning(
                    f"[UBA] Station {station_id} unexpected "
                    f"entry format: {latest_entry}"
                )
                continue

            value = latest_entry[2]
            if value is None:
                continue

            value = round(float(value), 2)
            logger.info(
                f"[UBA] Station {station_id} {param_name} "
                f"(scope={scope}) = {value} µg/m³ at {latest_key}"
            )
            return value, scope

        except requests.exceptions.RequestException as e:
            logger.error(
                f"[UBA] Station {station_id} request failed: {e}"
            )
            return None, None
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.error(
                f"[UBA] Station {station_id} parse error: {e}"
            )

    return None, None


def collect_uba_bremen():
    """
    Collects PM2.5 and PM10 from UBA Bremen reference stations.

    For each pollutant:
      - Tries BREMEN_STATIONS in order of preference
      - For each station, tries all relevant scopes
      - Stops at the first valid reading

    Inserts with device_id = 'uba-bremen-reference'.
    """
    logger.info("=" * 55)
    logger.info("UBA Germany collection started")

    pm25 = None
    pm10 = None

    # ── PM2.5 ──────────────────────────────────────────────────
    for station_id in BREMEN_STATIONS:
        logger.info(f"[UBA] Trying station {station_id} for PM2.5")
        value, scope = fetch_uba_value(
            station_id, PARAM_PM25, PM25_SCOPES, "PM2.5"
        )
        if value is not None:
            pm25 = value
            logger.info(
                f"[UBA] PM2.5 = {pm25} µg/m³ "
                f"(station={station_id}, scope={scope})"
            )
            break
    else:
        logger.warning(
            "[UBA] No PM2.5 data from any Bremen station. "
            "Station 616 may not measure PM2.5 — "
            "check https://luftdaten.umweltbundesamt.de and confirm "
            "which Bremen stations have PM2.5 sensors."
        )

    # ── PM10 ───────────────────────────────────────────────────
    for station_id in BREMEN_STATIONS:
        logger.info(f"[UBA] Trying station {station_id} for PM10")
        value, scope = fetch_uba_value(
            station_id, PARAM_PM10, PM10_SCOPES, "PM10"
        )
        if value is not None:
            pm10 = value
            logger.info(
                f"[UBA] PM10 = {pm10} µg/m³ "
                f"(station={station_id}, scope={scope})"
            )
            break

    if pm25 is None and pm10 is None:
        logger.warning(
            "[UBA] No data from any Bremen station. "
            "Will retry next cycle."
        )
        return

    # ── Insert ─────────────────────────────────────────────────
    conn   = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO sensor_data
            (time, device_id, pm25, pm10, temperature, humidity)
        VALUES
            (NOW(), %s, %s, %s, %s, %s)
    """

    try:
        cursor.execute(sql, (
            "uba-bremen-reference", pm25, pm10, None, None
        ))
        conn.commit()
        logger.info(
            f"[DB] UBA inserted: PM2.5={pm25}  PM10={pm10}"
        )

    except Exception as e:
        conn.rollback()
        logger.error(f"[DB] UBA insert failed: {e}")
    finally:
        cursor.close()
        conn.close()

    logger.info("UBA collection complete")
    logger.info("=" * 55)


if __name__ == "__main__":
    collect_uba_bremen()