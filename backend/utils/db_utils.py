# backend/utils/db_utils.py
import os
import logging
from pathlib import Path
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)
logger = logging.getLogger("Incident Engine Logger")
_client = None

def get_db():
    global _client
    if os.getenv("USE_DB") != "true":
        logger.info("Not Using MongoDB")
        return None
        
    if _client is None:
        uri = os.getenv("MONGO_URI")
        _client = MongoClient(uri)
    
    # Target database for this new project
    return _client['errAgent_DB']

def test_connection():
    db = get_db()
    if db is None:
        return False
    try:
        db.command('ping')
        logger.info("Successfully connected to MongoDB!")
        return True
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        return False