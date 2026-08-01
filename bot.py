# -*- coding: utf-8 -*-
import os
import random
import re
import time
import io
import telebot
import segno
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InputFile
from telebot.handler_backends import BaseMiddleware
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread, Timer
from queue import Queue

# Telegram's built-in identity when a group admin posts with "Remain anonymous" on.
GROUP_ANONYMOUS_BOT_ID = 1087968824

# --- RENDER KEEP-ALIVE SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running and healthy!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

# --- CONFIGURATION (Environment Variables) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
admin_id_env = os.getenv('ADMIN_ID')
ADMIN_ID = int(admin_id_env) if admin_id_env and admin_id_env.isdigit() else 0
UPI_ID = os.getenv('UPI_ID')
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')

# Emoji pool used to give each channel button a random face — picked fresh every time
# the channel list is rendered so the list feels lively and each entry looks distinct.
FACE_EMOJIS = [
    "😀", "😂", "🤣", "😎", "🤩", "😘", "🥳", "😜", "🤪", "😈",
    "🥶", "🥷", "👻", "🤡", "👹", "👾", "💀", "👽", "🦃", "🐱",
    "🐶", "🐼", "🐯", "🦁", "🐺", "🐧", "🐻", "🦊", "🐷", "⚡", 
    "🍌", "🍓", "🍾", "💋", "😈", "🙈", "😇", "😨", "❤", "🔥", "🥰"
]

# All Telegram-supported reaction emojis (Bot API 7.x) — used to auto-react to every incoming message.
# Telegram only accepts reactions from this specific set; arbitrary Unicode will be rejected.
REACT_EMOJIS = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "🌚", "🌭", "💯", "🤣", "⚡", "🍌",
    "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "😈", "😴", "😭",
    "🤓", "👻", "👀", "🎃", "🙈", "😇", "😨", "🙉", "🐝", "🤗",
    "🫡", "🎅", "💅", "🤪", "🗿", "🆒", "💘", "😘", "🦄", "😻",
    "🌸", "🎁", "🎈", "🎊",
]

bot = telebot.TeleBot(BOT_TOKEN, use_class_middlewares=True)

# --- AUTO-REACT QUEUE SYSTEM ---
# Uses a single persistent background thread to send message reactions sequentially.
# This prevents spawning separate threads for every message, avoids connection pool exhaustion,
# and handles Telegram rate limits (429) gracefully by sleeping and retrying.

reaction_queue = Queue()

def reaction_worker():
    import time
    while True:
        try:
            chat_id, message_id = reaction_queue.get()
            try:
                emoji = random.choice(REACT_EMOJIS)
                bot.set_message_reaction(
                    chat_id,
                    message_id,
                    reaction=[telebot.types.ReactionTypeEmoji(emoji)],
                    is_big=False
                )
            except telebot.apihelper.ApiTelegramException as e:
                # If we get rate limited (429), wait and retry
                if e.error_code == 429:
                    retry_after = 2
                    try:
                        if hasattr(e, 'result_json') and e.result_json and 'parameters' in e.result_json:
                            retry_after = e.result_json['parameters'].get('retry_after', 2)
                    except:
                        pass
                    time.sleep(retry_after)
                    # Re-queue the reaction request
                    reaction_queue.put((chat_id, message_id))
            except Exception:
                pass
            finally:
                reaction_queue.task_done()
        except Exception:
            pass

# Start the auto-react worker thread
Thread(target=reaction_worker, daemon=True).start()

class AutoReactMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message', 'channel_post']

    def pre_process(self, message, data):
        if message.from_user:
            try:
                if bot.user and message.from_user.id == bot.user.id:
                    return
            except Exception:
                pass

        is_anon_admin = (
            getattr(message, 'sender_chat', None) is not None or
            (message.from_user and message.from_user.id == GROUP_ANONYMOUS_BOT_ID) or
            message.from_user is None
        )

        # Track human members in groups/channels (Bot API cannot list all members).
        try:
            if (
                getattr(message, 'chat', None)
                and message.chat.type in ('group', 'supergroup', 'channel')
                and message.from_user
                and not message.from_user.is_bot
            ):
                track_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    getattr(message.from_user, 'username', None),
                    getattr(message.from_user, 'first_name', None),
                )
        except Exception:
            pass

        if message.from_user and message.from_user.is_bot and not is_anon_admin:
            return
        reaction_queue.put((message.chat.id, message.message_id))

    def post_process(self, message, data, exception):
        pass

bot.setup_middleware(AutoReactMiddleware())

client = MongoClient(MONGO_URI)
db = client['sub_management']
channels_col = db['channels']
users_col = db['users']
payments_col = db['payments']
seen_users_col = db['seen_users']  # every user who has ever started/interacted with the bot (for /broadcast)
counters_col = db['counters']      # lifetime totals (sales/revenue) that survive even if old payment logs get pruned
pending_checkouts_col = db['pending_checkouts']  # multi-channel cart checkouts awaiting admin approval
chat_members_col = db['chat_members']  # users seen in each group/channel (for /remove all)
tracked_chats_col = db['tracked_chats']  # every group/channel the bot is a member of (auto-detected on join)

def track_chat_member(chat_id, user_id, username=None, first_name=None):
    if not chat_id or not user_id:
        return
    try:
        doc = {"chat_id": chat_id, "user_id": user_id, "last_seen": datetime.now()}
        if username:
            doc["username"] = username
        if first_name:
            doc["first_name"] = first_name
        chat_members_col.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": doc},
            upsert=True
        )
    except Exception:
        pass

def untrack_chat_member(chat_id, user_id):
    try:
        chat_members_col.delete_one({"chat_id": chat_id, "user_id": user_id})
    except Exception:
        pass

def record_tracked_chat(chat_id, chat_type=None, chat_obj=None):
    """Remember that the bot is a member of this group/channel, so its member
    data can be collected and used for /remove all and similar commands."""
    if not chat_id:
        return
    doc = {
        "chat_id": int(chat_id),
        "type": chat_type,
        "active": True,
        "joined_at": datetime.now(),
    }
    if chat_obj:
        try:
            if getattr(chat_obj, 'title', None):
                doc["title"] = chat_obj.title
            if getattr(chat_obj, 'username', None):
                doc["username"] = chat_obj.username
        except Exception:
            pass
    try:
        tracked_chats_col.update_one({"chat_id": int(chat_id)}, {"$set": doc}, upsert=True)
    except Exception:
        pass

def untrack_chat(chat_id):
    try:
        tracked_chats_col.update_one({"chat_id": int(chat_id)}, {"$set": {"active": False}})
    except Exception:
        pass

def sync_chat_admins(chat_id):
    """Fetch and store the admins of a chat the bot belongs to.

    Telegram only lets bots read the ADMIN list of a chat — it provides no API
    to enumerate all (non-admin) members, so admins are the only "existing
    members" the bot can backfill automatically. Everyone else is collected
    passively (messages, joins/leaves) from the moment the bot joins."""
    try:
        admins = bot.get_chat_administrators(chat_id)
        admin_ids = []
        for a in admins:
            if not a.user:
                continue
            try:
                uid = int(a.user.id)
            except (TypeError, ValueError):
                continue
            admin_ids.append(uid)
            track_chat_member(chat_id, uid, getattr(a.user, 'username', None), getattr(a.user, 'first_name', None))
        tracked_chats_col.update_one(
            {"chat_id": int(chat_id)},
            {"$set": {"admin_ids": admin_ids, "last_synced": datetime.now()}},
            upsert=True
        )
    except Exception:
        pass

def sync_all_tracked_chats():
    """Re-sync admins for every chat the bot belongs to (used on startup)."""
    try:
        for doc in tracked_chats_col.find({"active": True}):
            sync_chat_admins(doc['chat_id'])
    except Exception:
        pass

# In-memory shopping cart per user: {user_id: [ {channel_id, name, t, price}, ... ]}
# Intentionally NOT persisted to Mongo — carts are transient (pre-payment); if the bot restarts
# mid-browsing the user just re-adds items, no paid transaction data is ever at risk.
user_carts = {}

# How old data has to be before /cleanup will offer to delete it
CLEANUP_PAYMENTS_DAYS = 180   # keep itemized payment logs for 6 months; lifetime totals in counters_col are unaffected
CLEANUP_SEENUSERS_DAYS = 90   # drop "seen user" tracking for people inactive 90+ days (only affects /broadcast reach)

# --- AUTO-VANISH SYSTEM ---
# Rule: messages sent directly in reply to a typed command (/start, /channels, /stats, etc.)
# are NEVER auto-deleted. Only menus reached by tapping an inline button (browsing channels,
# picking a plan, admin edit menus, etc.) get scheduled for deletion if left untouched.
# Prompts that are waiting on a text/photo reply (register_next_step_handler) are ALSO never
# auto-deleted, even if they were reached via a button tap, since deleting them mid-flow would
# make the admin/user lose the instructions they still need to complete the action.
MENU_VANISH_SECONDS = 30        # 30 seconds of inactivity on a button-driven menu
COMMAND_VANISH_SECONDS = 20     # how long a /command reply or any regular bot message stays visible
PAYMENT_VANISH_SECONDS = 30     # 30 seconds for QR code AFTER 'I Have Paid' is clicked (so user can come back)
DECISION_VANISH_SECONDS = 15    # how long the approval/rejection receipt stays after admin acts
ADMIN_REPLY_VANISH_SECONDS = 10 # how long admin confirmation/error replies stay before auto-deleting
SYNC_VANISH_SECONDS = 60        # how long /sync's long member-list replies stay before auto-deleting
QR_SHOW_SECONDS = 90            # how long the initial QR is shown before 'I Have Paid' is clicked

pending_deletes = {}  # (chat_id, message_id) -> Timer
last_bot_msg = {}    # user_id -> (chat_id, message_id)  — for animated dismissal of old replies

def cancel_delete(chat_id, message_id):
    key = (chat_id, message_id)
    timer = pending_deletes.pop(key, None)
    if timer:
        timer.cancel()

def schedule_delete(chat_id, message_id, delay=MENU_VANISH_SECONDS):
    """(Re)schedules a message for deletion after `delay` seconds. Calling this again on the
    same message (e.g. after editing it) simply resets the countdown."""
    cancel_delete(chat_id, message_id)
    key = (chat_id, message_id)
    def _delete():
        try:
            bot.delete_message(chat_id, message_id)
        except: pass
        pending_deletes.pop(key, None)
    timer = Timer(delay, _delete)
    timer.daemon = True
    pending_deletes[key] = timer
    timer.start()

def send_menu(chat_id, text, reply_markup=None, parse_mode=None, delay=MENU_VANISH_SECONDS):
    """Send a button-driven menu message; it self-deletes after `delay` seconds of inactivity."""
    msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    schedule_delete(chat_id, msg.message_id, delay)
    return msg

def edit_menu(chat_id, message_id, text, reply_markup=None, parse_mode=None, delay=MENU_VANISH_SECONDS, message_obj=None):
    """Edit a button-driven menu message in place, and reset its vanish timer.
    If the message to edit is a photo message or editing fails, we delete it and send a fresh text message."""
    is_photo = False
    if message_obj and getattr(message_obj, 'photo', None):
        is_photo = True

    if is_photo:
        cancel_delete(chat_id, message_id)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        schedule_delete(chat_id, msg.message_id, delay)
    else:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
            schedule_delete(chat_id, message_id, delay)
        except Exception:
            # Fallback: Delete and send fresh text message
            cancel_delete(chat_id, message_id)
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            schedule_delete(chat_id, msg.message_id, delay)


def edit_caption_menu(chat_id, message_id, caption, reply_markup=None, parse_mode=None, delay=MENU_VANISH_SECONDS):
    """Edit a photo message's caption in place, and reset its vanish timer."""
    bot.edit_message_caption(caption=caption, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup, parse_mode=parse_mode)
    schedule_delete(chat_id, message_id, delay)

def send_prompt(chat_id, text, reply_markup=None, parse_mode=None):
    """Send a message that is awaiting a text/photo reply via register_next_step_handler.
    These NEVER auto-vanish, regardless of how the flow was entered (command or button),
    because the person still needs to read it to complete their reply."""
    return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

def send_admin_reply(text, parse_mode=None, reply_markup=None, delay=ADMIN_REPLY_VANISH_SECONDS):
    """Send a confirmation or error reply to the admin that auto-deletes after `delay` seconds
    (default 20 s). Used for one-shot result messages like '✅ Price updated' or '❌ Invalid format'
    so the admin chat stays clean without the admin having to manually dismiss anything."""
    msg = bot.send_message(ADMIN_ID, text, parse_mode=parse_mode, reply_markup=reply_markup)
    schedule_delete(ADMIN_ID, msg.message_id, delay)
    return msg

def _safe_reply(message, text, parse_mode=None, delay=COMMAND_VANISH_SECONDS):
    """reply_to works in supergroups, but plain channels don't support message
    replies — there it silently fails and the admin sees nothing. Fall back to
    sending a regular message in that case. The reply auto-vanishes after
    `delay` seconds (default 20 s), keeping group/channel chat clean.
    If a Markdown-formatted message is rejected by Telegram (e.g. an unescaped
    character in a user's name breaks entity parsing), it is re-sent as plain
    text instead of being silently dropped."""
    def _send_with(parse):
        try:
            return bot.reply_to(message, text, parse_mode=parse)
        except Exception:
            try:
                return bot.send_message(message.chat.id, text, parse_mode=parse)
            except Exception:
                return None

    msg = _send_with(parse_mode)
    if msg is None and parse_mode:
        # Telegram rejected the Markdown — resend without formatting
        msg = _send_with(None)
    if msg and getattr(message.chat, 'type', None) in ('group', 'supergroup', 'channel'):
        schedule_delete(message.chat.id, msg.message_id, delay)
    return msg

