# ml/model_manager.py
import os
import joblib

class ModelManager:
    def __init__(self):
        self.model_dir = os.path.join(os.path.dirname(__file__), "models")
        self._loaded_models = {}

    def predict(self, device_id: str, X, return_std=True):
        """
        Dynamically extracts confidence standard deviations from specific localized model pipelines.
        X must be an explicitly structured 2D array with 1 feature: e.g., [[timestamp_epoch]]
        """
        model_key = f"{device_id}_pm25"
        
        if model_key not in self._loaded_models:
            model_path = os.path.join(self.model_dir, f"{model_key}.pkl")
            if not os.path.exists(model_path):
                raise FileNotFoundError(f"No configured model exists on disk for device identifier: {device_id}")
            
            self._loaded_models[model_key] = joblib.load(model_path)
            
        pipeline = self._loaded_models[model_key]
        
        scaler = pipeline.named_steps['scaler']
        gp_model = pipeline.named_steps['gp']
        
        X_scaled = scaler.transform(X)
        
        if return_std:
            mean, std = gp_model.predict(X_scaled, return_std=True)
            return mean[0], std[0]
        
        mean = gp_model.predict(X_scaled, return_std=False)
        return mean[0], 0.0