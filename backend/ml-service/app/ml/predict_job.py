# ml/predict_job.py
import datetime
from app.ml.model_manager import ModelManager
from db import get_connection, get_latest_device_timestamp

manager = ModelManager()

def predict_all():
    print("[PREDICT] Running periodic 1-hour ahead adaptive regional forecasting job...")
    
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
            
            # 3. Request mean values and uncertainties from the dynamic manager
            pm25_pred, pm25_std = manager.predict(device, current_time_input, return_std=True)
            
            # 4. Filter out any impossible negative mathematical values
            pm25_pred = max(0.0, float(pm25_pred))
            pm25_lower = max(0.0, float(pm25_pred - (1.96 * pm25_std)))
            pm25_upper = float(pm25_pred + (1.96 * pm25_std))
            
            pm10_pred = None  # Standing structural placeholder
            model_used_label = f"GP_Local_{device}"
            
            # 5. Connect and commit the values back to your tracking table schema
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
            
            print(f"[PREDICT] Saved future forecast for {device} targeting time: {forecast_timestamp} (PM2.5: {pm25_pred:.2f})")
            
        except FileNotFoundError:
            print(f"[PREDICT] Skipping {device}: Model binaries not yet compiled. Run train.py first.")
        except Exception as e:
            print(f"[PREDICT] Failed pipeline calculations for {device}: {e}")