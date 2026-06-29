from fastapi import FastAPI
from pydantic import BaseModel
from app.scheduler.scheduler import start_scheduler

app = FastAPI()

class InputData(BaseModel):
    value: float


@app.get("/")
def home():
    return {"message": "ML service running 🚀"}


@app.post("/predict")
def predict(data: InputData):
    result = data.value * 2  # placeholder ML logic
    return {"prediction": result}


# ✅ START scheduler properly
@app.on_event("startup")
def startup_event():
    start_scheduler()
    print("[FASTAPI] Scheduler started")


