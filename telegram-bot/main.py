import os
import re
import io
import json
import math
import logging
import asyncio
import tempfile
import threading
import pytz
import requests
from datetime import datetime, time, timedelta

try:
    from langdetect import detect as langdetect_detect
except Exception:
    langdetect_detect = None

from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    PicklePersistence, filters, JobQueue
)
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InputFile, BotCommand
)
from telegram.ext import CallbackContext
from web_server import keep_alive

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

ADMIN_IDS = [7196380140, 1827336632, 7039073770]

FEEDBACK_AWAITING = 3
BROADCAST_SELECT_CHAT = 10
BROADCAST_AWAITING_MESSAGE = 11
BROADCAST_CONFIRMATION = 12

SCHEDULE_SET_TIME = 20
SCHEDULE_SET_MESSAGE = 21
SCHEDULE_SELECT_TYPE = 23
SCHEDULE_SELECT_GROUP = 22

BOT_SETTINGS_SELECT   = 40
BOT_SETTINGS_AWAITING = 41

# ============================================================
# ID REGISTRY — atomic + debounced save
# ============================================================
ID_REGISTRY_FILE = os.path.join(os.path.dirname(__file__), "id_registry.json")
id_registry: dict = {}
_id_registry_lock = threading.Lock()
_id_save_timer: threading.Timer | None = None
_ID_DEBOUNCE_SEC = 5.0


