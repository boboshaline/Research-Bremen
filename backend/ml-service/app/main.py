from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Example request model
class InputData(BaseModel):
    value: float

@app.get("/")
def home():
    return {"message": "ML service running 🚀"}

@app.post("/predict")
def predict(data: InputData):
    result = data.value * 2  # placeholder ML logic
    return {"prediction": result}