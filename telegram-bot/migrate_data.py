"""
One-time migration script: Load id_registry.json and push all entries to MongoDB Atlas.
Run once: python3 migrate_data.py
"""
import os
import json
import logging

logging.basicConfig(level=logging.INFO)

MONGO_URI = os.getenv('MONGO_URI', 'mongodb+srv://zhengduo1539_db_user:jbDcpW77Sbbh3KXI@cluster0.l26dlgi.mongodb.net/?appName=Cluster0')
ID_REGISTRY_FILE = os.path.join(os.path.dirname(__file__), "id_registry.json")

try:
    from pymongo import MongoClient, UpdateOne
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    client.admin.command('ping')
    db = client['telegram_bot_db']
    logging.info("MongoDB connected.")
except Exception as e:
    logging.error(f"MongoDB connection failed: {e}")
    exit(1)

# --- Migrate id_registry.json ---
try:
    with open(ID_REGISTRY_FILE, 'r', encoding='utf-8') as f:
        id_registry = json.load(f)
    logging.info(f"Loaded {len(id_registry)} entries from id_registry.json")

    col = db['id_registry']
    ops = []
    for key, val in id_registry.items():
        doc = dict(val)
        doc['id'] = str(key)
        ops.append(UpdateOne({'id': str(key)}, {'$set': doc}, upsert=True))

    if ops:
        result = col.bulk_write(ops)
        logging.info(f"id_registry migrated: {result.upserted_count} new, {result.modified_count} updated")
    else:
        logging.info("No id_registry entries to migrate.")
except FileNotFoundError:
    logging.warning(f"id_registry.json not found at {ID_REGISTRY_FILE}")
except Exception as e:
    logging.error(f"id_registry migration error: {e}")

# --- Migrate plus_data.json ---
PLUS_DATA_FILE = os.path.join(os.path.dirname(__file__), "plus_data.json")
try:
    with open(PLUS_DATA_FILE, 'r', encoding='utf-8') as f:
        plus_data = json.load(f)
    col2 = db['plus_data']
    col2.update_one({'_type': 'plus_data'}, {'$set': {'_type': 'plus_data', **plus_data}}, upsert=True)
    logging.info("plus_data.json migrated to MongoDB.")
except FileNotFoundError:
    logging.warning("plus_data.json not found.")
except Exception as e:
    logging.error(f"plus_data migration error: {e}")

# --- Migrate data_msg_map.json ---
DATA_MSG_MAP_FILE = os.path.join(os.path.dirname(__file__), "data_msg_map.json")
try:
    with open(DATA_MSG_MAP_FILE, 'r', encoding='utf-8') as f:
        data_msg_map = json.load(f)
    col3 = db['data_msg_map']
    col3.update_one({'_type': 'data_msg_map'}, {'$set': {'_type': 'data_msg_map', 'data': data_msg_map}}, upsert=True)
    logging.info("data_msg_map.json migrated to MongoDB.")
except FileNotFoundError:
    logging.warning("data_msg_map.json not found.")
except Exception as e:
    logging.error(f"data_msg_map migration error: {e}")

logging.info("Migration complete.")
client.close()
