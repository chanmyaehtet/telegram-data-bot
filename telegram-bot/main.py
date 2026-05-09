import os
import re
import io
import math
import logging
import asyncio
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
    PicklePersistence, filters
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


# PLUS COUNTER DATA - MongoDB backed
# ============================================================

plus_counters: dict = {}
plus_names: dict = {}
plus_counted_msgs: dict = {}


def _plus_key_to_str(key: tuple) -> str:
    return f"{key[0]}:{key[1]}"


def _str_to_plus_key(s: str) -> tuple:
    parts = s.split(":", 1)
    return (int(parts[0]), int(parts[1]))


def save_plus_data() -> None:
    pass  # in-memory only


def load_plus_data() -> None:
    pass  # in-memory only; starts fresh each restart


# ============================================================
# DATA MSG MAP - MongoDB backed
# ============================================================
data_msg_map: dict = {}


def _data_key_to_str(key: tuple) -> str:
    return f"{key[0]}:{key[1]}"


def _str_to_data_key(s: str) -> tuple:
    parts = s.split(":", 1)
    return (int(parts[0]), int(parts[1]))


def save_data_msg_map() -> None:
    pass  # in-memory only


def load_data_msg_map() -> None:
    pass  # in-memory only; starts fresh each restart



# ============================================================
# STUB: get_mongo_db and get_all_duplicate_ids (MongoDB removed)
# ============================================================
def get_mongo_db():
    return None


def get_all_duplicate_ids() -> list:
    return []


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
# ADMIN ERROR NOTIFICATION
# ============================================================

async def notify_admins_error(context: CallbackContext, error_text: str) -> None:
    msg = (
        f"⚠️ <b>Bot Error Alert</b>\n\n"
        f"<pre>{error_text[:3000]}</pre>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot.send_message(
                chat_id=admin_id,
                text=msg,
                parse_mode='HTML'
            )
        except Exception as e:
            logging.warning(f"notify_admins_error: could not reach {admin_id}: {e}")


# ============================================================
# MATH CALCULATOR (PM only)
# ============================================================

def _safe_eval_math(expr: str):
    expr = expr.strip()
    expr = expr.replace('×', '*').replace('÷', '/').replace('^', '**')
    expr = expr.replace(',', '')

    allowed_names = {k: getattr(math, k) for k in dir(math) if not k.startswith('_')}
    allowed_names.update({'abs': abs, 'round': round, 'int': int, 'float': float})

    try:
        code = compile(expr, '<string>', 'eval')
        result = eval(code, {"__builtins__": {}}, allowed_names)
        return result
    except ZeroDivisionError:
        raise ValueError("Division by zero")
    except Exception:
        raise ValueError("Invalid expression")


def _looks_like_math(text: str) -> bool:
    text = text.strip()
    if re.match(r'^[\d\s\+\-\*\/\(\)\.\,\%\^×÷]+$', text):
        if re.search(r'\d', text) and re.search(r'[\+\-\*\/\^×÷]', text):
            return True
    if re.match(r'^[\d\s\(\)]+[\+\-\*\/\^×÷][\d\s\(\)\.]+', text):
        return True
    return False


async def handle_pm_math(update: Update, context: CallbackContext) -> None:
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
            result_str = str(int(result)) if result == int(result) else f"{result:.10g}"
        else:
            result_str = str(result)

        await msg.reply_text(
            f"🧮 <b>{text} = {result_str}</b>",
            parse_mode='HTML'
        )
    except ValueError:
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
        'Bot commands:\n\n'
        ' /form - Report template\n'
        ' /showdata - Show today\'s data\n'
        ' /cleardata - Clear today\'s data\n'
        ' /total_plus - Plus counter\n'
        ' /reset_plus - Reset plus counter\n'
        ' /feedback - Send feedback to admin\n'
        ' /guide - Usage guide\n'
        ' /hidemenu - Hide menu\n\n'
        '🧮 Math: Bot PM တွင် expression ရိုက်ပါ (e.g. 2+2)'
    )


async def report_form_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    await update.message.reply_text(
        "📋 Deposit Report Form Template\n\n"
        "ကော်ပီကူးယူ၍ ဖြည့်စွက်ပြီး ပို့ပေးပါ:\n\n"
        + REPORT_TEMPLATE
    )


async def main_menu_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    keyboard = [
        [KeyboardButton("/showdata"), KeyboardButton("/cleardata")],
        [KeyboardButton("/feedback"), KeyboardButton("/form")],
        [KeyboardButton("/total_plus"), KeyboardButton("/reset_plus")],
        [KeyboardButton("/guide"), KeyboardButton("/hidemenu")],
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    user_name = update.effective_user.full_name if update.effective_user else "User"

    await update.message.reply_text(
        f"မင်္ဂလာပါ။ {user_name}\n"
        "Bot အသုံးပြုနည်းသိအောင် /guide 📝 ကိုနှိပ်၍ကြည့်နိုင်ပါသည်။📌\n\n"
        "🧮 Bot PM တွင် math expression ရိုက်ပါ (e.g. 2+2)",
        reply_markup=reply_markup
    )


async def remove_menu(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    await update.message.reply_text(
        "Menu keyboard ကို ဖျက်လိုက်ပါပြီ။ /start ဖြင့် ပြန်ခေါ်နိုင်ပါသည်။😒",
        reply_markup=ReplyKeyboardRemove()
    )


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
            f"✅ Plus counter reset ပြုလုပ်ပြီးပါပြီ။"
        )
    else:
        await update.message.reply_text(f"No data found for today ({today_key}).")


async def admin_clearall_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != 'private':
        await update.message.reply_text("❌ Bot PM ထဲတွင်သာ အသုံးပြုနိုင်သည်။")
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
        f"ယနေ့ ({today_key}) data ရှိသော group <b>{sum(1 for d in group_data.values() if today_key in d)}</b> ခု ကို ရှင်းမည်။\nဆက်လုပ်မည်လား?",
        parse_mode='HTML',
        reply_markup=keyboard
    )


