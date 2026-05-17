"""
Migration script — imports id_registry_*.json into MongoDB.

Usage:
    MONGO_URI="mongodb+srv://..." python migrate.py path/to/id_registry.json

Optional env vars:
    MONGO_DB   — database name (default: telegram_bot)
    DRY_RUN    — set to "1" to print stats without writing to MongoDB
"""

import json
import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError


def migrate(json_path: str) -> None:
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    print(f"Loading data from: {json_path}")
    with open(json_path, encoding="utf-8") as f:
        raw: dict = json.load(f)

    total = len(raw)
    print(f"Records found: {total}")

    if dry_run:
        print("[DRY RUN] No data will be written.")
        _print_sample(raw)
        return

    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("ERROR: MONGO_URI environment variable is not set.")
        sys.exit(1)

    db_name = os.environ.get("MONGO_DB", "telegram_bot")
    client = MongoClient(uri)
    col = client[db_name]["id_registry"]

    # Ensure index exists
    col.create_index([("tg_id", ASCENDING)], unique=True, background=True)

    migration_ts = datetime.now(timezone.utc)
    batch_size = 500
    ops: list[UpdateOne] = []
    skipped = 0
    inserted = 0

    for key, entry in raw.items():
        tg_id = str(entry.get("id", key)).strip()
        if not tg_id:
            skipped += 1
            continue

        posters = [
            {"poster": p.get("poster", ""), "value": p.get("value", "")}
            for p in entry.get("posters", [])
            if isinstance(p, dict)
        ]

        # upsert: insert new, skip existing (preserves reg_at for repeat runs)
        ops.append(
            UpdateOne(
                {"tg_id": tg_id},
                {
                    "$setOnInsert": {
                        "tg_id": tg_id,
                        "reg_at": migration_ts,
                        "posters": posters,
                    }
                },
                upsert=True,
            )
        )

        if len(ops) >= batch_size:
            inserted += _flush(col, ops)
            ops.clear()

    if ops:
        inserted += _flush(col, ops)

    print(f"\nMigration complete.")
    print(f"  Records processed : {total}")
    print(f"  Skipped (no id)   : {skipped}")
    print(f"  Inserted (new)    : {inserted}")
    print(f"  Already existed   : {total - skipped - inserted}")
    client.close()


def _flush(col, ops: list) -> int:
    try:
        result = col.bulk_write(ops, ordered=False)
        n = result.upserted_count
        print(f"  Flushed batch: {len(ops)} ops, {n} inserted")
        return n
    except BulkWriteError as e:
        details = e.details
        n = details.get("nUpserted", 0)
        errors = len(details.get("writeErrors", []))
        print(f"  Batch had {errors} write error(s), {n} inserted")
        return n


def _print_sample(raw: dict) -> None:
    print("\nSample of converted documents:")
    for i, (key, entry) in enumerate(raw.items()):
        if i >= 5:
            break
        tg_id = str(entry.get("id", key)).strip()
        posters = entry.get("posters", [])
        print(f"  tg_id={tg_id!r}  posters={len(posters)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <path_to_json>")
        sys.exit(1)
    migrate(sys.argv[1])
