"""
preprocessing/preprocessor.py
================================
Data quality pipeline for triangulated air quality data.

WHAT THIS DOES:
    Takes raw readings from ALL four sources and:
    1. Detects outliers using rolling Z-score method
    2. Fills missing values using rolling window mean
    3. Produces a data quality report per city per source
    4. Produces a proper triangulation comparison table

WHY THIS MATTERS FOR TRIANGULATION:
    Each source has different data quality characteristics:

    Open-Meteo  → rarely missing, sometimes model error
    OpenAQ      → frequently missing in African cities
    UBA Germany → very reliable, reference grade
    AirQo       → variable quality, low-cost sensors

    After preprocessing, we can compare CLEAN values
    across sources. This comparison is Section 4
    of your research paper.

TWO CORE METHODS:

    1. ROLLING Z-SCORE OUTLIER DETECTION (window=24h, threshold=3.0)
       Uses a 24-hour rolling mean/std as baseline so diurnal
       pollution patterns (morning rush, evening cooking) are
       not falsely flagged as outliers. Global Z-score would
       flag real peaks as anomalies — this is the correct approach
       for time-series air quality data.

    2. ROLLING WINDOW IMPUTATION (window=6 readings)
       Missing value = average of previous 6 readings.
       At hourly data: window = 6 hours of history.
       We use PAST data only — using future data would be
       data leakage, invalidating our model evaluation.

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import pandas as pd
import numpy as np
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
from utils.db_connection import get_connection

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/preprocessor.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# STEP 1 — LOAD RAW DATA FROM DATABASE
# ══════════════════════════════════════════════════════════════

def load_raw_readings(hours=48):
    """
    Loads the last N hours of readings from all sources.

    Excludes future-timestamped rows (Open-Meteo forecast
    hours beyond now) — these are forecast artifacts, not
    observations, and should not enter the training pipeline.

    Returns: pandas DataFrame with all sources combined.
    """
    conn = get_connection()
    sql  = f"""
        SELECT
            id,
            time,
            device_id,
            pm25,
            pm10,
            temperature,
            humidity
        FROM sensor_data
        WHERE time >= NOW() - INTERVAL '{hours} hours'
          AND time <= NOW()
        ORDER BY device_id, time ASC
    """

    try:
        df = pd.read_sql(sql, conn)
        conn.close()

        logger.info(
            f"Loaded {len(df)} raw readings "
            f"from last {hours} hours (future rows excluded)"
        )
        logger.info(
            f"Sources present: "
            f"{df['device_id'].unique().tolist()}"
        )
        return df

    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        conn.close()
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════
# STEP 2 — OUTLIER DETECTION
# ══════════════════════════════════════════════════════════════

def detect_outliers_zscore(series, window=24, threshold=3.0):
    """
    Detects outliers using a ROLLING Z-score.

    WHY ROLLING, NOT GLOBAL:
        PM2.5 follows strong diurnal patterns — peaks during
        morning rush hour and evening cooking, drops overnight.
        A global Z-score treats these real peaks as anomalies.
        A rolling 24-hour window adapts the baseline to the
        local time-of-day pattern, flagging only genuine
        hardware spikes or measurement errors.

    HOW IT WORKS:
        For each point t:
            rolling_mean = mean of previous `window` readings
            rolling_std  = std  of previous `window` readings
            Z = |value - rolling_mean| / rolling_std
        If Z > threshold → flag as outlier

    Falls back to global Z-score for series shorter than window
    (e.g. AirQo with only 3-20 rows), since a rolling window
    would produce too many NaN baselines on short series.

    Parameters:
        series    (pd.Series): PM2.5 or PM10 values
        window    (int):       rolling window size (default 24h)
        threshold (float):     Z-score cutoff (default 3.0)

    Returns:
        pd.Series of booleans — True = outlier
    """
    if len(series.dropna()) < 3:
        return pd.Series([False] * len(series), index=series.index)

    # Use rolling Z-score for long series, global for short ones
    if len(series.dropna()) >= window:
        rolling_mean = series.rolling(window=window, min_periods=3).mean()
        rolling_std  = series.rolling(window=window, min_periods=3).std()
    else:
        # Short series: fall back to global statistics
        rolling_mean = pd.Series([series.mean()] * len(series), index=series.index)
        rolling_std  = pd.Series([series.std()]  * len(series), index=series.index)

    # Avoid division by zero when std is 0
    rolling_std = rolling_std.replace(0, np.nan)

    z_scores = np.abs((series - rolling_mean) / rolling_std)
    outliers = z_scores > threshold

    # NaN z-scores (insufficient history) → not an outlier
    outliers = outliers.fillna(False)

    outlier_count = outliers.sum()
    if outlier_count > 0:
        logger.info(
            f"Rolling Z-score outliers detected: {outlier_count} "
            f"(window={window}, threshold={threshold})"
        )

    return outliers


# ══════════════════════════════════════════════════════════════
# STEP 3 — MISSING VALUE IMPUTATION
# ══════════════════════════════════════════════════════════════

def impute_missing_rolling(series, window=6):
    """
    Fills missing (NaN) values using rolling window mean.

    Uses past data only (no forward-look) to prevent data
    leakage into model evaluation. See docstring in original
    for full explanation.
    """
    missing_before = series.isna().sum()

    if missing_before == 0:
        return series

    rolling_mean = series.rolling(window=window, min_periods=1).mean()
    filled       = series.fillna(rolling_mean)

    missing_after = filled.isna().sum()
    imputed       = missing_before - missing_after

    logger.info(
        f"Imputation: {imputed} values filled, "
        f"{missing_after} remain (window={window})"
    )

    return filled


# ══════════════════════════════════════════════════════════════
# STEP 4 — PROCESS ONE SOURCE + CITY COMBINATION
# ══════════════════════════════════════════════════════════════

def preprocess_group(df_group, label):
    """
    Runs full preprocessing on one city+source group.

    Pipeline order:
        1. Sort by time (critical for rolling window)
        2. Deduplicate on (time, device_id) — keeps first row
           This removes Open-Meteo duplicate future timestamps
        3. Detect PM2.5 outliers → set to NaN
        4. Detect PM10 outliers  → set to NaN
        5. Impute PM2.5 missing values
        6. Impute PM10 missing values
        7. Generate quality report
    """
    logger.info(f"\nPreprocessing: {label} ({len(df_group)} rows)")

    df = df_group.copy()
    df = df.sort_values("time").reset_index(drop=True)

    # ── Deduplication ──────────────────────────────────────────
    # Open-Meteo collector may have inserted the same future
    # timestamp on multiple runs (ON CONFLICT needs a unique
    # constraint — add one to sensor_data if not present).
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["time", "device_id"], keep="first")
    dupes_removed = before_dedup - len(df)
    if dupes_removed > 0:
        logger.warning(
            f"{label}: removed {dupes_removed} duplicate "
            f"(time, device_id) rows"
        )

    # ── Outlier detection ──────────────────────────────────────
    df["pm25_outlier"] = detect_outliers_zscore(df["pm25"])
    df["pm10_outlier"] = detect_outliers_zscore(df["pm10"])

    df.loc[df["pm25_outlier"], "pm25"] = np.nan
    df.loc[df["pm10_outlier"], "pm10"] = np.nan

    # ── Imputation ─────────────────────────────────────────────
    df["pm25_clean"] = impute_missing_rolling(df["pm25"])
    df["pm10_clean"] = impute_missing_rolling(df["pm10"])

    # ── Quality report ─────────────────────────────────────────
    total     = len(df)
    pm25_miss = df["pm25"].isna().sum()
    pm10_miss = df["pm10"].isna().sum()
    pm25_out  = int(df["pm25_outlier"].sum())
    pm10_out  = int(df["pm10_outlier"].sum())

    pm25_miss_pct = round((pm25_miss / total) * 100, 1) if total > 0 else 0
    pm10_miss_pct = round((pm10_miss / total) * 100, 1) if total > 0 else 0

    logger.info(
        f"\n{'─'*45}\n"
        f"  Group     : {label}\n"
        f"  Total rows: {total}\n"
        f"  PM2.5 missing : {pm25_miss} ({pm25_miss_pct}%)\n"
        f"  PM10  missing : {pm10_miss} ({pm10_miss_pct}%)\n"
        f"  PM2.5 outliers: {pm25_out}\n"
        f"  PM10  outliers: {pm10_out}\n"
        f"{'─'*45}"
    )

    return df


# ══════════════════════════════════════════════════════════════
# STEP 5 — TRIANGULATION COMPARISON
# ══════════════════════════════════════════════════════════════

def triangulation_comparison(all_results):
    """
    Compares PM2.5 values across sources per city.

    Groups results by city (extracted from device_id prefix
    e.g. 'openmeteo-bremen-de' → city='bremen') and prints
    a labelled comparison table per city.

    For Bremen, computes deviation from UBA reference to
    quantify each source's accuracy — this is Table 3
    in your research paper.
    """
    logger.info("\n" + "="*65)
    logger.info("TRIANGULATION COMPARISON")
    logger.info("="*65)

    # Extract city from device_id: 'openmeteo-nairobi-ke' → 'nairobi'
    def extract_city(device_id):
        parts = device_id.split("-")
        return parts[1] if len(parts) >= 2 else device_id

    # Group device_ids by city
    cities = {}
    for device_id in all_results:
        city = extract_city(device_id)
        cities.setdefault(city, []).append(device_id)

    for city, device_ids in sorted(cities.items()):
        logger.info(f"\nCity: {city.upper()}")
        logger.info(f"  {'Source':<30} {'PM2.5':>8} {'PM10':>8} {'Rows':>6}")
        logger.info(f"  {'─'*56}")

        uba_pm25 = None

        # Collect stats per source
        rows = []
        for device_id in sorted(device_ids):
            df  = all_results[device_id]
            p25 = df["pm25_clean"].mean()
            p10 = df["pm10_clean"].mean()
            n   = len(df)

            p25_str = f"{p25:.2f}" if not np.isnan(p25) else "No data"
            p10_str = f"{p10:.2f}" if not np.isnan(p10) else "No data"

            rows.append((device_id, p25, p10, n, p25_str, p10_str))

            if "uba" in device_id:
                uba_pm25 = p25

        for device_id, p25, p10, n, p25_str, p10_str in rows:
            # Compute deviation from UBA for Bremen sources
            deviation = ""
            if uba_pm25 and not np.isnan(p25) and "uba" not in device_id and city == "bremen":
                diff = p25 - uba_pm25
                pct  = (diff / uba_pm25) * 100
                sign = "+" if diff >= 0 else ""
                deviation = f"  ({sign}{pct:.0f}% vs UBA)"

            logger.info(
                f"  {device_id:<30} {p25_str:>8} {p10_str:>8} "
                f"{n:>6}{deviation}"
            )

    logger.info("\n" + "="*65)


# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════

def run_preprocessing_pipeline():
    """
    Runs the full preprocessing pipeline for all
    city + source combinations in the database.

    Called every 30 minutes by the scheduler.
    """
    logger.info("\n" + "="*55)
    logger.info(
        f"Preprocessing pipeline started: "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    df = load_raw_readings(hours=48)

    if df.empty:
        logger.warning("No data found — is the collector running?")
        return {}

    all_results = {}

    for device_id, group in df.groupby("device_id"):
        cleaned = preprocess_group(group, device_id)
        all_results[device_id] = cleaned

    if len(all_results) > 1:
        triangulation_comparison(all_results)

    logger.info(
        f"\nPreprocessing complete. "
        f"Processed {len(all_results)} city/source groups."
    )

    return all_results


# ── Run directly ───────────────────────────────────────────────
if __name__ == "__main__":
    results = run_preprocessing_pipeline()

    if results:
        print(f"\n{'='*55}")
        print("SAMPLE OUTPUT — Last 5 rows per group")
        print(f"{'='*55}")

        for label, df in results.items():
            print(f"\n{label}")
            print(
                df[[
                    "time",
                    "pm25", "pm25_clean", "pm25_outlier",
                    "pm10", "pm10_clean", "pm10_outlier"
                ]].tail(5).to_string(index=False)
            )
    else:
        print(
            "\nNo data processed. "
            "Make sure your collectors have run first."
        )