async def admin_resetplusall_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != 'private':
        await update.message.reply_text("❌ Bot PM ထဲတွင်သာ အသုံးပြုနိုင်သည်။")
        return

    if not plus_counters:
        await update.message.reply_text("ℹ️ ရှင်းလင်းစရာ Plus counter မရှိပါ။")
        return

    chat_count = len(set(k[0] for k in plus_counters))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ အတည်ပြု Reset မည်", callback_data="adminall_resetplus_confirm"),
        InlineKeyboardButton("❌ မလုပ်တော့ပါ", callback_data="adminall_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ <b>Group အားလုံး Plus Counter Reset မည်</b>\n\nGroup <b>{chat_count}</b> ခု ကို reset မည်။\nဆက်လုပ်မည်လား?",
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
            f"✅ <b>ရှင်းလင်းမှု ပြီးပါပြီ</b>\n\nGroup <b>{cleared_groups}</b> ခု၏ ယနေ့ ({today_key}) data ရှင်းပြီးပါပြီ။",
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
            f"✅ <b>Plus Counter Reset ပြီးပါပြီ</b>\n\nGroup <b>{chat_count}</b> ခု (entries <b>{key_count}</b>) reset ပြုလုပ်ပြီးပါပြီ။",
            parse_mode='HTML'
        )
        return


async def show_data(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    today_key = get_data_key()
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)

    context.application.bot_data.setdefault('group_data', {}).setdefault(chat_id, {})
    collected_data_list = context.application.bot_data['group_data'][chat_id].get(today_key, [])

    if not collected_data_list:
        await update.message.reply_text(f"No data collected yet for today ({today_key}) in this chat.")
        return

    grouped_data: dict = {}
    for entry in collected_data_list:
        parts = entry.split('    ')
        khaifa_name = parts[1].strip() if len(parts) >= 2 else "N/A"
        key = khaifa_name.replace(" ", "").lower()
        grouped_data.setdefault(key, []).append(entry)

    parts_list = []
    separator = "------------------------------------"
    for i, (_, entries) in enumerate(sorted(grouped_data.items())):
        if i > 0:
            parts_list.append(separator)
        parts_list.append("\n\n".join(entries))

    response_text = "\n".join(parts_list)

    if len(response_text) > 4096:
        await update.message.reply_text("Warning: Data too long. Partial display:")
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

    khaifa_match = re.search(r"(?:Khaifa|Khat)\s*[-\]]?\s*(.+?)(?:\r?\n|$)", full_text, re.IGNORECASE)
    extracted_khaifa = khaifa_match.group(1).strip() if khaifa_match else "N/A"

    date_match = re.search(r"Date\s*[-\]]?\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE)
    extracted_date = date_match.group(1).strip() if date_match else "N/A"

    email_phone_match = re.search(r"(?:Gmail|Email|Phone number|Phone)\s*[-\]]?\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE)
    extracted_email_phone = email_phone_match.group(1).strip() if email_phone_match else "N/A"

    # Extract ID field for registry check

# ============================================================
# ID REGISTRY COMMANDS
# ============================================================

# ============================================================
# FEEDBACK
# ============================================================

async def start_feedback(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        "Admin ထံ စာပေးပို့နိုင်ပါသည်။\n\n(ရပ်လိုပါက /cancel)"
    )
    return FEEDBACK_AWAITING


async def process_feedback(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    feedback_text = update.message.text

    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot.send_message(
                chat_id=admin_id,
                text=f"📩 <b>[NEW FEEDBACK]</b>\nFrom: {user.full_name} (@{user.username} - ID: {user.id})\n\n{feedback_text}",
                parse_mode='HTML'
            )
        except Exception:
            pass

    await update.message.reply_text("သင်၏ မှတ်ချက်ကို Admin ထံ ပေးပို့ပြီးပါပြီ။")
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
        except Exception:
            name = f"User {user_id}"
        keyboard.append([InlineKeyboardButton(f"👤 {name} ({user_id})", callback_data=f'bcast_id_{user_id}')])

    for group_id in sorted(list(groups)):
        try:
            chat = await context.application.bot.get_chat(chat_id=group_id)
            name = chat.title or f"Group {group_id}"
        except Exception:
            name = f"Group {group_id}"
        keyboard.append([InlineKeyboardButton(f"👥 {name} ({group_id})", callback_data=f'bcast_id_{group_id}')])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data='bcast_cancel')])

    await update.message.reply_text(
        "📢 Broadcast — ပေးပို့မည့် chat ရွေးချယ်ပါ:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BROADCAST_SELECT_CHAT


async def broadcast_select_chat(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    target_id_str = query.data[len('bcast_id_'):]
    context.user_data['target_broadcast_id'] = target_id_str

    try:
        chat = await context.application.bot.get_chat(chat_id=target_id_str)
        context.user_data['target_name'] = chat.title or chat.full_name
    except Exception:
        context.user_data['target_name'] = f"Chat {target_id_str}"

    await query.edit_message_text(
        f"✅ <b>{context.user_data['target_name']}</b> သို့ ပေးပို့ရန် ရွေးပြီး။\n\nMessage ကို forward (သို့) ရိုက်ထည့်ပါ။\n(/cancel ဖြင့် ရပ်နိုင်)",
        parse_mode='HTML'
    )
    return BROADCAST_AWAITING_MESSAGE


async def broadcast_await_message(update: Update, context: CallbackContext) -> int:
    msg = update.message
    context.user_data['broadcast_msg_id'] = msg.message_id
    context.user_data['broadcast_from_chat'] = msg.chat_id
    target_name = context.user_data.get('target_name', 'Chat')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Send", callback_data='bcast_confirm')],
        [InlineKeyboardButton("❌ Cancel", callback_data='bcast_cancel')]
    ])
    await msg.reply_text(
        f"📨 <b>{target_name}</b> သို့ ပေးပို့ရန် သေချာပါသလား?",
        reply_markup=keyboard,
        parse_mode='HTML'
    )
    return BROADCAST_CONFIRMATION


