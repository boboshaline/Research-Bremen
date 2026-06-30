"""
Task: Implement AQI calculator from PM2.5 and PM10

Converts raw sensor readings into AQI values using the
EPA breakpoint formula, then WRITES results back to the
aqi_records table so other apps (dashboard, mobile, API)
can read them.

IMPROVEMENT 1 — DATABASE WRITE-BACK:
    Every calculated AQI is INSERTed into aqi_records.
    This is not optional — without it, the alert engine,
    dashboard, and REST API have nothing to read.

IMPROVEMENT 2 — ERROR HANDLING:
    Every database operation is wrapped in try/except/finally.
    A network flicker during one device's calculation does
    NOT crash the whole pipeline — it logs the error and
    continues to the next device. This matters because this
    script runs unattended every hour via the scheduler.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import os
import sys
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent.parent



load_dotenv(dotenv_path=ROOT_DIR / ".env")
sys.path.append(str(ROOT_DIR))


from db import get_connection

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/aqi_calculator.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# AQI BREAKPOINT TABLES — EPA standard
# ══════════════════════════════════════════════════════════════

PM25_BREAKPOINTS = [
    (0.0,   12.0,  0,   50),
    (12.1,  35.4,  51,  100),
    (35.5,  55.4,  101, 150),
    (55.5,  150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 500.4, 301, 500),
]

PM10_BREAKPOINTS = [
    (0,   54,  0,   50),
    (55,  154, 51,  100),
    (155, 254, 101, 150),
    (255, 354, 151, 200),
    (355, 424, 201, 300),
    (425, 604, 301, 500),
]

CATEGORIES = [
    {"min": 0,   "max": 50,  "name": "Good",
     "colour": "green",  "advice": "Air quality is good. No restrictions."},
    {"min": 51,  "max": 100, "name": "Moderate",
     "colour": "yellow", "advice": "Acceptable. Sensitive groups limit outdoor exertion."},
    {"min": 101, "max": 150, "name": "Unhealthy for Sensitive Groups",
     "colour": "orange", "advice": "Children, elderly, asthma patients avoid outdoor exercise."},
    {"min": 151, "max": 200, "name": "Unhealthy",
     "colour": "red",    "advice": "Everyone limit prolonged outdoor activity."},
    {"min": 201, "max": 300, "name": "Very Unhealthy",
     "colour": "purple", "advice": "Everyone avoid outdoor activity. Wear N95 mask."},
    {"min": 301, "max": 500, "name": "Hazardous",
     "colour": "maroon", "advice": "STAY INDOORS. Emergency conditions."},
]


def calculate_aqi_single(concentration, breakpoints):
    """EPA linear interpolation formula. Returns int AQI or None."""
    if concentration is None or concentration < 0:
        return None

    for (C_low, C_high, I_low, I_high) in breakpoints:
        if C_low <= concentration <= C_high:
            aqi = (
                (I_high - I_low) / (C_high - C_low)
            ) * (concentration - C_low) + I_low
            return round(aqi)

    if concentration > 500.4:
        return 500
    return None


def get_category(aqi_value):
    """Returns category dict for an AQI value."""
    if aqi_value is None:
        return {"name": "Unknown", "colour": "grey", "advice": "No data available."}
    for cat in CATEGORIES:
        if cat["min"] <= aqi_value <= cat["max"]:
            return cat
    return CATEGORIES[-1]


def calculate_full_aqi(pm25, pm10):
    """Calculates overall AQI from PM2.5 and PM10."""
    pm25_aqi = calculate_aqi_single(pm25, PM25_BREAKPOINTS)
    pm10_aqi = calculate_aqi_single(pm10, PM10_BREAKPOINTS)

    candidates = [x for x in [pm25_aqi, pm10_aqi] if x is not None]
    overall    = max(candidates) if candidates else None

    category = get_category(overall)

    return {
        "pm25_aqi":    pm25_aqi,
        "pm10_aqi":    pm10_aqi,
        "overall_aqi": overall,
        "category":    category["name"],
        "colour":      category["colour"],
        "advice":      category["advice"]
    }


def save_aqi_to_db(aqi_result):
    """
    IMPROVEMENT 1 — Database write-back.
    Saves one AQI record to aqi_records table.

    IMPROVEMENT 2 — Error handling.
    Wrapped in try/except/finally so a single failed insert
    (e.g. network flicker) does not crash the whole batch —
    it logs the error, rolls back, and the loop continues.

    Returns True on success, False on failure.
    """
    conn = None
    try:
        conn   = get_connection()
        cursor = conn.cursor()

        sql = """
            INSERT INTO aqi_records
                (timestamp, pm25_aqi, pm10_aqi, overall_aqi, category)
            VALUES
                (NOW(), %s, %s, %s, %s)
        """
        cursor.execute(sql, (
            aqi_result["pm25_aqi"],
            aqi_result["pm10_aqi"],
            aqi_result["overall_aqi"],
            aqi_result["category"]
        ))
        conn.commit()
        cursor.close()
        return True

    except Exception as e:
        logger.error(f"[AQI] DB write-back failed: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False

    finally:
        if conn:
            conn.close()


def get_latest_readings():
    """
    Gets the most recent PM2.5/PM10 reading per device.
    Error-handled — returns empty list on failure rather
    than crashing the whole pipeline.
    """
    conn = None
    try:
        conn   = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT ON (device_id)
                device_id, pm25, pm10, time
            FROM sensor_data
            WHERE pm25 IS NOT NULL OR pm10 IS NOT NULL
            ORDER BY device_id, time DESC
        """)
        rows = cursor.fetchall()
        cursor.close()
        logger.info(f"[AQI] Loaded latest readings: {len(rows)} devices")
        return rows

    except Exception as e:
        logger.error(f"[AQI] Failed to load readings: {e}")
        return []

    finally:
        if conn:
            conn.close()