def track_msg(user_id, msg):
    """Remember `msg` as the latest bot reply for `user_id` so it can be
    animated-out when the next command fires."""
    last_bot_msg[user_id] = (msg.chat.id, msg.message_id)

def dismiss_previous(chat_id, user_id):
    """If there is a previous bot reply for this user, edit it to a vanish
    animation frame (💨 ✦ ·) and then delete it 2 seconds later.
    This runs entirely in a daemon thread so it never blocks the main flow."""
    entry = last_bot_msg.pop(user_id, None)
    if not entry:
        return
    prev_chat_id, prev_msg_id = entry
    # Cancel any existing scheduled-delete timer so the two don't race
    cancel_delete(prev_chat_id, prev_msg_id)
    def _animate_out():
        import time
        # Frame 1 — shrinking text suggests motion
        try:
            bot.edit_message_text("💨  ·  ·  ·", prev_chat_id, prev_msg_id)
        except: pass
        time.sleep(1)
        # Frame 2 — almost gone
        try:
            bot.edit_message_text("·", prev_chat_id, prev_msg_id)
        except: pass
        time.sleep(1)
        # Delete
        try:
            bot.delete_message(prev_chat_id, prev_msg_id)
        except: pass
    t = Thread(target=_animate_out, daemon=True)
    t.start()

def send_command_reply(message, text, reply_markup=None, parse_mode=None):
    """Reply to a typed /command:
    - Previous bot reply animates out (💨 → deleted after 2 s).
    - The bot's new reply auto-vanishes after COMMAND_VANISH_SECONDS (15 s).
    Returns the sent Message object."""
    user_id = message.from_user.id
    # Dismiss the old reply with animation
    dismiss_previous(message.chat.id, user_id)
    reply = bot.send_message(message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    schedule_delete(message.chat.id, reply.message_id, COMMAND_VANISH_SECONDS)
    track_msg(user_id, reply)
    return reply

def record_seen_user(user):
    """Upserts this user into seen_users_col so /broadcast can reach them later,
    regardless of whether they ever subscribe to anything. Called explicitly at the
    top of every user-facing entry point (rather than via a catch-all handler) so it
    can never intercept or interfere with /commands, next-step prompts, or callbacks."""
    if not user:
        return
    try:
        seen_users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "user_id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "last_seen": datetime.now(),
            }},
            upsert=True
        )
    except:
        pass

# --- HELPERS ---

def escape_markdown(text):
    """Escapes Telegram Markdown special characters in dynamic strings (usernames, channel titles, etc.)."""
    if not text:
        return ""
    text = str(text)
    for char in ['_', '*', '`', '[']:
        text = text.replace(char, f"\\{char}")
    return text

