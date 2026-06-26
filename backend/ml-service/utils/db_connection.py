"""
utils/db_connection.py
=======================
Connects Python scripts to the shared Timescale Cloud database.

Your partner set up this database using Prisma (Node.js).
We connect to the same database from Python using psycopg2.
Both tools talk to the same PostgreSQL tables.

Tables created by your partner:
    sensor_data   ← we insert air quality readings here
    predictions   ← GP/LSTM model forecasts go here
    aqi_records   ← AQI calculations go here
    alerts        ← threshold breach alerts go here

Authors: Shaline Wambui, Shalom Wanjiku
University of Bremen — Cosmos Labs
"""

import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """
    Returns a live connection to Timescale Cloud.
    SSL is mandatory — Timescale Cloud enforces this.
    """
    database_url= os.getenv("DATABASE_URL")
    
    if not database_url:
        print("[ERROR] DATABASE_URL is empty - .env not loading")
        raise ValueError("DATABASE_URL not found in environment")
    
    
    try:
        conn = psycopg2.connect(
            database_url,
            # sslmode="require"
        )
        return conn

    except psycopg2.OperationalError as e:
        print(f"[DB ERROR] Cannot connect to Timescale Cloud: {e}")
        raise


def test_connection():
    """
    Run this file directly to verify the connection works.
    python3 utils/db_connection.py
    """
    conn   = get_connection()
    cursor = conn.cursor()

    # Check PostgreSQL version
    cursor.execute("SELECT version();")
    version = cursor.fetchone()
    print(f"[DB OK] Connected to Timescale Cloud")
    print(f"[DB OK] {version[0][:60]}...")

    # Check all four tables exist
    cursor.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name;
    """)
    tables = [row[0] for row in cursor.fetchall()]
    print(f"\n[DB OK] Tables found: {tables}")

    expected = ["alerts", "aqi_records", "predictions", "sensor_data"]
    for t in expected:
        status = "✓" if t in tables else "✗ MISSING"
        print(f"  {status}  {t}")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    test_connection()