def _do_save_id_registry() -> None:
    global _id_save_timer
    _id_save_timer = None
    tmp_path = ID_REGISTRY_FILE + ".tmp"
    try:
        with _id_registry_lock:
            data_copy = dict(id_registry)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data_copy, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ID_REGISTRY_FILE)
    except Exception as e:
        logging.warning(f"save_id_registry error: {e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def save_id_registry() -> None:
    global _id_save_timer
    with _id_registry_lock:
        if _id_save_timer is not None:
            _id_save_timer.cancel()
        _id_save_timer = threading.Timer(_ID_DEBOUNCE_SEC, _do_save_id_registry)
        _id_save_timer.daemon = True
        _id_save_timer.start()


def save_id_registry_immediate() -> None:
    global _id_save_timer
    with _id_registry_lock:
        if _id_save_timer is not None:
            _id_save_timer.cancel()
            _id_save_timer = None
    _do_save_id_registry()


def load_id_registry() -> None:
    global id_registry
    for path in [ID_REGISTRY_FILE, ID_REGISTRY_FILE + ".tmp"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with _id_registry_lock:
                id_registry = data
            logging.info(f"id_registry loaded: {len(id_registry)} IDs from {path}")
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            logging.warning(f"load_id_registry error ({path}): {e}")
            continue
    logging.info("id_registry: starting fresh (no file found)")


load_id_registry()

# ============================================================
# PLUS COUNTER
# ============================================================
PLUS_DATA_FILE = os.path.join(os.path.dirname(__file__), "plus_data.json")

plus_counters: dict = {}
plus_names: dict = {}
plus_counted_msgs: dict = {}


def _plus_key_to_str(key: tuple) -> str:
    return f"{key[0]}:{key[1]}"


def _str_to_plus_key(s: str) -> tuple:
    parts = s.split(":", 1)
    return (int(parts[0]), int(parts[1]))


def save_plus_data() -> None:
    tmp_path = PLUS_DATA_FILE + ".tmp"
    try:
        data = {
            "counters":     {_plus_key_to_str(k): v for k, v in plus_counters.items()},
            "names":        {str(k): v for k, v in plus_names.items()},
            "counted_msgs": {_plus_key_to_str(k): v for k, v in plus_counted_msgs.items()},
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, PLUS_DATA_FILE)
    except Exception as e:
        logging.warning(f"save_plus_data error: {e}")


def load_plus_data() -> None:
    global plus_counters, plus_names, plus_counted_msgs
    for path in [PLUS_DATA_FILE, PLUS_DATA_FILE + ".tmp"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            plus_counters = {_str_to_plus_key(k): v for k, v in data.get("counters", {}).items()}
            plus_names = {int(k): v for k, v in data.get("names", {}).items()}
            raw_msgs = data.get("counted_msgs", {})
            if isinstance(raw_msgs, list):
                plus_counted_msgs = {}
            else:
                plus_counted_msgs = {_str_to_plus_key(k): v for k, v in raw_msgs.items()}
            logging.info(f"plus_data loaded: {len(plus_counters)} counters")
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            logging.warning(f"load_plus_data error ({path}): {e}")


load_plus_data()

# ============================================================
# DATA MSG MAP
# ============================================================
DATA_MSG_MAP_FILE = os.path.join(os.path.dirname(__file__), "data_msg_map.json")
data_msg_map: dict = {}


def _data_key_to_str(key: tuple) -> str:
    return f"{key[0]}:{key[1]}"


def _str_to_data_key(s: str) -> tuple:
    parts = s.split(":", 1)
    return (int(parts[0]), int(parts[1]))


def save_data_msg_map() -> None:
    tmp_path = DATA_MSG_MAP_FILE + ".tmp"
    try:
        serializable = {_data_key_to_str(k): v for k, v in data_msg_map.items()}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
        os.replace(tmp_path, DATA_MSG_MAP_FILE)
    except Exception as e:
        logging.warning(f"save_data_msg_map error: {e}")


def load_data_msg_map() -> None:
    global data_msg_map
    for path in [DATA_MSG_MAP_FILE, DATA_MSG_MAP_FILE + ".tmp"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            data_msg_map = {_str_to_data_key(k): v for k, v in raw.items()}
            logging.info(f"data_msg_map loaded: {len(data_msg_map)} entries")
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            logging.warning(f"load_data_msg_map error ({path}): {e}")


load_data_msg_map()

# ============================================================
# REPORT TEMPLATE
# ============================================================
REPORT_TEMPLATE = (
    "Gmail        - \n"
    "  \n"
    "Tele name    - \n"
    "    \n"
    "Username    - \n"
    "    \n"
    "Date        - \n"
    "    \n"
    "Age         - \n"
    "    \n"
    "Current work - \n"
    "    \n"
    "Phone number      - \n"
    "\n"
    "ID   - \n"
    "\n"
    "Khaifa - "
)


def get_yangon_tz() -> pytz.timezone:
    return pytz.timezone('Asia/Yangon')


def get_data_key() -> str:
    try:
        tz = get_yangon_tz()
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()

    cut_off_time = time(hour=18, minute=30, second=0)

    if now.time() < cut_off_time:
        work_day = now.date() - timedelta(days=1)
    else:
        work_day = now.date()

    return work_day.strftime('%Y-%m-%d')


get_today_key = get_data_key


async def save_chat_id(chat_id: int, context: CallbackContext, chat_type: str) -> None:
    if 'users' not in context.application.bot_data:
        context.application.bot_data['users'] = set()
    if 'groups' not in context.application.bot_data:
        context.application.bot_data['groups'] = set()

    if chat_type == 'private' and chat_id not in context.application.bot_data['users']:
        context.application.bot_data['users'].add(chat_id)
    elif chat_type in ['group', 'supergroup'] and chat_id not in context.application.bot_data['groups']:
        context.application.bot_data['groups'].add(chat_id)

    if context.application.persistence:
        await context.application.persistence.flush()


# ============================================================
# MATH CALCULATOR (PM only)
# ============================================================
def _safe_eval_math(expr: str):
    """
    Safely evaluate a mathematical expression.
    Supports: +, -, *, /, //, %, **, (), and math functions.
    """
    expr = expr.strip()
    expr = expr.replace('×', '*').replace('÷', '/').replace('^', '**')
    expr = expr.replace(',', '')

    allowed_names = {k: getattr(math, k) for k in dir(math) if not k.startswith('_')}
    allowed_names.update({'abs': abs, 'round': round, 'int': int, 'float': float})

    try:
        code = compile(expr, '<string>', 'eval')
        for node in code.co_consts:
            pass
        result = eval(code, {"__builtins__": {}}, allowed_names)
        return result
    except ZeroDivisionError:
        raise ValueError("Division by zero")
    except Exception:
        raise ValueError("Invalid expression")


def _looks_like_math(text: str) -> bool:
    """Check if the text looks like a math expression."""
    text = text.strip()
    pattern = r'^[\d\s\+\-\*\/\(\)\.\,\%\^×÷]+$'
    if re.match(pattern, text):
        if re.search(r'\d', text) and re.search(r'[\+\-\*\/\^×÷]', text):
            return True
    if re.search(r'^\d+(\.\d+)?$', text):
        return False
    if re.match(r'^[\d\s\(\)]+[\+\-\*\/\^×÷][\d\s\(\)\.]+', text):
        return True
    return False


async def handle_pm_math(update: Update, context: CallbackContext) -> None:
    """Handle math expressions in PM only."""
    msg = update.message
    if not msg or not msg.text:
        return
    if update.effective_chat.type != 'private':
        return

    text = msg.text.strip()

    if text.startswith('/'):
        return

    if not _looks_like_math(text):
        return

    try:
        result = _safe_eval_math(text)
        if isinstance(result, float):
            if result == int(result):
                result_str = str(int(result))
            else:
                result_str = f"{result:.10g}"
        else:
            result_str = str(result)

        await msg.reply_text(
            f"🧮 <b>{text} = {result_str}</b>",
            parse_mode='HTML'
        )
    except ValueError as e:
        pass
    except Exception:
        pass


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: CallbackContext) -> None:
    await main_menu_command(update, context)


async def help_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    await update.message.reply_text(
        'Bot commands and functions:\n\n'
        'Data Entry:\n'
        '1. Send a message containing "Khaifa -" and "Date -" to collect data automatically.\n'
        '\nUser Commands (Menu Buttons):\n'
        ' /form - Display the report submission template\n'
        ' /chk <number> - Check and track number usage\n'
        ' /showdata - Show today\'s collected data\n'
        ' /cleardata - Clear today\'s collected data\n'
        ' /feedback - Send feedback to admin\n'
        ' /hidemenu - Hide the menu buttons\n\n'
        '🧮 Math Calculator:\n'
        'Bot PM တွင် math expression ရိုက်ပါ (e.g. 2+2, 15*15-15, (100+50)*2)',
        parse_mode='Markdown'
    )


async def report_form_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    await update.message.reply_text(
        "📋 Deposit Report Form Template\n\n"
        "ကော်ပီကူးယူ၍ ဖြည့်စွက်ပြီး ပို့ပေးပါ:\n\n"
        + REPORT_TEMPLATE,
        parse_mode='Markdown'
    )


async def main_menu_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    keyboard = [
        [KeyboardButton("/showdata"), KeyboardButton("/cleardata")],
        [KeyboardButton("/feedback"), KeyboardButton("/chk")],
        [KeyboardButton("/form"), KeyboardButton("/total_plus")],
        [KeyboardButton("/reset_plus"), KeyboardButton("/guide")],
        [KeyboardButton("/hidemenu")]
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )

    user_name = update.effective_user.full_name if update.effective_user else "User"
    greeting_text = (
        f"မင်္ဂလာပါ။ {user_name}\n"
        "Bot အသုံးပြုနည်းသိအောင် /guide 📝 ကိုနှိပ်၍ကြည့်နိုင်ပါသည်။📌\n\n"
        "🧮 Bot PM တွင် math expression ရိုက်ပါ (e.g. 2+2)"
    )

    await update.message.reply_text(
        greeting_text,
        reply_markup=reply_markup
    )


async def remove_menu(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    reply_markup = ReplyKeyboardRemove()
    await update.message.reply_text(
        "Menu keyboard ကို ဖျက်လိုက်ပါပြီ.....။ /start ဖြင့် ပြန်ခေါ်နိုင်ပါသည်။😒😒",
        reply_markup=reply_markup
    )


async def check_command(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /chk <number>.")
        return

    check_number = " ".join(context.args).strip()

    user = update.effective_user
    if user and user.username:
        checker_label = f"@{user.username}"
    elif user and user.full_name:
        checker_label = user.full_name
    else:
        checker_label = "Unknown"

    records = context.application.bot_data.setdefault('check_records', {})

    existing = records.get(check_number, [])
    if isinstance(existing, int):
        existing = []

    existing.append(checker_label)
    records[check_number] = existing
    new_count = len(existing)

    if new_count >= 2:
        counts: dict = {}
        for name in existing:
            counts[name] = counts.get(name, 0) + 1
        checker_list = "\n".join(
            f"{name} → {cnt}" for name, cnt in counts.items()
        )
        await update.message.reply_text(
            f"🔍 <b>{check_number}</b>\n\n"
            f"<blockquote>ဤနံပါတ်သည် အကြိမ်ရေ <b>{new_count}</b> စစ်ဆေးထားပါသည်။\n\n"
            f"စစ်ဆေးထားသူများ\n"
            f"{checker_list}</blockquote>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            f"🔍 <b>{check_number}</b>\n\n"
            f"<blockquote>ပထမဦးဆုံး စစ်ဆေးခြင်းဖြစ်သည်။</blockquote>",
            parse_mode='HTML'
        )

    if context.application.persistence:
        await context.application.persistence.flush()


async def clear_data(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    today_key = get_data_key()
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    if ('group_data' in context.application.bot_data
            and chat_id in context.application.bot_data['group_data']
            and today_key in context.application.bot_data['group_data'][chat_id]):
        del context.application.bot_data['group_data'][chat_id][today_key]

        if context.application.persistence:
            await context.application.persistence.flush()

        for k in [k for k in plus_counters if k[0] == int(chat_id)]:
            del plus_counters[k]
        for k in [k for k in plus_counted_msgs if k[0] == int(chat_id)]:
            del plus_counted_msgs[k]
        save_plus_data()

        for k in [k for k in data_msg_map if k[0] == int(chat_id)]:
            del data_msg_map[k]
        save_data_msg_map()

        await update.message.reply_text(
            f"✅ Data deleted for today ({today_key}).\n"
            f"✅ Plus counter (+) များကိုလည်း reset ပြုလုပ်ပြီးပါပြီ။"
        )
    else:
        await update.message.reply_text(f"No data found for today ({today_key}).")


async def admin_clearall_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user.id not in ADMIN_IDS:
        return
    if chat.type != 'private':
        await update.message.reply_text("❌ ဤ command ကို Bot PM ထဲတွင်သာ အသုံးပြုနိုင်သည်။")
        return

    today_key = get_data_key()
    group_data = context.application.bot_data.get('group_data', {})
    has_data = any(today_key in days for days in group_data.values())

    if not has_data:
        await update.message.reply_text(f"ℹ️ ယနေ့ ({today_key}) ရှင်းလင်းစရာ data မရှိပါ။")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ အတည်ပြုရှင်းလင်းမည်", callback_data="adminall_clear_confirm"),
        InlineKeyboardButton("❌ မလုပ်တော့ပါ", callback_data="adminall_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ <b>Group အားလုံး ရှင်းလင်းမည်</b>\n\n"
        f"ယနေ့ ({today_key}) data ရှိသော group <b>{sum(1 for d in group_data.values() if today_key in d)}</b> ခု၏\n"
        f"• Deposit data\n• Plus counter\n\n"
        f"ကို တစ်ပြိုင်တည်း ရှင်းလင်းမည်။\n\nဆက်လုပ်မည်လား?",
        parse_mode='HTML',
        reply_markup=keyboard
    )


async def admin_resetplusall_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user.id not in ADMIN_IDS:
        return
    if chat.type != 'private':
        await update.message.reply_text("❌ ဤ command ကို Bot PM ထဲတွင်သာ အသုံးပြုနိုင်သည်။")
        return

    total_keys = len([k for k in plus_counters])
    if total_keys == 0:
        await update.message.reply_text("ℹ️ ရှင်းလင်းစရာ Plus counter မရှိပါ။")
        return

    chat_count = len(set(k[0] for k in plus_counters))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ အတည်ပြု Reset မည်", callback_data="adminall_resetplus_confirm"),
        InlineKeyboardButton("❌ မလုပ်တော့ပါ", callback_data="adminall_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ <b>Group အားလုံး Plus Counter Reset မည်</b>\n\n"
        f"Group <b>{chat_count}</b> ခု ၏ Plus counter ကို reset မည်။\n\nဆက်လုပ်မည်လား?",
        parse_mode='HTML',
        reply_markup=keyboard
    )


async def adminall_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Admin သာ ဤ action ကို ပြုလုပ်နိုင်သည်။")
        return

    data = query.data

    if data == "adminall_cancel":
        await query.edit_message_text("❌ ပယ်ဖျက်လိုက်ပါသည်။")
        return

    if data == "adminall_clear_confirm":
        today_key = get_data_key()
        group_data = context.application.bot_data.get('group_data', {})
        cleared_groups = 0

        for chat_id_str, days in group_data.items():
            if today_key in days:
                del days[today_key]
                chat_id_int = int(chat_id_str)
                for k in [k for k in plus_counters if k[0] == chat_id_int]:
                    del plus_counters[k]
                for k in [k for k in plus_counted_msgs if k[0] == chat_id_int]:
                    del plus_counted_msgs[k]
                for k in [k for k in data_msg_map if k[0] == chat_id_int]:
                    del data_msg_map[k]
                cleared_groups += 1

        save_plus_data()
        save_data_msg_map()
        if context.application.persistence:
            await context.application.persistence.flush()

        await query.edit_message_text(
            f"✅ <b>ရှင်းလင်းမှု ပြီးပါပြီ</b>\n\n"
            f"📊 Group <b>{cleared_groups}</b> ခု၏ ယနေ့ ({today_key}) data ကို ရှင်းလင်းပြီးပါပြီ။",
            parse_mode='HTML'
        )
        return

    if data == "adminall_resetplus_confirm":
        chat_count = len(set(k[0] for k in plus_counters))
        key_count = len(list(plus_counters.keys()))
        for k in list(plus_counters.keys()):
            del plus_counters[k]
        for k in list(plus_counted_msgs.keys()):
            del plus_counted_msgs[k]
        save_plus_data()

        await query.edit_message_text(
            f"✅ <b>Plus Counter Reset ပြီးပါပြီ</b>\n\n"
            f"➕ Group <b>{chat_count}</b> ခု၏ plus counter (<b>{key_count}</b> ဦး) ကို reset ပြုလုပ်ပြီးပါပြီ။",
            parse_mode='HTML'
        )
        return


async def show_data(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    today_key = get_data_key()
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    if 'group_data' not in context.application.bot_data:
        context.application.bot_data['group_data'] = {}

    if chat_id not in context.application.bot_data['group_data']:
        context.application.bot_data['group_data'][chat_id] = {}

    collected_data_list = context.application.bot_data['group_data'][chat_id].get(today_key, [])

    if not collected_data_list:
        await update.message.reply_text(f"No data collected yet for today ({today_key}) in this chat.")
        return

    grouped_data = {}

    for entry in collected_data_list:
        parts = entry.split('    ')

        khaifa_name = "N/A"
        if len(parts) >= 2:
            khaifa_name = parts[1].strip()

        normalized_key = khaifa_name.replace(" ", "").lower() if khaifa_name != "N/A" else "n/a"

        if normalized_key not in grouped_data:
            grouped_data[normalized_key] = []

        grouped_data[normalized_key].append(entry)

    final_response_parts = []
    separator = "------------------------------------"

    is_first_group = True
    sorted_groups = sorted(grouped_data.items())

    for normalized_key, entries in sorted_groups:
        if not is_first_group:
            final_response_parts.append(separator)

        is_first_group = False

        group_text = "\n\n".join(entries)
        final_response_parts.append(group_text)

    response_text = "\n".join(final_response_parts)

    if len(response_text) > 4096:
        await update.message.reply_text("Warning: Data too long. Displaying partial data.")
        await update.message.reply_text(response_text[:4000])
    else:
        await update.message.reply_text(response_text)

    _u = update.effective_user
    _mention = f"@{_u.username}" if (_u and _u.username) else (_u.full_name if _u else "User")
    await update.message.reply_text(
        f"<i>{_mention} report ပြင်ဆင်ပြီးပါက /cleardata နှိပ်ပါ။</i>",
        parse_mode='HTML'
    )


async def extract_and_save_data(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    full_text = update.message.text or update.message.caption

    if not full_text:
        return

    required_fields_present = all(
        re.search(field, full_text, re.IGNORECASE)
        for field in ["Khaifa", "Date"]
    )

    if not required_fields_present:
        return

    khaifa_match = re.search(r"(?:Khaifa|Khat)\s*[-\]]?\s*(.+?)(?:\r?\n|$)", full_text, re.IGNORECASE | re.DOTALL)
    extracted_khaifa = khaifa_match.group(1).strip() if khaifa_match else "N/A"

    date_match = re.search(r"Date\s*[-\]]?\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE | re.DOTALL)
    extracted_date = date_match.group(1).strip() if date_match else "N/A"

    email_phone_match = re.search(r"(?:Gmail|Email|Phone number|Phone)\s*[-\]]?\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE | re.DOTALL)
    extracted_email_phone = email_phone_match.group(1).strip() if email_phone_match else "N/A"

    # Extract ID field — allow flexible spaces after "ID" or "id"
    id_match = re.search(r"\b(?:ID|id)\s*[-:]\s*(.+?)(?:\n|$)", full_text)
    extracted_id_raw = id_match.group(1).strip() if id_match else None

    # Only register if ID field has actual content (not blank/dash/space)
    id_warning = ""
    _sender = update.effective_user
    _sender_mention = f"@{_sender.username}" if (_sender and _sender.username) else (_sender.full_name if _sender else None)

    if extracted_id_raw and re.search(r'\S', extracted_id_raw) and extracted_id_raw.strip() not in ("", "-", "N/A"):
        extracted_id = extracted_id_raw.strip()
        id_key = extracted_id.lower()

        with _id_registry_lock:
            existing_entry = id_registry.get(id_key)

        if existing_entry:
            _reg = existing_entry
            prev_entries = []
            if isinstance(_reg, dict):
                _raw_posters = _reg.get("posters", [])
                for _p in _raw_posters:
                    if isinstance(_p, dict):
                        prev_entries.append({
                            "poster": _p.get("poster", ""),
                            "value": _p.get("value", ""),
                        })
                    else:
                        prev_entries.append({"poster": str(_p), "value": ""})

            if prev_entries:
                _lines = []
                for _e in prev_entries:
                    _v = _e.get("value", "") or ""
                    _p = _e.get("poster", "") or ""
                    if _v:
                        _lines.append(f"{_p}  {_v}")
                    else:
                        _lines.append(_p)
                _posters_block = "\n".join(_lines)
                id_warning = (
                    f"\n\n⚠️ဤclient သည်ရောက်ပြီးသားဖြစ်ပါသည်။⚠️\n"
                    f"အောက်တွင်ဖော်ပြထားသည်။ဘယ်အဆင့်ရောက်နေလဲမေးမြန်းပါ။\n"
                    f"Deposit - @example\n"
                    f"Gmail - example\n\n"
                    f"{_posters_block}"
                )
            else:
                id_warning = (
                    f"\n\n⚠️ဤclient သည်ရောက်ပြီးသားဖြစ်ပါသည်။⚠️\n"
                    f"အောက်တွင်ဖော်ပြထားသည်။ဘယ်အဆင့်ရောက်နေလဲမေးမြန်းပါ။\n"
                    f"Deposit - @example\n"
                    f"Gmail - example"
                )

            if _sender_mention:
                _already = any(
                    e.get("poster") == _sender_mention and e.get("value", "") == extracted_email_phone
                    for e in prev_entries
                )
                if not _already:
                    prev_entries.append({"poster": _sender_mention, "value": extracted_email_phone})

            with _id_registry_lock:
                id_registry[id_key] = {"id": extracted_id, "posters": prev_entries}
            save_id_registry()
        else:
            posters = [{"poster": _sender_mention, "value": extracted_email_phone}] if _sender_mention else []
            with _id_registry_lock:
                id_registry[id_key] = {"id": extracted_id, "posters": posters}
            save_id_registry()

        # Khaifa only if ID is present
        _khaifa_match2 = re.search(r"(?:Khaifa|Khat)\s*[-\]]?\s*(.+?)(?:\r?\n|$)", full_text, re.IGNORECASE)
        if _khaifa_match2:
            _khaifa_val = _khaifa_match2.group(1).strip()
        else:
            _khaifa_val = None

    stored_entry = f"{extracted_date}    {extracted_khaifa}    {extracted_email_phone}"
    display_output = f"{stored_entry}{id_warning}"

    today_key = get_data_key()

    if 'group_data' not in context.application.bot_data:
        context.application.bot_data['group_data'] = {}

    if chat_id not in context.application.bot_data['group_data']:
        context.application.bot_data['group_data'][chat_id] = {}

    if today_key not in context.application.bot_data['group_data'][chat_id]:
        context.application.bot_data['group_data'][chat_id][today_key] = []

    context.application.bot_data['group_data'][chat_id][today_key].append(stored_entry)

    if context.application.persistence:
        await context.application.persistence.flush()

    sent_msg = await update.message.reply_text(display_output)

    data_msg_map[(int(chat_id), sent_msg.message_id)] = {
        "entry": stored_entry,
        "date_key": today_key,
        "chat_id": chat_id,
    }
    save_data_msg_map()


# ============================================================
# FEEDBACK
# ============================================================

async def start_feedback(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        "သင်သည် Owner အားယခု စာပေးပို့နိုင်ပါသည်။ ဤနေရာတွင် ကြိုက်နှစ်သက်ရာ စာကို ပေးပို့လိုက်ပါက Owner ဆီ စာရောက်ရှိမည် ဖြစ်သည်။\n\n"
        "(လုပ်ငန်းစဉ် ရပ်ဆိုင်းလိုပါက /cancel ကို သုံးပါ။)"
    )
    return FEEDBACK_AWAITING


async def process_feedback(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    feedback_text = update.message.text

    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot.send_message(
                chat_id=admin_id,
                text=f"📩 ***[NEW FEEDBACK]***\nFrom: {user.full_name} (@{user.username} - ID: {user.id})\n\nFeedback:\n{feedback_text}",
                parse_mode='Markdown'
            )
        except Exception:
            pass

    await update.message.reply_text("သင်၏အကြံပြုစာအား Owner ထံပေးပို့ပြီးပါပြီ။")
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text('❌ Action cancelled.')
    return ConversationHandler.END


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_start(update: Update, context: CallbackContext) -> int:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return ConversationHandler.END

    users = context.application.bot_data.get('users', set())
    groups = context.application.bot_data.get('groups', set())

    if not users and not groups:
        await update.message.reply_text("No tracked users or groups found.")
        return ConversationHandler.END

    keyboard = []

    for user_id in sorted(list(users)):
        try:
            user = await context.application.bot.get_chat(chat_id=user_id)
            name = user.full_name or f"User {user_id}"
            keyboard.append([InlineKeyboardButton(f"👤 User: {name} (ID: {user_id})", callback_data=f'bcast_id_{user_id}')])
        except Exception:
            keyboard.append([InlineKeyboardButton(f"👤 Untracked User (ID: {user_id})", callback_data=f'bcast_id_{user_id}')])

    for group_id in sorted(list(groups)):
        try:
            chat = await context.application.bot.get_chat(chat_id=group_id)
            name = chat.title or f"Group {group_id}"
            keyboard.append([InlineKeyboardButton(f"👥 Group: {name} (ID: {group_id})", callback_data=f'bcast_id_{group_id}')])
        except Exception:
            keyboard.append([InlineKeyboardButton(f"👥 Untracked Group (ID: {group_id})", callback_data=f'bcast_id_{group_id}')])

    keyboard.append([InlineKeyboardButton("❌ Cancel Broadcast", callback_data='bcast_cancel')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📢 Broadcast Service\n\nကျေးဇူးပြု၍ စာပေးပို့လိုသည့် User (သို့) Group ကို ရွေးချယ်ပါ:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return BROADCAST_SELECT_CHAT


async def broadcast_select_chat(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith('bcast_id_'):
        await query.edit_message_text("❌ ရွေးချယ်မှု မှားယွင်းပါသည်။")
        return ConversationHandler.END

    target_id_str = query.data[len('bcast_id_'):]
    context.user_data['target_broadcast_id'] = target_id_str

    try:
        chat = await context.application.bot.get_chat(chat_id=target_id_str)
        name = chat.title or chat.full_name
        context.user_data['target_name'] = name
    except Exception:
        context.user_data['target_name'] = f"Chat ID: {target_id_str}"

    await query.edit_message_text(
        f"✅ {context.user_data['target_name']} သို့ ပေးပို့ရန် ရွေးချယ်ပြီးပါပြီ။\n\n"
        "ပေးပို့လိုသည့် message ကို ဤနေရာသို့ forward (သို့) ရိုက်ထည့်ပေးပါ။\n\n"
        "(ရပ်လိုပါက /cancel)"
    )
    return BROADCAST_AWAITING_MESSAGE


def _describe_message(msg) -> str:
    if msg.photo:
        return "🖼 Photo"
    elif msg.video:
        return "🎥 Video"
    elif msg.document:
        return f"📄 Document ({msg.document.file_name or 'file'})"
    elif msg.audio:
        return "🎵 Audio"
    elif msg.animation:
        return "🎞 GIF/Animation"
    elif msg.voice:
        return "🎤 Voice message"
    elif msg.video_note:
        return "📹 Video note"
    elif msg.sticker:
        return f"🎁 Sticker ({msg.sticker.emoji or ''})"
    elif msg.text:
        preview = msg.text[:80] + ("…" if len(msg.text) > 80 else "")
        return f"📝 Text: {preview}"
    else:
        return "📦 Message"


async def broadcast_await_message(update: Update, context: CallbackContext) -> int:
    msg = update.message
    context.user_data['broadcast_msg_id'] = msg.message_id
    context.user_data['broadcast_from_chat'] = msg.chat_id
    target_name = context.user_data.get('target_name', 'ရွေးချယ်ထားသော Chat')
    description = _describe_message(msg)

    keyboard = [
        [InlineKeyboardButton("✅ Confirm Send", callback_data='bcast_confirm')],
        [InlineKeyboardButton("❌ Cancel Broadcast", callback_data='bcast_cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.reply_text(
        f"📨 <b>{target_name}</b> သို့ ပေးပို့ရန် သေချာပါသလား?\n\nအမျိုးအစား: {description}",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return BROADCAST_CONFIRMATION


async def broadcast_confirm(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    target_id = context.user_data.pop('target_broadcast_id', None)
    msg_id = context.user_data.pop('broadcast_msg_id', None)
    from_chat = context.user_data.pop('broadcast_from_chat', None)
    target_name = context.user_data.pop('target_name', 'Unknown Chat')
    context.user_data.pop('broadcast_message', None)

    if not target_id or not msg_id or not from_chat:
        await query.edit_message_text("❌ အချက်အလက်မပြည့်စုံ၍ ပေးပို့နိုင်ခြင်းမရှိပါ။")
        return ConversationHandler.END

    try:
        await context.application.bot.copy_message(
            chat_id=target_id,
            from_chat_id=from_chat,
            message_id=msg_id
        )
        await query.edit_message_text(
            f"✅ <b>{target_name}</b> ထံသို့ အောင်မြင်စွာ ပေးပို့ပြီးပါပြီ။",
            parse_mode='HTML'
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ {target_name} ထံသို့ ပေးပို့ရာတွင် အမှားဖြစ်ပွားပါသည်။\nError: {e}"
        )

    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("❌ Broadcast လုပ်ငန်းစဉ်ကို ဖျက်သိမ်းလိုက်ပါသည်။")
    elif update.message:
        await update.message.reply_text("❌ Broadcast cancelled.")

    context.user_data.pop('target_broadcast_id', None)
    context.user_data.pop('broadcast_message', None)
    context.user_data.pop('broadcast_msg_id', None)
    context.user_data.pop('broadcast_from_chat', None)
    context.user_data.pop('target_name', None)

    return ConversationHandler.END


# ============================================================
# ADMIN PANEL
# ============================================================

async def list_groups(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return

    groups = context.application.bot_data.get('groups', set())

    if not groups:
        await update.message.reply_text("The bot is not currently in any tracked groups.")
        return

    await update.message.reply_text("📋 Tracked Groups List:", parse_mode='Markdown')

    for group_id in list(groups):
        try:
            chat = await context.application.bot.get_chat(chat_id=group_id)
            group_name = chat.title
        except Exception:
            group_name = "Unknown Group (ID may be outdated)"

        response = f"**{group_name}** ({group_id})\n"

        keyboard = [
            [
                InlineKeyboardButton("🗑️ Clear All Data", callback_data=f'admin_clear_{group_id}'),
                InlineKeyboardButton("❌ Cancel", callback_data='admin_cancel')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text=response,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


async def clear_group_data_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Admin only.")
        return

    try:
        data_parts = query.data.split('_')
        group_id_to_clear = data_parts[2]
    except IndexError:
        await query.edit_message_text("❌ Error: Invalid clear command.")
        return

    chat_id_str = str(group_id_to_clear)

    if 'group_data' in context.application.bot_data and chat_id_str in context.application.bot_data['group_data']:
        del context.application.bot_data['group_data'][chat_id_str]

        if context.application.persistence:
            await context.application.persistence.flush()

        try:
            chat = await context.application.bot.get_chat(chat_id=group_id_to_clear)
            group_name = chat.title
        except Exception:
            group_name = "Unknown Group"

        await query.edit_message_text(
            f"✅ Group Data Cleared!\n{group_name} ({group_id_to_clear})'s data has been completely removed."
        )
    else:
        await query.edit_message_text(f"No data found for group ID {group_id_to_clear}.")


async def cancel_group_action(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Admin only.")
        return

    await query.edit_message_text("❌ Action cancelled.")


async def stats(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    user_count = len(context.application.bot_data.get('users', set()))
    group_count = len(context.application.bot_data.get('groups', set()))
    chk_count = len(context.application.bot_data.get('check_records', {}))

    with _id_registry_lock:
        id_count = len(id_registry)
        duplicate_ids = []
        for _id, _entry in id_registry.items():
            if isinstance(_entry, dict):
                _posters = _entry.get('posters', [])
            elif isinstance(_entry, list):
                _posters = _entry
            else:
                _posters = []
            if len(_posters) > 1:
                duplicate_ids.append((_id, len(_posters)))

    duplicate_ids.sort(key=lambda x: x[1], reverse=True)
    top_dupes = duplicate_ids[:5]

    top_block = ""
    if top_dupes:
        top_block = "\n\n🔁 Top duplicate IDs:\n" + "\n".join(
            f"  • {_id}  ({_n} posters)" for _id, _n in top_dupes
        )

    await update.message.reply_text(
        f"📊 Bot Statistics:\n"
        f"Total Users (Private Chats): {user_count}\n"
        f"Total Groups: {group_count}\n"
        f"Total Unique Numbers Checked (/chk): {chk_count}\n"
        f"Total Unique IDs Tracked: {id_count}\n"
        f"IDs with Duplicates: {len(duplicate_ids)}"
        f"{top_block}"
    )


async def admin_command(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data='adm_stats'),
         InlineKeyboardButton("👥 Groups", callback_data='adm_groups')],
        [InlineKeyboardButton("📢 Broadcast", callback_data='adm_broadcast')],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data='adm_botsettings')],
        [InlineKeyboardButton("❌ Close", callback_data='adm_close')],
    ])
    await update.message.reply_text(
        "🔧 <b>Admin Panel</b>",
        parse_mode='HTML',
        reply_markup=keyboard
    )


async def admin_panel_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Admin only.")
        return

    data = query.data

    if data == 'adm_close':
        await query.edit_message_text("✅ Admin panel closed.")
    elif data == 'adm_stats':
        user_count = len(context.application.bot_data.get('users', set()))
        group_count = len(context.application.bot_data.get('groups', set()))
        with _id_registry_lock:
            id_count = len(id_registry)
        await query.edit_message_text(
            f"📊 Stats:\nUsers: {user_count}\nGroups: {group_count}\nIDs tracked: {id_count}"
        )
    elif data == 'adm_groups':
        groups = context.application.bot_data.get('groups', set())
        await query.edit_message_text(f"👥 Tracked groups: {len(groups)}\nUse /listgroups for details.")
    elif data == 'adm_broadcast':
        await query.edit_message_text("📢 Use /broadcast command to send messages.")
    elif data == 'adm_botsettings':
        await bot_settings_menu_inline(query, context)


async def bot_settings_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    return await bot_settings_menu_inline(query, context)


async def bot_settings_menu_inline(query, context: CallbackContext) -> int:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Name", callback_data='admbs_name'),
         InlineKeyboardButton("📝 About", callback_data='admbs_about')],
        [InlineKeyboardButton("📄 Description", callback_data='admbs_desc')],
        [InlineKeyboardButton("❌ Cancel", callback_data='admbs_cancel')],
    ])
    await query.edit_message_text(
        "⚙️ <b>Bot Settings</b>\nဘာပြောင်းလဲလိုပါသလဲ?",
        parse_mode='HTML',
        reply_markup=keyboard
    )
    return BOT_SETTINGS_SELECT


async def bot_settings_select(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data[len('admbs_'):]
    context.user_data['admbs_field'] = field
    labels = {'name': 'Name', 'about': 'About', 'desc': 'Description'}
    await query.edit_message_text(
        f"✏️ New {labels.get(field, field)} ကို ရိုက်ထည့်ပါ:\n\n(ရပ်လိုပါက /cancel)"
    )
    return BOT_SETTINGS_AWAITING


async def bot_settings_apply(update: Update, context: CallbackContext) -> int:
    field = context.user_data.pop('admbs_field', None)
    text = update.message.text.strip()
    try:
        if field == 'name':
            await context.application.bot.set_my_name(text)
        elif field == 'about':
            await context.application.bot.set_my_short_description(text)
        elif field == 'desc':
            await context.application.bot.set_my_description(text)
        await update.message.reply_text(f"✅ Bot {field} ကို '{text}' ဟု ပြောင်းလိုက်ပါပြီ။")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END


async def bot_settings_cancel(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Bot settings ပြောင်းခြင်းကို ဖျက်သိမ်းလိုက်ပါသည်။")
    return ConversationHandler.END


# ============================================================
# DEPOSIT REPORT SYSTEM
# ============================================================
DEPOSIT_REPORT_KEY = 'deposit_reports'
WHATSAPP_REPORT_KEY = 'whatsapp_reports'


def _make_deposit_entry(jie: float, shou: float, section: str, msg_id: int, user_id=None) -> dict:
    return {
        'jie': jie,
        'shou': shou,
        'section': section,
        'msg_id': msg_id,
        'user_id': user_id,
        'parser_version': 2,
    }


def _upsert_deposit_entry(day_list: list, entry: dict) -> None:
    uid = entry.get('user_id')
    if uid is not None:
        for i, old in enumerate(day_list):
            if old.get('user_id') == uid:
                day_list[i] = entry
                return
    else:
        mid = entry.get('msg_id')
        for i, old in enumerate(day_list):
            if old.get('msg_id') == mid:
                day_list[i] = entry
                return
    day_list.append(entry)


def _parse_number_field(pattern: str, chunk: str):
    m = re.search(pattern + r'\s*([^\n]+)', chunk)
    if not m:
        return None
    raw = m.group(1).strip()
    eq_m = re.search(r'=\s*(\d+(?:\.\d+)?)', raw)
    if eq_m:
        return float(eq_m.group(1))
    num_m = re.search(r'(\d+(?:\.\d+)?)', raw)
    if num_m:
        return float(num_m.group(1))
    return None


def _parse_number_field_strict(pattern: str, chunk: str):
    m = re.search(pattern + r'[^\S\n]*([^\n]*)', chunk)
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw:
        return None
    num_m = re.search(r'^(\d+(?:\.\d+)?)$', raw.strip())
    if num_m:
        return float(num_m.group(1))
    num_m2 = re.search(r'^\s*(\d+(?:\.\d+)?)\s*$', raw)
    if num_m2:
        return float(num_m2.group(1))
    return None


def _parse_deposit_form(text: str):
    required_patterns = [
        r'接电报\s*[：:]',
        r'首冲\s*[：:]',
        r'👉\s*second',
        r'👉\s*third',
        r'👉\s*last',
        r"A\s+customer\s+who\s+doesn['']?t\s+go\s+to\s+the\s+killer\s*=",
        r'Yesterday\s+customer\s+arrive\s*=',
        r'百分之\s*[；;:]',
    ]
    if not all(re.search(pattern, text, re.IGNORECASE) for pattern in required_patterns):
        return None

    second_m = re.search(r'👉\s*second', text, re.IGNORECASE)
    third_m = re.search(r'👉\s*third', text, re.IGNORECASE)
    last_m = re.search(r'👉\s*last', text, re.IGNORECASE)
    if not second_m or not third_m or not last_m:
        return None
    if not (second_m.start() < third_m.start() < last_m.start()):
        return None

    slices = {
        'first': text[:second_m.start()],
        'second': text[second_m.start():third_m.start()],
        'third': text[third_m.start():last_m.start()],
        'last': text[last_m.start():],
    }

    def _extract(chunk):
        jie = _parse_number_field(r'接电报\s*[：:]', chunk)
        shou = _parse_number_field(r'首冲\s*[：:]', chunk)
        if jie is not None and shou is not None:
            return jie, shou
        return None

    def _extract_last(chunk):
        jie = _parse_number_field_strict(r'接电报\s*[：:]', chunk)
        shou = _parse_number_field_strict(r'首冲\s*[：:]', chunk)
        if jie is not None and shou is not None:
            return jie, shou
        return None

    r = _extract_last(slices['last'])
    if r:
        return r[0], r[1], 'last'
    for key in ['third', 'second', 'first']:
        r = _extract(slices[key])
        if r:
            return r[0], r[1], key
    return None


async def handle_deposit_report(update: Update, context: CallbackContext) -> None:
    msg = update.message
    if not msg:
        return
    text = msg.text or msg.caption or ''
    if '接电报' not in text or '首冲' not in text:
        return
    result = _parse_deposit_form(text)
    if not result:
        return
    jie, shou, section = result
    user_id = msg.from_user.id if msg.from_user else None
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(DEPOSIT_REPORT_KEY, {})
    chat_day = reports.setdefault(chat_id, {}).setdefault(today, [])
    _upsert_deposit_entry(
        chat_day,
        _make_deposit_entry(jie, shou, section, msg.message_id, user_id),
    )
    if context.application.persistence:
        await context.application.persistence.flush()


async def handle_deposit_report_edit(update: Update, context: CallbackContext) -> None:
    msg = update.edited_message
    if not msg:
        return
    text = msg.text or msg.caption or ''
    if '接电报' not in text or '首冲' not in text:
        return
    user_id = msg.from_user.id if msg.from_user else None
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(DEPOSIT_REPORT_KEY, {})
    day_list = reports.setdefault(chat_id, {}).setdefault(today, [])
    msg_id = msg.message_id

    result = _parse_deposit_form(text)

    def _fmt(v):
        return int(v) if v == int(v) else v

    key_fn = (lambda e: e.get('user_id') == user_id) if user_id else (lambda e: e.get('msg_id') == msg_id)
    updated = False
    for i, entry in enumerate(day_list):
        if key_fn(entry):
            if result:
                day_list[i] = _make_deposit_entry(result[0], result[1], result[2], msg_id, user_id)
                updated = True
            else:
                day_list.pop(i)
            break
    else:
        if result:
            _upsert_deposit_entry(
                day_list,
                _make_deposit_entry(result[0], result[1], result[2], msg_id, user_id),
            )
            updated = True

    if context.application.persistence:
        await context.application.persistence.flush()

    if updated and result:
        jie, shou, section = result
        pct = round(shou * 100 / jie, 2) if jie > 0 else 0
        await msg.reply_text(
            f"✅ Edit အတည်ပြုပြီး — [{section}]\n"
            f"接电报：{_fmt(jie)}   首冲：{_fmt(shou)}   ({pct}%)"
        )


async def deposit_total_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.get(DEPOSIT_REPORT_KEY, {})
    day_list = reports.get(chat_id, {}).get(today, [])
    valid_day_list = [
        r for r in day_list
        if r.get('parser_version') == 2 and r.get('section') in {'first', 'second', 'third', 'last'}
    ]

    if not valid_day_list:
        await update.message.reply_text("📊 ယနေ့ deposit report မရှိသေးပါ။")
        return

    total_jie = sum(r['jie'] for r in valid_day_list)
    total_shou = sum(r['shou'] for r in valid_day_list)

    if total_jie > 0:
        pct = round(total_shou * 100 / total_jie, 2)
        pct_str = f"{pct}%"
    else:
        pct_str = "N/A"

    def _fmt(v):
        return int(v) if v == int(v) else v

    await update.message.reply_text(
        f"📊 <b>Deposit Report Total</b>  ({today})\n\n"
        f"接电报 ：<b>{_fmt(total_jie)}</b>\n"
        f"首冲 ：<b>{_fmt(total_shou)}</b>\n"
        f"百分之：<b>{pct_str}</b>\n\n"
        f"<i>Total from {len(valid_day_list)} reports</i>",
        parse_mode='HTML'
    )

    reports.get(chat_id, {}).pop(today, None)
    if not reports.get(chat_id):
        reports.pop(chat_id, None)
    if context.application.persistence:
        await context.application.persistence.flush()


def _make_whatsapp_entry(jinfen: float, zhuanhua: float, register: float, section: str, msg_id: int, user_id=None) -> dict:
    return {
        'jinfen': jinfen,
        'zhuanhua': zhuanhua,
        'register': register,
        'section': section,
        'msg_id': msg_id,
        'user_id': user_id,
        'parser_version': 1,
    }


def _parse_whatsapp_form(text: str):
    required_patterns = [
        r'👉\s*first',
        r'👉\s*second',
        r'👉\s*third',
        r'👉\s*last',
        r'进粉数量\s*[：:]',
        r'转化到电报\s*[：:]',
        r'register\s*[：:]',
        r'百分之\s*[；;:]',
    ]
    if not all(re.search(pattern, text, re.IGNORECASE) for pattern in required_patterns):
        return None

    first_m = re.search(r'👉\s*first', text, re.IGNORECASE)
    second_m = re.search(r'👉\s*second', text, re.IGNORECASE)
    third_m = re.search(r'👉\s*third', text, re.IGNORECASE)
    last_m = re.search(r'👉\s*last', text, re.IGNORECASE)
    if not first_m or not second_m or not third_m or not last_m:
        return None
    if not (first_m.start() < second_m.start() < third_m.start() < last_m.start()):
        return None

    slices = {
        'first': text[first_m.start():second_m.start()],
        'second': text[second_m.start():third_m.start()],
        'third': text[third_m.start():last_m.start()],
        'last': text[last_m.start():],
    }

    def _extract(chunk):
        jinfen = _parse_number_field(r'进粉数量\s*[：:]', chunk)
        zhuanhua = _parse_number_field(r'转化到电报\s*[：:]', chunk)
        register = _parse_number_field(r'register\s*[：:]', chunk)
        if jinfen is not None and zhuanhua is not None:
            return jinfen, zhuanhua, (register if register is not None else 0)
        return None

    for key in ['last', 'third', 'second', 'first']:
        r = _extract(slices[key])
        if r:
            return r[0], r[1], r[2], key
    return None


async def handle_whatsapp_report(update: Update, context: CallbackContext) -> None:
    msg = update.message
    if not msg:
        return
    text = msg.text or msg.caption or ''
    if '进粉数量' not in text or '转化到电报' not in text:
        return
    result = _parse_whatsapp_form(text)
    if not result:
        return
    jinfen, zhuanhua, register, section = result
    user_id = msg.from_user.id if msg.from_user else None
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(WHATSAPP_REPORT_KEY, {})
    chat_day = reports.setdefault(chat_id, {}).setdefault(today, [])
    _upsert_deposit_entry(
        chat_day,
        _make_whatsapp_entry(jinfen, zhuanhua, register, section, msg.message_id, user_id),
    )
    if context.application.persistence:
        await context.application.persistence.flush()


async def handle_whatsapp_report_edit(update: Update, context: CallbackContext) -> None:
    msg = update.edited_message
    if not msg:
        return
    text = msg.text or msg.caption or ''
    if '进粉数量' not in text or '转化到电报' not in text:
        return
    user_id = msg.from_user.id if msg.from_user else None
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(WHATSAPP_REPORT_KEY, {})
    day_list = reports.setdefault(chat_id, {}).setdefault(today, [])
    msg_id = msg.message_id

    result = _parse_whatsapp_form(text)

    def _fmt(v):
        return int(v) if v == int(v) else v

    key_fn = (lambda e: e.get('user_id') == user_id) if user_id else (lambda e: e.get('msg_id') == msg_id)
    updated = False
    for i, entry in enumerate(day_list):
        if key_fn(entry):
            if result:
                day_list[i] = _make_whatsapp_entry(result[0], result[1], result[2], result[3], msg_id, user_id)
                updated = True
            else:
                day_list.pop(i)
            break
    else:
        if result:
            _upsert_deposit_entry(
                day_list,
                _make_whatsapp_entry(result[0], result[1], result[2], result[3], msg_id, user_id),
            )
            updated = True

    if context.application.persistence:
        await context.application.persistence.flush()

    if updated and result:
        jinfen, zhuanhua, register, section = result
        pct = round(zhuanhua * 100 / jinfen, 2) if jinfen > 0 else 0
        await msg.reply_text(
            f"✅ Edit အတည်ပြုပြီး — [{section}]\n"
            f"进粉数量：{_fmt(jinfen)}   转化到电报：{_fmt(zhuanhua)}   register：{_fmt(register)}   ({pct}%)"
        )


async def whatsapp_total_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.get(WHATSAPP_REPORT_KEY, {})
    day_list = reports.get(chat_id, {}).get(today, [])
    valid_day_list = [
        r for r in day_list
        if r.get('parser_version') == 1 and r.get('section') in {'first', 'second', 'third', 'last'}
    ]

    if not valid_day_list:
        await update.message.reply_text("📊 ယနေ့ WhatsApp report မရှိသေးပါ။")
        return

    total_jinfen = sum(r['jinfen'] for r in valid_day_list)
    total_zhuanhua = sum(r['zhuanhua'] for r in valid_day_list)
    total_register = sum(r.get('register', 0) for r in valid_day_list)

    def _fmt(v):
        return int(v) if v == int(v) else v

    pct_str = f"{round(total_zhuanhua * 100 / total_jinfen, 2)}%" if total_jinfen > 0 else "N/A"

    await update.message.reply_text(
        f"📊 <b>WhatsApp Report Total</b>  ({today})\n\n"
        f"进粉数量：<b>{_fmt(total_jinfen)}</b>\n"
        f"转化到电报：<b>{_fmt(total_zhuanhua)}</b>\n"
        f"register：<b>{_fmt(total_register)}</b>\n"
        f"百分之：<b>{pct_str}</b>\n\n"
        f"<i>Total from {len(valid_day_list)} reports</i>",
        parse_mode='HTML'
    )

    reports.get(chat_id, {}).pop(today, None)
    if not reports.get(chat_id):
        reports.pop(chat_id, None)
    if context.application.persistence:
        await context.application.persistence.flush()


def handle_report_forms(update: Update, context: CallbackContext):
    return asyncio.gather(
        handle_deposit_report(update, context),
        handle_whatsapp_report(update, context)
    )


def handle_report_form_edits(update: Update, context: CallbackContext):
    return asyncio.gather(
        handle_deposit_report_edit(update, context),
        handle_whatsapp_report_edit(update, context)
    )


# ============================================================
# SCHEDULE
# ============================================================

async def scheduled_message_job(context: CallbackContext) -> None:
    sched_id = context.job.data.get('sched_id')
    schedules = context.application.bot_data.get('schedules', {})
    sched = schedules.get(sched_id)
    if not sched:
        return

    for group_id in sched.get('group_ids', []):
        try:
            await context.application.bot.send_message(
                chat_id=group_id,
                text=sched['message']
            )
        except Exception as e:
            logging.warning(f"Scheduled message failed for {group_id}: {e}")

    if sched.get('type') == 'once':
        schedules.pop(sched_id, None)
        if context.application.persistence:
            await context.application.persistence.flush()


async def setschedule_command(update: Update, context: CallbackContext) -> int:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⏰ Schedule ထည့်ရန်:\n\n"
        "အချိန်ကို HH:MM ပုံစံဖြင့် ရိုက်ထည့်ပါ (Yangon time):\n"
        "ဥပမာ: 09:30 သို့မဟုတ် 18:00\n\n"
        "(ရပ်လိုပါက /cancel)"
    )
    return SCHEDULE_SET_TIME


async def schedule_set_time(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    m = re.match(r'^(\d{1,2}):(\d{2})$', text)
    if not m:
        await update.message.reply_text("❌ ပုံစံမမှန်ပါ။ HH:MM ဖြင့်ရိုက်ပါ (ဥပမာ: 09:30)")
        return SCHEDULE_SET_TIME

    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour > 23 or minute > 59:
        await update.message.reply_text("❌ အချိန်မမှန်ပါ။")
        return SCHEDULE_SET_TIME

    context.user_data['new_schedule_hour'] = hour
    context.user_data['new_schedule_minute'] = minute

    await update.message.reply_text(
        f"✅ အချိန်: {hour:02d}:{minute:02d} (Yangon)\n\n"
        "ပေးပို့မည့် message ကို ရိုက်ထည့်ပါ:"
    )
    return SCHEDULE_SET_MESSAGE


async def schedule_set_message(update: Update, context: CallbackContext) -> int:
    context.user_data['new_schedule_message'] = update.message.text

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ One-time (တစ်ကြိမ်သာ)", callback_data='sched_type_once')],
        [InlineKeyboardButton("🔁 Every Day (နေ့တိုင်း)", callback_data='sched_type_daily')],
        [InlineKeyboardButton("❌ Cancel", callback_data='sched_cancel')],
    ])
    await update.message.reply_text(
        "📌 Schedule အမျိုးအစား ရွေးချယ်ပါ:",
        reply_markup=keyboard
    )
    return SCHEDULE_SELECT_TYPE


async def schedule_select_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'sched_cancel':
        await query.edit_message_text("❌ Schedule ထည့်ခြင်းကို ဖျက်သိမ်းလိုက်ပါသည်။")
        return ConversationHandler.END

    if query.data == 'sched_type_once':
        context.user_data['new_schedule_type'] = 'once'
        type_label = "1️⃣ One-time (တစ်ကြိမ်သာ)"
    else:
        context.user_data['new_schedule_type'] = 'daily'
        type_label = "🔁 Every Day (နေ့တိုင်း)"

    groups = context.application.bot_data.get('groups', set())

    if not groups:
        await query.edit_message_text(
            "❌ Bot ကို Group တစ်ခုတွင် ထည့်ထားမှသာ Schedule ဆက်လက်သတ်မှတ်နိုင်သည်။"
        )
        return ConversationHandler.END

    keyboard = []
    for group_id in sorted(list(groups)):
        try:
            chat = await context.application.bot.get_chat(chat_id=group_id)
            name = chat.title or f"Group {group_id}"
        except Exception:
            name = f"Group {group_id}"
        keyboard.append([InlineKeyboardButton(f"👥 {name}", callback_data=f'sched_grp_{group_id}')])

    keyboard.append([InlineKeyboardButton("✅ All Groups", callback_data='sched_grp_ALL')])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data='sched_cancel')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ အမျိုးအစား: <b>{type_label}</b>\n\n👥 Group ရွေးချယ်ပါ:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return SCHEDULE_SELECT_GROUP


async def schedule_select_group(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    hour = context.user_data.pop('new_schedule_hour', None)
    minute = context.user_data.pop('new_schedule_minute', None)
    message_text = context.user_data.pop('new_schedule_message', None)
    sched_type = context.user_data.pop('new_schedule_type', 'daily')

    if hour is None or minute is None or not message_text:
        await query.edit_message_text("❌ အချက်အလက် မပြည့်စုံပါ။")
        return ConversationHandler.END

    groups = context.application.bot_data.get('groups', set())

    if query.data == 'sched_grp_ALL':
        selected_groups = list(groups)
    elif query.data.startswith('sched_grp_'):
        group_id_str = query.data[len('sched_grp_'):]
        selected_groups = [int(group_id_str)]
    elif query.data == 'sched_cancel':
        await query.edit_message_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")
        return ConversationHandler.END
    else:
        await query.edit_message_text("❌ Invalid selection.")
        return ConversationHandler.END

    import uuid
    from datetime import datetime as _dt, timedelta as _td
    sched_id = str(uuid.uuid4())[:8]

    if 'schedules' not in context.application.bot_data:
        context.application.bot_data['schedules'] = {}

    context.application.bot_data['schedules'][sched_id] = {
        'hour': hour,
        'minute': minute,
        'message': message_text,
        'group_ids': selected_groups,
        'type': sched_type,
    }

    tz = get_yangon_tz()

    if sched_type == 'once':
        now = _dt.now(tz)
        target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += _td(days=1)
        context.application.job_queue.run_once(
            scheduled_message_job,
            when=target_dt,
            name=sched_id,
            data={'sched_id': sched_id}
        )
        fire_label = target_dt.strftime("%Y-%m-%d %H:%M") + " (Yangon)"
    else:
        context.application.job_queue.run_daily(
            scheduled_message_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=sched_id,
            data={'sched_id': sched_id}
        )
        fire_label = f"နေ့တိုင်း {hour:02d}:{minute:02d} (Yangon)"

    if context.application.persistence:
        await context.application.persistence.flush()

    group_names = []
    for gid in selected_groups:
        try:
            chat = await context.application.bot.get_chat(chat_id=gid)
            group_names.append(chat.title or str(gid))
        except Exception:
            group_names.append(str(gid))

    groups_text = ", ".join(group_names) if group_names else "None"
    type_icon = "1️⃣ One-time" if sched_type == 'once' else "🔁 Every Day"

    await query.edit_message_text(
        f"✅ <b>Schedule ထည့်ပြီးပါပြီ!</b>\n\n"
        f"📌 Type: <b>{type_icon}</b>\n"
        f"⏰ Fire: <b>{fire_label}</b>\n"
        f"💬 Message: {message_text[:60]}{'...' if len(message_text) > 60 else ''}\n"
        f"👥 Groups: {groups_text}\n\n"
        f"🆔 Schedule ID: <code>{sched_id}</code>",
        parse_mode='HTML'
    )
    return ConversationHandler.END


async def schedule_cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("❌ Schedule ထည့်ခြင်းကို ဖျက်သိမ်းလိုက်ပါသည်။")
    elif update.message:
        await update.message.reply_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")

    context.user_data.pop('new_schedule_hour', None)
    context.user_data.pop('new_schedule_minute', None)
    context.user_data.pop('new_schedule_message', None)
    context.user_data.pop('new_schedule_type', None)
    return ConversationHandler.END


async def listschedules_command(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    schedules = context.application.bot_data.get('schedules', {})

    if not schedules:
        await update.message.reply_text("⏰ Active scheduled messages မရှိသေးပါ။")
        return

    text = "⏰ **Active Scheduled Messages:**\n\n"
    for idx, (sched_id, sched) in enumerate(schedules.items(), 1):
        sched_type = sched.get('type', 'daily')
        type_label = "1️⃣ One-time" if sched_type == 'once' else "🔁 Every Day"
        text += (
            f"{idx}. 🆔 `{sched_id}`\n"
            f"   📌 Type: {type_label}\n"
            f"   ⏱ Time: **{sched['hour']:02d}:{sched['minute']:02d}** (Yangon)\n"
            f"   💬 Message: {sched['message'][:50]}{'...' if len(sched['message']) > 50 else ''}\n"
            f"   👥 Groups: {len(sched['group_ids'])} group(s)\n\n"
        )

    await update.message.reply_text(text, parse_mode='Markdown')


async def removeschedule_command(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "🗑️ Schedule ဖျက်ရန်:\nFormat: /removeschedule <schedule_id>\n\n"
            "Schedule ID များကို ကြည့်ရန် /listschedules သုံးပါ။"
        )
        return

    sched_id = context.args[0].strip()
    schedules = context.application.bot_data.get('schedules', {})

    if sched_id not in schedules:
        await update.message.reply_text(f"❌ Schedule ID `{sched_id}` မတွေ့ပါ။", parse_mode='Markdown')
        return

    sched = schedules.pop(sched_id)

    current_jobs = context.application.job_queue.get_jobs_by_name(sched_id)
    for job in current_jobs:
        job.schedule_removal()

    if context.application.persistence:
        await context.application.persistence.flush()

    await update.message.reply_text(
        f"✅ Schedule **{sched['hour']:02d}:{sched['minute']:02d}** (ID: `{sched_id}`) ကို ဖျက်လိုက်ပါပြီ။",
        parse_mode='Markdown'
    )


async def delete_schedule_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Admin only.")
        return
    sched_id = query.data[len('del_sched_'):]
    schedules = context.application.bot_data.get('schedules', {})
    if sched_id in schedules:
        schedules.pop(sched_id)
        for job in context.application.job_queue.get_jobs_by_name(sched_id):
            job.schedule_removal()
        if context.application.persistence:
            await context.application.persistence.flush()
        await query.edit_message_text(f"✅ Schedule `{sched_id}` ဖျက်ပြီးပါပြီ။", parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ Schedule `{sched_id}` မတွေ့ပါ။", parse_mode='Markdown')


def restore_schedules(application: Application) -> None:
    from datetime import datetime as _dt, timedelta as _td
    schedules = application.bot_data.get('schedules', {})
    tz = get_yangon_tz()

    for sched_id, sched in schedules.items():
        sched_type = sched.get('type', 'daily')
        try:
            existing_jobs = application.job_queue.get_jobs_by_name(sched_id)
            if not existing_jobs:
                if sched_type == 'once':
                    now = _dt.now(tz)
                    target_dt = now.replace(
                        hour=sched['hour'], minute=sched['minute'],
                        second=0, microsecond=0
                    )
                    if target_dt <= now:
                        target_dt += _td(days=1)
                    application.job_queue.run_once(
                        scheduled_message_job,
                        when=target_dt,
                        name=sched_id,
                        data={'sched_id': sched_id}
                    )
                else:
                    application.job_queue.run_daily(
                        scheduled_message_job,
                        time=time(hour=sched['hour'], minute=sched['minute'], tzinfo=tz),
                        name=sched_id,
                        data={'sched_id': sched_id}
                    )
        except Exception as e:
            logging.warning(f"Failed to restore schedule {sched_id}: {e}")


# ============================================================
# PLUS COUNTER
# ============================================================

async def handle_plus_reply(update: Update, context: CallbackContext) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    original = msg.reply_to_message
    sender = original.from_user
    if not sender:
        return

    sender_id  = sender.id
    chat_id    = msg.chat.id
    msg_id     = original.message_id
    msg_key    = (chat_id, msg_id)
    count_key  = (chat_id, sender_id)

    if msg_key in plus_counted_msgs:
        given_count = plus_counted_msgs[msg_key]["count"]
        await original.reply_text(f"⚠️ ဤ အချက်အလက်အား (+) ပေးပြီးပြီ ဖြစ်ပါသည်။ (+{given_count})")
        return

    display_name = sender.full_name or sender.username or str(sender_id)
    plus_names[sender_id] = display_name
    plus_counters[count_key] = plus_counters.get(count_key, 0) + 1
    count = plus_counters[count_key]
    plus_counted_msgs[msg_key] = {"count": count, "sender_id": sender_id}
    save_plus_data()

    await original.reply_text(f"+{count}")


async def handle_minus_reply(update: Update, context: CallbackContext) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    original = msg.reply_to_message
    chat_id  = msg.chat.id
    msg_id   = original.message_id
    msg_key  = (chat_id, msg_id)

    if msg_key in data_msg_map:
        record   = data_msg_map.pop(msg_key)
        entry    = record["entry"]
        date_key = record["date_key"]
        cid_str  = record["chat_id"]

        group_data = context.application.bot_data.get('group_data', {})
        entries = group_data.get(cid_str, {}).get(date_key, [])

        if entry in entries:
            entries.remove(entry)
            group_data.setdefault(cid_str, {})[date_key] = entries
            if context.application.persistence:
                await context.application.persistence.flush()

        save_data_msg_map()
        await original.reply_text(
            f"🗑️ အောက်ပါ အချက်အလက်ကို ပယ်ဖျက်လိုက်ပါသည်:\n`{entry}`",
            parse_mode='Markdown'
        )
        return

    if msg_key not in plus_counted_msgs:
        await original.reply_text("⚠️ ဤ message သည် Deposit data (သို့) (+) မဟုတ်သောကြောင့် ပယ်ဖျက်၍ မရပါ။")
        return

    record      = plus_counted_msgs.pop(msg_key)
    given_count = record["count"]
    sender_id   = record["sender_id"]
    count_key   = (chat_id, sender_id)

    if count_key in plus_counters and plus_counters[count_key] > 0:
        plus_counters[count_key] -= 1
    save_plus_data()
    await original.reply_text(f"🗑️ +{given_count} ကိုပယ်ဖျက်လိုက်ပါသည်။")


async def total_plus_command(update: Update, context: CallbackContext) -> None:
    current_chat = update.effective_chat.id

    chat_entries = {uid: cnt for (cid, uid), cnt in plus_counters.items() if cid == current_chat}

    if not chat_entries:
        await update.message.reply_text("📊 ဤ chat တွင် မည်သည့် (+) reply မှ မရှိသေးပါ။")
        return

    lines = []
    grand_total = 0
    for idx, (uid, cnt) in enumerate(chat_entries.items(), start=1):
        grand_total += cnt
        name = plus_names.get(uid, str(uid))
        lines.append(f"  {idx}. {name} → +{cnt}")

    detail = "\n".join(lines)
    await update.message.reply_text(
        f"📊 **Plus Counter Summary**\n\n"
        f"{detail}\n\n"
        f"**Total = {grand_total}**",
        parse_mode='Markdown'
    )

    _u = update.effective_user
    _mention = f"@{_u.username}" if (_u and _u.username) else (_u.full_name if _u else "User")
    await update.message.reply_text(
        f"<i>{_mention} အလုပ်ဆင်းမည်ဆိုပါက /reset_plus နှိပ်ခဲ့ပါ</i>",
        parse_mode='HTML'
    )


async def reset_plus_command(update: Update, context: CallbackContext) -> None:
    current_chat = update.effective_chat.id
    keys_to_del = [k for k in plus_counters if k[0] == current_chat]

    if not keys_to_del:
        await update.message.reply_text("📊 ဤ chat တွင် ရှင်းလင်းစရာ Plus counter မရှိသေးပါ။")
        return

    for k in keys_to_del:
        del plus_counters[k]
    for k in [k for k in plus_counted_msgs if k[0] == current_chat]:
        del plus_counted_msgs[k]
    save_plus_data()

    await update.message.reply_text(
        f"✅ Plus counter reset ပြုလုပ်ပြီးပါပြီ။\n"
        f"🗑️ ဤ chat ထဲရှိ အဖွဲ့ဝင် {len(keys_to_del)} ဦး၏ ရေတွက်မှတ်သားမှုများ ပြန်လည်မစသည်။"
    )


# ============================================================
# DAILY BACKUP — 6:30 AM Yangon → admin PM (1827336632)
# ============================================================
BACKUP_TARGET_ID = 1827336632


async def daily_backup_job(context: CallbackContext) -> None:
    """Send id_registry.json to admin PM every day at 06:30 Yangon time."""
    save_id_registry_immediate()

    tz = get_yangon_tz()
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")

    try:
        with open(ID_REGISTRY_FILE, "rb") as f:
            file_bytes = io.BytesIO(f.read())
        file_bytes.name = f"id_registry_{date_str}.json"
        file_bytes.seek(0)

        with _id_registry_lock:
            id_count = len(id_registry)

        await context.application.bot.send_document(
            chat_id=BACKUP_TARGET_ID,
            document=InputFile(file_bytes, filename=f"id_registry_{date_str}.json"),
            caption=(
                f"📦 Daily Backup — {date_str}\n"
                f"🕠 06:30 Yangon\n"
                f"📋 Total IDs: {id_count}"
            )
        )
        logging.info(f"Daily backup sent to {BACKUP_TARGET_ID}")
    except Exception as e:
        logging.warning(f"daily_backup_job error: {e}")


# ============================================================
# GUIDE
# ============================================================

async def guide_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    guide_text = (
        "📖 *Bot အသုံးပြုနည်း လမ်းညွှန်ချက်*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "📊 */showdata*\n"
        "ယနေ့ Deposit group ထဲတွင်ပို့ထားသော အချက်အလက်များကို report တင်ရန်တစ်စုတစ်စည်းထဲ ထုတ်ပေးသည်။\n\n"

        "🗑️ */cleardata*\n"
        "ယနေ့ စုစည်းပေးထားတဲ့ deposit report data များကို နောက်ရက် data များ ရောမနှောမဖြစ်ရန် အလုပ်မဆင်းခင် /cleardata အသုံးပြုပါ။\n\n"

        "‼️ *Deposit Data ပယ်ဖျက်နည်း*\n"
        "Bot က extract လုပ်ပြီး reply ပြန်သော data message ကို \\- ဖြင့် reply လုပ်ပါ။\n"
        "Bot က ထို entry ကို /showdata မှ ချက်ချင်း ဖျက်ပေးမည်။\n\n"

        "✉️ */feedback*\n"
        "Admin ထံသို့ မှတ်ချက် သို့မဟုတ် တိုင်ကြားချက် ပေးပို့သည်။\n\n"

        "📋 */form*\n"
        "Deposit report form template ကို ကူးယူ၍ ဖြည့်သွင်းပေးပို့ရန် template ထုတ်ပေးသည်။\n\n"

        "🧮 *Math Calculator*\n"
        "Bot PM တွင် math expression ရိုက်ပါ — bot က အဖြေပေးမည်\n"
        "ဥပမာ: 2\\+2, 15\\*15\\-15, \\(100\\+50\\)\\*2, 2183699\\+3314743\n\n"

        "➕ *Whatsapp Plus Counter စနစ်*\n"
        "Register group တွင် အဖွဲ့ဝင် ပေးပို့သော message ကို \\+ ဖြင့် reply လုပ်ပါ → bot က \\+1, \\+2, \\+3\\.\\.\\. ရေတွက်သည်\n"
        "မှားယွင်း၍ ပေးမိပါက \\- ဖြင့် reply ပြန်လုပ်ပါ → bot က ပယ်ဖျက်ပေးသည်\n\n"

        "📊 */total\\_plus*\n"
        "ဤ chat ထဲရှိ အဖွဲ့ဝင် တစ်ဦးချင်းစီ၏ plus counter ပေါင်းစုပေးသည်။\n\n"

        "🔄 */reset\\_plus*\n"
        "Register group ထဲတွင် botဖြင့် \\+ ရေတွက်ခြင်းပြုလုပ်ပြီးပါက အလုပ်မဆင်းခင် /reset\\_plus ကိုနှိပ်ပါ။\n\n"

        "🔍 */chk \\<နံပါတ်\\>*\n"
        "ဖုန်းနံပါတ် / ID တစ်ခုကို စစ်ဆေးသည်။\n\n"

        "🙈 */hidemenu*\n"
        "Keyboard button panel ကို ဖျောက်သည်။ ပြန်ဖွင့်ရန် /start သို့မဟုတ် /menu နှိပ်ပါ။\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *Bot owner* \\- @satepryin1khouklite1"
    )
    await update.message.reply_text(guide_text, parse_mode='MarkdownV2')


# ============================================================
# POST INIT
# ============================================================

async def post_init(application: Application) -> None:
    restore_schedules(application)

    tz = get_yangon_tz()
    application.job_queue.run_daily(
        daily_backup_job,
        time=time(hour=6, minute=30, tzinfo=tz),
        name="daily_backup"
    )
    logging.info("Daily backup job scheduled at 06:30 Yangon")

    await application.bot.set_my_commands([
        BotCommand("start",          "Bot ကို စတင်ပါ / Menu ဖွင့်ပါ"),
        BotCommand("menu",           "Main menu ဖွင့်ပါ"),
        BotCommand("guide",          "Bot အသုံးပြုနည်း လမ်းညွှန်ချက်"),
        BotCommand("showdata",       "ယနေ့ deposit data အားလုံးကြည့်ပါ"),
        BotCommand("cleardata",      "ယနေ့ data နှင့် plus counter ဖျက်ပါ"),
        BotCommand("chk",            "ဖုန်းနံပါတ် / ID စစ်ဆေးပါ"),
        BotCommand("form",           "Deposit report form template ထုတ်ပါ"),
        BotCommand("total_plus",     "Plus counter စုစုပေါင်း ကြည့်ပါ"),
        BotCommand("reset_plus",     "Plus counter ကို ရှင်းလင်းပါ"),
        BotCommand("feedback",       "Admin ထံ မှတ်ချက် ပေးပို့ပါ"),
        BotCommand("hidemenu",       "Keyboard button panel ဖျောက်ပါ"),
        BotCommand("help",           "အကူအညီ"),
        BotCommand("stats",          "Bot အသုံးပြုမှု စာရင်း (Admin)"),
        BotCommand("listgroups",     "Group list ကြည့်ပါ (Admin)"),
        BotCommand("listschedules",  "Schedule list ကြည့်ပါ (Admin)"),
        BotCommand("admin",          "Admin panel ဖွင့်ပါ (Admin)"),
        BotCommand("clearall",       "Group အားလုံး data ရှင်းပါ - PM only (Admin)"),
        BotCommand("resetplusall",   "Group အားလုံး plus counter reset - PM only (Admin)"),
        BotCommand("deposit_total",  "接电报/首冲 report total ကြည့်ပါ"),
        BotCommand("whatsapp_total", "WhatsApp report total ကြည့်ပါ"),
    ])


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is not set!")
        return

    persistence = PicklePersistence(filepath=os.path.join(os.path.dirname(__file__), 'bot_data.pickle'))

    application = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("menu", main_menu_command))
    application.add_handler(CommandHandler("hidemenu", remove_menu))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("showdata", show_data))
    application.add_handler(CommandHandler("cleardata", clear_data))
    application.add_handler(CommandHandler("chk", check_command))
    application.add_handler(CommandHandler("form", report_form_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("listgroups", list_groups))
    application.add_handler(CommandHandler("listschedules", listschedules_command))
    application.add_handler(CommandHandler("removeschedule", removeschedule_command))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(CallbackQueryHandler(clear_group_data_callback, pattern=r'^admin_clear_-?\d+$'))
    application.add_handler(CallbackQueryHandler(cancel_group_action, pattern='^admin_cancel$'))

    bot_settings_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot_settings_menu, pattern='^adm_botsettings$')],
        states={
            BOT_SETTINGS_SELECT: [
                CallbackQueryHandler(bot_settings_select, pattern='^admbs_(name|about|desc)$'),
                CallbackQueryHandler(bot_settings_cancel, pattern='^admbs_cancel$'),
            ],
            BOT_SETTINGS_AWAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_apply),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(bot_settings_cancel, pattern='^admbs_cancel$'),
            CommandHandler('cancel', cancel_conversation),
        ],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(bot_settings_handler)

    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern='^adm_'))
    application.add_handler(CallbackQueryHandler(delete_schedule_callback, pattern='^del_sched_'))

    feedback_handler = ConversationHandler(
        entry_points=[CommandHandler("feedback", start_feedback)],
        states={
            FEEDBACK_AWAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_feedback)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        allow_reentry=True
    )
    application.add_handler(feedback_handler)

    broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start, filters=filters.User(ADMIN_IDS))],
        states={
            BROADCAST_SELECT_CHAT: [CallbackQueryHandler(broadcast_select_chat, pattern='^bcast_id_')],
            BROADCAST_AWAITING_MESSAGE: [MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL |
                 filters.AUDIO | filters.ANIMATION | filters.VOICE | filters.VIDEO_NOTE |
                 filters.Sticker.ALL) & ~filters.COMMAND,
                broadcast_await_message
            )],
            BROADCAST_CONFIRMATION: [CallbackQueryHandler(broadcast_confirm, pattern='^bcast_confirm$')]
        },
        fallbacks=[
            CallbackQueryHandler(broadcast_cancel, pattern='^bcast_cancel$'),
            CommandHandler('cancel', cancel_conversation)
        ],
        allow_reentry=True
    )
    application.add_handler(broadcast_handler)

    schedule_handler = ConversationHandler(
        entry_points=[CommandHandler("setschedule", setschedule_command, filters=filters.User(ADMIN_IDS))],
        states={
            SCHEDULE_SET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_set_time)],
            SCHEDULE_SET_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_set_message)],
            SCHEDULE_SELECT_TYPE: [CallbackQueryHandler(schedule_select_type, pattern='^sched_type_|^sched_cancel$')],
            SCHEDULE_SELECT_GROUP: [CallbackQueryHandler(schedule_select_group, pattern='^sched_grp_|^sched_cancel$')],
        },
        fallbacks=[
            CallbackQueryHandler(schedule_cancel, pattern='^sched_cancel$'),
            CommandHandler('cancel', cancel_conversation)
        ],
        allow_reentry=True
    )
    application.add_handler(schedule_handler)

    # Plus counter
    application.add_handler(MessageHandler(
        filters.REPLY & filters.Regex(r'^\+$'),
        handle_plus_reply
    ))
    application.add_handler(MessageHandler(
        filters.REPLY & filters.Regex(r'^\-$'),
        handle_minus_reply
    ))
    application.add_handler(CommandHandler("total_plus", total_plus_command))
    application.add_handler(CommandHandler("reset_plus", reset_plus_command))

    # Admin bulk
    application.add_handler(CommandHandler("clearall", admin_clearall_command))
    application.add_handler(CommandHandler("resetplusall", admin_resetplusall_command))
    application.add_handler(CallbackQueryHandler(adminall_callback, pattern=r'^adminall_'))

    application.add_handler(CommandHandler("guide", guide_command))
    application.add_handler(CommandHandler("deposit_total", deposit_total_command))
    application.add_handler(CommandHandler("whatsapp_total", whatsapp_total_command))

    # Report form handlers
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        handle_deposit_report
    ), group=1)
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        handle_whatsapp_report
    ), group=1)
    application.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION),
        handle_deposit_report_edit
    ), group=1)
    application.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION),
        handle_whatsapp_report_edit
    ), group=1)

    # Math calculator — PM only, group=2 (runs after commands/conversations)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_pm_math
    ), group=2)

    # Main data extraction
    application.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.CAPTION,
        extract_and_save_data
    ))

    async def error_handler(update, context) -> None:
        from telegram.error import Conflict, NetworkError
        err = context.error
        if isinstance(err, Conflict):
            logging.warning("Telegram Conflict: another bot instance may be running.")
            return
        if isinstance(err, NetworkError):
            logging.warning(f"NetworkError (will retry): {err}")
            return
        logging.error(f"Unhandled error: {err}", exc_info=err)

    application.add_error_handler(error_handler)

    # drop_pending_updates=True — bot restart တဲ့အချိန် offline ဆီ message တွေ ignore လုပ်မည်
    application.run_polling(poll_interval=1.0, drop_pending_updates=True)


if __name__ == '__main__':
    keep_alive()
    main()
