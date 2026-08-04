import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from utils.config import require_env
from utils.errors import ConfigurationError

load_dotenv()

logger = logging.getLogger("mongodb_export")

_client = None
_db = None


def _get_pymongo():
    try:
        import pymongo
        from bson import ObjectId
    except ImportError as exc:
        raise ConfigurationError(
            "pymongo is required for the MongoDB connector -- install it with: pip install pymongo"
        ) from exc
    return pymongo


def get_database():
    global _client, _db

    if _db is not None:
        return _db

    pymongo = _get_pymongo()

    mongo_uri = os.getenv("MONGO_URI")
    mongo_database = os.getenv("MONGO_DATABASE")
    require_env(MONGO_URI=mongo_uri, MONGO_DATABASE=mongo_database)

    logger.info("Connecting to MongoDB database '%s'", mongo_database)
    _client = pymongo.MongoClient(mongo_uri)
    _db = _client[mongo_database]
    return _db


def _stringify_object_ids(doc: Any) -> Any:
    from bson import ObjectId

    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, dict):
        return {key: _stringify_object_ids(value) for key, value in doc.items()}
    if isinstance(doc, list):
        return [_stringify_object_ids(item) for item in doc]
    return doc


def find_all(
    collection_name: str,
    query: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    _get_pymongo()
    db = get_database()

    cursor = db[collection_name].find(query or {}, projection)
    return [_stringify_object_ids(doc) for doc in cursor]
