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
    print("[TRAIN] Starting localized training execution loop...")
    
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT device_id FROM sensor_data WHERE device_id IS NOT NULL;")
    devices = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    for device in devices:
        print(f"[TRAIN] Fetching timeline series records for {device}...")
        data = fetch_sensor_data(device_id=device)
        
        if len(data) < 5:
            print(f"[TRAIN] Skipping {device}: Insufficient metrics for spatial/temporal fitting.")
            continue
            
        # Structure X explicitly as a 2D array with 1 single feature (Time)
        X = np.array([row['timestamp_epoch'] for row in data]).reshape(-1, 1)
        y_pm25 = np.array([row['pm25'] for row in data])
        
        kernel = C(1.0, (1e-3, 1e3)) * RBF(10, (1e-2, 1e2))
        pm25_pipeline = Pipeline([
            ('scaler', MinMaxScaler()),
            ('gp', GaussianProcessRegressor(kernel=kernel, n_restarts_optimizer=9, alpha=0.1, random_state=42))
        ])
        
        print(f"[TRAIN] Training Gaussian Process Regression Pipeline for {device}...")
        pm25_pipeline.fit(X, y_pm25)
        
        model_path = os.path.join(MODEL_DIR, f"{device}_pm25.pkl")
        joblib.dump(pm25_pipeline, model_path)
        print(f"[TRAIN] Saved isolated model successfully to {model_path}")

if __name__ == "__main__":
    train_all_local_models()