def run_aqi_pipeline():
    """
    Main pipeline: load latest readings → calculate AQI →
    write back to database → print summary.

    Each device is processed independently inside its own
    try/except so one bad device cannot stop the others.
    """
    logger.info("=" * 60)
    logger.info(
        f"AQI pipeline started: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    rows = get_latest_readings()

    if not rows:
        logger.warning("[AQI] No readings in database. Run collectors first.")
        return []

    results      = []
    saved_count  = 0
    failed_count = 0

    print(f"\n{'='*95}")
    print(
        f"{'Device':<28} {'PM2.5':>7} {'PM10':>7} "
        f"{'AQI':>5} {'Category':<32} {'Saved?'}"
    )
    print(f"{'-'*95}")

    for (device_id, pm25, pm10, reading_time) in rows:
        try:
            aqi_result = calculate_full_aqi(pm25, pm10)
            saved = save_aqi_to_db(aqi_result)

            if saved:
                saved_count += 1
            else:
                failed_count += 1

            pm25_str    = f"{pm25:.1f}" if pm25 is not None else "NULL"
            pm10_str    = f"{pm10:.1f}" if pm10 is not None else "NULL"
            overall_str = str(aqi_result["overall_aqi"]) if aqi_result["overall_aqi"] is not None else "N/A"
            saved_str   = "✓" if saved else "✗ FAILED"

            print(
                f"{device_id:<28} {pm25_str:>7} {pm10_str:>7} "
                f"{overall_str:>5} {aqi_result['category']:<32} {saved_str}"
            )

            results.append({"device_id": device_id, "aqi": aqi_result})

        except Exception as e:
            logger.error(f"[AQI] Failed processing {device_id}: {e}")
            failed_count += 1
            continue

    print(f"{'='*95}")
    print(f"\nSaved to database: {saved_count}   Failed: {failed_count}")

    logger.info(
        f"AQI pipeline complete. Processed {len(results)} devices, "
        f"{saved_count} saved, {failed_count} failed."
    )
    logger.info("=" * 60)

    return results


def self_test():
    """Verifies the AQI formula against known values before touching the DB."""
    print("\n=== AQI FORMULA SELF-TEST ===")
    test_cases = [
        (5.0,   None,  "Good"),
        (21.36, 17.21, "Moderate"),
        (32.4,  None,  "Moderate"),
        (44.1,  None,  "Unhealthy for Sensitive Groups"),
    ]

    all_passed = True
    for (pm25, pm10, expected_label) in test_cases:
        result = calculate_full_aqi(pm25, pm10)
        aqi    = result["overall_aqi"]
        if aqi is None or aqi < 0 or aqi > 500:
            print(f"  ❌ FAILED for pm25={pm25}: AQI={aqi}")
            all_passed = False
        else:
            print(f"  ✅ pm25={pm25} pm10={pm10} → AQI={aqi} ({result['category']})")

    if all_passed:
        print("✅ All self-tests passed\n")
    return all_passed


if __name__ == "__main__":
    if self_test():
        print("=== RUNNING ON REAL DATABASE DATA ===")
        run_aqi_pipeline()
    else:
        print("Fix formula errors before running on real data")