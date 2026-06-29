# scheduler/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from app.ml.predict_job import predict_all

scheduler = BackgroundScheduler()

def start_scheduler():
    scheduler.add_job(
        predict_all, 
        'interval', 
        minutes=5, 
        id='run_ml_prediction',
        replace_existing=True
    )
    scheduler.start()