async def broadcast_confirm(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    target_id = context.user_data.pop('target_broadcast_id', None)
    msg_id = context.user_data.pop('broadcast_msg_id', None)
    from_chat = context.user_data.pop('broadcast_from_chat', None)
    target_name = context.user_data.pop('target_name', 'Unknown')

    if not target_id or not msg_id or not from_chat:
        await query.edit_message_text("❌ အချက်အလက်မပြည့်စုံ။")
        return ConversationHandler.END

    try:
        await context.application.bot.copy_message(
            chat_id=target_id, from_chat_id=from_chat, message_id=msg_id
        )
        await query.edit_message_text(f"✅ <b>{target_name}</b> ထံ ပေးပို့ပြီးပါပြီ။", parse_mode='HTML')
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")

    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Broadcast ဖျက်သိမ်းလိုက်ပါသည်။")
    elif update.message:
        await update.message.reply_text("❌ Broadcast cancelled.")
    for key in ['target_broadcast_id', 'broadcast_msg_id', 'broadcast_from_chat', 'target_name']:
        context.user_data.pop(key, None)
    return ConversationHandler.END


# ============================================================
# ADMIN PANEL
# ============================================================

async def list_users(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return

    users = context.application.bot_data.get('users', set())
    if not users:
        await update.message.reply_text("👤 PM user မရှိသေးပါ။")
        return

    lines = [f"👤 <b>PM Users</b> ({len(users)} ဦး)\n"]
    for uid in sorted(list(users)):
        try:
            chat = await context.application.bot.get_chat(chat_id=uid)
            name = chat.full_name or f"User {uid}"
            username = f" (@{chat.username})" if chat.username else ""
            lines.append(f"• {name}{username} — <code>{uid}</code>")
        except Exception:
            lines.append(f"• User <code>{uid}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def list_groups(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return

    groups = context.application.bot_data.get('groups', set())
    if not groups:
        await update.message.reply_text("Bot က group မှာ မရှိသေးပါ။")
        return

    await update.message.reply_text("📋 Tracked Groups:")
    for group_id in list(groups):
        try:
            chat = await context.application.bot.get_chat(chat_id=group_id)
            group_name = chat.title
        except Exception:
            group_name = "Unknown Group"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑️ Clear Data", callback_data=f'admin_clear_{group_id}'),
            InlineKeyboardButton("❌ Cancel", callback_data='admin_cancel')
        ]])
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>{group_name}</b> ({group_id})",
            reply_markup=keyboard,
            parse_mode='HTML'
        )


async def clear_group_data_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("Admin only.")
        return

    group_id_to_clear = query.data.split('_')[2]
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
        await query.edit_message_text(f"✅ {group_name} ({group_id_to_clear}) data ရှင်းပြီးပါပြီ။")
    else:
        await query.edit_message_text(f"No data for group {group_id_to_clear}.")


async def cancel_group_action(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Cancelled.")


async def stats(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return

    user_count = len(context.application.bot_data.get('users', set()))
    group_count = len(context.application.bot_data.get('groups', set()))

    duplicates = get_all_duplicate_ids()
    dup_count = len(duplicates)

    dup_preview = ""
    if duplicates:
        top5 = duplicates[:5]
        lines = []
        for d in top5:
            lines.append(f"  • <code>{d['id']}</code> ({d['poster_count']} posters)")
        dup_preview = "\n\n🔁 <b>Top Duplicate IDs:</b>\n" + "\n".join(lines)
        if dup_count > 5:
            dup_preview += f"\n  ... နှင့် {dup_count - 5} ခု ထပ်ရှိ"

    await update.message.reply_text(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👤 Users (PM): {user_count}\n"
        f"👥 Groups: {group_count}\n\n"
        f"🆔 Duplicate IDs: {dup_count}"
        f"{dup_preview}",
        parse_mode='HTML'
    )


async def admin_command(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data='adm_stats'),
         InlineKeyboardButton("👥 Groups", callback_data='adm_groups')],
        [InlineKeyboardButton("📢 Broadcast", callback_data='adm_broadcast')],
        [InlineKeyboardButton("🆔 Duplicate IDs", callback_data='adm_duplicates')],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data='adm_botsettings')],
        [InlineKeyboardButton("❌ Close", callback_data='adm_close')],
    ])
    await update.message.reply_text("🔧 <b>Admin Panel</b>", parse_mode='HTML', reply_markup=keyboard)


async def admin_panel_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Admin only.")
        return

    data = query.data
    if data == 'adm_close':
        await query.edit_message_text("✅ Closed.")
    elif data == 'adm_stats':
        user_count = len(context.application.bot_data.get('users', set()))
        group_count = len(context.application.bot_data.get('groups', set()))
        dup_count = len(get_all_duplicate_ids())
        await query.edit_message_text(
            f"📊 Users: {user_count}\nGroups: {group_count}\n🔁 Duplicates: {dup_count}"
        )
    elif data == 'adm_groups':
        groups = context.application.bot_data.get('groups', set())
        await query.edit_message_text(f"👥 Groups: {len(groups)}\n/listgroups ဖြင့် details ကြည့်ပါ။")
    elif data == 'adm_broadcast':
        await query.edit_message_text("📢 /broadcast command သုံးပါ။")
    elif data == 'adm_duplicates':
        duplicates = get_all_duplicate_ids()
        if not duplicates:
            await query.edit_message_text("✅ Duplicate ID မရှိပါ။")
        else:
            lines = [f"🔁 <b>Duplicate IDs</b> ({len(duplicates)} ခု)\n"]
            for i, dup in enumerate(duplicates[:10], 1):
                poster_names = ", ".join(dup['posters'])
                lines.append(f"{i}. <code>{dup['id']}</code> — {dup['poster_count']} posters\n   {poster_names}")
            if len(duplicates) > 10:
                lines.append(f"... နှင့် {len(duplicates) - 10} ခု ထပ်ရှိ")
            await query.edit_message_text("\n".join(lines), parse_mode='HTML')
    elif data == 'adm_botsettings':
        await _bot_settings_inline(query, context)


async def _bot_settings_inline(query, context: CallbackContext) -> int:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Bot Name", callback_data='admbs_name'),
            InlineKeyboardButton("U0001f4dd Short About", callback_data='admbs_about'),
        ],
        [
            InlineKeyboardButton("U0001f4c4 Description", callback_data='admbs_desc'),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data='admbs_cancel')],
    ])
    await query.edit_message_text(
        "⚙️ <b>Bot Settings</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        "ပြောင်းလစ်လိုသည့် setting ကိုနှိပပာ:\n\n"
        "• <b>Bot Name</b> — Telegram အခြာပြသောနာမည်သ\n"
        "• <b>Short About</b> — Profile အခြာအကျည်ချုပ်\n"
        "• <b>Description</b> — Bot ဖွင်မည့်အခာပြသောဖောဖြောချက်",
        parse_mode='HTML',
        reply_markup=keyboard
    )
    return BOT_SETTINGS_SELECT