def format_label(total_minutes):
    """Turns a stored total-minutes duration into a readable 'Xd Xh Xm' label.
    A plan can also be stored with the special key 'lifetime' for permanent access."""
    if str(total_minutes).lower() == "lifetime":
        return "Lifetime ♾️"
    total_minutes = int(total_minutes)
    days, rem = divmod(total_minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins: parts.append(f"{mins}m")
    return " ".join(parts) if parts else "0m"

def parse_duration_and_price(token):
    """'Days:Hours:Mins:Price' -> (total_minutes_str, price_str). Also accepts
    'Lifetime:Price' (or 'Life:Price' / 'Forever:Price') for a permanent plan.
    Raises ValueError if malformed."""
    parts = [p.strip() for p in token.strip().split(':')]

    if len(parts) == 2 and parts[0].lower() in ('lifetime', 'life', 'forever'):
        pr = parts[1]
        if not pr.isdigit():
            raise ValueError("Price must be a whole number")
        return "lifetime", pr

    if len(parts) != 4:
        raise ValueError("Expected Days:Hours:Mins:Price or Lifetime:Price")
    d, h, m, pr = parts
    if not (d.isdigit() and h.isdigit() and m.isdigit() and pr.isdigit()):
        raise ValueError("All parts must be whole numbers")
    total_minutes = int(d) * 1440 + int(h) * 60 + int(m)
    if total_minutes <= 0:
        raise ValueError("Duration must be greater than 0")
    return str(total_minutes), pr

def parse_duration_only(token):
    """'Days:Hours:Mins' -> total_minutes_str. Also accepts 'lifetime' / 'life' / 'forever'
    on its own for a permanent plan. Raises ValueError if malformed."""
    stripped = token.strip()
    if stripped.lower() in ('lifetime', 'life', 'forever'):
        return "lifetime"

    parts = [p.strip() for p in stripped.split(':')]
    if len(parts) != 3:
        raise ValueError("Expected Days:Hours:Mins or 'lifetime'")
    d, h, m = parts
    if not (d.isdigit() and h.isdigit() and m.isdigit()):
        raise ValueError("All parts must be whole numbers")
    total_minutes = int(d) * 1440 + int(h) * 60 + int(m)
    if total_minutes <= 0:
        raise ValueError("Duration must be greater than 0")
    return str(total_minutes)

def format_plans_text(plans):
    if not plans:
        return "No plans set yet."
    lines = []
    for t, pr in plans.items():
        lines.append(f"• {format_label(t)} — ₹{pr}")
    return "\n".join(lines)

def _build_plan_selection(ch_data):
    """Builds the (text, markup) for the plan-picker of one channel. Tapping a plan adds
    it to the user's cart rather than paying immediately, so multiple channels can be
    bought together in one checkout."""
    markup = InlineKeyboardMarkup()
    for p_time, p_price in ch_data['plans'].items():
        label = format_label(p_time)
        markup.add(InlineKeyboardButton(f"💳 {label} - ₹{p_price}", callback_data=f"cartadd_{ch_data['channel_id']}_{p_time}"))

    markup.add(InlineKeyboardButton("⬅️ Back to Channels", callback_data="cart_browse"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    desc_part = f"\n\n📝 *About:* {ch_data['description']}" if ch_data.get('description') else ""
    text = f"Yoo \n\nAb yaha tak agaya hai to plan bhi lele dalle 😁 \n\nYou are joining: *{ch_data['name']}*.{desc_part}\n\nPlease select a subscription plan below:"
    return text, markup

def send_plan_selection(chat_id, ch_data):
    """Used for a /start deep-link entry: sends a brand new message.
    If the channel has a screenshot, it is shown as a photo with the plans as caption."""
    text, markup = _build_plan_selection(ch_data)
    screenshot = ch_data.get('screenshot_file_id')
    if screenshot:
        try:
            bot.send_photo(chat_id, screenshot, caption=text, reply_markup=markup, parse_mode="Markdown")
            return
        except Exception:
            pass  # fallback to text if photo fails
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

def edit_plan_selection(chat_id, message_id, ch_data):
    """Used when a user taps a channel button.
    If the channel has a screenshot, the current text message is replaced by a photo message
    (delete + send new photo) so the user sees the channel banner above the pricing.
    Without a screenshot the message is edited in-place as before."""
    text, markup = _build_plan_selection(ch_data)
    screenshot = ch_data.get('screenshot_file_id')
    if screenshot:
        # Delete the existing text message and send a fresh photo message
        cancel_delete(chat_id, message_id)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        try:
            msg = bot.send_photo(chat_id, screenshot, caption=text, reply_markup=markup, parse_mode="Markdown")
            schedule_delete(chat_id, msg.message_id, MENU_VANISH_SECONDS)
        except Exception:
            # Screenshot broken — fallback to plain text
            fallback = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            schedule_delete(chat_id, fallback.message_id, MENU_VANISH_SECONDS)
    else:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")

# --- SHOPPING CART (multi-channel checkout) ---

def get_cart(user_id):
    return user_carts.setdefault(user_id, [])

def cart_total(items):
    return sum(int(i['price']) for i in items)

def add_to_cart(user_id, ch_id, t):
    """Adds a channel+plan to the user's cart. Returns the channel doc, or None if the
    plan no longer exists. Adding the same channel+plan twice is a no-op (no duplicates)."""
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data or t not in ch_data.get('plans', {}):
        return None
    price = int(ch_data['plans'][t])
    items = get_cart(user_id)
    for i in items:
        if i['channel_id'] == ch_id and i['t'] == t:
            return ch_data  # already in cart
    items.append({"channel_id": ch_id, "name": ch_data['name'], "t": t, "price": price})
    return ch_data

def build_cart_summary(user_id):
    """Builds the (text, markup) for the cart summary / checkout screen."""
    items = get_cart(user_id)
    if not items:
        text = "🛒 Your cart is empty.\n\nBrowse channels below to add a subscription."
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📺 Browse Channels", callback_data="cart_browse"))
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
        return text, markup

    lines = ["🛒 *Your Cart*\n"]
    for i in items:
        lines.append(f"• {i['name']} — {format_label(i['t'])} — ₹{i['price']}")
    total = cart_total(items)
    lines.append(f"\n💰 *Total: ₹{total}*")
    text = "\n".join(lines)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ Add Another Channel", callback_data="cart_browse"))
    markup.add(InlineKeyboardButton(f"✅ Checkout & Pay ₹{total}", callback_data="cart_checkout"))
    markup.add(InlineKeyboardButton("🗑 Clear Cart", callback_data="cart_clear_ask"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    return text, markup

def build_channel_list(user_id):
    """Builds the (text, markup) for browsing all channels, with a cart button if the
    user already has items waiting. Returns (None, None) if no channels exist."""
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    markup = InlineKeyboardMarkup()
    count = 0
    for idx, ch in enumerate(cursor, start=1):
        emoji = random.choice(FACE_EMOJIS)
        markup.add(InlineKeyboardButton(f"{emoji} {idx}. {ch['name']}", callback_data=f"browse_{ch['channel_id']}"))
        count += 1

    if count == 0:
        return None, None

    items = get_cart(user_id)
    if items:
        total = cart_total(items)
        markup.add(InlineKeyboardButton(f"🛒 View Cart ({len(items)}) — ₹{total}", callback_data="cart_view"))

    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    text = ("👋 Welcome Dallo !\n\nShaana banne ki Koshish mat karna  😂😂\n\nPlease select a channel/group you'd like to join.\n\n"
            "💡 You can add multiple channels to your cart and pay for all of them at once!")
    return text, markup

def show_all_channels(chat_id, user_id):
    """Shows every channel the admin manages, so a user can pick one to join.
    Reached directly via /start or /buy; the new message is tracked for animated dismissal
    on the next command."""
    text, markup = build_channel_list(user_id)
    if text is None:
        reply = bot.send_message(chat_id, "No channels are available right now. Please check back later, or contact the admin.",
                          reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}")))
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(user_id, reply)
        return
    reply = bot.send_message(chat_id, text, reply_markup=markup)
    schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
    track_msg(user_id, reply)

def edit_all_channels(chat_id, message_id, user_id, message_obj=None):
    """Same as show_all_channels, but edits an existing button-driven message in place
    (used for 'Back to Channels' / 'Add Another Channel' taps)."""
    text, markup = build_channel_list(user_id)
    if text is None:
        edit_menu(chat_id, message_id, "No channels are available right now. Please check back later, or contact the admin.",
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}")),
                  message_obj=message_obj)
        return
    edit_menu(chat_id, message_id, text, reply_markup=markup, message_obj=message_obj)


def format_time_left(seconds):
    mins_left = int(seconds // 60)
    if mins_left < 60:
        return f"{mins_left} min"
    elif mins_left < 1440:
        return f"{mins_left // 60}h {mins_left % 60}m"
    else:
        return f"{mins_left // 1440}d {(mins_left % 1440) // 60}h"

def is_active_subscription(sub, now=None):
    """True if this user record still represents active access."""
    if sub.get('lifetime'):
        return True
    if now is None:
        now = datetime.now().timestamp()
    expiry = sub.get('expiry')
    return expiry is not None and expiry > now

def parse_rmuser_callback(data):
    """Parse rmuser_* / rmuserconfirm_* callback_data safely (handles negative channel IDs)."""
    if data.startswith('rmuserconfirm_'):
        prefix_len = len('rmuserconfirm_')
    elif data.startswith('rmuser_'):
        prefix_len = len('rmuser_')
    else:
        raise ValueError(f"Unexpected callback data: {data}")
    rest = data[prefix_len:]
    u_id_str, ch_id_str = rest.split('_', 1)
    return int(u_id_str), int(ch_id_str)

def _sleep_retry_after(e):
    """Sleep for the retry_after value Telegram sends with a 429 error (with a floor)."""
    retry_after = 2
    try:
        if hasattr(e, 'result_json') and e.result_json and 'parameters' in e.result_json:
            retry_after = e.result_json['parameters'].get('retry_after', 2)
    except Exception:
        pass
    time.sleep(max(0.5, retry_after))

def remove_user_from_chat(chat_id, user_id):
    """Ban a user from a channel/group so they lose access immediately.
    Returns (removed: bool, detail: str). Keeps the ban in place so old invite links can't be reused."""
    chat_id = int(chat_id)
    user_id = int(user_id)

    # Transient rate-limit (429) errors get retried a few times so mass-removal
    # loops (/remove all) don't silently fail halfway through.
    for attempt in range(3):
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ('creator', 'administrator'):
                return False, "That user is a chat admin/owner and cannot be removed by the bot."
            if member.status in ('left', 'kicked'):
                return True, "User was already not in the chat."
            break  # membership known, user is present -> proceed to ban
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                _sleep_retry_after(e)
                continue
            err_lower = str(e).lower()
            if any(term in err_lower for term in ['user not found', 'user_not_participant', 'participant_id_invalid', 'not a member', 'not in the chat', 'chat not found']):
                return True, "User was already not in the chat."
            return False, f"Could not check membership: {e}"
        except Exception as e:
            return False, f"Could not check membership: {e}"

    for attempt in range(3):
        try:
            bot.ban_chat_member(chat_id, user_id)
            # Deliberately do NOT unban — they are unbanned only when a new plan is approved.
            return True, "Removed from chat."
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                _sleep_retry_after(e)
                continue
            err = str(e).lower()
            if any(term in err for term in ['user not found', 'user_not_participant', 'participant_id_invalid', 'not a member', 'not in the chat']):
                return True, "User was already not in the chat."
            if 'not enough rights' in err or 'administrator rights' in err:
                return False, "Bot is not an admin in that chat, or lacks 'Ban users' permission."
            if 'chat_admin_required' in err:
                return False, "Bot must be an admin in that chat with permission to ban users."
            return False, str(e)
        except Exception as e:
            return False, str(e)
    return False, "Telegram kept rate-limiting the request (429). Try again in a moment."

def _is_chat_admin_message(message):
    """True if sender may use /remove inside a group or channel."""
    chat_id = message.chat.id
    sender_chat = getattr(message, 'sender_chat', None)

    if sender_chat is not None and sender_chat.id == chat_id:
        return True

    if message.from_user:
        if ADMIN_ID and message.from_user.id == ADMIN_ID:
            return True
        if message.from_user.id == GROUP_ANONYMOUS_BOT_ID:
            return True
        try:
            member = bot.get_chat_member(chat_id, message.from_user.id)
            if member.status in ('creator', 'administrator'):
                return True
        except Exception:
            pass

    if message.chat.type == 'channel' and sender_chat is not None:
        return True

    return False

def _get_protected_admin_ids(chat_id):
    admin_ids = {GROUP_ANONYMOUS_BOT_ID}
    if ADMIN_ID:
        admin_ids.add(ADMIN_ID)
    try:
        admin_ids.add(bot.get_me().id)
    except Exception:
        pass
    try:
        for a in bot.get_chat_administrators(chat_id):
            if a.user:
                admin_ids.add(a.user.id)
    except Exception:
        pass
    return admin_ids

def _refresh_username_for(user_ids, target_lower, limit=400):
    """Ask Telegram for each candidate's CURRENT username until one matches.

    Works because the bot has a private DM chat with every subscriber (they bought
    through the bot), so getChat(uid) succeeds even when the username stored in
    seen_users/chat_members is stale, missing, or was never recorded.
    Returns (user_id, display_username) or (None, None)."""
    checked = 0
    for uid in user_ids:
        if checked >= limit:
            break
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        checked += 1
        try:
            chat = bot.get_chat(uid)
            uname = getattr(chat, 'username', None)
            if uname and uname.lower() == target_lower:
                return int(uid), uname
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                _sleep_retry_after(e)
        except Exception:
            pass
        time.sleep(0.03)
    return None, None

def _resolve_username_to_id(raw, chat_id=None, candidate_ids=None):
    """Resolve a @username / bare username to (numeric user_id, display_username).

    Checks, in order:
      1. Telegram getChat — authoritative for users the bot has already met
         (also handles usernames that were reused after being freed)
      2. seen_users  — anyone who has ever used the bot
      3. chat_members — anyone observed inside a specific chat
      4. chat_members — anyone observed in any chat the bot tracks
      5. candidate_ids — refresh each candidate's current username from Telegram
         (subscribers always succeed here because the bot has their DM chat)
    """
    username = raw.strip().lstrip('@')
    if not username or not all(c.isalnum() or c == '_' for c in username):
        return None, None
    username_lower = username.lower()

    try:
        chat = bot.get_chat(f"@{username}")
        if chat and getattr(chat, 'type', None) == 'private' and getattr(chat, 'id', None):
            return int(chat.id), (getattr(chat, 'username', None) or username)
    except Exception:
        pass

    query = {"username": {"$regex": f"^{username}$", "$options": "i"}}

    doc = None
    try:
        doc = seen_users_col.find_one(query)
    except Exception:
        doc = None

    if not doc and chat_id is not None:
        try:
            doc = chat_members_col.find_one({"chat_id": {"$in": [int(chat_id), str(chat_id)]}, "username": query["username"]})
        except Exception:
            doc = None

    if not doc:
        try:
            doc = chat_members_col.find_one(query)
        except Exception:
            doc = None

    if doc and doc.get("user_id"):
        return int(doc["user_id"]), (doc.get("username") or username)

    if candidate_ids:
        return _refresh_username_for(candidate_ids, username_lower)

    return None, None

_MEMBER_LINE_RE_USERNAME = re.compile(r'@([A-Za-z0-9_]{5,})')
_MEMBER_LINE_RE_ID = re.compile(r'(?<!@)\b(\d{6,})\b')

def _parse_member_line(line):
    """Parse one member-list line into (user_id, username); either may be None.

    Accepts: 123456789 / @username / Name @username / Name @username (123456789)
    / 123456789 Name, etc. Lines starting with '#' or '//' are skipped as comments."""
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('//'):
        return None, None
    ids = [int(x) for x in _MEMBER_LINE_RE_ID.findall(line)]
    unames = [u for u in _MEMBER_LINE_RE_USERNAME.findall(line)]
    user_id = ids[0] if ids else None
    username = unames[0] if unames else None
    return user_id, username

def import_chat_members(chat_id, text):
    """Bulk-import a member list (usernames / user IDs) into chat_members_col.

    Returns a list of {"user_id", "username"} dicts. User IDs are stored as-is;
    usernames are stored too — resolved to a numeric ID when possible, otherwise
    kept as a username-only record (user_id=None) so importing by username never
    requires the user to have interacted with the bot."""
    imported = []
    for raw_line in text.splitlines():
        user_id, username = _parse_member_line(raw_line)
        if user_id:
            imported.append({"user_id": user_id, "username": username})
        elif username:
            resolved_id, resolved_name = _resolve_username_to_id(username)
            if resolved_id:
                imported.append({"user_id": resolved_id, "username": resolved_name or username})
            else:
                imported.append({"user_id": None, "username": username})
    return imported

def store_imported_members(chat_id, imported):
    """Persist an import result. Returns the number of members stored.
    Usernames that couldn't be resolved to a numeric ID are still stored as
    username-only records so the import never requires user interaction."""
    saved = 0
    for item in imported:
        uid = item["user_id"]
        uname = item.get("username")
        if uid:
            track_chat_member(chat_id, uid, uname, None)
            try:
                seen_users_col.update_one(
                    {"user_id": uid},
                    {"$set": {"user_id": uid, "username": uname or "", "last_seen": datetime.now()}},
                    upsert=True
                )
            except Exception:
                pass
            saved += 1
        elif uname:
            try:
                chat_members_col.update_one(
                    {"chat_id": chat_id, "user_id": None, "username": uname},
                    {"$set": {"chat_id": chat_id, "user_id": None, "username": uname,
                              "last_seen": datetime.now()}},
                    upsert=True
                )
            except Exception:
                pass
            saved += 1
    return saved

def _channel_subscriber_ids(chat_id):
    """Numeric user_ids of everyone subscribed to this channel (from users_col).
    Matches channel_id stored as int OR string for safety."""
    ids = set()
    try:
        for s in users_col.find({"channel_id": {"$in": [int(chat_id), str(chat_id)]}}):
            try:
                ids.add(int(s['user_id']))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return ids

def _chat_seen_member_ids(chat_id):
    """Numeric user_ids the bot has observed inside this chat (message + member updates)."""
    ids = set()
    try:
        for m in chat_members_col.find({"chat_id": {"$in": [int(chat_id), str(chat_id)]}}):
            try:
                ids.add(int(m['user_id']))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return ids

def _admin_channel_ids():
    """Channel ids (int) registered by the bot owner."""
    ids = set()
    try:
        for c in channels_col.find({"admin_id": ADMIN_ID}):
            try:
                ids.add(int(c['channel_id']))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return ids

def _admin_active_subscriber_ids():
    """Numeric user_ids of every ACTIVE subscriber across the admin's channels
    (used by /removeuser in the private DM)."""
    ids = set()
    now = datetime.now().timestamp()
    admin_channel_ids = _admin_channel_ids()
    try:
        for s in users_col.find({}):
            if not is_active_subscription(s, now):
                continue
            try:
                if int(s['channel_id']) in admin_channel_ids:
                    ids.add(int(s['user_id']))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return ids

def _kick_from_group(chat_id, user_id):
    """Remove user from this group/channel. Keeps ban so they cannot rejoin with old links."""
    removed, detail = remove_user_from_chat(chat_id, user_id)
    if removed:
        users_col.delete_one({"user_id": user_id, "channel_id": chat_id})
        chat_members_col.delete_one({"chat_id": chat_id, "user_id": user_id})
    return removed, detail

def setup_commands():
    """Registers the '/' command menu in Telegram — a different list for the admin vs everyone else."""
    user_commands = [
        BotCommand("start", "Browse channels & get started"),
        BotCommand("buy", "Browse channels to subscribe"),
        BotCommand("myplans", "Check your active subscriptions"),
        BotCommand("cancel", "Cancel a pending payment"),
        BotCommand("help", "Help & contact admin"),
    ]
    admin_commands = user_commands + [
        BotCommand("add", "Add a new channel"),
        BotCommand("channels", "Manage channels (edit/delete)"),
        BotCommand("removeuser", "Remove a subscriber early"),
        BotCommand("stats", "View bot stats & revenue"),
        BotCommand("broadcast", "Message everyone who has used the bot"),
        BotCommand("dbstats", "Check database storage usage"),
        BotCommand("cleanup", "Free up database space"),
        BotCommand("import", "Import member list for a group/channel"),
        BotCommand("sync", "Tracked chats & re-sync members"),
    ]
    group_admin_commands = [
        BotCommand("remove", "Remove user(s) from this group/channel"),
        BotCommand("sync", "Sync member data of this group/channel"),
        BotCommand("import", "Import member list for this group/channel"),
    ]
    bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    if ADMIN_ID:
        bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(ADMIN_ID))
    try:
        scope_cls = (
            getattr(telebot.types, 'BotCommandScopeAllChatAdministrators', None) or
            getattr(telebot.types, 'BotCommandScopeAllGroupChatAdministrators', None)
        )
        if scope_cls:
            bot.set_my_commands(group_admin_commands, scope=scope_cls())
    except Exception:
        pass

# --- START / ENTRY POINT ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    record_seen_user(message.from_user)
    text = message.text.split()

    # User entry via Deep Link (e.g. shared invite link for a specific channel)
    if len(text) > 1:
        try:
            ch_id = int(text[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})
            if ch_data:
                send_plan_selection(message.chat.id, ch_data)
                return
        except: pass

    # Admin Panel Greeting
    if user_id == ADMIN_ID:
        dismiss_previous(message.chat.id, user_id)
        reply = bot.send_message(message.chat.id,
            "✅ *Admin Panel Active!*\n\n"
            "/add - Add a new channel & prices\n"
            "/channels - Manage existing channels (edit price, duration, delete)\n"
            "/removeuser - Remove a subscriber before their plan expires\n"
            "/stats - View bot stats & revenue\n"
            "/broadcast - Message everyone who has used the bot\n"
            "/dbstats - Check database storage usage\n"
            "/cleanup - Free up database space\n"
            "/import - Bulk-import a group/channel member list\n"
            "/buy - Preview the buyer flow yourself", parse_mode="Markdown")
        schedule_delete(message.chat.id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(user_id, reply)
    else:
        # No deep link, not the admin -> let them browse all available channels
        show_all_channels(message.chat.id, user_id)

# --- USER: EXTRA COMMANDS (/buy, /myplans, /help) ---

@bot.message_handler(commands=['buy'])
def buy_handler(message):
    record_seen_user(message.from_user)
    # Dismiss previous bot reply with animation
    dismiss_previous(message.chat.id, message.from_user.id)
    show_all_channels(message.chat.id, message.from_user.id)

@bot.message_handler(commands=['myplans'])
def myplans_handler(message):
    user_id = message.from_user.id
    record_seen_user(message.from_user)
    subs = list(users_col.find({"user_id": user_id}))
    now = datetime.now().timestamp()

    lines = []
    for s in subs:
        ch = channels_col.find_one({"channel_id": s['channel_id']})
        ch_name = ch['name'] if ch else "Unknown Channel"
        if s.get('lifetime'):
            lines.append(f"• *{ch_name}* — Lifetime access ♾️")
            continue
        remaining = s['expiry'] - now
        if remaining <= 0:
            continue
        lines.append(f"• *{ch_name}* — expires in {format_time_left(remaining)}")

    if not lines:
        send_command_reply(message, "You don't have any active subscriptions right now.\n\nUse /buy to browse channels.")
        return

    send_command_reply(message, "📋 *Your Active Subscriptions:*\n\n" + "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def help_handler(message):
    record_seen_user(message.from_user)
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    send_command_reply(message,
        "ℹ️ *How this works:*\n\n"
        "1. Use /buy to see available channels\n"
        "2. Pick a channel and a plan\n"
        "3. Pay via UPI and tap 'I Have Paid'\n"
        "4. Send a screenshot of your receipt\n"
        "5. Wait for admin approval, then use your join link\n\n"
        "Use /myplans anytime to check your active subscriptions.",
        reply_markup=markup, parse_mode="Markdown")

# --- USER: BROWSE ALL CHANNELS ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('browse_'))
def browse_channel(call):
    record_seen_user(call.from_user)
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id)
    if not ch_data:
        bot.send_message(call.message.chat.id, "❌ This channel is no longer available.")
        return
    edit_plan_selection(call.message.chat.id, call.message.message_id, ch_data)

# --- ADMIN: CHANNEL LIST ---

@bot.message_handler(commands=['channels'], func=lambda m: m.from_user.id == ADMIN_ID)
def list_channels(message):
    show_channel_list(message.chat.id)

def show_channel_list(chat_id, message_id=None):
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0
    for idx, ch in enumerate(cursor, start=1):
        emoji = random.choice(FACE_EMOJIS)
        markup.add(InlineKeyboardButton(f"{emoji} {idx}. {ch['name']}", callback_data=f"manage_{ch['channel_id']}"))
        count += 1

    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))

    text = "No channels found. Click below to add one." if count == 0 else "Your Managed Channels:"
    if message_id:
        # Reached by tapping a button (e.g. Back, or after deleting a channel) -> auto-vanish
        edit_menu(chat_id, message_id, text, reply_markup=markup)
    else:
        # Direct /channels command: animate out previous, send new, schedule vanish
        dismiss_previous(chat_id, ADMIN_ID)
        reply = bot.send_message(chat_id, text, reply_markup=markup)
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(ADMIN_ID, reply)

@bot.callback_query_handler(func=lambda call: call.data == "back_channels")
def cb_back_channels(call):
    bot.answer_callback_query(call.id)
    show_channel_list(call.message.chat.id, call.message.message_id)

# --- ADMIN: ADD NEW CHANNEL ---

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel_start(message):
    # Awaiting a forward -> prompt never auto-vanishes
    msg = send_prompt(ADMIN_ID, "Please ensure the bot is an Admin in your channel, then FORWARD any message from that channel here.")
    bot.register_next_step_handler(msg, get_plans)

@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    # Reached via button, but this is now a prompt awaiting a forward -> never auto-vanish
    msg = send_prompt(ADMIN_ID, "Please FORWARD any message from your channel here.")
    bot.register_next_step_handler(msg, get_plans)

def get_plans(message):
    if message.forward_from_chat:
        ch_id = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title
        msg = send_prompt(ADMIN_ID,
            f"Channel Detected: *{ch_name}*\n\nEnter plans in format (Days:Hours:Mins:Price):\n`D:H:M:Price, D:H:M:Price` \n\n"
            "Example:\n`1:0:0:99, 0:2:30:49`\n(1 Day for ₹99, and 2 hours 30 mins for ₹49)\n\n"
            "For a permanent plan, use `lifetime:Price` instead, e.g. `lifetime:999`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name)
    else:
        send_admin_reply("❌ Error: Message was not forwarded. Use /add to try again.")

def finalize_channel(message, ch_id, ch_name):
    try:
        raw_plans = message.text.split(',')
        plans_dict = {}
        for p in raw_plans:
            total_minutes, price = parse_duration_and_price(p)
            plans_dict[total_minutes] = price
        if not plans_dict:
            raise ValueError

        channels_col.update_one({"channel_id": ch_id}, {"$set": {"name": ch_name, "plans": plans_dict, "admin_id": ADMIN_ID}}, upsert=True)
        bot_username = bot.get_me().username
        send_admin_reply(f"✅ Plans saved!\n\nInvite Link:\n`https://t.me/{bot_username}?start={ch_id}`", parse_mode="Markdown")

        # Now ask for a channel screenshot/banner
        msg = send_prompt(ADMIN_ID,
            "📸 *Optional:* Send a screenshot or banner image of your channel so users can preview it before buying.\n\n"
            "This image will be shown above the pricing when a user taps on this channel.\n\n"
            "Send a photo now, or type /skip to finish without one.", parse_mode="Markdown")
        bot.register_next_step_handler(msg, save_channel_screenshot, ch_id, True)
    except Exception:
        send_admin_reply("❌ Invalid format. Please use `Days:Hours:Mins:Price` or `lifetime:Price`, comma-separated. Use /add to retry.")

def save_channel_screenshot(message, ch_id, is_initial=False):
    """Saves (or removes) the channel screenshot after finalize or editss flow."""
    if message.text and message.text.strip().lower() in ('/skip', 'skip'):
        # Admin chose to skip — clear any existing screenshot
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"screenshot_file_id": ""}})
        if is_initial:
            msg = send_prompt(ADMIN_ID,
                "📝 *Optional:* Send a short description or 'about' caption for your channel.\n\n"
                "This text will be shown to users when browsing this channel's plans.\n\n"
                "Send the text now, or type /skip to finish without one.", parse_mode="Markdown")
            bot.register_next_step_handler(msg, save_channel_description, ch_id)
        else:
            send_admin_reply("✅ Screenshot removed.")
        return
    if not message.photo:
        # Not a photo and not a skip command — re-prompt
        msg = send_prompt(ADMIN_ID,
            "❌ That doesn’t look like a photo.\n\nPlease send an image of the channel, or type /skip to finish without one.")
        bot.register_next_step_handler(msg, save_channel_screenshot, ch_id, is_initial)
        return
    file_id = message.photo[-1].file_id  # highest resolution
    channels_col.update_one({"channel_id": ch_id}, {"$set": {"screenshot_file_id": file_id}})
    if is_initial:
        msg = send_prompt(ADMIN_ID,
            "📝 *Optional:* Send a short description or 'about' caption for your channel.\n\n"
            "This text will be shown to users when browsing this channel's plans.\n\n"
            "Send the text now, or type /skip to finish without one.", parse_mode="Markdown")
        bot.register_next_step_handler(msg, save_channel_description, ch_id)
    else:
        send_admin_reply("✅ Screenshot saved! Users will now see the channel preview above pricing.")

def save_channel_description(message, ch_id):
    if message.text and message.text.strip().lower() in ('/skip', 'skip'):
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"description": ""}})
        send_admin_reply("✅ Channel saved! No description set.")
        return

    if not message.text:
        msg = send_prompt(ADMIN_ID, "❌ Please send text for the channel description, or type /skip to finish without one.")
        bot.register_next_step_handler(msg, save_channel_description, ch_id)
        return

    description = message.text.strip()
    channels_col.update_one({"channel_id": ch_id}, {"$set": {"description": description}})
    send_admin_reply(f"✅ Channel saved with description!\n\n_{description}_", parse_mode="Markdown")

