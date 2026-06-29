# db.py
import os
import time
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL")

def get_connection():
    if not DB_URL:
        raise Exception("DATABASE_URL environment variable is not configured.")
    return psycopg2.connect(DB_URL)

def fetch_sensor_data(device_id=None):
    """Fetches valid timestamp epochs and target readings for specific or distinct devices."""
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if device_id:
        query = """
            SELECT EXTRACT(EPOCH FROM time) as timestamp_epoch, pm25, pm10 
            FROM sensor_data 
            WHERE device_id = %s AND (pm25 IS NOT NULL OR pm10 IS NOT NULL)
            ORDER BY time ASC
        """
        cur.execute(query, (device_id,))
        results = cur.fetchall()
    else:
        query = "SELECT DISTINCT device_id FROM sensor_data WHERE device_id IS NOT NULL"
        cur.execute(query)
        results = cur.fetchall()
        
    cur.close()
    conn.close()
    return results

def get_latest_device_timestamp(device_id):
    """Finds the absolute newest record timestamp context for a specific location/device."""
    conn = get_connection()
    cur = conn.cursor()
    
    query = """
        SELECT EXTRACT(EPOCH FROM time), time 
        FROM sensor_data 
        WHERE device_id = %s AND (pm25 IS NOT NULL OR pm10 IS NOT NULL)
        ORDER BY time DESC 
        LIMIT 1;
    """
    cur.execute(query, (device_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    if not result:
        now = time.time()
        return now, datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        
    return float(result[0]), result[1]