async def bot_settings_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    return await _bot_settings_inline(query, context)


async def bot_settings_select(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data[len('admbs_'):]
    context.user_data['admbs_field'] = field
    labels = {'name': 'Name', 'about': 'About', 'desc': 'Description'}
    await query.edit_message_text(f"✏️ New {labels.get(field, field)} ရိုက်ထည့်ပါ:\n(/cancel ဖြင့် ရပ်)")
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
        labels = {'name': 'Name', 'about': 'Short About', 'desc': 'Description'}
        await update.message.reply_text(
            f"✅ Bot <b>{labels.get(field, field)}</b> ‘{text}’ သိုပြောင်ပြီးပီးပီးပာပြီးသည်၊",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END


async def bot_settings_cancel(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")
    return ConversationHandler.END


# ============================================================
# DEPOSIT REPORT SYSTEM
# ============================================================
DEPOSIT_REPORT_KEY = 'deposit_reports'
WHATSAPP_REPORT_KEY = 'whatsapp_reports'


def _make_deposit_entry(jie: float, shou: float, section: str, msg_id: int, user_id=None) -> dict:
    return {'jie': jie, 'shou': shou, 'section': section, 'msg_id': msg_id, 'user_id': user_id, 'parser_version': 2}


def _upsert_deposit_entry(day_list: list, entry: dict) -> None:
    uid = entry.get('user_id')
    mid = entry.get('msg_id')
    key_fn = (lambda o: o.get('user_id') == uid) if uid else (lambda o: o.get('msg_id') == mid)
    for i, old in enumerate(day_list):
        if key_fn(old):
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
    return float(num_m.group(1)) if num_m else None


def _parse_number_field_strict(pattern: str, chunk: str):
    m = re.search(pattern + r'[^\S\n]*([^\n]*)', chunk)
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw:
        return None
    num_m = re.search(r'^\s*(\d+(?:\.\d+)?)\s*$', raw)
    return float(num_m.group(1)) if num_m else None


def _parse_deposit_form(text: str):
    required_patterns = [r'接电报\s*[：:]', r'首冲\s*[：:]', r'👉\s*second', r'👉\s*third', r'👉\s*last']
    if not all(re.search(p, text, re.IGNORECASE) for p in required_patterns):
        return None

    second_m = re.search(r'👉\s*second', text, re.IGNORECASE)
    third_m = re.search(r'👉\s*third', text, re.IGNORECASE)
    last_m = re.search(r'👉\s*last', text, re.IGNORECASE)
    if not (second_m and third_m and last_m):
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
        return (jie, shou) if jie is not None and shou is not None else None

    def _extract_strict(chunk):
        jie = _parse_number_field_strict(r'接电报\s*[：:]', chunk)
        shou = _parse_number_field_strict(r'首冲\s*[：:]', chunk)
        return (jie, shou) if jie is not None and shou is not None else None

    r = _extract_strict(slices['last'])
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
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(DEPOSIT_REPORT_KEY, {})
    chat_day = reports.setdefault(chat_id, {}).setdefault(today, [])
    _upsert_deposit_entry(chat_day, _make_deposit_entry(jie, shou, section, msg.message_id, msg.from_user.id if msg.from_user else None))
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
    result = _parse_deposit_form(text)

    def _fmt(v): return int(v) if v == int(v) else v
    key_fn = (lambda e: e.get('user_id') == user_id) if user_id else (lambda e: e.get('msg_id') == msg.message_id)
    updated = False
    for i, entry in enumerate(day_list):
        if key_fn(entry):
            if result:
                day_list[i] = _make_deposit_entry(result[0], result[1], result[2], msg.message_id, user_id)
                updated = True
            else:
                day_list.pop(i)
            break
    else:
        if result:
            _upsert_deposit_entry(day_list, _make_deposit_entry(result[0], result[1], result[2], msg.message_id, user_id))
            updated = True

    if context.application.persistence:
        await context.application.persistence.flush()
    if updated and result:
        jie, shou, section = result
        pct = round(shou * 100 / jie, 2) if jie > 0 else 0
        await msg.reply_text(f"✅ Edit [{section}]\n接电报：{_fmt(jie)}   首冲：{_fmt(shou)}   ({pct}%)")


async def deposit_total_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    day_list = context.application.bot_data.get(DEPOSIT_REPORT_KEY, {}).get(chat_id, {}).get(today, [])
    valid = [r for r in day_list if r.get('parser_version') == 2 and r.get('section') in {'first', 'second', 'third', 'last'}]

    if not valid:
        await update.message.reply_text("📊 ယနေ့ deposit report မရှိသေးပါ။")
        return

    def _fmt(v): return int(v) if v == int(v) else v
    total_jie = sum(r['jie'] for r in valid)
    total_shou = sum(r['shou'] for r in valid)
    pct_str = f"{round(total_shou * 100 / total_jie, 2)}%" if total_jie > 0 else "N/A"

    await update.message.reply_text(
        f"📊 <b>Deposit Total</b>  ({today})\n\n"
        f"接电报：<b>{_fmt(total_jie)}</b>\n首冲：<b>{_fmt(total_shou)}</b>\n百分之：<b>{pct_str}</b>\n\n"
        f"<i>From {len(valid)} reports</i>",
        parse_mode='HTML'
    )
    context.application.bot_data.get(DEPOSIT_REPORT_KEY, {}).get(chat_id, {}).pop(today, None)
    if context.application.persistence:
        await context.application.persistence.flush()


def _make_whatsapp_entry(jinfen, zhuanhua, register, section, msg_id, user_id=None) -> dict:
    return {'jinfen': jinfen, 'zhuanhua': zhuanhua, 'register': register, 'section': section,
            'msg_id': msg_id, 'user_id': user_id, 'parser_version': 1}


def _parse_whatsapp_form(text: str):
    required = [r'👉\s*first', r'👉\s*second', r'👉\s*third', r'👉\s*last',
                r'进粉数量\s*[：:]', r'转化到电报\s*[：:]', r'register\s*[：:]']
    if not all(re.search(p, text, re.IGNORECASE) for p in required):
        return None

    first_m = re.search(r'👉\s*first', text, re.IGNORECASE)
    second_m = re.search(r'👉\s*second', text, re.IGNORECASE)
    third_m = re.search(r'👉\s*third', text, re.IGNORECASE)
    last_m = re.search(r'👉\s*last', text, re.IGNORECASE)
    if not (first_m and second_m and third_m and last_m):
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
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    reports = context.application.bot_data.setdefault(WHATSAPP_REPORT_KEY, {})
    _upsert_deposit_entry(reports.setdefault(chat_id, {}).setdefault(today, []),
                          _make_whatsapp_entry(jinfen, zhuanhua, register, section, msg.message_id, msg.from_user.id if msg.from_user else None))
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
    result = _parse_whatsapp_form(text)

    def _fmt(v): return int(v) if v == int(v) else v
    key_fn = (lambda e: e.get('user_id') == user_id) if user_id else (lambda e: e.get('msg_id') == msg.message_id)
    updated = False
    for i, entry in enumerate(day_list):
        if key_fn(entry):
            if result:
                day_list[i] = _make_whatsapp_entry(result[0], result[1], result[2], result[3], msg.message_id, user_id)
                updated = True
            else:
                day_list.pop(i)
            break
    else:
        if result:
            _upsert_deposit_entry(day_list, _make_whatsapp_entry(result[0], result[1], result[2], result[3], msg.message_id, user_id))
            updated = True

    if context.application.persistence:
        await context.application.persistence.flush()
    if updated and result:
        jinfen, zhuanhua, register, section = result
        pct = round(zhuanhua * 100 / jinfen, 2) if jinfen > 0 else 0
        await msg.reply_text(f"✅ Edit [{section}]\n进粉数量：{_fmt(jinfen)}   转化到电报：{_fmt(zhuanhua)}   register：{_fmt(register)}   ({pct}%)")


async def whatsapp_total_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    chat_id = str(update.effective_chat.id)
    today = get_data_key()
    day_list = context.application.bot_data.get(WHATSAPP_REPORT_KEY, {}).get(chat_id, {}).get(today, [])
    valid = [r for r in day_list if r.get('parser_version') == 1 and r.get('section') in {'first', 'second', 'third', 'last'}]

    if not valid:
        await update.message.reply_text("📊 ယနေ့ WhatsApp report မရှိသေးပါ။")
        return

    def _fmt(v): return int(v) if v == int(v) else v
    total_jinfen = sum(r['jinfen'] for r in valid)
    total_zhuanhua = sum(r['zhuanhua'] for r in valid)
    total_register = sum(r.get('register', 0) for r in valid)
    pct_str = f"{round(total_zhuanhua * 100 / total_jinfen, 2)}%" if total_jinfen > 0 else "N/A"

    await update.message.reply_text(
        f"📊 <b>WhatsApp Total</b>  ({today})\n\n"
        f"进粉数量：<b>{_fmt(total_jinfen)}</b>\n转化到电报：<b>{_fmt(total_zhuanhua)}</b>\n"
        f"register：<b>{_fmt(total_register)}</b>\n百分之：<b>{pct_str}</b>\n\n"
        f"<i>From {len(valid)} reports</i>",
        parse_mode='HTML'
    )
    context.application.bot_data.get(WHATSAPP_REPORT_KEY, {}).get(chat_id, {}).pop(today, None)
    if context.application.persistence:
        await context.application.persistence.flush()


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
            await context.application.bot.send_message(chat_id=group_id, text=sched['message'])
        except Exception as e:
            logging.warning(f"Scheduled message failed for {group_id}: {e}")

    if sched.get('type') == 'once':
        schedules.pop(sched_id, None)
        if context.application.persistence:
            await context.application.persistence.flush()


async def setschedule_command(update: Update, context: CallbackContext) -> int:
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "⏰ Schedule time ကို HH:MM ပုံစံဖြင့် ရိုက်ပါ (Yangon time):\nဥပမာ: 09:30\n\n(/cancel ဖြင့် ရပ်)"
    )
    return SCHEDULE_SET_TIME


async def schedule_set_time(update: Update, context: CallbackContext) -> int:
    m = re.match(r'^(\d{1,2}):(\d{2})$', update.message.text.strip())
    if not m:
        await update.message.reply_text("❌ HH:MM ပုံစံ မမှန်ပါ (ဥပမာ: 09:30)")
        return SCHEDULE_SET_TIME
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        await update.message.reply_text("❌ အချိန်မမှန်ပါ။")
        return SCHEDULE_SET_TIME
    context.user_data['new_schedule_hour'] = hour
    context.user_data['new_schedule_minute'] = minute
    await update.message.reply_text(f"✅ {hour:02d}:{minute:02d} (Yangon)\n\nMessage ကို ရိုက်ထည့်ပါ:")
    return SCHEDULE_SET_MESSAGE


async def schedule_set_message(update: Update, context: CallbackContext) -> int:
    context.user_data['new_schedule_message'] = update.message.text
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ One-time", callback_data='sched_type_once')],
        [InlineKeyboardButton("🔁 Every Day", callback_data='sched_type_daily')],
        [InlineKeyboardButton("❌ Cancel", callback_data='sched_cancel')],
    ])
    await update.message.reply_text("📌 Schedule type ရွေးပါ:", reply_markup=keyboard)
    return SCHEDULE_SELECT_TYPE


