import os
import logging
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

_client = None
_col = None


def _get_collection():
    global _client, _col
    if _col is not None:
        return _col
    uri = os.getenv("MONGODB_URI")
    if not uri:
        logging.warning("MONGODB_URI not set — ID registry disabled")
        return None
    try:
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _client.admin.command("ping")
        db_name = os.getenv("MONGO_DB", "deposit_bot")
        col = _client[db_name]["id_registry"]
        col.create_index([("tg_id", ASCENDING)], unique=True, background=True)
        _col = col
        logging.info("ID registry MongoDB collection ready")
    except Exception as e:
        logging.warning(f"ID registry MongoDB error: {e}")
        _col = None
    return _col


def registry_register_user(tg_id: str) -> bool:
    """
    Register a Telegram user in the ID registry.
    Returns True if newly registered, False if already existed.
    Uses optimized field names: tg_id, reg_at, posters.
    """
    col = _get_collection()
    if col is None:
        return False
    tg_id = str(tg_id).strip()
    now = datetime.now(timezone.utc)
    try:
        col.insert_one({"tg_id": tg_id, "reg_at": now, "posters": []})
        logging.info(f"ID registry: new user {tg_id}")
        return True
    except DuplicateKeyError:
        return False
    except PyMongoError as e:
        logging.warning(f"ID registry register error: {e}")
        return False


def registry_get_user(tg_id: str):
    """Return user document (tg_id, reg_at, posters) or None."""
    col = _get_collection()
    if col is None:
        return None
    try:
        return col.find_one({"tg_id": str(tg_id).strip()}, {"_id": 0})
    except PyMongoError as e:
        logging.warning(f"ID registry get_user error: {e}")
        return None


def registry_is_registered(tg_id: str) -> bool:
    """Return True if user exists in the registry."""
    col = _get_collection()
    if col is None:
        return False
    try:
        return col.count_documents({"tg_id": str(tg_id).strip()}, limit=1) == 1
    except PyMongoError:
        return False


def registry_add_poster(tg_id: str, poster: str, value: str = "") -> bool:
    """Append a poster entry to an existing user. Returns True on success."""
    col = _get_collection()
    if col is None:
        return False
    try:
        r = col.update_one(
            {"tg_id": str(tg_id).strip()},
            {"$push": {"posters": {"poster": poster, "value": value}}}
        )
        return r.matched_count == 1
    except PyMongoError as e:
        logging.warning(f"ID registry add_poster error: {e}")
        return False


def registry_count() -> int:
    """Return total registered users."""
    col = _get_collection()
    if col is None:
        return 0
    try:
        return col.count_documents({})
    except PyMongoError:
        return 0
