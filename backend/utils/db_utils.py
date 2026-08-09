# backend/utils/db_utils.py
import os
import logging
from pathlib import Path
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)
logger = logging.getLogger("Incident Engine Logger")
_client = None
_client_uri = None

def get_db():
    global _client, _client_uri
    if os.getenv("USE_DB") != "true":
        logger.info("Not Using MongoDB")
        return None

    uri = os.getenv("MONGO_URI")
    db_name = os.getenv("MONGO_DB_NAME", "errAgent_DB")

    if not uri:
        logger.error("MONGO_URI environment variable is missing.")
        return None

    # Recreate client when URI changes (e.g., password rotation while dev server stays up).
    if _client is None or _client_uri != uri:
        try:
            candidate = MongoClient(uri, serverSelectionTimeoutMS=5000)
            candidate.admin.command("ping")
            _client = candidate
            _client_uri = uri
            logger.info("MongoDB client initialized successfully.")
        except PyMongoError as e:
            logger.error(f"MongoDB connection failed: {e}")
            _client = None
            _client_uri = None
            return None
    
    # Target database for this new project
    return _client[db_name]

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