# --- ADMIN: MANAGE A SPECIFIC CHANNEL ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_'))
def manage_ch(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id)
    if not ch_data:
        send_admin_reply("❌ Channel not found (it may have been deleted.)")
        return

    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={ch_id}"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✏️ Edit Plans", callback_data=f"editplans_{ch_id}"))
    markup.add(InlineKeyboardButton("📝 Edit About/Description", callback_data=f"editdesc_{ch_id}"))
    markup.add(InlineKeyboardButton("📸 Update Screenshot", callback_data=f"editss_{ch_id}"))
    markup.add(InlineKeyboardButton("🗑 Delete Channel", callback_data=f"delch_{ch_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back to Channels", callback_data="back_channels"))

    ss_status = "✅ Screenshot set" if ch_data.get('screenshot_file_id') else "❌ No screenshot yet"
    desc_status = ch_data.get('description', 'None set')
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚙️ Settings for: *{ch_data['name']}*\n\n"
        f"🔗 Invite Link:\n`{link}`\n\n"
        f"📝 Description:\n_{desc_status}_\n\n"
        f"💰 Current Plans:\n{format_plans_text(ch_data['plans'])}\n\n"
        f"🖼 {ss_status}",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('editss_'))
def edit_screenshot_prompt(call):
    """Admin tapped 'Update Screenshot' from the manage menu."""
    ch_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    ch_name = ch_data['name'] if ch_data else str(ch_id)
    has_existing = bool(ch_data and ch_data.get('screenshot_file_id'))
    hint = " (or type /skip to *remove* the current one)" if has_existing else " (or type /skip to finish without one)"
    msg = send_prompt(ADMIN_ID,
        f"📸 Send a new screenshot / banner image for *{ch_name}*{hint}.",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_channel_screenshot, ch_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('editdesc_'))
def edit_description_prompt(call):
    """Admin tapped 'Edit About/Description' from the manage menu."""
    ch_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    ch_name = ch_data['name'] if ch_data else str(ch_id)
    has_existing = bool(ch_data and ch_data.get('description'))
    hint = " (or type /skip to *remove* the current description)" if has_existing else " (or type /skip to finish without one)"
    msg = send_prompt(ADMIN_ID,
        f"📝 Send a new description / about caption for *{ch_name}*{hint}.",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_channel_description_edit, ch_id)

def save_channel_description_edit(message, ch_id):
    if message.text and message.text.strip().lower() in ('/skip', 'skip'):
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"description": ""}})
        send_admin_reply("✅ Description removed.")
        return

    if not message.text:
        msg = send_prompt(ADMIN_ID, "❌ Please send text for the channel description, or type /skip to finish without one.")
        bot.register_next_step_handler(msg, save_channel_description_edit, ch_id)
        return

    description = message.text.strip()
    channels_col.update_one({"channel_id": ch_id}, {"$set": {"description": description}})
    send_admin_reply(f"✅ Description updated!\n\n_{description}_", parse_mode="Markdown")



@bot.callback_query_handler(func=lambda call: call.data.startswith('delch_'))
def confirm_delete_channel(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id)
    if not ch_data:
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delchconfirm_{ch_id}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data=f"manage_{ch_id}"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚠️ Are you sure you want to delete *{ch_data['name']}*? This cannot be undone.",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delchconfirm_'))
def delete_channel(call):
    ch_id = int(call.data.split('_')[1])
    channels_col.delete_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id, "Channel deleted.")
    show_channel_list(call.message.chat.id, call.message.message_id)

