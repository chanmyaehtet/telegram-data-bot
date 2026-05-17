# ID Registry — MongoDB Module

Clean implementation of the Telegram bot ID registry system.

## Files

| File | Purpose |
|---|---|
| `registry.py` | Core registry module — import this into your bot |
| `migrate.py` | One-time migration script for the existing JSON data |
| `requirements.txt` | Python dependencies |

## MongoDB Schema (optimized)

Collection: `id_registry`

| Field | Type | Description |
|---|---|---|
| `tg_id` | string | Telegram user ID or username (indexed, unique) |
| `reg_at` | datetime | UTC timestamp of first registration |
| `posters` | array | List of `{poster, value}` entries |

Index: unique on `tg_id` — ensures fast lookups and prevents duplicates.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_URI` | Yes | — | MongoDB connection string (already set on Render) |
| `MONGO_DB` | No | `telegram_bot` | Database name |

---

## Usage in your bot

```python
from id_registry.registry import register_user, get_user, is_registered, add_poster

# When a new user starts the bot
result = register_user(str(update.effective_user.id))
if result["created"]:
    await update.message.reply_text("You have been registered.")
else:
    await update.message.reply_text("You are already registered.")

# Look up a user
user = get_user("123456789")
if user:
    print(user["tg_id"], user["reg_at"], user["posters"])

# Check registration status
if is_registered("123456789"):
    ...

# Add a poster entry
add_poster("123456789", poster="@someone", value="some_name")
```

---

## One-time data migration

Run this once on Render (or locally with your Render MONGO_URI) to import the existing JSON data.

```bash
pip install -r id_registry/requirements.txt
MONGO_URI="mongodb+srv://..." python id_registry/migrate.py id_registry_20260517.json
```

The migration is **safe to re-run** — it uses upsert logic, so existing records are never overwritten.

To preview what will be migrated without writing anything:

```bash
DRY_RUN=1 python id_registry/migrate.py id_registry_20260517.json
```

Field mapping from original data:

| Original | Optimized |
|---|---|
| `id` | `tg_id` |
| *(none)* | `reg_at` (set to migration timestamp) |
| `posters[].poster` | `posters[].poster` *(unchanged)* |
| `posters[].value` | `posters[].value` *(unchanged)* |