async def schedule_select_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'sched_cancel':
        await query.edit_message_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")
        return ConversationHandler.END

    context.user_data['new_schedule_type'] = 'once' if query.data == 'sched_type_once' else 'daily'
    type_label = "1️⃣ One-time" if context.user_data['new_schedule_type'] == 'once' else "🔁 Every Day"

    groups = context.application.bot_data.get('groups', set())
    if not groups:
        await query.edit_message_text("❌ Group မရှိသေးပါ။ Bot ကို group ထဲ ထည့်ပါ။")
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

    await query.edit_message_text(
        f"✅ Type: <b>{type_label}</b>\n\n👥 Group ရွေးပါ:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return SCHEDULE_SELECT_GROUP


async def schedule_select_group(update: Update, context: CallbackContext) -> int:
    import uuid
    from datetime import datetime as _dt, timedelta as _td

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
        selected_groups = [int(query.data[len('sched_grp_'):])]
    elif query.data == 'sched_cancel':
        await query.edit_message_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")
        return ConversationHandler.END
    else:
        await query.edit_message_text("❌ Invalid.")
        return ConversationHandler.END

    sched_id = str(uuid.uuid4())[:8]
    context.application.bot_data.setdefault('schedules', {})[sched_id] = {
        'hour': hour, 'minute': minute, 'message': message_text,
        'group_ids': selected_groups, 'type': sched_type,
    }

    tz = get_yangon_tz()
    if sched_type == 'once':
        now = _dt.now(tz)
        target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += _td(days=1)
        context.application.job_queue.run_once(scheduled_message_job, when=target_dt, name=sched_id, data={'sched_id': sched_id})
        fire_label = target_dt.strftime("%Y-%m-%d %H:%M") + " (Yangon)"
    else:
        context.application.job_queue.run_daily(
            scheduled_message_job, time=time(hour=hour, minute=minute, tzinfo=tz), name=sched_id, data={'sched_id': sched_id}
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

    await query.edit_message_text(
        f"✅ <b>Schedule ထည့်ပြီး!</b>\n\n"
        f"⏰ {fire_label}\n💬 {message_text[:60]}{'...' if len(message_text) > 60 else ''}\n"
        f"👥 {', '.join(group_names)}\n🆔 <code>{sched_id}</code>",
        parse_mode='HTML'
    )
    return ConversationHandler.END


async def schedule_cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ ဖျက်သိမ်းလိုက်ပါသည်။")
    elif update.message:
        await update.message.reply_text("❌ Cancelled.")
    for key in ['new_schedule_hour', 'new_schedule_minute', 'new_schedule_message', 'new_schedule_type']:
        context.user_data.pop(key, None)
    return ConversationHandler.END


async def listschedules_command(update: Update, context: CallbackContext) -> None:
    schedules = context.application.bot_data.get('schedules', {})
    if not schedules:
        await update.message.reply_text("⏰ Active schedule မရှိသေးပါ။")
        return

    text = "⏰ <b>Active Schedules:</b>\n\n"
    for i, (sched_id, s) in enumerate(schedules.items(), 1):
        t = "1️⃣ One-time" if s.get('type') == 'once' else "🔁 Daily"
        text += f"{i}. <code>{sched_id}</code> | {t} | {s['hour']:02d}:{s['minute']:02d} | Groups: {len(s['group_ids'])}\n"
        text += f"   💬 {s['message'][:50]}{'...' if len(s['message']) > 50 else ''}\n\n"
    await update.message.reply_text(text, parse_mode='HTML')


async def removeschedule_command(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /removeschedule <schedule_id>\n/listschedules ဖြင့် IDs ကြည့်ပါ")
        return
    sched_id = context.args[0].strip()
    schedules = context.application.bot_data.get('schedules', {})
    if sched_id not in schedules:
        await update.message.reply_text(f"❌ Schedule <code>{sched_id}</code> မတွေ့ပါ။", parse_mode='HTML')
        return
    sched = schedules.pop(sched_id)
    for job in context.application.job_queue.get_jobs_by_name(sched_id):
        job.schedule_removal()
    if context.application.persistence:
        await context.application.persistence.flush()
    await update.message.reply_text(f"✅ Schedule <code>{sched_id}</code> ({sched['hour']:02d}:{sched['minute']:02d}) ဖျက်ပြီးပါပြီ။", parse_mode='HTML')


def restore_schedules(application: Application) -> None:
    from datetime import datetime as _dt, timedelta as _td
    schedules = application.bot_data.get('schedules', {})
    tz = get_yangon_tz()
    for sched_id, sched in schedules.items():
        try:
            if not application.job_queue.get_jobs_by_name(sched_id):
                if sched.get('type') == 'once':
                    now = _dt.now(tz)
                    target_dt = now.replace(hour=sched['hour'], minute=sched['minute'], second=0, microsecond=0)
                    if target_dt <= now:
                        target_dt += _td(days=1)
                    application.job_queue.run_once(scheduled_message_job, when=target_dt, name=sched_id, data={'sched_id': sched_id})
                else:
                    application.job_queue.run_daily(
                        scheduled_message_job,
                        time=time(hour=sched['hour'], minute=sched['minute'], tzinfo=tz),
                        name=sched_id, data={'sched_id': sched_id}
                    )
        except Exception as e:
            logging.warning(f"restore schedule {sched_id} failed: {e}")


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

    sender_id = sender.id
    chat_id = msg.chat.id
    msg_key = (chat_id, original.message_id)
    count_key = (chat_id, sender_id)

    if msg_key in plus_counted_msgs:
        given_count = plus_counted_msgs[msg_key]["count"]
        await original.reply_text(f"⚠️ ဤ message အား (+) ပေးပြီးပြီ ဖြစ်ပါသည်။ (+{given_count})")
        return

    plus_names[sender_id] = sender.full_name or sender.username or str(sender_id)
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
    chat_id = msg.chat.id
    msg_key = (chat_id, original.message_id)

    if msg_key in data_msg_map:
        record = data_msg_map.pop(msg_key)
        entry = record["entry"]
        date_key = record["date_key"]
        cid_str = record["chat_id"]

        group_data = context.application.bot_data.get('group_data', {})
        entries = group_data.get(cid_str, {}).get(date_key, [])
        if entry in entries:
            entries.remove(entry)
            group_data.setdefault(cid_str, {})[date_key] = entries
            if context.application.persistence:
                await context.application.persistence.flush()

        save_data_msg_map()
        await original.reply_text(f"🗑️ ပယ်ဖျက်လိုက်ပါသည်:\n`{entry}`", parse_mode='Markdown')
        return

    if msg_key not in plus_counted_msgs:
        await original.reply_text("⚠️ ဤ message သည် Deposit data (သို့) (+) မဟုတ်ပါ။")
        return

    record = plus_counted_msgs.pop(msg_key)
    given_count = record["count"]
    sender_id = record["sender_id"]
    count_key = (chat_id, sender_id)
    if count_key in plus_counters and plus_counters[count_key] > 0:
        plus_counters[count_key] -= 1
    save_plus_data()
    await original.reply_text(f"🗑️ +{given_count} ကို ပယ်ဖျက်လိုက်ပါသည်။")


async def total_plus_command(update: Update, context: CallbackContext) -> None:
    current_chat = update.effective_chat.id
    chat_entries = {uid: cnt for (cid, uid), cnt in plus_counters.items() if cid == current_chat}

    if not chat_entries:
        await update.message.reply_text("📊 ဤ chat တွင် (+) reply မရှိသေးပါ။")
        return

    lines = []
    grand_total = 0
    for i, (uid, cnt) in enumerate(chat_entries.items(), 1):
        grand_total += cnt
        name = plus_names.get(uid, str(uid))
        lines.append(f"  {i}. {name} → +{cnt}")

    await update.message.reply_text(
        f"📊 <b>Plus Counter</b>\n\n" + "\n".join(lines) + f"\n\n<b>Total = {grand_total}</b>",
        parse_mode='HTML'
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
        await update.message.reply_text("📊 Reset စရာ Plus counter မရှိပါ။")
        return

    for k in keys_to_del:
        del plus_counters[k]
    for k in [k for k in plus_counted_msgs if k[0] == current_chat]:
        del plus_counted_msgs[k]
    save_plus_data()
    await update.message.reply_text(f"✅ Plus counter reset ပြုလုပ်ပြီးပါပြီ။ ({len(keys_to_del)} ဦး)")


# ============================================================
# GUIDE
# ============================================================

# ============================================================
# GUIDE PAGES - Multi-page inline navigation
# ============================================================

GUIDE_PAGES = [
    {
        "title": "📖 Bot လမ်းညွှန် (1/5) — Report Form",
        "text": (
            "<b>📋 Report Form ပုံစံ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "/form ကိုနှိပ်၍ template ကူးယူပါ။\n\n"
            "<b>ဖြည့်ရမည့် field များ:</b>\n"
            "• Gmail\n"
            "• Tele name\n"
            "• Username\n"
            "• Date\n"
            "• Age\n"
            "• Current work\n"
            "• Phone number\n"
            "• ID\n"
            "• Khaifa\n\n"
            "ဖြည့်ပြီးပါက group ထဲ paste လုပ်ပါ။"
        ),
    },
    {
        "title": "📖 Bot လမ်းညွှန် (2/5) — Data စီမံခန့်ခွဲမှု",
        "text": (
            "<b>📊 Data စီမံခန့်ခွဲမှု</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>/showdata</b>\n"
            "ယနေ့ deposit data တစ်စုတစ်စည်းထုတ်ပေးသည်။\n\n"
            "<b>/cleardata</b>\n"
            "ယနေ့ data နှင့် plus counter ရှင်းလင်းသည်။\n"
            "⚠️ နေ့တိုင်း အလုပ်မဆင်းမီ သုံးပါ။\n\n"
            "<b>‼️ Data တစ်ခုတည်း ဖျက်နည်း:</b>\n"
            "Bot reply ပြန်သော message ကို\n"
            "<code>-</code> ဖြင့် reply ပြန်ပါ → ဆောင်ရွက်ပေးမည်။\n\n"
            "<b>/deposit_total</b> — Deposit report ကြည့်\n"
            "<b>/whatsapp_total</b> — WhatsApp report ကြည့်"
        ),
    },
    {
        "title": "📖 Bot လမ်းညွှန် (3/5) — Plus Counter",
        "text": (
            "<b>➕ Plus Counter စနစ်</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Message ကို <code>+</code> ဖြင့် reply ပြန်ပါ\n"
            "→ bot က <b>+1, +2, +3...</b> ရေတွက်ပေးမည်။\n\n"
            "မှားမိပါက <code>-</code> ဖြင့် reply → ပယ်ဖျက်ပေးမည်။\n\n"
            "<b>/total_plus</b> — Plus counter summary ကြည့်\n"
            "<b>/reset_plus</b> — Plus counter ရှင်းလင်း\n\n"
            "<b>🧮 Math Calculator</b>\n"
            "Bot PM ထဲတွင် expression ရိုက်ရုံဖြင့် တွက်ပေးသည်:\n"
            "ဥပမာ: <code>2+2</code>, <code>15*15</code>, <code>100/4</code>"
        ),
    },
    {
        "title": "📖 Bot လမ်းညွှန် (4/5) — ✉️ Feedback & Menu",
        "text": (
            "<b>✉️ Feedback ပေးပို့နည်း</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "/feedback ကိုနှိပ်ပြီး\n"
            "Admin ထံ မှတ်ချက်/အကြံပြုချက် ပေးပို့နိုင်သည်။\n\n"
            "<b>📱 Menu စီမံခန့်ခွဲမှု</b>\n"
            "<b>/menu</b> — Main menu ဖွင့်\n"
            "<b>/start</b> — Bot စတင် / menu ပြ\n"
            "<b>/hidemenu</b> — Keyboard ဖျောက်\n\n"
            "<b>🔎 Command အားလုံး:</b>\n"
            "<b>/help</b> ကိုနှိပ်ပြီး command list အပြည့်ကြည့်နိုင်သည်။"
        ),
    },
    {
        "title": "📖 Bot လမ်းညွှန် (5/5) — ⚠️ Duplicate ID သတိပေးပုံ",
        "text": (
            "<b>⚠️ ID Duplicate သတိပေးပုံစံ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Duplicate စစ်ဆေးတွေ့ပါက bot က ဤပုံစံဖြင့် သတိပေးမည်:\n\n"
            "<i>⚠️ ဤ client သည် ရောက်ပြီးသားဖြစ်ပါသည်။⚠️\n"
            "အောက်တွင်ဖော်ပြထားသည်။ဘယ်အဆင့်ရောက်နေလဲမေးမြန်းပါ။\n"
            "Deposit - @example\n"
            "Gmail - example</i>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>Bot owner</b> — @satepryin1khouklite1"
        ),
    },
]


def _guide_keyboard(page: int) -> InlineKeyboardMarkup:
    total = len(GUIDE_PAGES)
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"guide_page_{page - 1}"))
    row.append(InlineKeyboardButton("🏠 Home", callback_data="guide_page_0"))
    if page < total - 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"guide_page_{page + 1}"))
    return InlineKeyboardMarkup([row])