# --- ADMIN: EDIT PLANS ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('editplans_'))
def edit_plans_menu(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id)
    if not ch_data:
        return

    markup = InlineKeyboardMarkup()
    for t, pr in ch_data['plans'].items():
        markup.add(InlineKeyboardButton(f"{format_label(t)} - ₹{pr}", callback_data=f"editplan_{ch_id}_{t}"))
    markup.add(InlineKeyboardButton("➕ Add New Plan", callback_data=f"addplan_{ch_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"manage_{ch_id}"))

    edit_menu(call.message.chat.id, call.message.message_id,
        f"✏️ Edit Plans for *{ch_data['name']}*\n\nTap a plan below to edit its price/duration, or add a new one:",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('editplan_'))
def edit_single_plan(call):
    _, ch_id, t = call.data.split('_')
    ch_id = int(ch_id)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot.answer_callback_query(call.id)
    if not ch_data or t not in ch_data['plans']:
        return
    price = ch_data['plans'][t]

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💲 Edit Price", callback_data=f"editprice_{ch_id}_{t}"))
    markup.add(InlineKeyboardButton("⏱ Edit Duration", callback_data=f"editdur_{ch_id}_{t}"))
    markup.add(InlineKeyboardButton("🗑 Delete Plan", callback_data=f"delplan_{ch_id}_{t}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"editplans_{ch_id}"))

    edit_menu(call.message.chat.id, call.message.message_id,
        f"Plan: *{format_label(t)}* — ₹{price}\n\nWhat would you like to do?",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('editprice_'))
def edit_price_prompt(call):
    _, ch_id, t = call.data.split('_')
    bot.answer_callback_query(call.id)
    # Awaiting a text reply -> never auto-vanish, even though reached via button tap
    msg = send_prompt(ADMIN_ID, f"Send the new price for the *{format_label(t)}* plan (numbers only, e.g. `149`):", parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_new_price, int(ch_id), t)

def save_new_price(message, ch_id, t):
    new_price = message.text.strip()
    if not new_price.isdigit():
        send_admin_reply("❌ Invalid price. Please enter numbers only. Use /channels to try again.")
        return
    channels_col.update_one({"channel_id": ch_id}, {"$set": {f"plans.{t}": new_price}})
    send_admin_reply(f"✅ Price updated to ₹{new_price} for the {format_label(t)} plan.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('editdur_'))
def edit_duration_prompt(call):
    _, ch_id, t = call.data.split('_')
    bot.answer_callback_query(call.id)
    # Awaiting a text reply -> never auto-vanish, even though reached via button tap
    msg = send_prompt(ADMIN_ID, f"Send the new duration as `Days:Hours:Mins` for this plan (currently {format_label(t)}).\n\ne.g. `1:2:30` for 1 day 2 hours 30 mins, or send `lifetime` for permanent access:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_new_duration, int(ch_id), t)

def save_new_duration(message, ch_id, old_t):
    try:
        new_t = parse_duration_only(message.text)
    except:
        send_admin_reply("❌ Invalid duration. Please use `Days:Hours:Mins`, e.g. `1:2:30`, or `lifetime`. Use /channels to try again.")
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price = ch_data['plans'].get(old_t)
    if price is None:
        send_admin_reply("❌ That plan no longer exists.")
        return
    # Remove old key, add new key with the same price
    channels_col.update_one({"channel_id": ch_id}, {"$unset": {f"plans.{old_t}": ""}})
    channels_col.update_one({"channel_id": ch_id}, {"$set": {f"plans.{new_t}": price}})
    send_admin_reply(f"✅ Duration updated to {format_label(new_t)} (price stays ₹{price}).")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delplan_'))
def delete_plan(call):
    _, ch_id, t = call.data.split('_')
    ch_id = int(ch_id)
    channels_col.update_one({"channel_id": ch_id}, {"$unset": {f"plans.{t}": ""}})
    bot.answer_callback_query(call.id, "Plan deleted.")
    # Refresh the edit-plans menu
    fake_call = call
    fake_call.data = f"editplans_{ch_id}"
    edit_plans_menu(fake_call)

@bot.callback_query_handler(func=lambda call: call.data.startswith('addplan_'))
def add_plan_prompt(call):
    ch_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    # Awaiting a text reply -> never auto-vanish, even though reached via button tap
    msg = send_prompt(ADMIN_ID, "Send the new plan in format `Days:Hours:Mins:Price`.\n\nExample: `0:3:0:49` (3 hours for ₹49)\n\nFor a permanent plan, use `lifetime:Price`, e.g. `lifetime:999`", parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_new_plan, ch_id)

def save_new_plan(message, ch_id):
    try:
        total_minutes, price = parse_duration_and_price(message.text)
        channels_col.update_one({"channel_id": ch_id}, {"$set": {f"plans.{total_minutes}": price}})
        send_admin_reply(f"✅ New plan added: {format_label(total_minutes)} — ₹{price}")
    except:
        send_admin_reply("❌ Invalid format. Please use `Days:Hours:Mins:Price` or `lifetime:Price`. Use /channels to try again.")

# --- USER: SHOPPING CART ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('cartadd_'))
def cart_add_handler(call):
    record_seen_user(call.from_user)
    _, ch_id, t = call.data.split('_')
    ch_id = int(ch_id)
    ch_data = add_to_cart(call.from_user.id, ch_id, t)
    bot.answer_callback_query(call.id, "Added to cart! 🛒")
    if not ch_data:
        edit_menu(call.message.chat.id, call.message.message_id, "❌ That plan is no longer available.",
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("📺 Browse Channels", callback_data="cart_browse")),
                  message_obj=call.message)
        return
    # Option selected -> the plan-picker message instantly becomes the cart summary (no lingering message)
    text, markup = build_cart_summary(call.from_user.id)
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown", message_obj=call.message)

@bot.callback_query_handler(func=lambda call: call.data == "cart_view")
def cart_view_handler(call):
    bot.answer_callback_query(call.id)
    text, markup = build_cart_summary(call.from_user.id)
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown", message_obj=call.message)

@bot.callback_query_handler(func=lambda call: call.data == "cart_browse")
def cart_browse_handler(call):
    bot.answer_callback_query(call.id)
    edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message)

@bot.callback_query_handler(func=lambda call: call.data == "cart_clear_ask")
def cart_clear_ask_handler(call):
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Clear Cart", callback_data="cart_clear_confirm"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="cart_view"))
    edit_menu(call.message.chat.id, call.message.message_id, "⚠️ Clear your entire cart?", reply_markup=markup, message_obj=call.message)

@bot.callback_query_handler(func=lambda call: call.data == "cart_clear_confirm")
def cart_clear_confirm_handler(call):
    user_carts[call.from_user.id] = []
    bot.answer_callback_query(call.id, "Cart cleared.")
    edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message)


# --- USER: CHECKOUT & PAYMENT ---

QR_BRAND_HEX = (11, 83, 148)    # deep blue — the QR's data modules
QR_ACCENT_HEX = (5, 51, 102)    # darker navy — the three corner finder patterns
QR_BRAND_CSS = '#0B5394'
QR_ACCENT_CSS = '#053366'
QR_LOGO_TEXT = 'AB'
_BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'fonts', 'Poppins-Bold.ttf')

def _qr_logo_font(size):
    """Bold font for the 'AB' logo: prefer the bundled Poppins-Bold, else a system
    TTF, else Pillow's scalable built-in default font."""
    for path in (_BUNDLED_FONT,
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                 '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
                 '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf',
                 '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def _make_ab_logo(size):
    """Render a crisp rounded-square 'AB' logo: soft drop shadow, clean white tile
    with a thin brand ring, and bold brand-blue text — all anti-aliased for a
    sharp, modern look. Transparent background, ready for pasting."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    radius = int(size * 0.26)
    ring = max(2, int(size * 0.03))
    shadow_offset = max(2, int(size * 0.045))
    pad = shadow_offset + 2

    # 1) Soft shadow layer (subtle navy haze beneath the tile)
    shadow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad + shadow_offset, size - 1 - pad, size - 1 - pad + shadow_offset],
        radius=radius, fill=(11, 83, 148, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, size // 40)))
    img.alpha_composite(shadow)

    # 2) Clean white tile with a thin brand ring
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [pad, pad, size - 1 - pad, size - 1 - pad], radius=radius,
        fill=(255, 255, 255, 255), outline=QR_BRAND_HEX + (255,), width=ring)

    # 3) Bold 'AB' in brand blue, optically centered
    font = _qr_logo_font(int(size * 0.46))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), QR_LOGO_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
              QR_LOGO_TEXT, fill=QR_BRAND_HEX + (255,), font=font)
    return img

def _make_payment_qr(upi_id, amount):
    """Generate a styled, logo-embedded payment QR (UPI deep link) as a BytesIO
    PNG stream ready for send_photo. High error correction keeps the centered
    'AB' logo from breaking scanning; a crisp scale + two-tone finder patterns
    give it a sharp, modern look."""
    data = f"upi://pay?pa={upi_id}&am={amount}&cu=INR"
    buf = io.BytesIO()
    segno.make(data, error='h').save(
        buf, kind='png', scale=14, border=3,
        dark=QR_BRAND_CSS, light='#FFFFFF',
        finder_dark=QR_ACCENT_CSS, finder_light='#FFFFFF')
    buf.seek(0)
    img = Image.open(buf).convert('RGB')
    logo = _make_ab_logo(int(img.size[0] * 0.23))
    img.paste(logo, ((img.size[0] - logo.size[0]) // 2, (img.size[1] - logo.size[1]) // 2), logo)
    out = io.BytesIO()
    img.save(out, format='PNG')
    out.seek(0)
    return out

@bot.callback_query_handler(func=lambda call: call.data == "cart_checkout")
def cart_checkout_handler(call):
    user_id = call.from_user.id
    items = get_cart(user_id)
    bot.answer_callback_query(call.id)
    if not items:
        edit_all_channels(call.message.chat.id, call.message.message_id, user_id)
        return
    total = cart_total(items)

    # Option selected -> delete the cart-summary message immediately (don't wait for the timer)
    cancel_delete(call.message.chat.id, call.message.message_id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

    result = pending_checkouts_col.insert_one({
        "user_id": user_id, "items": items, "total": total, "created_at": datetime.now()
    })
    token = str(result.inserted_id)

    lines = [f"• {escape_markdown(i['name'])} — {format_label(i['t'])} — ₹{i['price']}" for i in items]
    caption = (
        "🧾 *Checkout Summary*\n" + "\n".join(lines) +
        f"\n\n💰 *Total: ₹{total}*\nUPI ID: `{UPI_ID}`\n\n"
        "Please complete the payment and tap 'I Have Paid', then send a screenshot to the admin."
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"coutpaid_{token}"))
    markup.add(InlineKeyboardButton("❌ Cancel Payment", callback_data=f"coutcancel_{token}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))

    # Payment QR shown for QR_SHOW_SECONDS initially; once 'I Have Paid' is tapped the timer
    # is reset to PAYMENT_VANISH_SECONDS so the user can still come back and scan if needed.
    try:
        qr_file = _make_payment_qr(UPI_ID, total)
        msg = bot.send_photo(call.message.chat.id, InputFile(qr_file, 'payment_qr.png'),
                             caption=caption, reply_markup=markup, parse_mode="Markdown")
        schedule_delete(call.message.chat.id, msg.message_id, QR_SHOW_SECONDS)
    except Exception:
        # If the QR can't be shown, clean up the checkout instead of leaving it stuck.
        try:
            pending_checkouts_col.delete_one({"_id": ObjectId(token)})
        except Exception:
            pass
        edit_menu(call.message.chat.id, call.message.message_id,
            "❌ Couldn't show the payment QR right now. Please try again.",
            reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("📺 Browse Channels", callback_data="cart_browse")),
            message_obj=call.message)
        return

@bot.callback_query_handler(func=lambda call: call.data.startswith('coutpaid_'))
def cout_paid_handler(call):
    token = call.data.split('_', 1)[1]
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id, "✅ Got it! Please send your payment screenshot now.")

    # Keep QR alive for PAYMENT_VANISH_SECONDS (30s) after 'I Have Paid' is tapped so the user
    # can still scan it if they need to go back and complete the payment.
    schedule_delete(call.message.chat.id, call.message.message_id, PAYMENT_VANISH_SECONDS)

    # Awaiting the screenshot -> never auto-vanish
    msg = send_prompt(call.message.chat.id,
        "📸 Please send a screenshot of your payment receipt now.\n\n"
        "If you tapped 'I Have Paid' by mistake, type /cancel to cancel the payment.")
    bot.register_next_step_handler(msg, receive_cart_screenshot, token)

def receive_cart_screenshot(message, token):
    # Let the user bail out of the payment flow at any point instead of being stuck
    if message.text and message.text.strip().lower() in ('/cancel', 'cancel', '/cancle', 'cancle', '/stop', 'stop', '/abort', 'abort'):
        if _cancel_pending_checkout(token, message.from_user.id):
            send_command_reply(message, "✅ Payment cancelled. Your items are back in your cart.")
        else:
            send_command_reply(message, "This checkout is no longer active.")
        show_all_channels(message.chat.id, message.from_user.id)
        return

    if not message.photo:
        msg = send_prompt(message.chat.id,
            "❌ That doesn't look like a photo. Please send a screenshot image of your payment receipt.\n\n"
            "If you tapped 'I Have Paid' by mistake, type /cancel to cancel the payment.")
        bot.register_next_step_handler(msg, receive_cart_screenshot, token)
        return

    try:
        doc = pending_checkouts_col.find_one({"_id": ObjectId(token)})
    except Exception:
        doc = None
    if not doc:
        bot.send_message(message.chat.id, "❌ This checkout has expired or was already processed. Please use /buy to start again.")
        return

    user = message.from_user
    file_id = message.photo[-1].file_id  # highest resolution version

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve All", callback_data=f"coutapp_{token}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"coutrej_{token}"))

    username_tag = f"@{user.username}" if user.username else "No username"
    lines = [f"• {escape_markdown(i['name'])} — {format_label(i['t'])} — ₹{i['price']}" for i in doc['items']]
    caption = (
        "🔔 *Payment Verification Required!*\n\n"
        f"User: {escape_markdown(user.first_name)} ({username_tag})\n"
        f"User ID: `{user.id}`\n\n"
        + "\n".join(lines) +
        f"\n\n💰 *Total: ₹{doc['total']}*"
    )
    # Not auto-vanished yet: only vanishes shortly AFTER the admin approves/rejects (see below)
    try:
        bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        # Don't leave the user hanging mid-flow — tell them delivery failed and ping the admin
        try:
            bot.send_message(message.chat.id, "❌ Couldn't deliver your receipt to the admin right now. Please try again or contact the admin.")
        except Exception:
            pass
        try:
            err_msg = bot.send_message(ADMIN_ID, f"❌ Failed to deliver a payment screenshot: {e}")
            schedule_delete(ADMIN_ID, err_msg.message_id, ADMIN_REPLY_VANISH_SECONDS)
        except Exception:
            pass
        return

    u_markup = InlineKeyboardMarkup().add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    conf_msg = bot.send_message(message.chat.id, "✅ Your receipt has been sent for verification. Please wait for Admin approval.\nApproval time : 5-10 minutes \nBe Patient", reply_markup=u_markup)
    schedule_delete(message.chat.id, conf_msg.message_id, COMMAND_VANISH_SECONDS)

    # Cart has now moved into the pending checkout — clear it so a fresh /buy starts empty
    user_carts.pop(message.from_user.id, None)

def _restore_cart_items(user_id, items):
    """Put cancelled checkout items back into the user's cart, without creating duplicates."""
    cart = get_cart(user_id)
    existing = {(i['channel_id'], i['t']) for i in cart}
    for i in items:
        key = (i['channel_id'], i['t'])
        if key not in existing:
            cart.append(i)
            existing.add(key)

def _cancel_pending_checkout(token, user_id):
    """Delete a pending checkout owned by `user_id`, restoring its items to the cart.
    Returns True if a checkout was cancelled."""
    try:
        doc = pending_checkouts_col.find_one({"_id": ObjectId(token)})
    except Exception:
        doc = None
    if not doc:
        return False
    try:
        if int(doc.get('user_id', -1)) != int(user_id):
            return False
    except (TypeError, ValueError):
        return False
    try:
        _restore_cart_items(user_id, doc.get('items', []))
    except Exception:
        pass
    try:
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
    except Exception:
        pass
    return True

@bot.callback_query_handler(func=lambda call: call.data.startswith('coutcancel_'))
def cout_cancel_handler(call):
    token = call.data.split('_', 1)[1]
    bot.answer_callback_query(call.id, "Payment cancelled.")
    _cancel_pending_checkout(token, call.from_user.id)
    # Back to browsing — the QR photo message gets replaced by the channel list
    edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message)

@bot.message_handler(commands=['cancel'])
def cancel_command_handler(message):
    """Abort any pending payment / checkout for this user and clear their cart."""
    user_id = message.from_user.id
    record_seen_user(message.from_user)
    cancelled = 0
    try:
        cancelled = pending_checkouts_col.delete_many({"user_id": user_id}).deleted_count
    except Exception:
        cancelled = 0
    user_carts.pop(user_id, None)
    if cancelled:
        send_command_reply(message, "✅ Payment cancelled. Your pending checkout has been removed. Use /buy to start again.")
    else:
        send_command_reply(message, "You don't have any pending payment right now.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('coutrej_'))
def cout_reject_handler(call):
    token = call.data.split('_', 1)[1]
    bot.answer_callback_query(call.id, "Rejected.")
    try:
        doc = pending_checkouts_col.find_one({"_id": ObjectId(token)})
    except Exception:
        doc = None
    if doc:
        try:
            rej_msg = bot.send_message(doc['user_id'], "❌ Your payment could not be verified. Please contact the admin for help.")
            schedule_delete(doc['user_id'], rej_msg.message_id, COMMAND_VANISH_SECONDS)
        except: pass
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
    # Vanishes shortly after the decision is made (not before)
    edit_caption_menu(call.message.chat.id, call.message.message_id,
        "❌ Rejected this checkout.\n(this message will vanish shortly)",
        delay=DECISION_VANISH_SECONDS)

# --- APPROVAL & EXPIRY ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('coutapp_'))
def cout_approve_handler(call):
    token = call.data.split('_', 1)[1]
    try:
        doc = pending_checkouts_col.find_one({"_id": ObjectId(token)})
    except Exception:
        doc = None
    if not doc:
        bot.answer_callback_query(call.id, "Already processed or expired.")
        return
    bot.answer_callback_query(call.id, "Approving...")

    u_id = doc['user_id']
    result_lines = []
    error_lines = []

    # Approve each channel independently so one failing channel (e.g. the bot lost admin
    # rights there) never aborts the whole checkout and leaves it stuck in between.
    for item in doc['items']:
        ch_id = item['channel_id']
        t = item['t']
        price = item['price']
        name = item['name']
        try:
            # Unban the user first in case they were previously kicked/expired and are repurchasing.
            # This is the ONLY place where a banned user gets unban — tied to a real payment approval.
            try:
                bot.unban_chat_member(ch_id, u_id, only_if_banned=True)
            except Exception:
                pass  # Not in channel / already not banned — safe to ignore

            if t == "lifetime":
                link = bot.create_chat_invite_link(ch_id, member_limit=1)  # no expiry
                users_col.update_one(
                    {"user_id": u_id, "channel_id": ch_id},
                    {"$set": {"expiry": None, "lifetime": True, "reminded_24h": True, "reminded_1h": True}},
                    upsert=True
                )
                result_lines.append(f"• {name} — Lifetime ♾️\nJoin Link: {link.invite_link}")
            else:
                mins = int(t)
                expiry_datetime = datetime.now() + timedelta(minutes=mins)
                expiry_ts = int(expiry_datetime.timestamp())
                link = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)
                users_col.update_one(
                    {"user_id": u_id, "channel_id": ch_id},
                    {"$set": {"expiry": expiry_datetime.timestamp(), "lifetime": False, "reminded_24h": False, "reminded_1h": False}},
                    upsert=True
                )
                result_lines.append(f"• {name} — {format_label(t)}\nJoin Link: {link.invite_link}")

            # Log the sale for /stats (itemized, prunable via /cleanup)
            payments_col.insert_one({
                "user_id": u_id, "channel_id": ch_id, "minutes": t,
                "amount": price, "timestamp": datetime.now()
            })
            # Also bump lifetime counters — these stay accurate even after old payment logs are pruned
            counters_col.update_one({"_id": "stats"}, {"$inc": {"total_sales": 1, "total_revenue": price}}, upsert=True)
        except Exception as e:
            error_lines.append(f"• {name}: {e}")

    # Always close out the checkout so it can never stay stuck half-processed.
    user_carts.pop(u_id, None)
    try:
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
    except Exception:
        pass

    if result_lines:
        try:
            bot.send_message(u_id,
                "🥳 *Payment Approved!*\n\n" + "\n\n".join(result_lines) +
                "\n\n⚠️ Note: Each link/access expires per its own plan (unless marked Lifetime).\n\nEnjoyyy!!!",
                parse_mode="Markdown")
            # Approval message stays for a bit longer so user can read and copy the join link
            # (no schedule_delete here — this is an important message the user needs to act on)
        except Exception:
            try:
                bot.send_message(u_id,
                    "🥳 Payment Approved!\n\n" + "\n\n".join(result_lines) +
                    "\n\nNote: Each link/access expires per its own plan (unless marked Lifetime).")
            except Exception:
                pass

    if error_lines:
        try:
            bot.send_message(u_id,
                "⚠️ Some of your purchases could not be approved:\n\n" + "\n".join(error_lines) +
                "\n\nPlease contact the admin to resolve this.")
        except Exception:
            pass
        try:
            err_msg = bot.send_message(ADMIN_ID, "⚠️ Partial approval errors:\n" + "\n".join(error_lines))
            schedule_delete(ADMIN_ID, err_msg.message_id, ADMIN_REPLY_VANISH_SECONDS)
        except Exception:
            pass

    # Vanishes shortly after the decision is made (not before)
    try:
        edit_caption_menu(call.message.chat.id, call.message.message_id,
            f"✅ Approved checkout for user {u_id} ({len(doc['items'])} channel(s)).\n(this message will vanish shortly)",
            delay=DECISION_VANISH_SECONDS)
    except Exception:
        pass

@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats_handler(message):
    total_channels = channels_col.count_documents({"admin_id": ADMIN_ID})
    now = datetime.now().timestamp()
    active_subs = users_col.count_documents({
        "$or": [
            {"lifetime": True},
            {"expiry": {"$gt": now}}
        ]
    })

    # Lifetime totals come from counters_col, not payments_col, so they stay correct
    # even after /cleanup deletes old itemized payment logs.
    counters = counters_col.find_one({"_id": "stats"}) or {}
    total_sales = counters.get("total_sales", 0)
    total_revenue = counters.get("total_revenue", 0)

    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    month_revenue = sum(p.get('amount', 0) for p in payments_col.find({"timestamp": {"$gte": month_start}}))

    text = (
        "📊 *Bot Stats*\n\n"
        f"📺 Channels: {total_channels}\n"
        f"👥 Active Subscriptions: {active_subs}\n"
        f"🧾 Total Sales: {total_sales}\n"
        f"💰 Total Revenue: ₹{total_revenue}\n"
        f"📅 This Month's Revenue: ₹{month_revenue}"
    )
    send_command_reply(message, text, parse_mode="Markdown")

# --- ADMIN: DATABASE STORAGE (dbstats / cleanup) ---

@bot.message_handler(commands=['dbstats'], func=lambda m: m.from_user.id == ADMIN_ID)
def dbstats_handler(message):
    """Sends DB storage breakdown. The /dbstats command message is deleted immediately,
    and the reply vanishes after COMMAND_VANISH_SECONDS.
    Plain text (no Markdown) is used deliberately — collection names like 'seen_users' contain
    underscores, which Telegram's legacy Markdown parser misreads as unclosed italic markers."""
    try:
        stats = db.command("dbStats")
        data_mb = stats.get('dataSize', 0) / (1024 * 1024)
        storage_mb = stats.get('storageSize', 0) / (1024 * 1024)
        index_mb = stats.get('indexSize', 0) / (1024 * 1024)

        lines = [
            "🗄 Database Storage\n",
            f"Data size: {data_mb:.2f} MB",
            f"Storage size (on disk): {storage_mb:.2f} MB",
            f"Index size: {index_mb:.2f} MB",
            "",
            "Per-collection:",
        ]
        for name in ["channels", "users", "payments", "seen_users", "counters", "pending_checkouts"]:
            try:
                cstats = db.command("collStats", name)
                csize = cstats.get('size', 0) / (1024 * 1024)
                ccount = cstats.get('count', 0)
                lines.append(f"• {name}: {ccount} docs — {csize:.2f} MB")
            except Exception:
                lines.append(f"• {name}: 0 docs — 0.00 MB")

        lines.append("\nUse /cleanup to free up space.")
        # Dismiss previous, send new reply scheduled to vanish
        dismiss_previous(message.chat.id, message.from_user.id)
        reply = bot.send_message(message.chat.id, "\n".join(lines))
        schedule_delete(message.chat.id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(message.from_user.id, reply)
    except Exception as e:
        send_command_reply(message, f"❌ Couldn't fetch DB stats: {e}")

@bot.message_handler(commands=['cleanup'], func=lambda m: m.from_user.id == ADMIN_ID)
def cleanup_handler(message):
    # Animate out previous bot reply, then show cleanup menu
    dismiss_previous(message.chat.id, message.from_user.id)
    show_cleanup_menu(message.chat.id, user_id=message.from_user.id)

def show_cleanup_menu(chat_id, message_id=None, user_id=None):
    now = datetime.now()
    payments_cutoff = now - timedelta(days=CLEANUP_PAYMENTS_DAYS)
    seenusers_cutoff = now - timedelta(days=CLEANUP_SEENUSERS_DAYS)

    old_payments_count = payments_col.count_documents({"timestamp": {"$lt": payments_cutoff}})
    old_seenusers_count = seen_users_col.count_documents({"last_seen": {"$lt": seenusers_cutoff}})

    text = (
        "🧹 *Free Up Database Space*\n\n"
        f"🧾 Old payment logs (older than {CLEANUP_PAYMENTS_DAYS} days): *{old_payments_count}* records\n"
        "   _(Your Total Sales/Revenue in /stats are unaffected — those are tracked separately and won't change.)_\n\n"
        f"👤 Inactive users (not seen in {CLEANUP_SEENUSERS_DAYS}+ days): *{old_seenusers_count}* records\n"
        "   _(Only affects who /broadcast can reach — active subscribers are never touched.)_\n\n"
        "Tap below to delete a category. This cannot be undone."
    )
    markup = InlineKeyboardMarkup()
    if old_payments_count > 0:
        markup.add(InlineKeyboardButton(f"🗑 Delete {old_payments_count} old payment logs", callback_data="cleanuppay_ask"))
    if old_seenusers_count > 0:
        markup.add(InlineKeyboardButton(f"🗑 Delete {old_seenusers_count} inactive users", callback_data="cleanupseen_ask"))
    if old_payments_count == 0 and old_seenusers_count == 0:
        markup.add(InlineKeyboardButton("✅ Nothing to clean up right now", callback_data="cleanup_refresh"))
    else:
        markup.add(InlineKeyboardButton("🔄 Refresh counts", callback_data="cleanup_refresh"))

    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        # Direct /cleanup command: send and schedule vanish after 15 s
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        if user_id:
            track_msg(user_id, reply)

@bot.callback_query_handler(func=lambda call: call.data == "cleanup_refresh")
def cb_cleanup_refresh(call):
    bot.answer_callback_query(call.id)
    show_cleanup_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "cleanuppay_ask")
def cb_cleanup_payments_ask(call):
    bot.answer_callback_query(call.id)
    cutoff = datetime.now() - timedelta(days=CLEANUP_PAYMENTS_DAYS)
    count = payments_col.count_documents({"timestamp": {"$lt": cutoff}})
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Delete Them", callback_data="cleanuppay_confirm"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="cleanup_refresh"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚠️ Delete *{count}* payment logs older than {CLEANUP_PAYMENTS_DAYS} days?\n\n"
        f"Your lifetime Total Sales/Revenue in /stats will stay accurate — only the itemized old records are removed.",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "cleanuppay_confirm")
def cb_cleanup_payments_confirm(call):
    bot.answer_callback_query(call.id, "Deleting...")
    cutoff = datetime.now() - timedelta(days=CLEANUP_PAYMENTS_DAYS)
    result = payments_col.delete_many({"timestamp": {"$lt": cutoff}})
    edit_menu(call.message.chat.id, call.message.message_id,
        f"✅ Deleted {result.deleted_count} old payment logs.\n\nUse /dbstats to see updated storage usage.",
        reply_markup=None)

@bot.callback_query_handler(func=lambda call: call.data == "cleanupseen_ask")
def cb_cleanup_seenusers_ask(call):
    bot.answer_callback_query(call.id)
    cutoff = datetime.now() - timedelta(days=CLEANUP_SEENUSERS_DAYS)
    count = seen_users_col.count_documents({"last_seen": {"$lt": cutoff}})
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Delete Them", callback_data="cleanupseen_confirm"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="cleanup_refresh"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚠️ Delete *{count}* inactive users (not seen in {CLEANUP_SEENUSERS_DAYS}+ days)?\n\n"
        f"They just won't be reachable by future /broadcast messages — if they message the bot again, they'll be re-tracked automatically.",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "cleanupseen_confirm")
