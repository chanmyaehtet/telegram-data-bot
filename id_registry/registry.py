import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

_client: MongoClient | None = None
_col: Collection | None = None


def _get_collection() -> Collection:
    global _client, _col
    if _col is None:
        uri = os.environ["MONGO_URI"]
        _client = MongoClient(uri)
        db_name = os.environ.get("MONGO_DB", "telegram_bot")
        db = _client[db_name]
        _col = db["id_registry"]
        _col.create_index([("tg_id", ASCENDING)], unique=True, background=True)
    return _col


# ---------------------------------------------------------------------------
# Core registry operations
# ---------------------------------------------------------------------------

def register_user(tg_id: str) -> dict:
    """
    Register a user in the ID registry.
    Returns the existing document if already registered, otherwise inserts and
    returns the new document.

    Stored fields (optimized):
      tg_id   — Telegram user ID or username
      reg_at  — UTC timestamp of first registration
      posters — list of {poster, value} entries added by other users
    """
    col = _get_collection()
    tg_id = str(tg_id).strip()
    now = datetime.now(timezone.utc)

    doc = {
        "tg_id": tg_id,
        "reg_at": now,
        "posters": [],
    }

    try:
        col.insert_one(doc)
        doc.pop("_id", None)
        return {"created": True, "user": doc}
    except DuplicateKeyError:
        existing = col.find_one({"tg_id": tg_id}, {"_id": 0})
        return {"created": False, "user": existing}


def get_user(tg_id: str) -> dict | None:
    """
    Look up a user by Telegram ID.
    Returns the document (without _id) or None if not found.
    """
    col = _get_collection()
    return col.find_one({"tg_id": str(tg_id).strip()}, {"_id": 0})


def is_registered(tg_id: str) -> bool:
    """Return True if the user already exists in the registry."""
    col = _get_collection()
    return col.count_documents({"tg_id": str(tg_id).strip()}, limit=1) == 1


def add_poster(tg_id: str, poster: str, value: str = "") -> bool:
    """
    Append a poster entry to an existing user's record.
    Returns True on success, False if the user was not found.
    """
    col = _get_collection()
    result = col.update_one(
        {"tg_id": str(tg_id).strip()},
        {"$push": {"posters": {"poster": poster, "value": value}}},
    )
    return result.matched_count == 1


def get_registry_count() -> int:
    """Return the total number of registered users."""
    return _get_collection().count_documents({})
