"""

THE MISSING LINK: connects GP model predictions to AQI.

WHY THIS FILE MATTERS FOR YOUR RESEARCH:
    Your GP model predicts: "PM2.5 will be 14.2 µg/m³ in 1 hour"
    This file converts that into: "AQI will be 56 (Moderate) 
    in 1 hour, with 95% confidence between AQI 48 and AQI 64"

    This is the actual answer to your research question:
    not just "can AI estimate PM2.5" but "can AI tell us
    whether air quality will become unhealthy" — which is
    the question that matters for public health.

WHY WE CONVERT THE CONFIDENCE INTERVAL TOO:
    A point prediction of AQI=56 hides the uncertainty.
    Converting BOTH pm25_lower and pm25_upper into AQI
    gives a confidence RANGE: "AQI between 48 and 64".
    If that range crosses a health threshold (e.g. 100),
    that uncertainty itself is the alert-worthy information.

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
from app.aqi.aqi_calculator import (
    calculate_aqi_single,
    get_category,
    PM25_BREAKPOINTS,
    PM10_BREAKPOINTS
)

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/aqi_forecaster.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_unprocessed_predictions():
    """
    Gets predictions that haven't had AQI calculated yet.
    Uses the most recent prediction per model_used (device).
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ON (model_used)
            id, model_used, forecast_time,
            pm25_predicted, pm25_lower, pm25_upper,
            pm10_predicted
        FROM predictions
        WHERE pm25_predicted IS NOT NULL
        ORDER BY model_used, created_at DESC;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    logger.info(f"Loaded {len(rows)} latest predictions")
    return rows


def forecast_aqi_with_uncertainty(pm25, pm25_lower, pm25_upper, pm10=None):
    """
    Converts a PM2.5 prediction AND its confidence interval
    into AQI point estimate + AQI confidence range.

    WORKED EXAMPLE:
        pm25=14.2, pm25_lower=13.8, pm25_upper=14.7

        AQI(14.2) = 56  ← point forecast
        AQI(13.8) = 55  ← lower bound forecast
        AQI(14.7) = 58  ← upper bound forecast

        Result: "AQI will be 56, ranging 55-58"
        This is a NARROW, confident forecast.

    CONTRAST EXAMPLE (sparse data):
        pm25=25.4, pm25_lower=8.8, pm25_upper=41.9

        AQI(25.4) = 80   ← point forecast (Moderate)
        AQI(8.8)  = 37   ← lower bound (Good)
        AQI(41.9) = 117  ← upper bound (Unhealthy for Sensitive)

        Result: "AQI could be anywhere from Good to Unhealthy"
        This WIDE range is itself the research finding —
        sparse data produces health-relevant uncertainty,
        not just numerical uncertainty.

    Returns: dict with point AQI and range AQI
    """
    aqi_point = calculate_aqi_single(pm25, PM25_BREAKPOINTS)
    aqi_lower = calculate_aqi_single(pm25_lower, PM25_BREAKPOINTS) if pm25_lower is not None else None
    aqi_upper = calculate_aqi_single(pm25_upper, PM25_BREAKPOINTS) if pm25_upper is not None else None

    pm10_aqi = calculate_aqi_single(pm10, PM10_BREAKPOINTS) if pm10 is not None else None

    category = get_category(aqi_point)

    # Check if the uncertainty range crosses a health-relevant
    # threshold (e.g. Good→Moderate at 50, Moderate→Unhealthy at 100)
    crosses_threshold = False
    if aqi_lower is not None and aqi_upper is not None:
        # Check each major threshold boundary
        for boundary in [50, 100, 150, 200, 300]:
            if aqi_lower < boundary <= aqi_upper:
                crosses_threshold = True
                break

    return {
        "aqi_point":          aqi_point,
        "aqi_lower":          aqi_lower,
        "aqi_upper":          aqi_upper,
        "aqi_range_width":    (aqi_upper - aqi_lower) if (aqi_upper and aqi_lower) else None,
        "pm10_aqi":           pm10_aqi,
        "category":           category["name"],
        "colour":             category["colour"],
        "advice":             category["advice"],
        "crosses_threshold":  crosses_threshold
    }


def run_aqi_forecast_pipeline():
    """
    Main function — converts all latest GP predictions
    into AQI forecasts with uncertainty ranges.

    This is what makes your GP model's output
    health-meaningful rather than just a number.
    """
    logger.info("=" * 70)
    logger.info("AQI FORECAST PIPELINE — converting predictions to AQI")

    predictions = get_unprocessed_predictions()

    if not predictions:
        logger.warning("No predictions found. Run predict_job.py first.")
        return []

    results = []

    print(f"\n{'='*100}")
    print(
        f"{'Device':<32} {'PM2.5 fc':>9} {'AQI':>5} "
        f"{'AQI range':>12} {'Category':<28} {'Alert?'}"
    )
    print("-" * 100)

    for (pred_id, model_used, forecast_time,
         pm25, pm25_lower, pm25_upper, pm10) in predictions:

        result = forecast_aqi_with_uncertainty(
            pm25, pm25_lower, pm25_upper, pm10
        )

        device_id = model_used.replace("GP_Local_", "")

        range_str = (
            f"{result['aqi_lower']}-{result['aqi_upper']}"
            if result['aqi_lower'] is not None
            else "N/A"
        )
        alert_flag = "⚠️ THRESHOLD" if result['crosses_threshold'] else ""

        print(
            f"{device_id:<32} {pm25:>9.2f} "
            f"{str(result['aqi_point']):>5} {range_str:>12} "
            f"{result['category']:<28} {alert_flag}"
        )

        results.append({
            "device_id": device_id,
            "forecast_time": forecast_time,
            "aqi_forecast": result
        })

    print("=" * 100)

    # Summary of uncertainty findings — this is your research data
    print(f"\nUNCERTAINTY ANALYSIS:")
    print(f"{'-'*60}")
    widths = [
        r["aqi_forecast"]["aqi_range_width"]
        for r in results
        if r["aqi_forecast"]["aqi_range_width"] is not None
    ]
    if widths:
        print(f"  Narrowest AQI range: {min(widths)} points")
        print(f"  Widest AQI range:    {max(widths)} points")
        print(f"  Average AQI range:   {sum(widths)/len(widths):.1f} points")

    crossing_count = sum(
        1 for r in results if r["aqi_forecast"]["crosses_threshold"]
    )
    print(f"  Forecasts crossing a health threshold: {crossing_count}/{len(results)}")

    logger.info(f"AQI forecast complete for {len(results)} devices")
    logger.info("=" * 70)

    return results


if __name__ == "__main__":
    run_aqi_forecast_pipeline()