def cb_cleanup_seenusers_confirm(call):
    bot.answer_callback_query(call.id, "Deleting...")
    cutoff = datetime.now() - timedelta(days=CLEANUP_SEENUSERS_DAYS)
    result = seen_users_col.delete_many({"last_seen": {"$lt": cutoff}})
    edit_menu(call.message.chat.id, call.message.message_id,
        f"✅ Deleted {result.deleted_count} inactive user records.\n\nUse /dbstats to see updated storage usage.",
        reply_markup=None)

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast_start(message):
    # Dismiss previous bot reply
    dismiss_previous(message.chat.id, message.from_user.id)
    # Awaiting the broadcast text -> prompt never auto-vanishes
    msg = send_prompt(ADMIN_ID, "Send the message you want to broadcast to everyone who has ever started/used this bot:")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(message):
    if not message.text:
        send_admin_reply("❌ Please send plain text for the broadcast (photos/stickers/etc. aren't supported yet). Use /broadcast to try again.")
        return

    user_ids = seen_users_col.distinct("user_id")
    if not user_ids:
        send_admin_reply("⚠️ No users have interacted with the bot yet — broadcast not sent to anyone.")
        return

    sent, failed = 0, 0
    errors = []
    for uid in user_ids:
        try:
            bot.send_message(uid, message.text)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(f"{uid}: {e}")

    result = f"✅ Broadcast sent to {sent} users. Failed: {failed}."
    if errors:
        # Show up to 5 concrete error reasons so you can see WHY sends failed (e.g. blocked bot)
        result += "\n\nFailure details (first 5):\n" + "\n".join(errors[:5])
    # Broadcast result is important info — keep slightly longer than regular messages
    res_msg = bot.send_message(ADMIN_ID, result)
    schedule_delete(ADMIN_ID, res_msg.message_id, 30)

