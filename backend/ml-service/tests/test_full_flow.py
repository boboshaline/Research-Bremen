"""

Task: Test full data flow end to end

Verifies: collector → database → GP model → AQI → no errors.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import sys
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_connection
from app.aqi.aqi_calculator import run_aqi_pipeline, self_test


def check(label, condition, detail=""):
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"  {status}  {label}")
    if not condition and detail:
        print(f"         Detail: {detail}")
    return condition


def test_database_connection():
    print("\n── Step 1: Database Connection ──")
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT NOW();")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return check("Timescale Cloud connection", result is not None)
    except Exception as e:
        return check("Timescale Cloud connection", False, str(e))


def test_tables_exist():
    print("\n── Step 2: Tables Exist ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name;
    """)
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    required = ["sensor_data", "predictions", "aqi_records", "alerts"]
    return all(check(f"Table '{t}' exists", t in tables) for t in required)


def test_sensor_data_present():
    print("\n── Step 3: Sensor Data ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(pm25), COUNT(DISTINCT device_id)
        FROM sensor_data;
    """)
    total, pm25_count, devices = cur.fetchone()
    cur.close()
    conn.close()

    print(f"         Total rows: {total}  PM2.5 readings: {pm25_count}  Devices: {devices}")
    ok1 = check("At least 50 rows in sensor_data", total >= 50)
    ok2 = check("At least 3 devices reporting", devices >= 3)
    return ok1 and ok2


def test_gp_predictions_present():
    print("\n── Step 4: GP Model Predictions ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(DISTINCT model_used)
        FROM predictions
        WHERE pm25_predicted IS NOT NULL;
    """)
    total, models = cur.fetchone()
    cur.close()
    conn.close()

    print(f"         Predictions: {total}  Distinct models: {models}")
    return check("GP model has produced predictions", total > 0)


def test_aqi_formula():
    print("\n── Step 5: AQI Formula Validation ──")
    return check("AQI self-test passed", self_test())


def test_aqi_pipeline_writeback():
    print("\n── Step 6: AQI Pipeline + Database Write-back ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM aqi_records;")
    before = cur.fetchone()[0]
    cur.close()
    conn.close()

    results = run_aqi_pipeline()

    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM aqi_records;")
    after = cur.fetchone()[0]
    cur.close()
    conn.close()

    print(f"         aqi_records before: {before}  after: {after}")
    ok1 = check("AQI pipeline ran without crashing", len(results) > 0)
    ok2 = check("New rows written to aqi_records", after > before)
    return ok1 and ok2


def run_full_test():
    print("\n" + "="*60)
    print("FULL SYSTEM END-TO-END TEST")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)

    test_start_time = datetime.now(timezone.utc)

    steps = [
        ("Database connection",        test_database_connection),
        ("Tables exist",               test_tables_exist),
        ("Sensor data present",        test_sensor_data_present),
        ("GP model predictions exist", test_gp_predictions_present),
        ("AQI formula correct",        test_aqi_formula),
    ]

    results = {}
    for name, fn in steps:
        try:
            results[name] = fn()
        except Exception as e:
            print(f"\n  ❌ EXCEPTION in '{name}': {e}")
            results[name] = False

    # AQI write-back step is separate because it needs cleanup after
    try:
        ok, _ = test_aqi_pipeline_writeback()
        results["AQI pipeline + write-back"] = ok
    except Exception as e:
        print(f"\n  ❌ EXCEPTION in 'AQI pipeline + write-back': {e}")
        results["AQI pipeline + write-back"] = False

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    passed = sum(1 for v in results.values() if v)
    total  = len(results)

    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'}  {name}")

    print(f"\n  Result: {passed}/{total} steps passed")

    if passed == total:
        print("\n  ✅ FULL PIPELINE WORKING — collector → DB → GP model → AQI")
    else:
        failed = [n for n, v in results.items() if not v]
        print(f"\n  ❌ Failed: {failed}")

    # Always clean up test rows, pass or fail
    cleanup_test_rows(test_start_time)

    return passed == total

def test_aqi_pipeline_writeback():
    print("\n── Step 6: AQI Pipeline + Database Write-back ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM aqi_records;")
    before = cur.fetchone()[0]
    cur.close()
    conn.close()

    # Mark the test run start time so we can clean up
    # only the rows THIS test created — not real production data
    test_start_time = datetime.now(timezone.utc)

    results = run_aqi_pipeline()

    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM aqi_records;")
    after = cur.fetchone()[0]
    cur.close()
    conn.close()

    print(f"         aqi_records before: {before}  after: {after}")
    ok1 = check("AQI pipeline ran without crashing", len(results) > 0)
    ok2 = check("New rows written to aqi_records", after > before)

    return ok1 and ok2, test_start_time

def cleanup_test_rows(test_start_time):
    """
    Removes aqi_records rows created during THIS test run only.
    Uses the timestamp marker so we never touch real production
    data collected by the scheduler — only rows from manual
    test executions.
    """
    print("\n── Cleanup: Removing test-generated AQI rows ──")
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        DELETE FROM aqi_records
        WHERE timestamp >= %s;
    """, (test_start_time,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f"  Removed {deleted} test rows from aqi_records")

if __name__ == "__main__":
    success = run_full_test()
    exit(0 if success else 1)