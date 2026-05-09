"""
MongoDB-backed persistence for python-telegram-bot v20+.
Replaces PicklePersistence so bot data survives Render restarts.
"""
import logging
import copy
from typing import Any, Dict, Optional, Tuple
from collections import defaultdict

from telegram.ext import BasePersistence

logger = logging.getLogger(__name__)


class MongoPersistence(BasePersistence):
    """
    Persistence that stores all data in MongoDB.
    Falls back to in-memory if MongoDB is unavailable.
    Compatible with python-telegram-bot v20, v21, v22+
    """

    def __init__(self, mongo_uri: str, db_name: str = "telegram_bot_db"):
        super().__init__()
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self._db = None
        self._bot_data: dict = {}
        self._chat_data: dict = defaultdict(dict)
        self._user_data: dict = defaultdict(dict)
        self._conversations: dict = {}
        self._callback_data: Optional[Any] = None
        self._loaded = False

    def _get_db(self):
        if self._db is not None:
            return self._db
        try:
            from pymongo import MongoClient
            client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
            client.admin.command('ping')
            self._db = client[self.db_name]
            logger.info("MongoPersistence: MongoDB connected.")
            return self._db
        except Exception as e:
            logger.error(f"MongoPersistence: MongoDB connection failed: {e}")
            return None

    def _col(self, name: str):
        db = self._get_db()
        if db is None:
            return None
        return db[name]

    def _load_all(self):
        if self._loaded:
            return
        self._loaded = True

        col = self._col('persistence')
        if col is None:
            logger.warning("MongoPersistence: no DB, using in-memory only.")
            return

        try:
            doc = col.find_one({'_key': 'bot_data'})
            if doc:
                self._bot_data = {k: v for k, v in doc.items() if not k.startswith('_')}
                for key in ['users', 'groups']:
                    if key in self._bot_data and isinstance(self._bot_data[key], list):
                        self._bot_data[key] = set(self._bot_data[key])
            logger.info(f"MongoPersistence: bot_data loaded ({len(self._bot_data)} keys)")
        except Exception as e:
            logger.warning(f"MongoPersistence: load bot_data error: {e}")

        try:
            doc = col.find_one({'_key': 'chat_data'})
            if doc:
                self._chat_data = defaultdict(dict, {
                    k: v for k, v in doc.items() if not k.startswith('_')
                })
            logger.info(f"MongoPersistence: chat_data loaded ({len(self._chat_data)} chats)")
        except Exception as e:
            logger.warning(f"MongoPersistence: load chat_data error: {e}")

        try:
            doc = col.find_one({'_key': 'user_data'})
            if doc:
                self._user_data = defaultdict(dict, {
                    k: v for k, v in doc.items() if not k.startswith('_')
                })
            logger.info(f"MongoPersistence: user_data loaded ({len(self._user_data)} users)")
        except Exception as e:
            logger.warning(f"MongoPersistence: load user_data error: {e}")

        try:
            doc = col.find_one({'_key': 'conversations'})
            if doc:
                self._conversations = {k: v for k, v in doc.items() if not k.startswith('_')}
            logger.info(f"MongoPersistence: conversations loaded ({len(self._conversations)} handlers)")
        except Exception as e:
            logger.warning(f"MongoPersistence: load conversations error: {e}")

    def _safe_copy(self, data: Any) -> Any:
        try:
            return copy.deepcopy(data)
        except Exception:
            return data

    def _prepare_for_mongo(self, data: dict) -> dict:
        """Recursively convert sets to lists for MongoDB storage."""
        result = {}
        for k, v in data.items():
            if isinstance(v, set):
                result[k] = list(v)
            elif isinstance(v, dict):
                result[k] = self._prepare_for_mongo(v)
            else:
                result[k] = v
        return result

    def _save_to_mongo(self, key: str, data: dict):
        col = self._col('persistence')
        if col is None:
            return
        try:
            prepared = self._prepare_for_mongo(self._safe_copy(data))
            prepared['_key'] = key
            col.update_one({'_key': key}, {'$set': prepared}, upsert=True)
        except Exception as e:
            logger.warning(f"MongoPersistence: save '{key}' error: {e}")

    # ---- BasePersistence abstract methods ----

    async def get_bot_data(self) -> dict:
        self._load_all()
        return self._safe_copy(self._bot_data)

    async def update_bot_data(self, data: dict) -> None:
        self._bot_data = self._safe_copy(data)
        self._save_to_mongo('bot_data', self._bot_data)

    async def get_chat_data(self) -> dict:
        self._load_all()
        return defaultdict(dict, self._safe_copy(dict(self._chat_data)))

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        self._chat_data[str(chat_id)] = self._safe_copy(data)
        self._save_to_mongo('chat_data', dict(self._chat_data))

    async def drop_chat_data(self, chat_id: int) -> None:
        self._chat_data.pop(str(chat_id), None)
        self._save_to_mongo('chat_data', dict(self._chat_data))

    async def get_user_data(self) -> dict:
        self._load_all()
        return defaultdict(dict, self._safe_copy(dict(self._user_data)))

    async def update_user_data(self, user_id: int, data: dict) -> None:
        self._user_data[str(user_id)] = self._safe_copy(data)
        self._save_to_mongo('user_data', dict(self._user_data))

    async def drop_user_data(self, user_id: int) -> None:
        self._user_data.pop(str(user_id), None)
        self._save_to_mongo('user_data', dict(self._user_data))

    async def get_callback_data(self) -> Optional[Any]:
        return self._callback_data

    async def update_callback_data(self, data: Any) -> None:
        self._callback_data = data

    async def get_conversations(self, name: str) -> dict:
        self._load_all()
        raw = self._conversations.get(name, {})
        result = {}
        for k, v in raw.items():
            if isinstance(k, str) and ',' in k:
                try:
                    parts = k.split(',')
                    key = tuple(int(p.strip()) for p in parts)
                    result[key] = v
                except Exception:
                    result[k] = v
            else:
                result[k] = v
        return result

    async def update_conversation(self, name: str, key: Any, new_state: Optional[Any]) -> None:
        if name not in self._conversations:
            self._conversations[name] = {}

        str_key = ','.join(str(k) for k in key) if isinstance(key, tuple) else str(key)

        if new_state is None:
            self._conversations[name].pop(str_key, None)
        else:
            self._conversations[name][str_key] = new_state

        col = self._col('persistence')
        if col is None:
            return
        try:
            prepared = {k: v for k, v in self._conversations.items()}
            prepared['_key'] = 'conversations'
            col.update_one({'_key': 'conversations'}, {'$set': prepared}, upsert=True)
        except Exception as e:
            logger.warning(f"MongoPersistence: update_conversation error: {e}")

    async def flush(self) -> None:
        """Force-save all data to MongoDB."""
        col = self._col('persistence')
        if col is None:
            return
        try:
            prepared_bot = self._prepare_for_mongo(self._safe_copy(self._bot_data))
            prepared_bot['_key'] = 'bot_data'
            col.update_one({'_key': 'bot_data'}, {'$set': prepared_bot}, upsert=True)

            prepared_chat = self._prepare_for_mongo(dict(self._chat_data))
            prepared_chat['_key'] = 'chat_data'
            col.update_one({'_key': 'chat_data'}, {'$set': prepared_chat}, upsert=True)

            prepared_user = self._prepare_for_mongo(dict(self._user_data))
            prepared_user['_key'] = 'user_data'
            col.update_one({'_key': 'user_data'}, {'$set': prepared_user}, upsert=True)

            conv_prepared = {k: v for k, v in self._conversations.items()}
            conv_prepared['_key'] = 'conversations'
            col.update_one({'_key': 'conversations'}, {'$set': conv_prepared}, upsert=True)

            logger.debug("MongoPersistence: flush complete.")
        except Exception as e:
            logger.warning(f"MongoPersistence: flush error: {e}")