@bot.message_handler(commands=['removeuser'])
def removeuser_handler(message):
    if not message.from_user:
        return
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        send_command_reply(message, f"❌ Access denied. Your User ID (`{message.from_user.id}`) is not configured as ADMIN_ID.", parse_mode="Markdown")
        return
    if message.chat.type != 'private':
        _safe_reply(message, "⚠️ /removeuser only works in a private chat with the bot.\n\nIn groups, use `/remove` to kick a member.", parse_mode="Markdown")
        return

    # Dismiss previous bot reply
    dismiss_previous(message.chat.id, message.from_user.id)

    args = message.text.split()[1:]
    if args:
        target = args[0].strip()
        ch_filter = None
        if len(args) > 1:
            try:
                ch_filter = int(args[1].strip())
            except ValueError:
                pass

        # `/removeuser all` (or `/remove all` in a DM) -> revoke every active
        # subscriber across the admin's channels.
        if target.lower() == 'all':
            now = datetime.now().timestamp()
            admin_channel_ids = _admin_channel_ids()
            subs = []
            for s in users_col.find({}):
                if not is_active_subscription(s, now):
                    continue
                try:
                    if int(s['channel_id']) in admin_channel_ids:
                        subs.append(s)
                except (TypeError, ValueError):
                    continue
            if ch_filter is not None:
                subs = [s for s in subs if int(s['channel_id']) == ch_filter]
            if not subs:
                send_admin_reply("ℹ️ No active subscribers to remove.")
                return
            count = 0
            failed = []
            for s in subs:
                removed, detail = _kick_from_group(s['channel_id'], s['user_id'])
                if removed:
                    count += 1
                else:
                    failed.append(f"{s['channel_id']}: {detail}")
                time.sleep(0.05)
            msg = f"✅ Removed *{count}* active subscriber record(s) across your channels."
            if failed:
                msg += "\n\n⚠️ Some bans failed:\n" + "\n".join(failed[:5])
            send_admin_reply(msg, parse_mode="Markdown")
            return

        target_uid = None
        if target.isdigit() or (target.startswith('-') and target[1:].isdigit()):
            target_uid = int(target)
        else:
            target_uid, _ = _resolve_username_to_id(target, candidate_ids=_admin_active_subscriber_ids())

        if target_uid:
            query = {"user_id": target_uid}
            if ch_filter:
                query["channel_id"] = ch_filter
            user_subs = list(users_col.find(query))
            if user_subs:
                count = 0
                failed = []
                for s in user_subs:
                    removed, detail = _kick_from_group(s['channel_id'], s['user_id'])
                    if removed:
                        count += 1
                    else:
                        failed.append(f"{s['channel_id']}: {detail}")
                    time.sleep(0.05)
                try:
                    rev_msg = bot.send_message(target_uid, "⚠️ Your subscription access has been revoked by the admin.")
                    schedule_delete(target_uid, rev_msg.message_id, COMMAND_VANISH_SECONDS)
                except Exception:
                    pass
                msg = f"✅ Removed subscription for user `{target_uid}` ({count} channel subscription(s) cleared)."
                if failed:
                    msg += "\n\n⚠️ Some bans failed:\n" + "\n".join(failed[:5])
                send_admin_reply(msg, parse_mode="Markdown")
                return
            else:
                send_admin_reply(f"⚠️ No subscriptions found for `{escape_markdown(target)}` ({target_uid}).", parse_mode="Markdown")
                return
        else:
            send_admin_reply(f"❌ Could not resolve `{escape_markdown(target)}`. Check the spelling (usernames are case-insensitive), try their numeric User ID, or use `/removeuser` without arguments to pick from the menu.", parse_mode="Markdown")
            return

    show_active_users(message.chat.id, user_id=message.from_user.id)


@bot.message_handler(commands=['remove'])
@bot.channel_post_handler(commands=['remove'])
def group_remove_handler(message):
    """In groups/channels: any chat admin can kick members. In private DM: same as /removeuser for bot owner."""
    if message.chat.type == 'private':
        removeuser_handler(message)
        return

    if message.chat.type not in ('group', 'supergroup', 'channel'):
        _safe_reply(message, "❌ /remove only works in groups or channels.")
        return

    chat_id = message.chat.id

    if not _is_chat_admin_message(message):
        _safe_reply(message, "❌ Only chat administrators can use /remove.")
        return

    raw_text = message.text or getattr(message, 'caption', None) or ''

    # Case 1: reply to a user's message
    if message.reply_to_message:
        target = message.reply_to_message
        if getattr(target, 'sender_chat', None) is not None:
            _safe_reply(message, "❌ Cannot remove an anonymous admin or channel identity post.")
            return
        target_user = target.from_user
        if not target_user:
            _safe_reply(message, "❌ Could not identify the user from that message.")
            return
        if target_user.is_bot or target_user.id == GROUP_ANONYMOUS_BOT_ID:
            _safe_reply(message, "❌ Cannot remove a bot or anonymous admin.")
            return

        admin_ids = _get_protected_admin_ids(chat_id)
        if target_user.id in admin_ids:
            _safe_reply(message, "❌ Cannot remove a chat administrator.")
            return

        removed, detail = _kick_from_group(chat_id, target_user.id)
        if removed:
            _safe_reply(message, f"✅ Removed {target_user.first_name or 'user'} (`{target_user.id}`) from this chat.\n_{detail}_", parse_mode="Markdown")
        else:
            _safe_reply(message, f"❌ Failed to remove user: {detail}")
        return

    args = raw_text.split()[1:] if raw_text else []
    if not args:
        _safe_reply(message,
            "💡 *Usage in this group/channel:*\n\n"
            "• Reply to someone's message with `/remove`\n"
            "• `/remove @username`\n"
            "• `/remove <user_id>`\n"
            "• `/remove all` — remove all non-admin members/subscribers of this chat\n\n"
            "_The bot must be an admin with Ban users permission._",
            parse_mode="Markdown"
        )
        return

    arg = args[0].strip()

    # Case 2: /remove all
    if arg.lower() == 'all':
        admin_ids = _get_protected_admin_ids(chat_id)
        # Source of truth: paid subscribers of THIS channel (from users_col).
        # Fallback: anyone the bot has observed in the chat (message / join events).
        subscriber_ids = _channel_subscriber_ids(chat_id)
        seen_ids = _chat_seen_member_ids(chat_id)
        target_ids = (subscriber_ids | seen_ids) - admin_ids

        removed_count = 0
        failed_count = 0
        for u_id in target_ids:
            removed, _ = _kick_from_group(chat_id, u_id)
            if removed:
                removed_count += 1
            else:
                failed_count += 1
            time.sleep(0.05)

        chat_members_col.delete_many({"chat_id": {"$in": [int(chat_id), str(chat_id)]}, "user_id": {"$nin": list(admin_ids)}})

        _safe_reply(message,
            f"🧹 *Mass removal complete*\n\n"
            f"Subscribers found: *{len(subscriber_ids)}*\n"
            f"Members seen in chat: *{len(seen_ids)}*\n"
            f"Removed: *{removed_count}*\n"
            f"Failed: *{failed_count}*\n\n"
            "_Removal is based on your subscribers (users_col) plus anyone the bot "
            "has observed in this chat. The bot must be an admin with Ban users permission._",
            parse_mode="Markdown"
        )
        return

    # Case 3: /remove <user_id>
    if arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()):
        target_id = int(arg)
        admin_ids = _get_protected_admin_ids(chat_id)
        if target_id in admin_ids:
            _safe_reply(message, "❌ Cannot remove a chat administrator.")
            return
        removed, detail = _kick_from_group(chat_id, target_id)
        if removed:
            _safe_reply(message, f"✅ Removed user `{target_id}` from this chat.\n_{detail}_", parse_mode="Markdown")
        else:
            _safe_reply(message, f"❌ Failed to remove user `{target_id}`: {detail}", parse_mode="Markdown")
        return

    # Case 4: /remove @username
    if arg.startswith('@') or (arg and not arg.isdigit() and all(c.isalnum() or c == '_' for c in arg.lstrip('@'))):
        target_id, resolved_name = _resolve_username_to_id(
            arg,
            chat_id=chat_id,
            candidate_ids=_channel_subscriber_ids(chat_id) | _chat_seen_member_ids(chat_id),
        )
        if not target_id:
            _safe_reply(message,
                f"❌ User `{arg}` not found in my records.\n\n"
                "The bot can only see users who message/join after it was added.\n"
                "Try:\n"
                "• Reply to their message with `/remove`\n"
                "• Use `/remove <user_id>`\n"
                "• Run `/sync` first to collect member data",
                parse_mode="Markdown"
            )
            return
        admin_ids = _get_protected_admin_ids(chat_id)
        if target_id in admin_ids:
            _safe_reply(message, "❌ Cannot remove a chat administrator.")
            return
        display = f"@{resolved_name}" if resolved_name else str(target_id)
        removed, detail = _kick_from_group(chat_id, target_id)
        if removed:
            _safe_reply(message, f"✅ Removed {display} (`{target_id}`) from this chat.\n_{detail}_", parse_mode="Markdown")
        else:
            _safe_reply(message, f"❌ Failed to remove {display}: {detail}", parse_mode="Markdown")
        return

    _safe_reply(message, "❌ Invalid argument. Use `/remove @username`, `/remove <user_id>`, `/remove all`, or reply to a message.", parse_mode="Markdown")


@bot.chat_member_handler(func=lambda update: True)
def on_chat_member_update(update):
    """Keep chat_members_col in sync when people join or leave."""
    try:
        chat_id = update.chat.id
        user = update.new_chat_member.user
        if not user or user.is_bot:
            return
        status = update.new_chat_member.status
        if status in ('member', 'restricted', 'administrator', 'creator'):
            track_chat_member(chat_id, user.id, getattr(user, 'username', None), getattr(user, 'first_name', None))
        elif status in ('left', 'kicked'):
            untrack_chat_member(chat_id, user.id)
    except Exception:
        pass

@bot.my_chat_member_handler(func=lambda update: True)
def on_my_chat_member_update(update):
    """Auto-collect chat data the moment the bot is added to a group/channel.

    Telegram sends the bot a my_chat_member update when it joins. At that point
    the bot records the chat and pulls the admin list (the only member data the
    Bot API lets a bot backfill). Non-admin members who were already present are
    still collected passively as they message/join afterwards."""
    try:
        chat_id = update.chat.id
        new_status = getattr(update.new_chat_member, 'status', None)
        old_status = getattr(update.old_chat_member, 'status', None)

        if new_status in ('member', 'administrator'):
            record_tracked_chat(chat_id, update.chat.type, update.chat)
            sync_chat_admins(chat_id)
            # Re-register the bot itself so it is never a removal target
            track_chat_member(chat_id, bot.get_me().id, None, "bot")
        elif new_status in ('left', 'kicked'):
            untrack_chat(chat_id)
        elif old_status in ('member', 'administrator') and new_status == 'administrator':
            # Bot promoted -> we can now read the admin list
            sync_chat_admins(chat_id)
    except Exception:
        pass

