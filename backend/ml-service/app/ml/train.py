# ml/train.py
import os
import joblib
import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel as C
from db import fetch_sensor_data, get_connection

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

def train_all_local_models():
    print("[TRAIN] Starting localized training execution loop for PM2.5 and PM10...")
    
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT device_id FROM sensor_data WHERE device_id IS NOT NULL;")
    devices = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    for device in devices:
        print(f"[TRAIN] Fetching timeline series records for {device}...")
        data = fetch_sensor_data(device_id=device)
        
        if len(data) < 3:
            print(f"[TRAIN] Skipping {device}: Insufficient metrics for spatial/temporal fitting.")
            continue
            
        # Common time feature matrix X
        X = np.array([row['timestamp_epoch'] for row in data]).reshape(-1, 1)
        
        # 1. Process PM2.5 Targets
        # y_pm25 = np.array([row['pm25'] for row in data])
        y_pm25 = np.array([row['pm25'] if row['pm25'] is not None else np.nan for row in data], dtype=float)
        # valid_pm25_idx = ~np.isnan(y_pm25) & (y_pm25 != None)
        valid_pm25_idx = ~np.isnan(y_pm25)
        
        if np.sum(valid_pm25_idx) >= 5:
            X_pm25 = X[valid_pm25_idx]
            y_pm25_clean = y_pm25[valid_pm25_idx].astype(float)
            
            kernel_pm25 = C(1.0, (1e-3, 1e3)) * RBF(10, (1e-2, 1e2))
            pm25_pipeline = Pipeline([
                ('scaler', MinMaxScaler()),
                ('gp', GaussianProcessRegressor(kernel=kernel_pm25, n_restarts_optimizer=9, alpha=0.1, random_state=42))
            ])
            print(f"[TRAIN] Training PM2.5 Pipeline for {device}...")
            pm25_pipeline.fit(X_pm25, y_pm25_clean)
            
            model_path_pm25 = os.path.join(MODEL_DIR, f"{device}_pm25.pkl")
            joblib.dump(pm25_pipeline, model_path_pm25)
            print(f"[TRAIN] Saved PM2.5 model successfully to {model_path_pm25}")
        else:
            print(f"[TRAIN] Skipping PM2.5 for {device}: Insufficient valid target values.")

        # 2. Process PM10 Targets
        # y_pm10 = np.array([row['pm10'] for row in data])
        # valid_pm10_idx = ~np.isnan(y_pm10) & (y_pm10 != None)
        y_pm10 = np.array([row['pm10'] if row['pm10'] is not None else np.nan for row in data], dtype=float)
        valid_pm10_idx = ~np.isnan(y_pm10)
        
        
        if np.sum(valid_pm10_idx) >= 5:
            X_pm10 = X[valid_pm10_idx]
            y_pm10_clean = y_pm10[valid_pm10_idx].astype(float)
            
            kernel_pm10 = C(1.0, (1e-3, 1e3)) * RBF(10, (1e-2, 1e2))
            pm10_pipeline = Pipeline([
                ('scaler', MinMaxScaler()),
                ('gp', GaussianProcessRegressor(kernel=kernel_pm10, n_restarts_optimizer=9, alpha=0.1, random_state=42))
            ])
            print(f"[TRAIN] Training PM10 Pipeline for {device}...")
            pm10_pipeline.fit(X_pm10, y_pm10_clean)
            
            model_path_pm10 = os.path.join(MODEL_DIR, f"{device}_pm10.pkl")
            joblib.dump(pm10_pipeline, model_path_pm10)
            print(f"[TRAIN] Saved PM10 model successfully to {model_path_pm10}")
        else:
            print(f"[TRAIN] Skipping PM10 for {device}: Insufficient valid target values.")

if __name__ == "__main__":
    train_all_local_models()