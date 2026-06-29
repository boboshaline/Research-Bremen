# ml/predict_job.py
import datetime
from app.ml.model_manager import ModelManager
from db import get_connection, get_latest_device_timestamp

manager = ModelManager()

def predict_all():
    print("[PREDICT] Running periodic 1-hour ahead adaptive dual-target (PM2.5 & PM10) forecasting job...")
    
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT device_id FROM sensor_data WHERE device_id IS NOT NULL;")
    devices = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    for device in devices:
        try:
            # 1. Dynamically target the exact timeline context for this specific region
            latest_epoch, latest_datetime = get_latest_device_timestamp(device)
            
            # 2. Shift calculation parameters forward by precisely 1 hour (3600 seconds)
            one_hour_in_seconds = 3600
            forecast_epoch = latest_epoch + one_hour_in_seconds
            forecast_timestamp = latest_datetime + datetime.timedelta(hours=1)
            
            current_time_input = [[forecast_epoch]] 
            
            # 3. Request predictions for PM2.5 with full uncertainty bounds
            try:
                pm25_pred, pm25_std = manager.predict(device, "pm25", current_time_input, return_std=True)
                pm25_pred = max(0.0, float(pm25_pred))
                pm25_lower = max(0.0, float(pm25_pred - (1.96 * pm25_std)))
                pm25_upper = float(pm25_pred + (1.96 * pm25_std))
            except FileNotFoundError:
                print(f"[PREDICT] Info: No PM2.5 model variant found for {device}. Skipping PM2.5 parameters.")
                pm25_pred, pm25_lower, pm25_upper = None, None, None

            # 4. Request predictions for PM10
            try:
                pm10_pred, _ = manager.predict(device, "pm10", current_time_input, return_std=False)
                pm10_pred = max(0.0, float(pm10_pred))
            except FileNotFoundError:
                print(f"[PREDICT] Info: No PM10 model variant found for {device}. Skipping PM10 parameters.")
                pm10_pred = None

            # 5. Connect and commit the combined values back to your tracking table schema
            if pm25_pred is not None or pm10_pred is not None:
                model_used_label = f"GP_Local_{device}"
                
                db_conn = get_connection()
                db_cur = db_conn.cursor()
                
                insert_query = """
                    INSERT INTO predictions (
                        created_at, forecast_time, pm25_predicted, pm25_lower, pm25_upper, pm10_predicted, model_used
                    ) VALUES (NOW(), %s, %s, %s, %s, %s, %s);
                """
                
                db_cur.execute(insert_query, (
                    forecast_timestamp,
                    pm25_pred,
                    pm25_lower,
                    pm25_upper,
                    pm10_pred,
                    model_used_label
                ))
                
                db_conn.commit()
                db_cur.close()
                db_conn.close()
                print(f"[PREDICT] Successfully committed dual forecast for {device} target time: {forecast_timestamp}")
            else:
                print(f"[PREDICT] Skipping DB commit for {device}: No operational models found.")
                
        except Exception as e:
            print(f"[PREDICT] Failed pipeline calculations for {device}: {e}")