@bot.message_handler(commands=['sync'])
@bot.channel_post_handler(commands=['sync'])
def sync_handler(message):
    """/sync — in a group/channel any chat admin can trigger a member sync; in a
    private DM the bot owner can list tracked chats or re-sync one chat."""
    chat = message.chat

    # ---- Group / channel: chat admins trigger a member sync ----
    if getattr(chat, 'type', None) in ('group', 'supergroup', 'channel'):
        if not _is_chat_admin_message(message):
            _safe_reply(message, "❌ Only chat administrators can use /sync.")
            return

        chat_id = chat.id
        record_tracked_chat(chat_id, chat.type, chat)
        # Pull the admin list (the only pre-existing member data the Bot API exposes)
        sync_chat_admins(chat_id)

        # Backfill subscribers of this channel (from users_col) into chat_members so
        # /remove all & /remove @username can find them even if member events were missed.
        try:
            for s in users_col.find({"channel_id": {"$in": [int(chat_id), str(chat_id)]}}):
                uid = s.get('user_id')
                if not uid:
                    continue
                seen = None
                try:
                    seen = seen_users_col.find_one({"user_id": uid})
                except Exception:
                    pass
                uname = seen.get('username') if seen else None
                fname = seen.get('first_name') if seen else None
                track_chat_member(chat_id, uid, uname, fname)
        except Exception:
            pass

        members = list(chat_members_col.find({"chat_id": {"$in": [int(chat_id), str(chat_id)]}}))
        members.sort(key=lambda m: (str(m.get('username') or '').lower(), str(m.get('user_id') or '')))
        admin_ids = _get_protected_admin_ids(chat_id)
        try:
            bot_uid = bot.get_me().id
        except Exception:
            bot_uid = None

        user_lines = []
        for m in members:
            uid = m.get('user_id')
            uname = m.get('username')
            if uid == bot_uid:
                continue
            if not uid:
                # Username-only record (imported via /import but not yet resolved to a
                # numeric ID) — show it so imported members are visible after a sync.
                if uname:
                    user_lines.append(f"• @{escape_markdown(uname)} — ⏳ awaiting numeric ID")
                continue
            name = escape_markdown(m.get('first_name') or 'Unknown')
            tag = ""
            if uid in admin_ids:
                tag = " 👑(admin)"
            if uname:
                user_lines.append(f"• {name} (@{escape_markdown(uname)}) — `{uid}`{tag}")
            else:
                user_lines.append(f"• {name} — `{uid}`{tag}")

        if not user_lines:
            _safe_reply(message,
                "ℹ️ *Sync Complete* — but no members found yet.\n\n"
                "The bot can only see users who message or join after it was added (plus admins).\n"
                "Silent pre-existing members will appear here once they send a message or join via the bot's invite link.",
                parse_mode="Markdown",
                delay=SYNC_VANISH_SECONDS
            )
            return

        # First message carries the header plus the user list; if that would exceed
        # ~3500 chars (Telegram caps a message at ~4096), the remainder is sent as
        # "👥" continuation chunks. Names/usernames are escaped so a stray '_' in a
        # member's name can't make Telegram reject the whole message.
        header = f"✅ *Sync Complete!*\n📊 Stored *{len(user_lines)}* user(s) in the database.\n\n*Synced users are:*\n"
        messages = [header]
        for line in user_lines:
            if len(messages[-1]) + len(line) + 1 > 3500:
                messages.append("👥 ")
            messages[-1] += line + "\n"
        for text in messages:
            _safe_reply(message, text, parse_mode="Markdown", delay=SYNC_VANISH_SECONDS)
        return

    # ---- Private DM (bot owner only): tracked chats overview / targeted sync ----
    if not message.from_user or not ADMIN_ID or message.from_user.id != ADMIN_ID:
        send_command_reply(message, "❌ Access denied.")
        return

    args = message.text.split()[1:]
    if args:
        try:
            chat_id = int(args[0].strip())
        except ValueError:
            send_command_reply(message, "❌ Invalid chat id. Usage: `/sync` or `/sync <chat_id>`", parse_mode="Markdown")
            return
        sync_chat_admins(chat_id)
        count = chat_members_col.count_documents({"chat_id": chat_id})
        send_command_reply(message, f"✅ Re-synced admins for chat `{chat_id}`.\nKnown members there: {count}", parse_mode="Markdown")
        return

    chats = list(tracked_chats_col.find({"active": True}))
    if not chats:
        send_command_reply(message, "ℹ️ No tracked chats yet. Add the bot to a group/channel and it will be recorded automatically.")
        return
    lines = ["📋 *Chats the bot is in:*"]
    for c in chats:
        title = c.get('title') or c.get('type') or 'chat'
        n = chat_members_col.count_documents({"chat_id": c['chat_id']})
        lines.append(f"• {title} (`{c['chat_id']}`) — {n} members tracked")
    send_command_reply(message, "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(commands=['import'])
@bot.channel_post_handler(commands=['import'])
def import_members_handler(message):
    """Bulk-import a group/channel member list.

    In a group/channel: any chat admin can import for the CURRENT chat:
      /import                -> reply to a text message or .txt/.csv file
                                that contains the member list
      /import @u1 @u2 123456789  -> inline list

    In a private DM the bot owner can import for a specific chat:
      /import <chat_id>      -> reply to a text message or .txt/.csv file
      /import <chat_id> @u1 @u2 123456789  -> inline list

    Each list line accepts: 123456789 / @username / Name @username /
    Name @username (123456789). User IDs are stored directly; usernames are
    resolved to a numeric ID via Telegram when possible and otherwise stored
    as username-only records — no interaction from those users is required."""
    chat = message.chat
    chat_type = getattr(chat, 'type', None)
    is_chat = chat_type in ('group', 'supergroup', 'channel')

    if is_chat:
        if not _is_chat_admin_message(message):
            _safe_reply(message, "❌ Only chat administrators can use /import.")
            return
        chat_id = chat.id
        args = message.text.split()[1:]
        list_args = args
    else:
        if not message.from_user or not ADMIN_ID or message.from_user.id != ADMIN_ID:
            send_command_reply(message, "❌ Access denied.")
            return
        args = message.text.split()[1:]
        if not args:
            send_command_reply(message,
                "📥 *Import Member List*\n\n"
                "Send `/import <chat_id>` as a reply to a text message or a `.txt`/`.csv` "
                "file containing the member list (copy it from the group's member list "
                "in the Telegram app).\n\n"
                "Each line can be:\n"
                "• `123456789`\n"
                "• `@username`\n"
                "• `Name @username`\n"
                "• `Name @username (123456789)`\n\n"
                "Example: `/import -100123456789` replied to your list file.",
                parse_mode="Markdown")
            return
        try:
            chat_id = int(args[0].strip())
        except ValueError:
            send_command_reply(message, "❌ Invalid chat id. Usage: `/import <chat_id>`", parse_mode="Markdown")
            return
        list_args = args[1:]

    raw_text = ""
    reply = message.reply_to_message
    if reply:
        if reply.text:
            raw_text = reply.text
        elif reply.caption:
            raw_text = reply.caption
        elif reply.document:
            try:
                file_info = bot.get_file(reply.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                raw_text = downloaded.decode('utf-8', errors='replace')
            except Exception as e:
                _safe_reply(message, f"❌ Could not read the file: {e}")
                return
    if not raw_text:
        raw_text = " ".join(list_args).replace(',', ' ').replace(';', '\n')

    if not raw_text.strip():
        if is_chat:
            _safe_reply(message,
                "❌ No member list found. Reply to a text message / file, or paste the list "
                "inline after /import.\nUsage: `/import @user1 123456789`", parse_mode="Markdown")
        else:
            send_command_reply(message,
                "❌ No member list found. Reply to a text message / file, or paste the list "
                "inline after the chat id.\nUsage: `/import <chat_id> @user1 123456789`",
                parse_mode="Markdown")
        return

    imported = import_chat_members(chat_id, raw_text)
    saved = store_imported_members(chat_id, imported)

    total = len(imported)
    if total == 0:
        if is_chat:
            _safe_reply(message,
                "❌ No valid entries found in that input.\n\n"
                "Each line must contain a user ID or an `@username` (e.g. `@john_doe`).",
                parse_mode="Markdown")
        else:
            send_command_reply(message,
                "❌ No valid entries found in that input.\n\n"
                "Each line must contain a user ID or an `@username` (e.g. `@john_doe`).",
                parse_mode="Markdown")
        return

    summary = (f"✅ *Import Complete!*\n"
               f"📊 Stored *{saved}* entr{'y' if saved == 1 else 'ies'} for chat `{chat_id}`.\n"
               f"• Every username / user ID is now saved to the database — no user interaction needed.\n")
    if is_chat:
        _safe_reply(message, summary, parse_mode="Markdown")
    else:
        send_command_reply(message, summary, parse_mode="Markdown")

def show_active_users(chat_id, message_id=None, user_id=None):
    now = datetime.now().timestamp()
    subs = [s for s in users_col.find({}) if is_active_subscription(s, now)]
    markup = InlineKeyboardMarkup()

    header = (
        "🗑 *Remove Subscriber Access*\n\n"
        "💡 *Usage options:*\n"
        "• Tap any user button below to remove access\n"
        "• `/removeuser <user_id>` (e.g. `/removeuser 123456789`)\n"
        "• `/removeuser @username` (e.g. `/removeuser @john_doe`)\n\n"
    )

    if not subs:
        text = header + "ℹ️ *Active Subscribers:* No active subscribers right now."
        markup = None
    else:
        text = header + "👥 *Active Subscribers:* Tap a user below to remove them immediately:"
        for s in subs:
            ch = channels_col.find_one({"channel_id": s['channel_id']})
            ch_name = ch['name'] if ch else "Unknown Channel"

            seen_u = seen_users_col.find_one({"user_id": s['user_id']})
            if seen_u and seen_u.get('first_name'):
                display_name = seen_u['first_name']
                if seen_u.get('username'):
                    display_name += f" (@{seen_u['username']})"
            else:
                display_name = str(s['user_id'])

            if s.get('lifetime'):
                time_str = "Lifetime ♾️"
            else:
                remaining = s['expiry'] - now
                time_str = format_time_left(remaining)
            markup.add(InlineKeyboardButton(f"🗑 {display_name} — {ch_name} ({time_str} left)",
                                             callback_data=f"rmuser_{s['user_id']}_{s['channel_id']}"))

    if message_id:
        # Reached by tapping a button -> auto-vanish
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        # Direct /removeuser command: send and schedule vanish after 15 s
        reply = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        if user_id:
            track_msg(user_id, reply)

@bot.callback_query_handler(func=lambda call: call.data.startswith('rmuser_') and not call.data.startswith('rmuserconfirm_') and call.data != 'rmuser_back')
def confirm_remove_user(call):
    try:
        u_id, ch_id = parse_rmuser_callback(call.data)
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid selection. Use /remove again.")
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    ch_name = escape_markdown(ch_data['name']) if ch_data else "Unknown Channel"
    bot.answer_callback_query(call.id)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Remove Now", callback_data=f"rmuserconfirm_{u_id}_{ch_id}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="rmuser_back"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚠️ Remove user `{u_id}` from *{ch_name}* right now, before their plan expires?",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "rmuser_back")
def rmuser_back(call):
    bot.answer_callback_query(call.id)
    show_active_users(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('rmuserconfirm_'))
def remove_user_now(call):
    try:
        u_id, ch_id = parse_rmuser_callback(call.data)
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid selection. Use /remove again.")
        return

    removed, detail = _kick_from_group(ch_id, u_id)
    if not removed:
        bot.answer_callback_query(call.id, "Could not remove user.")
        send_admin_reply(f"❌ Failed to remove user `{u_id}` from chat `{ch_id}`.\n\n{escape_markdown(detail)}", parse_mode="Markdown")
        show_active_users(call.message.chat.id, call.message.message_id)
        return

    bot.answer_callback_query(call.id, "User removed.")

    try:
        rev_msg = bot.send_message(u_id, "⚠️ Your access has been revoked by the admin.")
        schedule_delete(u_id, rev_msg.message_id, COMMAND_VANISH_SECONDS)
    except Exception:
        pass

    send_admin_reply(f"✅ Removed user `{u_id}` from chat `{ch_id}`. {escape_markdown(detail)}", parse_mode="Markdown")
    show_active_users(call.message.chat.id, call.message.message_id)

# Automate Kicking
def kick_expired_users():
    now = datetime.now().timestamp()
    # Lifetime subscribers have expiry=None and are never kicked
    expired_users = users_col.find({"expiry": {"$lte": now}, "lifetime": {"$ne": True}})
    bot_username = bot.get_me().username

    for user in expired_users:
        try:
            removed, _ = _kick_from_group(user['channel_id'], user['user_id'])
            if not removed:
                continue

            rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}"
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Re-join / Renew", url=rejoin_url))

            exp_msg = bot.send_message(user['user_id'], "⚠️ Your subscription has expired.\n\nTo join again or renew, please click the button below:", reply_markup=markup)
            users_col.delete_one({"_id": user['_id']})
        except: pass

# Automate Expiry Reminders (24h and 1h before a plan expires)
def send_expiry_reminders():
    now = datetime.now().timestamp()
    bot_username = bot.get_me().username

    def _notify(user, remaining, flag_field):
        ch = channels_col.find_one({"channel_id": user['channel_id']})
        ch_name = ch['name'] if ch else "your channel"
        rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}"
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Renew Now", url=rejoin_url))
        try:
            bot.send_message(
                user['user_id'],
                f"⏰ *Reminder:* your subscription to *{ch_name}* expires in {format_time_left(remaining)}.\n\n"
                f"Renew now so you don't lose access!",
                reply_markup=markup, parse_mode="Markdown"
            )
        except:
            pass
        # Mark as sent even if the DM failed (e.g. user blocked the bot), so we don't retry forever
        users_col.update_one({"_id": user['_id']}, {"$set": {flag_field: True}})

    # 24-hour window: expiry is within the next 24h but more than 1h away, and not yet reminded
    # (lifetime subscribers have expiry=None and are excluded entirely)
    for user in users_col.find({
        "expiry": {"$lte": now + 24 * 3600, "$gt": now + 3600},
        "reminded_24h": {"$ne": True},
        "lifetime": {"$ne": True}
    }):
        _notify(user, user['expiry'] - now, "reminded_24h")

    # 1-hour window: expiry is within the next hour, still active, and not yet reminded
    for user in users_col.find({
        "expiry": {"$lte": now + 3600, "$gt": now},
        "reminded_1h": {"$ne": True},
        "lifetime": {"$ne": True}
    }):
        _notify(user, user['expiry'] - now, "reminded_1h")

# --- STARTUP ---
def bootstrap_counters():
    """One-time migration: if counters_col doesn't exist yet, seed it from whatever
    payment history already exists, so /stats totals don't reset to zero after this update."""
    if counters_col.count_documents({"_id": "stats"}) == 0:
        existing_sales = payments_col.count_documents({})
        existing_revenue = sum(p.get('amount', 0) for p in payments_col.find({}))
        counters_col.insert_one({"_id": "stats", "total_sales": existing_sales, "total_revenue": existing_revenue})
        print(f"Bootstrapped counters from existing history: {existing_sales} sales, ₹{existing_revenue} revenue.")

if __name__ == '__main__':
    keep_alive()
    bootstrap_counters()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users, 'interval', minutes=1)
    scheduler.add_job(send_expiry_reminders, 'interval', minutes=5)
    scheduler.add_job(sync_all_tracked_chats, 'interval', minutes=30)
    scheduler.start()
    bot.remove_webhook()
    # Small pause so that if a previous deploy's instance is still shutting down
    # (common on Render during a redeploy), it has time to release the getUpdates
    # connection before this instance starts polling — avoids a 409 Conflict.
    import time
    time.sleep(2)
    setup_commands()
    print("Bot is running...")
    # skip_pending=True: ignore any updates that piled up while no instance was polling
    # (e.g. during a redeploy), so old messages aren't reprocessed on restart.
    bot.infinity_polling(
        timeout=20,
        long_polling_timeout=10,
        skip_pending=True,
        allowed_updates=[
            'message', 'edited_message', 'callback_query',
            'channel_post', 'edited_channel_post',
            'chat_member', 'my_chat_member',
        ],
    )