async def guide_command(update: Update, context: CallbackContext) -> None:
    await save_chat_id(update.effective_chat.id, context, update.effective_chat.type)
    page = GUIDE_PAGES[0]
    await update.message.reply_text(
        f"{page['title']}\n━━━━━━━━━━━━━━━━━━━━\n\n{page['text']}",
        parse_mode='HTML',
        reply_markup=_guide_keyboard(0)
    )


async def guide_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    page_idx = int(query.data.split("_")[-1])
    if page_idx < 0 or page_idx >= len(GUIDE_PAGES):
        return
    page = GUIDE_PAGES[page_idx]
    await query.edit_message_text(
        f"{page['title']}\n━━━━━━━━━━━━━━━━━━━━\n\n{page['text']}",
        parse_mode='HTML',
        reply_markup=_guide_keyboard(page_idx)
    )



# POST INIT
# ============================================================

async def post_init(application: Application) -> None:
    restore_schedules(application)
    await application.bot.set_my_commands([
        BotCommand("start",          "Bot စတင် / Menu ဖွင့်"),
        BotCommand("menu",           "Main menu"),
        BotCommand("guide",          "Bot လမ်းညွှန်"),
        BotCommand("showdata",       "ယနေ့ data ကြည့်"),
        BotCommand("cleardata",      "ယနေ့ data ဖျက်"),
        BotCommand("form",           "Report template"),
        BotCommand("total_plus",     "Plus counter ကြည့်"),
        BotCommand("reset_plus",     "Plus counter ရှင်း"),
        BotCommand("feedback",       "Admin ထံ မှတ်ချက်"),
        BotCommand("hidemenu",       "Keyboard ဖျောက်"),
        BotCommand("help",           "Help"),
        BotCommand("stats",          "Bot stats (Admin)"),
        BotCommand("listusers",      "User list (Admin)"),
        BotCommand("listgroups",     "Group list (Admin)"),
        BotCommand("listschedules",  "Schedule list"),
        BotCommand("admin",          "Admin panel (Admin)"),
        BotCommand("clearall",       "Data အားလုံး ရှင်း (Admin PM)"),
        BotCommand("resetplusall",   "Plus counter အားလုံး reset (Admin PM)"),
        BotCommand("deposit_total",  "Deposit report total"),
        BotCommand("whatsapp_total", "WhatsApp report total"),
    ])


# ============================================================
# ERROR HANDLER WITH ADMIN NOTIFICATION
# ============================================================

async def error_handler(update: object, context: CallbackContext) -> None:
    import traceback
    from telegram.error import Conflict, NetworkError, TimedOut

    err = context.error

    if isinstance(err, Conflict):
        logging.warning("Telegram Conflict: another bot instance running.")
        return
    if isinstance(err, (NetworkError, TimedOut)):
        logging.warning(f"Network error (will retry): {err}")
        return

    logging.error(f"Unhandled error: {err}", exc_info=err)

    tb_str = "".join(traceback.format_exception(type(err), err, err.__traceback__))

    update_info = ""
    if isinstance(update, Update):
        chat = update.effective_chat
        user = update.effective_user
        update_info = (
            f"\n👤 User: {user.full_name if user else 'N/A'} (ID: {user.id if user else 'N/A'})\n"
            f"💬 Chat: {chat.title if chat and chat.title else 'PM'} (ID: {chat.id if chat else 'N/A'})"
        )

    error_msg = (
        f"⚠️ <b>Bot Error</b>{update_info}\n\n"
        f"<b>Error:</b> <code>{str(err)[:500]}</code>\n\n"
        f"<b>Traceback:</b>\n<pre>{tb_str[-1500:]}</pre>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot.send_message(
                chat_id=admin_id,
                text=error_msg,
                parse_mode='HTML'
            )
        except Exception as e:
            logging.warning(f"Could not notify admin {admin_id}: {e}")


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
    application.add_handler(CommandHandler("form", report_form_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("listusers", list_users))
    application.add_handler(CommandHandler("listgroups", list_groups))
    application.add_handler(CommandHandler("listschedules", listschedules_command))
    application.add_handler(CommandHandler("removeschedule", removeschedule_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("clearall", admin_clearall_command))
    application.add_handler(CommandHandler("resetplusall", admin_resetplusall_command))
    application.add_handler(CommandHandler("guide", guide_command))
    application.add_handler(CallbackQueryHandler(guide_callback, pattern=r'^guide_page_\d+$'))
    application.add_handler(CommandHandler("deposit_total", deposit_total_command))
    application.add_handler(CommandHandler("whatsapp_total", whatsapp_total_command))
    application.add_handler(CommandHandler("total_plus", total_plus_command))
    application.add_handler(CommandHandler("reset_plus", reset_plus_command))

    application.add_handler(CallbackQueryHandler(clear_group_data_callback, pattern=r'^admin_clear_-?\d+$'))
    application.add_handler(CallbackQueryHandler(cancel_group_action, pattern='^admin_cancel$'))
    application.add_handler(CallbackQueryHandler(adminall_callback, pattern=r'^adminall_'))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern='^adm_'))

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
        allow_reentry=True, per_message=False,
    )
    application.add_handler(bot_settings_handler)

    feedback_handler = ConversationHandler(
        entry_points=[CommandHandler("feedback", start_feedback)],
        states={FEEDBACK_AWAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_feedback)]},
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
        entry_points=[CommandHandler("setschedule", setschedule_command)],
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

    application.add_handler(MessageHandler(filters.REPLY & filters.Regex(r'^\+$'), handle_plus_reply))
    application.add_handler(MessageHandler(filters.REPLY & filters.Regex(r'^\-$'), handle_minus_reply))

    application.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_deposit_report
    ), group=1)
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_whatsapp_report
    ), group=1)
    application.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION), handle_deposit_report_edit
    ), group=1)
    application.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION), handle_whatsapp_report_edit
    ), group=1)

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_pm_math
    ), group=2)

    application.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.CAPTION, extract_and_save_data
    ))

    application.add_error_handler(error_handler)

    application.run_polling(poll_interval=1.0, drop_pending_updates=True)


if __name__ == '__main__':
    keep_alive()
    main()
