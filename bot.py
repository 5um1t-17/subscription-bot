# -*- coding: utf-8 -*-
import os
import random
import re
import time
import io
import secrets
import traceback
from html import escape
import telebot
try:
    import segno
except ImportError:
    segno = None
try:
    import qrcode
except ImportError:
    qrcode = None
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InputFile, InputMediaPhoto
from telebot.handler_backends import BaseMiddleware, CancelUpdate
from pymongo import MongoClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
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

def contact_admin_url():
    if CONTACT_USERNAME:
        return f"https://t.me/{CONTACT_USERNAME}"
    return None

# Emoji pool used to give each channel button a random face — picked fresh every time
# the channel list is rendered so the list feels lively and each entry looks distinct.
FACE_EMOJIS = [
    "😀", "😂", "🤣", "😎", "🤩", "😘", "🥳", "😜", "🤪", "😈",
    "👻", "💀", "👽", "🐱",
    "🦊", "🐷", "⚡",
    "🍌", "🍓", "🍾", "💋", "😈", "😇", "😨", "❤", "🔥", "🥰"
]

# All Telegram-supported reaction emojis (Bot API 7.x) — used to auto-react to every incoming message.
# Telegram only accepts reactions from this specific set; arbitrary Unicode will be rejected.
# NOTE: the previous version of this list mixed in ~100 emoji that are NOT on Telegram's allowed
# reaction set (hearts, weather, food, animal emoji that look similar but aren't accepted). Every
# time one of those got randomly picked, set_message_reaction failed with REACTION_INVALID — a real,
# silent cause of missed reactions this whole time. This list is Telegram's actual valid set only.
REACT_EMOJIS = [
    "👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🙏", "👌", "🕊",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀",
]

class _BotExceptionLogger(telebot.ExceptionHandler):
    """telebot's default behavior for an exception raised inside a middleware or
    handler is to log it via logger.debug(), which is invisible in production unless
    debug-level logging is explicitly enabled — the update is then silently abandoned.
    This makes those failures actually show up in the console/Render logs instead of
    disappearing with zero trace (which is exactly how a message could fail to get an
    auto-reaction with nothing showing up in the logs)."""
    def handle(self, exception):
        print(f"[bot] unhandled exception in a handler/middleware: {exception}")
        traceback.print_exc()
        return True  # mark as handled so telebot doesn't also raise it elsewhere

# num_threads defaults to 2 — that's ONE shared pool of 2 threads processing every
# incoming update (messages, callbacks, everything) for the whole bot. A burst like
# forwarding 20 messages at once bottlenecks through just those 2 threads alongside
# whatever else the bot is doing at that moment. Raised to give bursts real headroom.
bot = telebot.TeleBot(BOT_TOKEN, use_class_middlewares=True, num_threads=16,
                      exception_handler=_BotExceptionLogger())

# --- AUTO-REACT QUEUE SYSTEM ---
# One queue + one worker thread PER CHAT (spawned lazily, exits when idle) instead of a
# single bot-wide queue/worker. Telegram enforces its own reaction rate limit of roughly
# 1 reaction/sec PER CHAT — nothing in this code can send faster than that for a single
# chat. What a single global worker DID get wrong: a burst in one chat (e.g. 100 messages
# sent/forwarded back-to-back) would occupy the one worker thread and delay/starve
# reactions for every OTHER chat talking to the bot at the same time. Per-chat queues fix
# that part.
#
# REACTION_BACKLOG_CAP: if set to an int, once a chat's backlog passes that size the
# worker skips the oldest queued reactions and jumps to the most recent ones (useful if
# you'd rather the bot stay "caught up" than slowly react to a burst from minutes ago).
# Set to None (default) to react to EVERY queued message, no skipping — a 100-message
# burst will all get reacted to, just paced out at ~1/sec by Telegram's own limit, so the
# last ones will land roughly 1.5-2 minutes after the first. Note the queue is in-memory:
# if the process restarts mid-burst, whatever's still queued at that moment is lost.
REACTION_BACKLOG_CAP = None

# --- AUTO-REACT QUEUE SYSTEM ---
# One queue + one worker thread PER CHAT (spawned lazily, exits when idle) instead of a
# single bot-wide queue/worker. Telegram enforces its own reaction rate limit of roughly
# 1 reaction/sec PER CHAT — nothing in this code can send faster than that for a single
# chat. What a single global worker DID get wrong: a burst in one chat (e.g. 100 messages
# sent/forwarded back-to-back) would occupy the one worker thread and delay/starve
# reactions for every OTHER chat talking to the bot at the same time. Per-chat queues fix
# that part.
#
# REACTION_BACKLOG_CAP: if set to an int, once a chat's backlog passes that size the
# worker skips the oldest queued reactions and jumps to the most recent ones (useful if
# you'd rather the bot stay "caught up" than slowly react to a burst from minutes ago).
# Set to None (default) to react to EVERY queued message, no skipping — a 100-message
# burst will all get reacted to, just paced out at ~1/sec by Telegram's own limit, so the
# last ones will land roughly 1.5-2 minutes after the first. Note the queue is in-memory
# (backed by MongoDB, see below) so a mid-burst restart is recovered on startup.
REACTION_BACKLOG_CAP = None

# Every failure is retried, not just 429s — a bare network hiccup, or Telegram briefly
# not being ready to accept a reaction on a message that was JUST sent a moment ago
# (a real, observed transient condition), used to be treated as a permanent failure and
# silently dropped on the very first try. Now anything gets up to this many attempts
# before the bot actually gives up and logs it.
#
# Telegram doesn't just throttle reactions at a flat rate — during a burst it escalates
# the retry_after it hands back the more you exceed the limit (e.g. 2s, then 4s, then
# 8s...). A low attempt cap can get exhausted by that escalation alone during a big
# forward, even though every individual wait was honored correctly. Since each chat has
# its own isolated queue/worker, a high cap here costs nothing but patience for that one
# chat — it doesn't block or slow down anything else.
REACTION_MAX_ATTEMPTS = 25
REACTION_RETRY_DELAY = 1.5  # base delay (seconds) between retries for non-429 errors

reaction_queues = {}       # chat_id -> Queue
import threading as _threading
_reaction_queues_lock = _threading.Lock()

def _requeue_or_giveup(q, chat_id, message_id, attempt, delay, error=None):
    """Shared retry logic for both 429s and any other error. Keeps retrying (paced by
    `delay`) up to REACTION_MAX_ATTEMPTS before finally giving up and logging it clearly,
    so a real permanent failure is at least visible instead of just looking like a miss."""
    if attempt < REACTION_MAX_ATTEMPTS:
        time.sleep(delay)
        q.put((message_id, attempt + 1))
    else:
        print(f"[auto-react] giving up on chat {chat_id} message {message_id} "
              f"after {attempt} attempts" + (f": {error}" if error else ""))
        _clear_pending_reaction(chat_id, message_id)

def _reaction_worker_for_chat(chat_id):
    q = reaction_queues[chat_id]
    while True:
        try:
            message_id, attempt = q.get(timeout=5)
        except Exception:
            # Queue empty for 5s straight -> retire this chat's worker thread instead of
            # leaving idle threads around forever. The empty-check-and-remove happens
            # under the SAME lock that queue_reaction() uses for its put(), so a message
            # arriving at this exact instant can never be silently orphaned: either this
            # thread sees it and stays alive, or queue_reaction() finds the entry already
            # gone and spins up a fresh worker for it.
            with _reaction_queues_lock:
                if q.empty():
                    reaction_queues.pop(chat_id, None)
                    return
            continue

        # Optional stale-backlog trim — disabled by default (REACTION_BACKLOG_CAP=None)
        # so nothing is skipped and every queued message gets a reaction.
        if REACTION_BACKLOG_CAP is not None and q.qsize() > REACTION_BACKLOG_CAP:
            skipped = 0
            while q.qsize() > 1:
                try:
                    q.get_nowait()
                    q.task_done()
                    skipped += 1
                except Exception:
                    break
            if skipped:
                print(f"[auto-react] chat {chat_id}: skipped {skipped} stale queued reaction(s) after a burst")

        try:
            emoji = random.choice(REACT_EMOJIS)
            bot.set_message_reaction(
                chat_id,
                message_id,
                reaction=[telebot.types.ReactionTypeEmoji(emoji)],
                is_big=False
            )
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = 2
                try:
                    if hasattr(e, 'result_json') and e.result_json and 'parameters' in e.result_json:
                        retry_after = e.result_json['parameters'].get('retry_after', 2)
                except Exception:
                    pass
                _requeue_or_giveup(q, chat_id, message_id, attempt, delay=max(0.5, retry_after), error=e)
            else:
                # Previously dropped after a single try — now retried like everything
                # else, since plenty of these (e.g. a message not yet reactable a split
                # second after being sent) are transient, not permanent.
                _requeue_or_giveup(q, chat_id, message_id, attempt, delay=REACTION_RETRY_DELAY, error=e)
        except Exception as e:
            _requeue_or_giveup(q, chat_id, message_id, attempt, delay=REACTION_RETRY_DELAY, error=e)
        else:
            _clear_pending_reaction(chat_id, message_id)
        finally:
            q.task_done()

def _persist_pending_reaction(chat_id, message_id):
    """Durably record a queued-but-not-yet-sent reaction so it survives a process
    restart (Render redeploy, crash, etc.) mid-burst. Upsert makes re-queuing the
    same message (e.g. during startup replay) a safe no-op rather than a duplicate."""
    try:
        pending_reactions_col.update_one(
            {"chat_id": chat_id, "message_id": message_id},
            {"$set": {"chat_id": chat_id, "message_id": message_id, "queued_at": datetime.now()}},
            upsert=True
        )
    except Exception:
        pass

def _clear_pending_reaction(chat_id, message_id):
    """Remove the durable record once a reaction is sent (or permanently given up on)."""
    try:
        pending_reactions_col.delete_one({"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass

def resume_pending_reactions():
    """Called once at startup: re-queues any reactions that were persisted but never
    completed before the last restart, so a burst interrupted mid-way (e.g. by a
    redeploy) picks back up instead of silently losing whatever was still queued."""
    try:
        docs = list(pending_reactions_col.find({}))
    except Exception as e:
        print(f"[auto-react] could not read pending reactions on startup: {e}")
        return
    if not docs:
        return
    for doc in docs:
        try:
            queue_reaction(doc['chat_id'], doc['message_id'])
        except Exception:
            pass
    print(f"[auto-react] resumed {len(docs)} pending reaction(s) left over from before restart")

def queue_reaction(chat_id, message_id):
    """Queue a message for an auto-reaction, starting a worker thread for this chat
    if one isn't already running. Also persisted to MongoDB so it survives a restart.
    The put() happens under the same lock used to decide whether a worker retires, so
    a message can never land in an orphaned queue with nobody left to process it."""
    _persist_pending_reaction(chat_id, message_id)
    with _reaction_queues_lock:
        q = reaction_queues.get(chat_id)
        if q is None:
            q = Queue()
            reaction_queues[chat_id] = q
            Thread(target=_reaction_worker_for_chat, args=(chat_id,), daemon=True).start()
        q.put((message_id, 0))

def reaction_backlog_size(chat_id=None):
    """Total number of reactions still waiting (in-memory queues, which is what
    actually drives the delay the admin experiences). Pass a chat_id to check just
    one chat, or omit for the total across every chat."""
    with _reaction_queues_lock:
        if chat_id is not None:
            q = reaction_queues.get(chat_id)
            return q.qsize() if q else 0
        return sum(q.qsize() for q in reaction_queues.values())

def _drain_queue(q):
    """Empty a Queue object in place, returning how many items were removed.
    Note: if a worker thread has already pulled an item off and is mid-send, that
    one message will still get its reaction — this only clears what's still waiting."""
    removed = 0
    while True:
        try:
            q.get_nowait()
            q.task_done()
            removed += 1
        except Exception:
            break
    return removed

def clear_reaction_cache(chat_id=None):
    """Wipe the reaction backlog — both the in-memory queue(s) and the persisted
    MongoDB records — so the bot stops working through a large/stale backlog and
    reacts promptly to new messages again. Returns (in_memory_cleared, persisted_cleared).

    Pass a chat_id to clear just that chat's backlog, or omit to clear everything.
    Whatever is cleared here will simply never get a reaction — this trades
    completeness for the bot staying responsive/current, so use it when a backlog
    has grown large enough that reacting to it is no longer useful (e.g. after a
    100+ message forward you don't actually care about reacting to anymore)."""
    in_memory_cleared = 0
    with _reaction_queues_lock:
        if chat_id is not None:
            q = reaction_queues.get(chat_id)
            if q:
                in_memory_cleared = _drain_queue(q)
        else:
            for q in reaction_queues.values():
                in_memory_cleared += _drain_queue(q)
    try:
        if chat_id is not None:
            persisted_cleared = pending_reactions_col.delete_many({"chat_id": chat_id}).deleted_count
        else:
            persisted_cleared = pending_reactions_col.delete_many({}).deleted_count
    except Exception:
        persisted_cleared = 0
    return in_memory_cleared, persisted_cleared

class AutoReactMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message', 'channel_post']

    def pre_process(self, message, data):
        # telebot itself calls middleware.pre_process() with NO surrounding try/except —
        # if anything below raises, telebot's worker pool catches it internally, logs it
        # at DEBUG level only (invisible in normal logs), and just abandons this one
        # update. That's exactly how a message could miss its reaction with nothing
        # showing up anywhere. This top-level try/except guarantees visibility, and
        # still attempts the reaction as a fallback even if something else here fails.
        try:
            self._pre_process(message, data)
        except Exception as e:
            print(f"[auto-react] pre_process error for chat {getattr(getattr(message, 'chat', None), 'id', '?')} "
                  f"message {getattr(message, 'message_id', '?')}: {e}")
            try:
                queue_reaction(message.chat.id, message.message_id)
            except Exception:
                pass

    def _pre_process(self, message, data):
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
        queue_reaction(message.chat.id, message.message_id)

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
free_trials_col = db['free_trials']          # admin-configured free trial plans (logically separate from paid plans)
free_trial_claims_col = db['free_trial_claims']  # PERMANENT "one trial ever" claim records (never deleted)
free_trial_claims_history_col = db['free_trial_claims_history']  # archive of legacy duplicate claims moved during index migration
pending_reactions_col = db['pending_reactions']  # durable auto-react queue backing (survives process restarts)
carts_col = db['carts']                        # mirror of in-memory carts, for abandoned-cart nudges
scheduled_broadcasts_col = db['scheduled_broadcasts']  # timed /broadcast messages awaiting their slot
waitlist_col = db['waitlist']                  # users who asked to be notified when a paused channel returns
expired_subs_col = db['expired_subs']          # lapsed subscriptions, for the 3-day win-back DM
settings_col = db['settings']                  # small key/value bot settings (e.g. main menu image)
offer_bundles_col = db['offer_bundles']        # admin-created custom bundles (fixed manual price, any set of channels)
pending_offer_bundle_checkouts_col = db['pending_offer_bundle_checkouts']  # bundle purchases awaiting admin approval
fj_pending_requests_col = db['fj_pending_requests']  # Force Join pending join requests (event-based tracking)

def get_menu_image_file_id():
    """Returns the Telegram file_id of the admin-set main menu image, or None if not set."""
    doc = settings_col.find_one({"_id": "menu_image"})
    return doc.get("file_id") if doc else None

def set_menu_image_file_id(file_id):
    settings_col.update_one({"_id": "menu_image"}, {"$set": {"file_id": file_id}}, upsert=True)

def clear_menu_image():
    settings_col.delete_one({"_id": "menu_image"})

# =====================================================================
# FORCE JOIN — mandatory channel membership gate
# ---------------------------------------------------------------------
# Admin-configurable (no code changes or restarts needed): the admin can
# enable/disable the gate and point it at any Telegram channel. When enabled,
# every user is checked BEFORE any handler runs (see ForceJoinMiddleware
# below), so a user who hasn't joined the configured channel cannot reach the
# bot via /start, any other command, a menu button, or a callback.
#
# The check is FAIL-OPEN on API/config errors: an invalid channel, the bot not
# being in the channel, or a transient Telegram hiccup never locks every user
# out permanently. The admin panel surfaces such problems via the Verify button.
#
# Settings are stored in settings_col under _id "force_join" so changes made
# by the admin take effect immediately — no restart required.
# =====================================================================

FJ_SETTINGS_ID = "force_join"
FJ_MSG_TEXT = "<i>Hey <a href='tg://user?id={user_id}'>{username}</a> </i>👋\n\nJoin the channels below to unlock access 🚀"
FJ_BTN_JOIN = "Join Channel"
FJ_BTN_RETRY = "🔄 Try Again"
FJ_CB_RETRY = "fj_retry"
FJ_CB_JOIN = "fj_join"
FJ_BLOCK_COOLDOWN_SECONDS = 4   # don't re-send the block screen on every keystroke
_fj_last_block = {}             # user_id -> time.monotonic() of the last block screen

def get_force_join_settings():
    """Read the current Force Join config from settings_col (fail-safe defaults).
    Supports both legacy single-channel format and new multi-channel format."""
    doc = settings_col.find_one({"_id": FJ_SETTINGS_ID})
    if not doc:
        return {"enabled": False, "channels": [], "image_file_id": None, "auto_approve": False}
    
    # Backward compatibility: convert legacy single-channel to list format
    if "channels" not in doc:
        channels = []
        legacy_channel = doc.get("channel")
        legacy_title = doc.get("channel_title")
        if legacy_channel:
            channels.append({
                "channel": legacy_channel,
                "title": legacy_title or legacy_channel,
                "is_private": False,
                "invite_link": None,
            })
        return {
            "enabled": bool(doc.get("enabled", False)),
            "channels": channels,
            "image_file_id": doc.get("image_file_id"),
            "auto_approve": bool(doc.get("auto_approve", False)),
        }
    
    # Ensure all channels have required fields
    channels = []
    for ch in doc.get("channels", []):
        channels.append({
            "channel": ch.get("channel", ""),
            "title": ch.get("title") or ch.get("channel", ""),
            "is_private": ch.get("is_private", False),
            "invite_link": ch.get("invite_link"),
        })
    
    return {
        "enabled": bool(doc.get("enabled", False)),
        "channels": channels,
        "image_file_id": doc.get("image_file_id"),
        "auto_approve": bool(doc.get("auto_approve", False)),
    }

def save_force_join_settings(**fields):
    settings_col.update_one({"_id": FJ_SETTINGS_ID}, {"$set": dict(fields)}, upsert=True)

# --- Event-based pending join-request tracking ---

def _fj_record_pending_request(chat_id, user_id, invite_link=None):
    """Record a pending join request in MongoDB. Idempotent: upsert by unique key."""
    try:
        fj_pending_requests_col.update_one(
            {"channel_id": int(chat_id), "user_id": int(user_id)},
            {"$set": {
                "channel_id": int(chat_id),
                "user_id": int(user_id),
                "requested_at": datetime.now(),
                "invite_link": invite_link,
                "active": True,
            }},
            upsert=True
        )
        print(f"JOIN_REQUEST_RECEIVED channel={chat_id} user={user_id}")
    except Exception as e:
        print(f"[fj_track] error recording pending request for user {user_id} in chat {chat_id}: {e}")

def _fj_remove_pending_request(chat_id, user_id):
    """Remove a pending join request record (e.g. approved, rejected, expired)."""
    try:
        result = fj_pending_requests_col.delete_one({"channel_id": int(chat_id), "user_id": int(user_id)})
        print(f"[fj_track] removed pending request for user {user_id} in chat {chat_id}: deleted={result.deleted_count}")
    except Exception as e:
        print(f"[fj_track] error removing pending request for user {user_id} in chat {chat_id}: {e}")

def _fj_has_pending_request(user_id, chat_id):
    """Check MongoDB for an active pending join-request record."""
    try:
        doc = fj_pending_requests_col.find_one({"channel_id": int(chat_id), "user_id": int(user_id), "active": True})
        return doc is not None
    except Exception:
        return False

def _fj_is_force_join_channel(chat_id):
    """Check if the given chat_id is configured as a Force Join channel."""
    try:
        settings = get_force_join_settings()
        channels = settings.get('channels', [])
        for ch in channels:
            resolved = _fj_resolve_chat_id(ch.get('channel'))
            if resolved == chat_id:
                return True
        return False
    except Exception:
        return False

def _fj_resolve_chat_id(channel):
    """Accept '@username', bare 'username' or a numeric chat id string and return a
    value accepted by getChat/getChatMember (int for numeric ids). None if empty."""
    if not channel:
        return None
    raw = str(channel).strip()
    if not raw:
        return None
    if raw.lstrip('-').isdigit():
        return int(raw)
    return raw.lstrip('@')

def _fj_channel_url(channel_obj):
    """Best-effort https://t.me/... link for a channel dict, or None if it
    cannot be built. For private channels, uses the stored invite link."""
    if isinstance(channel_obj, dict):
        channel = channel_obj.get('channel', '')
        invite_link = channel_obj.get('invite_link')
        is_private = channel_obj.get('is_private', False)
    else:
        channel = str(channel_obj)
        invite_link = None
        is_private = False
    
    raw = str(channel).strip() if channel else ''
    if not raw:
        return None
    
    # For private channels, use the stored invite link
    if is_private and invite_link:
        return invite_link
    
    # For public channels or channels without stored invite link
    if raw.lstrip('-').isdigit():
        try:
            uname = getattr(bot.get_chat(int(raw)), 'username', None)
            return f"https://t.me/{uname}" if uname else None
        except Exception:
            return None
    return f"https://t.me/{raw.lstrip('@')}"

def _fj_membership_status(user_id, settings):
    """Returns ('joined', None), ('not_joined', None) or ('error', reason).
    For multiple channels, user must join ALL of them.
    Check is based on:
    1. get_chat_member() confirming membership
    2. MongoDB pending join-request record (event-based tracking)
    reason is 'config' (channel gone / bot not in it / no rights) or 'transient'
    (network, rate-limit, other API failure). Never raises — the middleware and
    the Try-Again button both rely on this to keep the bot running no matter what."""
    channels = settings.get('channels', [])
    if not channels:
        return 'error', 'config'
    
    not_joined = []
    for ch in channels:
        chat_id = _fj_resolve_chat_id(ch.get('channel'))
        if chat_id is None:
            print(f"[fj_status] skipping channel {ch.get('title', '?')} - unresolved chat_id")
            continue
        
        member_status = None
        try:
            member = bot.get_chat_member(chat_id, user_id)
            member_status = getattr(member, 'status', None)
        except telebot.apihelper.ApiTelegramException as e:
            code = getattr(e, 'error_code', None)
            desc = (getattr(e, 'description', '') or '').lower()
            if code == 400 and any(msg in desc for msg in ['user not found', 'user_not_participant', 'participant', 'not found']):
                member_status = None  # definitely not a member
            else:
                print(f"[fj_status] api error for user {user_id} in chat {chat_id}: code={code} desc={desc}")
        except Exception as e:
            print(f"[fj_status] unexpected error checking member for user {user_id} in chat {chat_id}: {e}")
        
        is_member = member_status in ('creator', 'administrator', 'member') or (
            member_status == 'restricted' and getattr(member, 'is_member', None) in (None, True)
        )
        
        # Check MongoDB pending request record
        pending = _fj_has_pending_request(user_id, chat_id)
        print(f"FORCE_JOIN_CHECK user={user_id} channel={chat_id} member={is_member} pending_request={pending}")
        
        if is_member:
            print(f"FORCE_JOIN_RESULT user={user_id} allowed=true reason=member")
            continue
        if pending:
            print(f"FORCE_JOIN_RESULT user={user_id} allowed=true reason=pending_request")
            continue
        
        # Neither member nor pending request
        not_joined.append(ch.get('title') or ch.get('channel'))
    
    if not_joined:
        print(f"FORCE_JOIN_RESULT user={user_id} allowed=false reason=not_joined")
        return 'not_joined', not_joined
    
    print(f"FORCE_JOIN_RESULT user={user_id} allowed=true reason=member")
    return 'joined', None

def user_has_force_join_pass(user_id):
    """REUSABLE membership gate. True = the user may proceed, False = they must be
    shown the Force Join screen. This is the single place the check lives: the
    middleware calls it for every update, and the Try-Again button calls it too.
    Fail-open on config/API problems so a misconfigured channel or a Telegram
    outage can never lock everyone out."""
    try:
        settings = get_force_join_settings()
    except Exception:
        return True
    if not settings.get('enabled'):
        return True
    channels = settings.get('channels', [])
    if not channels:
        return True  # enabled but no channels configured yet -> fail open
    try:
        verdict, _reason = _fj_membership_status(user_id, settings)
    except Exception:
        return True
    if verdict == 'error':
        return True  # invalid channel / missing permissions / API hiccup -> fail open
    return verdict == 'joined'

def send_force_join_block(chat_id, user_id):
    """Send (or refresh, throttled by cooldown) the 'join our channels' gate screen:
    optional banner image, the lock message, and Join/Try Again buttons.
    The message persists (vanish_delay=None) so the user can tap Try Again."""
    now = time.monotonic()
    if now - _fj_last_block.get(user_id, 0) < FJ_BLOCK_COOLDOWN_SECONDS:
        return None
    _fj_last_block[user_id] = now
    settings = get_force_join_settings()
    channels = settings.get('channels', [])
    markup = InlineKeyboardMarkup(row_width=1)
    
    # Add a join button for each channel
    for idx, ch in enumerate(channels, 1):
        url = _fj_channel_url(ch)
        if url:
            markup.add(InlineKeyboardButton(f"Join Channel {idx}", url=url))
        else:
            markup.add(InlineKeyboardButton(f"Join Channel {idx}", callback_data=FJ_CB_JOIN))
    
    markup.add(InlineKeyboardButton(FJ_BTN_RETRY, callback_data=FJ_CB_RETRY))
    
    # Build personalized message with username
    try:
        user_chat = bot.get_chat(user_id)
        username = getattr(user_chat, 'first_name', None) or getattr(user_chat, 'title', None) or str(user_id)
        text = FJ_MSG_TEXT.format(user_id=user_id, username=escape(username))
    except Exception:
        text = FJ_MSG_TEXT.format(user_id=user_id, username=str(user_id))
    
    # For private channels, add instructions about join requests
    private_channels = [ch for ch in channels if ch.get('is_private')]
    if private_channels:
        text += ""
    
    try:
        img = settings.get('image_file_id')
        if img:
            return bot.send_photo(chat_id, img, caption=text, reply_markup=markup, parse_mode="HTML", vanish_delay=None)
        return bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML", vanish_delay=None)
    except Exception as e:
        print(f"[force-join] could not send block screen to {user_id}: {e}")
        return None

class ForceJoinMiddleware(BaseMiddleware):
    """Runs before every handler and cancels any update coming from a user who
    hasn't joined the configured channel. Because it sits at the update level,
    the gate covers /start, every other command, menu buttons and callbacks —
    there is no way for an unjoined user to bypass the /start check by using
    something else. Admins, the bot itself, non-private chats and the Force Join
    buttons themselves are always allowed through."""
    def __init__(self):
        self.update_types = ['message', 'callback_query', 'edited_message']

    def pre_process(self, update, data):
        try:
            # --- callback query ---
            if hasattr(update, 'data') and getattr(update, 'message', None) is not None:
                chat = getattr(update.message, 'chat', None)
                user = getattr(update, 'from_user', None)
                if chat is None or user is None or getattr(chat, 'type', None) != 'private':
                    return None
                user_id = user.id
                if user_id == ADMIN_ID:
                    return None
                if (update.data or '').startswith('fj_'):
                    return None  # Force Join's own buttons must always be reachable
                if not user_has_force_join_pass(user_id):
                    try:
                        bot.answer_callback_query(update.id, "🔒 Please join our channel first.")
                    except Exception:
                        pass
                    send_force_join_block(chat.id, user_id)
                    return CancelUpdate()
                return None
            # --- plain message / edited message ---
            chat = getattr(update, 'chat', None)
            user = getattr(update, 'from_user', None)
            if chat is None or user is None or getattr(chat, 'type', None) != 'private':
                return None
            if user.id == ADMIN_ID:
                return None
            if getattr(bot, 'user', None) and user.id == bot.user.id:
                return None
            if not user_has_force_join_pass(user.id):
                send_force_join_block(chat.id, user.id)
                return CancelUpdate()
            return None
        except Exception as e:
            print(f"[force-join] middleware error: {e}")
            return None

    def post_process(self, message, data, exception):
        pass

bot.setup_middleware(ForceJoinMiddleware())

# ---- Force Join join-request event tracking ----

@bot.chat_join_request_handler(func=lambda req: _fj_is_force_join_channel(req.chat.id))
def handle_fj_chat_join_request(req):
    """Capture join requests for configured Force Join channels and store them in MongoDB.
    If auto_approve is enabled, automatically approve the request."""
    try:
        chat_id = req.chat.id
        user_id = req.from_user.id
        invite_link = getattr(req, 'invite_link', None)
        
        # Only track if this is a configured Force Join channel
        if not _fj_is_force_join_channel(chat_id):
            return
        
        # Record the pending request
        _fj_record_pending_request(chat_id, user_id, invite_link)
        
        # Auto-approve if enabled
        settings = get_force_join_settings()
        if settings.get('auto_approve'):
            try:
                bot.approve_chat_join_request(chat_id, user_id)
                print(f"[fj_autoapprove] Auto-approved join request for user {user_id} in chat {chat_id}")
                # Remove the pending request record since it's now approved
                _fj_remove_pending_request(chat_id, user_id)
            except Exception as e:
                print(f"[fj_autoapprove] Failed to auto-approve join request for user {user_id} in chat {chat_id}: {e}")
    except Exception as e:
        print(f"[fj_track] error handling chat_join_request: {e}")

@bot.chat_member_handler(func=lambda update: _fj_is_force_join_channel(update.chat.id))
def handle_fj_chat_member_update(update):
    """Clean up pending request records when a user's membership status changes.
    If a user becomes a member, remove their pending request record.
    If a user is kicked/leaves, also remove the pending request record."""
    try:
        chat_id = update.chat.id
        new_member = update.new_chat_member
        user_id = new_member.user.id
        status = getattr(new_member, 'status', None)
        
        # If user became a member, or was removed, clean up pending request
        if status in ('creator', 'administrator', 'member', 'restricted', 'left', 'kicked'):
            _fj_remove_pending_request(chat_id, user_id)
    except Exception as e:
        print(f"[fj_track] error handling chat_member update: {e}")

# ---- Admin: /forcejoin settings panel ----

@bot.message_handler(commands=['forcejoin'], func=lambda m: m.from_user.id == ADMIN_ID)
def forcejoin_command(message):
    send_force_join_menu(message.chat.id)

def send_force_join_menu(chat_id, message_id=None):
    s = get_force_join_settings()
    status = "🟢 Enabled" if s.get('enabled') else "🔴 Disabled"
    channels = s.get('channels', [])
    
    if channels:
        channel_lines = []
        for i, ch in enumerate(channels, 1):
            title = ch.get('title') or ch.get('channel') or f"Channel {i}"
            channel_type = "🔒 Private" if ch.get('is_private') else "🌐 Public"
            channel_lines.append(f"{i}. {escape_markdown(title)} (`{escape_markdown(ch.get('channel', ''))}`) — {channel_type}")
        channel_str = "\n".join(channel_lines)
    else:
        channel_str = "— no channels set —"
    
    text = ("╭━━━ 🔒 𝙁𝙊𝙍𝘾𝙀 𝙅𝙊𝙄𝙉 ━━━╮\n\n"
            f"Status: {status}\n"
            f"Channels:\n{channel_str}\n\n"
            "When enabled, users must join ALL channels below before they can "
            "use the bot. Covers /start, all commands, menu buttons & callbacks.\n\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "🔴 Disable" if s.get('enabled') else "🟢 Enable",
        callback_data="fj_disable" if s.get('enabled') else "fj_enable"))
    markup.add(InlineKeyboardButton(
        "🟢 Auto-Approve: ON" if s.get('auto_approve') else "🔴 Auto-Approve: OFF",
        callback_data="fj_toggle_autoapprove"))
    markup.add(InlineKeyboardButton("➕ Add Channel", callback_data="fj_setchannel"))
    if channels:
        markup.add(InlineKeyboardButton("➖ Remove Channel", callback_data="fj_removechannel_menu"))
    markup.add(InlineKeyboardButton("🖼 Set Banner Image", callback_data="fj_setbanner"))
    if s.get('image_file_id'):
        markup.add(InlineKeyboardButton("🗑 Remove Banner", callback_data="fj_rmbanner"))
    markup.add(InlineKeyboardButton("🔎 Verify Channels", callback_data="fj_verify"))
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        send_admin_reply(text, parse_mode="Markdown", reply_markup=markup)

def _fj_admin(call):
    """Gate every admin Force Join callback through the existing admin check."""
    if not _require_admin(call):
        return False
    bot.answer_callback_query(call.id)
    return True

@bot.callback_query_handler(func=lambda call: call.data == "fj_enable")
def cb_fj_enable(call):
    if not _fj_admin(call):
        return
    save_force_join_settings(enabled=True)
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "fj_disable")
def cb_fj_disable(call):
    if not _fj_admin(call):
        return
    save_force_join_settings(enabled=False)
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "fj_toggle_autoapprove")
def cb_fj_toggle_autoapprove(call):
    if not _fj_admin(call):
        return
    s = get_force_join_settings()
    new_state = not s.get('auto_approve', False)
    save_force_join_settings(auto_approve=new_state)
    bot.answer_callback_query(call.id, f"Auto-Approve {'enabled' if new_state else 'disabled'}.")
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "fj_setchannel")
def cb_fj_setchannel(call):
    if not _fj_admin(call):
        return
    msg = send_prompt(call.message.chat.id,
        "Send the channel username or numeric chat ID to add to Force Join, e.g. `@my_updates` or `-1001234567890`.\n\n"
        "Make sure the bot is a member (admin) of that channel so membership checks can run.\n\n"
        "Type /skip to cancel.",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, _fj_save_channel)

def _fj_save_channel(message):
    raw = (getattr(message, 'text', '') or '').strip()
    if raw.lower() in ('/skip', 'skip'):
        send_admin_reply("❌ Cancelled — channel not added.")
        return
    chat_id = _fj_resolve_chat_id(raw)
    if chat_id is None:
        send_admin_reply("❌ That doesn't look like a channel. Send a username (e.g. @my_updates), a numeric ID, or a private invite link.")
        return
    try:
        chat_obj = bot.get_chat(chat_id)
    except Exception:
        send_admin_reply("❌ Could not find that channel. Double-check the username/ID and that the bot can access it.")
        return
    title = getattr(chat_obj, 'title', None) or raw
    username = getattr(chat_obj, 'username', None)
    is_private = not bool(username)
    invite_link = getattr(chat_obj, 'invite_link', None)
    
    # Add to channels list (multi-channel support)
    settings = get_force_join_settings()
    channels = settings.get('channels', [])
    
    # Check if channel already exists
    for ch in channels:
        if ch.get('channel') == raw:
            send_admin_reply(f"❌ Channel *{escape_markdown(title)}* is already in the list.", parse_mode="Markdown")
            return
    
    channels.append({
        "channel": raw,
        "title": title,
        "is_private": is_private,
        "invite_link": invite_link,
    })
    save_force_join_settings(channels=channels)
    
    channel_type = "🔒 Private" if is_private else "🌐 Public"
    send_admin_reply(
        f"✅ Added *{escape_markdown(title)}* ({raw}) to Force Join.\n"
        f"Type: {channel_type}\n"
        f"Total channels: {len(channels)}\n\n"
        f"Open /forcejoin to see the updated settings.",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "fj_removechannel_menu")
def cb_fj_removechannel_menu(call):
    if not _fj_admin(call):
        return
    s = get_force_join_settings()
    channels = s.get('channels', [])
    if not channels:
        send_force_join_menu(call.message.chat.id, call.message.message_id)
        return
    
    text = "🗑 *Remove Force Join Channel*\n\nSelect a channel to remove:"
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(channels, 1):
        title = ch.get('title') or ch.get('channel') or f"Channel {i}"
        markup.add(InlineKeyboardButton(f"🗑 {title}", callback_data=f"fj_rmch_{i}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="fj_back"))
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "fj_back")
def cb_fj_back(call):
    if not _fj_admin(call):
        return
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('fj_rmch_'))
def cb_fj_remove_channel(call):
    if not _fj_admin(call):
        return
    try:
        idx = int(call.data.split('_')[2]) - 1
    except (ValueError, IndexError):
        return
    s = get_force_join_settings()
    channels = s.get('channels', [])
    if 0 <= idx < len(channels):
        removed = channels.pop(idx)
        save_force_join_settings(channels=channels)
        bot.answer_callback_query(call.id, f"Removed {removed.get('title', 'channel')}")
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "fj_setbanner")
def cb_fj_setbanner(call):
    if not _fj_admin(call):
        return
    msg = send_prompt(call.message.chat.id,
        "🖼 Send the image/banner to show on the Force Join screen. The message above "
        "the buttons will still read \"🔒 Please join our channel to use this bot.\"\n\n"
        "Type /skip to cancel.")
    bot.register_next_step_handler(msg, _fj_save_banner)

def _fj_save_banner(message):
    if (getattr(message, 'text', '') or '').strip().lower() in ('/skip', 'skip'):
        send_admin_reply("❌ Cancelled — banner unchanged.")
        return
    if not getattr(message, 'photo', None):
        msg = send_prompt(ADMIN_ID,
            "❌ That doesn't look like an image. Please send a PNG/JPG photo, or type /skip to cancel.")
        bot.register_next_step_handler(msg, _fj_save_banner)
        return
    save_force_join_settings(image_file_id=message.photo[-1].file_id)
    send_admin_reply("✅ Force Join banner saved! It will appear above the block screen.")

@bot.callback_query_handler(func=lambda call: call.data == "fj_rmbanner")
def cb_fj_rmbanner(call):
    if not _fj_admin(call):
        return
    save_force_join_settings(image_file_id=None)
    send_force_join_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "fj_verify")
def cb_fj_verify(call):
    if not _fj_admin(call):
        return
    s = get_force_join_settings()
    channels = s.get('channels', [])
    if not channels:
        send_admin_reply("❌ No channels configured yet — add one first.")
        return
    lines = []
    for i, ch in enumerate(channels, 1):
        channel = ch.get('channel', '')
        chat_id = _fj_resolve_chat_id(channel)
        channel_type = "🔒 Private" if ch.get('is_private') else "🌐 Public"
        lines.append(f"━━━ Channel {i}: {ch.get('title', channel)} ({channel_type}) ━━━")
        try:
            chat_obj = bot.get_chat(chat_id)
            title = getattr(chat_obj, 'title', None) or channel
            uname = getattr(chat_obj, 'username', None)
            lines.append(f"✅ Resolves: {title}")
            lines.append(f"🔗 https://t.me/{uname}" if uname else "🔗 No public username (invite-only).")
        except Exception:
            lines.append("❌ Could not resolve. Check username/ID and bot permissions.")
        if getattr(bot, 'user', None):
            try:
                bot_member = bot.get_chat_member(chat_id, bot.user.id)
                bstatus = getattr(bot_member, 'status', None)
                if bstatus in ('creator', 'administrator', 'member'):
                    lines.append(f"🤖 Bot is in the channel ({bstatus}) — checks will work.")
                else:
                    lines.append(f"⚠️ Bot status: {bstatus}. Add the bot so checks work.")
            except Exception:
                lines.append("⚠️ Bot is not in the channel. Add it so membership checks work.")
        
        # For private channels, check MongoDB for pending requests and event tracking status
        if ch.get('is_private'):
            try:
                # Check MongoDB for active pending requests
                pending_count = fj_pending_requests_col.count_documents({
                    "channel_id": int(chat_id),
                    "active": True
                })
                lines.append(f"📋 Pending join requests in DB: {pending_count}")
                
                # Check if chat_join_request handler is working
                lines.append("✅ Event-based join-request tracking is active.")
            except Exception as e:
                lines.append(f"⚠️ Could not check pending requests: {e}")
    send_admin_reply("\n".join(lines))

# ---- User-facing Force Join buttons ----

@bot.callback_query_handler(func=lambda call: call.data == FJ_CB_JOIN)
def cb_fj_join(call):
    """Fallback when the configured channel(s) have no public t.me link."""
    s = get_force_join_settings()
    channels = s.get('channels', [])
    if len(channels) == 1:
        channel = channels[0].get('channel', '')
        bot.answer_callback_query(call.id, f"Open Telegram and search for: {channel}", show_alert=True)
    elif len(channels) > 1:
        lines = [f"Channel {i}: {ch.get('channel', '')}" for i, ch in enumerate(channels, 1)]
        bot.answer_callback_query(call.id, "Join these channels in Telegram:\n" + "\n".join(lines), show_alert=True)
    else:
        bot.answer_callback_query(call.id, "No channels configured.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == FJ_CB_RETRY)
def cb_fj_retry(call):
    """'🔄 Try Again' — re-checks membership. Joined -> grant access and show the
    normal interface; not joined -> stay blocked with a clear alert."""
    user_id = call.from_user.id
    record_seen_user(call.from_user)
    print(f"[fj_retry] user={user_id} checking force join pass")
    passed = user_has_force_join_pass(user_id)
    print(f"[fj_retry] user={user_id} passed={passed}")
    if passed:
        bot.answer_callback_query(call.id, "✅ Welcome! You're all set.")
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        cancel_delete(chat_id, msg_id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        show_main_menu(chat_id, user_id)
    else:
        bot.answer_callback_query(call.id, "🔒 You haven't joined yet. Join the channel first.", show_alert=True)

# --- CUSTOM OFFER BUNDLES ---
# Bundles are admin-created products with a manually entered fixed price. The
# normal multi-channel cart remains available, but it never applies a discount.

# --- ABANDONED CART NUDGE ---
CART_NUDGE_MINUTES = 30        # one gentle reminder 30 min after the last cart update
CART_MAX_AGE_DAYS = 7          # drop stale persisted carts after a week

# --- WIN-BACK ---
WINBACK_AFTER_DAYS = 3         # DM a "come back" offer once a sub has been lapsed 3+ days
WINBACK_LOOKBACK_DAYS = 60     # backfill only subs that lapsed within the last 60 days

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
_bundle_nav_state = {}  # chat_id -> {screenshots, current_index, bundle_id, message_id}

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
MENU_VANISH_SECONDS = 90        # 90 seconds of inactivity on a button-driven menu
COMMAND_VANISH_SECONDS = 90     # how long a /command reply or any regular bot message stays visible
PAYMENT_VANISH_SECONDS = 120    # 2 minutes for the QR code AFTER 'I Have Paid' is clicked (so user can come back)
ADMIN_REPLY_VANISH_SECONDS = 90 # how long admin confirmation/error replies stay before auto-deleting
SYNC_VANISH_SECONDS = 90        # how long /sync's long member-list replies stay before auto-deleting
QR_SHOW_SECONDS = 120           # how long the initial QR is shown before 'I Have Paid' is clicked (2 minutes)
DEFAULT_VANISH_SECONDS = 90     # fallback for bot messages/replies that don't have a custom vanish rule
# Permanent messages (never auto-vanish, delay=None passed explicitly at their call sites):
#   - the "please wait for admin approval" message shown after a payment screenshot is sent
#     (deleted explicitly the moment admin approves/rejects, not on a timer — see
#     _clear_pending_review_messages)
#   - the join-link message sent to the user once admin approves their payment
#   - the approve/reject confirmation shown to the admin

pending_deletes = {}  # (chat_id, message_id) -> Timer
pending_review_messages = {}  # token -> {'user_chat_id':..., 'user_msg_id':..., 'admin_chat_id':..., 'admin_msg_id':...}
last_bot_msg = {}    # user_id -> (chat_id, message_id)  — for animated dismissal of old replies

def cancel_delete(chat_id, message_id):
    key = (chat_id, message_id)
    timer = pending_deletes.pop(key, None)
    if timer:
        timer.cancel()

def schedule_delete(chat_id, message_id, delay=MENU_VANISH_SECONDS):
    """(Re)schedules a message for deletion after `delay` seconds. Calling this again on the
    same message (e.g. after editing it) simply resets the countdown. Pass delay=None for a
    message that should never auto-vanish — cancels any previously scheduled deletion for it
    and schedules nothing new."""
    cancel_delete(chat_id, message_id)
    if delay is None:
        return
    key = (chat_id, message_id)
    def _delete():
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        pending_deletes.pop(key, None)
    timer = Timer(delay, _delete)
    timer.daemon = True
    pending_deletes[key] = timer
    timer.start()

_original_send_message = bot.send_message
_original_reply_to = bot.reply_to
_original_send_photo = bot.send_photo

def _vanishing_send_message(chat_id, text, *args, **kwargs):
    vanish_delay = kwargs.pop('vanish_delay', DEFAULT_VANISH_SECONDS)
    msg = _original_send_message(chat_id, text, *args, **kwargs)
    if msg and getattr(msg, 'message_id', None) and vanish_delay is not None:
        schedule_delete(chat_id, msg.message_id, vanish_delay)
    return msg


def _vanishing_reply_to(message, text, *args, **kwargs):
    vanish_delay = kwargs.pop('vanish_delay', DEFAULT_VANISH_SECONDS)
    msg = _original_reply_to(message, text, *args, **kwargs)
    if msg and getattr(msg, 'message_id', None) and vanish_delay is not None:
        try:
            schedule_delete(msg.chat.id, msg.message_id, vanish_delay)
        except Exception:
            pass
    return msg


def _vanishing_send_photo(chat_id, photo, *args, **kwargs):
    vanish_delay = kwargs.pop('vanish_delay', DEFAULT_VANISH_SECONDS)
    msg = _original_send_photo(chat_id, photo, *args, **kwargs)
    if msg and getattr(msg, 'message_id', None) and vanish_delay is not None:
        schedule_delete(chat_id, msg.message_id, vanish_delay)
    return msg


bot.send_message = _vanishing_send_message
bot.reply_to = _vanishing_reply_to
bot.send_photo = _vanishing_send_photo

def _clear_pending_review_messages(token):
    entry = pending_review_messages.pop(token, None)
    if not entry:
        return
    for chat_id, msg_id in ((entry.get('user_chat_id'), entry.get('user_msg_id')), (entry.get('admin_chat_id'), entry.get('admin_msg_id'))):
        if chat_id and msg_id:
            cancel_delete(chat_id, msg_id)
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass


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
    if not ADMIN_ID:
        return None
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
        except Exception:
            pass
        time.sleep(1)
        # Frame 2 — almost gone
        try:
            bot.edit_message_text("·", prev_chat_id, prev_msg_id)
        except Exception:
            pass
        time.sleep(1)
        # Delete
        try:
            bot.delete_message(prev_chat_id, prev_msg_id)
        except Exception:
            pass
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
    """Upserts this user into seen_users_col so /broadcast and admin commands can reach them later,
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
                "first_name": getattr(user, 'first_name', None),
                "last_name": getattr(user, 'last_name', None),
                "username": getattr(user, 'username', None),
                "last_seen": datetime.now(),
            }},
            upsert=True
        )
    except Exception:
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

def get_ordered_plan_items(ch_data):
    """Returns (plan_key, price) pairs in the admin's chosen display order (plan_order
    field). Any plan not yet in plan_order (e.g. just added) is appended at the end in
    its natural order, and stale plan_order entries for deleted plans are ignored."""
    plans = ch_data.get('plans') or {}
    order = ch_data.get('plan_order') or []
    ordered_keys = [k for k in order if k in plans]
    remaining_keys = [k for k in plans if k not in ordered_keys]
    return [(k, plans[k]) for k in ordered_keys + remaining_keys]

def format_plans_text(ch_data):
    plans = ch_data.get('plans') if isinstance(ch_data, dict) and 'plans' in ch_data else ch_data
    if not plans:
        return "No plans set yet."
    items = get_ordered_plan_items(ch_data) if isinstance(ch_data, dict) and 'plans' in ch_data else list(plans.items())
    lines = [f"• {format_label(t)} — ₹{pr}" for t, pr in items]
    return "\n".join(lines)

def _build_paused_plan_selection(ch_data, user_id=None):
    """Plan-picker UI for a paused channel: no pricing is sold, users can instead
    join a waitlist so they're notified the moment the channel is back."""
    markup = InlineKeyboardMarkup()
    on_waitlist = False
    if user_id:
        try:
            on_waitlist = waitlist_col.count_documents({"channel_id": ch_data['channel_id'], "user_id": int(user_id)}) > 0
        except Exception:
            pass
    if on_waitlist:
        markup.add(InlineKeyboardButton("✅ You're on the waitlist", callback_data=f"waitlist_{ch_data['channel_id']}"))
    else:
        markup.add(InlineKeyboardButton("🔔 Notify me when it's back", callback_data=f"waitlist_{ch_data['channel_id']}"))

    markup.add(InlineKeyboardButton("⬅️ Back to Channels", callback_data="cart_browse"))
    markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    desc_part = f"\n\n📝 <b>About:</b> <b><i>{escape(ch_data['description'])}</i></b>" if ch_data.get('description') else ""
    text = f"⏸ <b>{escape(ch_data['name'])}</b> is locked 🔒\n\nNo new sign-ups right now.\nJoin the waitlist and we'll hit you up the second it's back ⚡"
    return text, markup

def _build_plan_selection(ch_data, user_id=None):
    """Builds the (text, markup) for the plan-picker of one channel. Tapping a plan adds
    it to the user's cart rather than paying immediately, so multiple channels can be
    bought together in one checkout. Enabled admin-managed free trials for this channel
    are shown separately as one-time 'FREE' offers — they never enter the cart/payment.
    Free trials always render first, above every paid plan, regardless of plan order.
    A paused channel shows the waitlist UI instead of any plans."""
    if ch_data.get('paused'):
        return _build_paused_plan_selection(ch_data, user_id)

    markup = InlineKeyboardMarkup()

    try:
        for tr in free_trials_col.find({"channel_id": ch_data['channel_id'], "enabled": True}).sort("created_at", 1):
            markup.add(InlineKeyboardButton(f"🎁 {tr['name']} — FREE", callback_data=f"trialclaim_{tr['trial_id']}"))
    except Exception:
        pass

    for p_time, p_price in get_ordered_plan_items(ch_data):
        label = format_label(p_time)
        markup.add(InlineKeyboardButton(f"💳 {label} - ₹{p_price}", callback_data=f"cartadd_{ch_data['channel_id']}_{p_time}"))

    markup.add(InlineKeyboardButton("⬅️ Back to Channels", callback_data="cart_browse"))
    markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    desc_part = f"\n\n📝 <b>About:</b> <b><i>{escape(ch_data['description'])}</i></b>" if ch_data.get('description') else ""
    text = f"Yoo 👀\n\nAb yaha tak agaya hai to plan bhi lele dalle 😁\n\nYou're joining: <b>{escape(ch_data['name'])}</b> 👇{desc_part}\n\nPick your vibe below:"
    return text, markup

def send_plan_selection(chat_id, ch_data, user_id=None):
    """Used for a /start deep-link entry: sends a brand new message.
    If the channel has a screenshot, it is shown as a photo with the plans as caption."""
    text, markup = _build_plan_selection(ch_data, user_id)
    screenshot = ch_data.get('screenshot_file_id')
    if screenshot:
        try:
            bot.send_photo(chat_id, screenshot, caption=text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass  # fallback to text if photo fails
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

def edit_plan_selection(chat_id, message_id, ch_data, user_id=None):
    """Used when a user taps a channel button.
    If the channel has a screenshot, the current text message is replaced by a photo message
    (delete + send new photo) so the user sees the channel banner above the pricing.
    Without a screenshot the message is edited in-place as before."""
    text, markup = _build_plan_selection(ch_data, user_id)
    screenshot = ch_data.get('screenshot_file_id')
    if screenshot:
        # Delete the existing text message and send a fresh photo message
        cancel_delete(chat_id, message_id)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        try:
            msg = bot.send_photo(chat_id, screenshot, caption=text, reply_markup=markup, parse_mode="HTML")
            schedule_delete(chat_id, msg.message_id, MENU_VANISH_SECONDS)
        except Exception:
            # Screenshot broken — fallback to plain text
            fallback = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            schedule_delete(chat_id, fallback.message_id, MENU_VANISH_SECONDS)
    else:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML")

# --- SHOPPING CART (multi-channel checkout) ---

def get_cart(user_id):
    """Returns the user's in-memory cart. If it's not loaded yet (e.g. right after a
    process restart), the persisted copy in carts_col is restored so an abandoned-cart
    nudge's 'View Cart' button still shows the right items."""
    if user_id not in user_carts:
        try:
            doc = carts_col.find_one({"user_id": int(user_id)})
        except Exception:
            doc = None
        if doc and doc.get('items'):
            user_carts[user_id] = list(doc['items'])
        else:
            user_carts[user_id] = []
    return user_carts[user_id]

def _persist_cart(user_id):
    """Mirror the in-memory cart into carts_col so abandoned carts can be nudged.
    A fresh cart records its created_at; subsequent changes only bump updated_at.
    Any item change re-arms the nudge (nudged=False) so a genuinely new cart session
    can be reminded again — a cart left untouched after its nudge stays marked."""
    items = user_carts.get(user_id) or []
    if not items:
        _clear_persisted_cart(user_id)
        return
    try:
        carts_col.update_one(
            {"user_id": int(user_id)},
            {"$set": {"items": items, "updated_at": datetime.now(), "nudged": False},
             "$setOnInsert": {"created_at": datetime.now()}},
            upsert=True
        )
    except Exception:
        pass

def _clear_persisted_cart(user_id):
    try:
        carts_col.delete_one({"user_id": int(user_id)})
    except Exception:
        pass

def cart_total(items):
    return sum(int(i['price']) for i in items)

def bundle_discount(items):
    """Compatibility helper for the existing cart: custom bundles are separate
    products, so a normal cart is always charged at its itemized total."""
    return 0, cart_total(items)

def add_to_cart(user_id, ch_id, t):
    """Adds a channel+plan to the user's cart. Returns the channel doc, or None if the
    plan no longer exists or the channel is paused. Adding the same channel+plan twice
    is a no-op (no duplicates)."""
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data or ch_data.get('paused') or t not in ch_data.get('plans', {}):
        return None
    price = int(ch_data['plans'][t])
    items = get_cart(user_id)
    for i in items:
        if i['channel_id'] == ch_id and i['t'] == t:
            return ch_data  # already in cart
    items.append({"channel_id": ch_id, "name": ch_data['name'], "t": t, "price": price})
    _persist_cart(user_id)
    return ch_data

def build_cart_summary(user_id):
    """Builds the (text, markup) for the cart summary / checkout screen."""
    items = get_cart(user_id)
    if not items:
        text = "🛒 Your cart is empty.\n\nBrowse channels below and add something 🔥"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse"))
        contact_url = contact_admin_url()
        if contact_url:
            markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
        return text, markup

    lines = ["🛒 <b>Your Cart</b> 🛒\n"]
    for i in items:
        lines.append(f"• {escape(i['name'])} — {format_label(i['t'])} — ₹{i['price']}")
    total = cart_total(items)
    discount, grand_total = bundle_discount(items)
    lines.append(f"\n💰 <b>Subtotal: ₹{total}</b>")
    lines.append(f"💡 <b>Total: ₹{grand_total}</b>")
    text = "\n".join(lines)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ Add Another Channel", callback_data="cart_browse"))
    markup.add(InlineKeyboardButton(f"✅ Checkout & Pay ₹{grand_total}", callback_data="cart_checkout"))
    markup.add(InlineKeyboardButton("🗑 Clear Cart", callback_data="cart_clear_ask"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    return text, markup

RANK_BADGES = {1: "🥇", 2: "🥈", 3: "🥉"}  # top 3 by position always get a medal

def get_sorted_channels(admin_id):
    """Returns all of this admin's channels sorted by their manually-assigned 'order'
    field (lower = earlier). Channels without an order yet (e.g. just created) sort
    after ordered ones, in their natural DB order, until normalized by
    _ensure_channel_order()."""
    docs = list(channels_col.find({"admin_id": admin_id}))
    ordered = sorted((d for d in docs if 'order' in d), key=lambda d: d['order'])
    unordered = [d for d in docs if 'order' not in d]
    return ordered + unordered

def _ensure_channel_order(admin_id):
    """Normalizes every channel's 'order' field to a clean contiguous 1..N sequence
    matching their current sort position, persisting it. Called before showing/using
    the reorder menu so swaps always work against clean, gapless values."""
    channels = get_sorted_channels(admin_id)
    for i, ch in enumerate(channels, start=1):
        if ch.get('order') != i:
            channels_col.update_one({"channel_id": ch['channel_id']}, {"$set": {"order": i}})
            ch['order'] = i
    return channels

def channel_button_label(ch, position):
    """Clean minimal style: top-3 by position get a medal + name, everything else gets
    a plain running number + name. Paused channels get a ⏸ prefix."""
    name = ch['name']
    if ch.get('paused'):
        name = f"⏸ {name}"
    badge = RANK_BADGES.get(position)
    if badge:
        return f"{badge} {name}"
    return f"{position}. {name}"

def build_channel_list(user_id, back_to_menu=False):
    """Builds the (text, markup) for browsing all channels, with a cart button if the
    user already has items waiting. Returns (None, None) if no channels exist.
    When back_to_menu is True, an extra 'Home' button is appended (used when this
    view was reached from the main hub, so the user can step back out to it)."""
    channels = get_sorted_channels(ADMIN_ID)
    print(f"[build_channel_list] user={user_id} back_to_menu={back_to_menu} channel_count={len(channels)}")
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(channels, start=1):
        markup.add(InlineKeyboardButton(channel_button_label(ch, i), callback_data=f"browse_{ch['channel_id']}"))

    if not channels:
        return None, None

    markup.add(InlineKeyboardButton("🔍 Search Channels", callback_data="search_prompt"))

    items = get_cart(user_id)
    if items:
        total = cart_total(items)
        discount, grand_total = bundle_discount(items)
        if discount:
            markup.add(InlineKeyboardButton(f"🛒 View Cart ({len(items)}) — ₹{grand_total}", callback_data="cart_view"))
        else:
            markup.add(InlineKeyboardButton(f"🛒 View Cart ({len(items)}) — ₹{total}", callback_data="cart_view"))

    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    if back_to_menu:
        markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
        text = (f"👋 <b>Welcome Dallo !</b> \n\nShaana banne ki Koshish mat karna 😂😂\n\nPick a channel/group you'd like to join below 👇\n\n"
            f"💡 <b><i>Stack multiple channels in your cart and pay once — easy money 🫶🏻</i></b>")
    else:
        text = (f"👋 <b>Pick a channel</b> 👇\n\n"
                f"💡 <b><i>Stack multiple channels in your cart and pay once — easy money 🫶🏻</i></b>")
    return text, markup

def show_all_channels(chat_id, user_id, back_to_menu=False):
    """Shows every channel the admin manages, so a user can pick one to join.
    Reached directly via /buy (or the '😍 Premium Groups' button on the main menu); the
    new message is tracked for animated dismissal on the next command."""
    try:
        print(f"[show_all_channels] chat={chat_id} user={user_id} back_to_menu={back_to_menu}")
        text, markup = build_channel_list(user_id, back_to_menu=back_to_menu)
        print(f"[show_all_channels] text_is_none={text is None} channels_present={text is not None}")
        if text is None:
            contact_url = contact_admin_url()
            reply_markup = InlineKeyboardMarkup()
            if contact_url:
                reply_markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
            if back_to_menu:
                reply_markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
            reply = bot.send_message(chat_id, "Nothing here right now 😕\nCheck back later or contact the admin.",
                              reply_markup=reply_markup)
            schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
            track_msg(user_id, reply)
            return
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(user_id, reply)
    except Exception as e:
        print(f"[show_all_channels] error: {e}")
        bot.send_message(chat_id, "⚠️ Couldn't load channels. Please try again later.")

def edit_all_channels(chat_id, message_id, user_id, message_obj=None, back_to_menu=False):
    """Same as show_all_channels, but edits an existing button-driven message in place
    (used for 'Back to Channels' / 'Add Another Channel' / '😍 Premium Groups' taps)."""
    try:
        text, markup = build_channel_list(user_id, back_to_menu=back_to_menu)
        if text is None:
            contact_url = contact_admin_url()
            reply_markup = InlineKeyboardMarkup()
            if contact_url:
                reply_markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
            if back_to_menu:
                reply_markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
            edit_menu(chat_id, message_id, "Nothing here right now 😕\nCheck back later or contact the admin.",
                      reply_markup=reply_markup,
                      message_obj=message_obj)
            return
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=message_obj)
    except Exception as e:
        print(f"[edit_all_channels] error: {e}")
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        bot.send_message(chat_id, "⚠️ Couldn't load channels. Tap below to try again.",
                         reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse")))

# --- USER: MAIN MENU (shown on plain /start) ---

def build_main_menu():
    """Builds the (text, markup) for the main hub shown on a plain /start (no deep link,
    non-admin): a landing screen with Premium Groups, Offers, and Contact — rather than
    dumping the full channel list on the user immediately."""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("😍 Premium Groups", callback_data="main_channels"))
    if offer_bundles_col.count_documents({"enabled": True}) > 0:
        markup.add(InlineKeyboardButton("📦 Offers", callback_data="main_obundles"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact", url=contact_url))
    text = (f"Hey {{username}} 🔥\n\n"
            "Lessss Gooo 👇\n\n"
            "┌────────────────────────┐\n"
            "│ 😍 Premium Groups      │\n"
            "│    Browse channels     │\n"
            "└────────────────────────┘\n"
            "┌────────────────────────┐\n"
            "│ 📦 Offers              │\n"
            "│    Deals & bundles     │\n"
            "└────────────────────────┘\n"
            "┌────────────────────────┐\n"
            "│ 📞 Contact             │\n"
            "│    Help & support      │\n"
            "└────────────────────────┘")
    return text, markup

def _render_main_menu(chat_id, user_id=None, message_id=None):
    """Sends the main hub, as a photo (with caption) if the admin has set a menu image,
    otherwise as plain text. If message_id is given, the old message is removed first —
    Telegram can't edit a text message into a photo message (or vice versa) in place, so
    switching between the two always means delete + resend."""
    text, markup = build_main_menu()
    if message_id:
        cancel_delete(chat_id, message_id)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    image_file_id = get_menu_image_file_id()
    reply = None
    if image_file_id:
        try:
            reply = bot.send_photo(chat_id, image_file_id, caption=text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            reply = None  # bad/expired file_id -> fall back to text below
    if reply is None:
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
    if user_id:
        track_msg(user_id, reply)
    return reply

def show_main_menu(chat_id, user_id):
    """Sends the main hub as a fresh message (used on /start)."""
    dismiss_previous(chat_id, user_id)
    _render_main_menu(chat_id, user_id=user_id)

def edit_main_menu(chat_id, message_id, message_obj=None):
    """Rebuilds the main hub in place (used for the 'Home' back button)."""
    _render_main_menu(chat_id, message_id=message_id)

@bot.callback_query_handler(func=lambda call: call.data == "main_menu_back")
def cb_main_menu_back(call):
    bot.answer_callback_query(call.id)
    edit_main_menu(call.message.chat.id, call.message.message_id, message_obj=call.message)

# --- ADMIN: SET MAIN MENU IMAGE ---
# get_menu_image_file_id/set_menu_image_file_id/clear_menu_image already existed and
# _render_main_menu already used get_menu_image_file_id() — but nothing ever called the
# setter, so there was no actual way for the admin to add the image in the first place.
# This wires up the missing admin-facing command + button for it.

@bot.message_handler(commands=['setmenuimage'], func=lambda m: m.from_user.id == ADMIN_ID)
def set_menu_image_start(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗑 Remove current image", callback_data="menuimg_remove"))
    msg = send_prompt(ADMIN_ID,
        "🖼 Send the PNG/JPG image you want shown as the main menu banner.\n\n"
        "It will appear above the Premium Groups & Offers menu for every user.\n\n"
        "Send a photo now, or type /skip to cancel.", reply_markup=markup)
    bot.register_next_step_handler(msg, save_menu_image)

@bot.callback_query_handler(func=lambda call: call.data == "menuimg_remove")
def cb_menuimg_remove(call):
    bot.answer_callback_query(call.id, "Menu image removed.")
    clear_menu_image()
    send_admin_reply("✅ Main menu image removed. The menu will show as plain text again.")

def save_menu_image(message):
    if message.text and message.text.strip().lower() in ('/skip', 'skip'):
        send_admin_reply("❌ Cancelled — menu image unchanged.")
        return
    if not message.photo:
        msg = send_prompt(ADMIN_ID,
            "❌ That doesn't look like an image. Please send a PNG/JPG photo, or type /skip to cancel.")
        bot.register_next_step_handler(msg, save_menu_image)
        return
    file_id = message.photo[-1].file_id  # highest resolution
    set_menu_image_file_id(file_id)
    send_admin_reply("✅ Main menu image saved! It will now show above the menu for every user.")

@bot.callback_query_handler(func=lambda call: call.data == "main_channels")
def cb_main_channels(call):
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id,
                       message_obj=call.message, back_to_menu=True)

@bot.callback_query_handler(func=lambda call: call.data == "main_offers")
def cb_main_offers(call):
    bot.answer_callback_query(call.id)
    text = ("🎁 <b>Current Offers</b>\n\n"
            "Choose a custom offer to get several channels at the exact fixed price "
            "set by the admin, or browse individual channels.")
    markup = InlineKeyboardMarkup()
    if offer_bundles_col.count_documents({"enabled": True}) > 0:
        markup.add(InlineKeyboardButton("🎉 View Offers", callback_data="main_obundles"))
    markup.add(InlineKeyboardButton("😍 Premium Groups", callback_data="main_channels"))
    markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=call.message)

# --- CUSTOM OFFER BUNDLES ---
# A bundle stores a snapshot of its selected channel ids, but channel names are
# resolved again when rendered and approved. This keeps renamed channels readable
# and prevents deleted/unmanaged channels from being granted accidentally.
bundle_selection_state = {}

def _safe_bundle_id(raw):
    raw = (raw or '').strip()
    return raw if re.match(r'^[A-Za-z0-9]+$', raw) else None

def _bundle_doc(raw):
    bundle_id = _safe_bundle_id(raw)
    return offer_bundles_col.find_one({"bundle_id": bundle_id, "admin_id": ADMIN_ID}) if bundle_id else None

def _bundle_channels(bundle):
    result = []
    for raw_id in bundle.get('channel_ids', []):
        try:
            ch = channels_col.find_one({"channel_id": int(raw_id), "admin_id": ADMIN_ID})
        except (TypeError, ValueError):
            ch = None
        if ch:
            result.append(ch)
    return result

def _bundle_duration_label(bundle):
    return "Lifetime ♾️" if bundle.get('duration_minutes') == 'lifetime' else format_label(bundle.get('duration_minutes'))

def _render_bundle_list(chat_id, message_id=None):
    bundles = list(offer_bundles_col.find({"admin_id": ADMIN_ID}).sort("created_at", 1))
    markup = InlineKeyboardMarkup()
    for bundle in bundles:
        status = "🟢" if bundle.get('enabled') else "🔴"
        toggle_label = "🔒 Disable" if bundle.get('enabled') else "🟢 Enable"
        markup.row(
            InlineKeyboardButton(
                f"{status} {bundle.get('title', 'Unnamed')} — ₹{bundle.get('price', 0)}",
                callback_data=f"obdetail_{bundle['bundle_id']}"),
            InlineKeyboardButton(toggle_label, callback_data=f"obtogglelist_{bundle['bundle_id']}"))
    markup.add(InlineKeyboardButton("➕ Create Offer", callback_data="obadd"))
    text = "'🎉 *Offers*\n\nStack channels at one fixed price — built by the admin."
    if not bundles:
        text += "\n\nNo offers yet."
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        send_admin_reply(text, parse_mode="Markdown", reply_markup=markup, delay=COMMAND_VANISH_SECONDS)

@bot.message_handler(commands=['bundles'], func=lambda m: m.from_user.id == ADMIN_ID)
def bundles_command(message):
    _render_bundle_list(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "main_obundles")
def cb_main_bundles(call):
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    _bundle_nav_state.pop(chat_id, None)
    _render_user_bundles(chat_id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "oblist")
def cb_bundle_admin_list(call):
    if _require_admin(call):
        bot.answer_callback_query(call.id)
        _render_bundle_list(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'noop')
def bundle_nav_noop(call):
    bot.answer_callback_query(call.id)


def _render_user_bundles(chat_id, message_id=None):
    bundles = list(offer_bundles_col.find({"admin_id": ADMIN_ID, "enabled": True}).sort("created_at", 1))
    markup = InlineKeyboardMarkup()
    for bundle in bundles:
        markup.add(InlineKeyboardButton(f"🎉 {bundle.get('title')} — ₹{bundle.get('price')}",
                                        callback_data=f"obuy_{bundle['bundle_id']}"))
    markup.add(InlineKeyboardButton("😍 Premium Groups", callback_data="main_channels"))
    markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
    text = "🎉 <b>Offers</b>\n\nEach offer has a fixed price and includes the channels shown below."
    if not bundles:
        text = "No offers available right now."
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

def _send_bundle_preview_media(chat_id, bundle, channels):
    """Send bundle preview with numbered channels and attractive UI."""
    try:
        screenshots = [ch.get('screenshot_file_id') for ch in channels if ch.get('screenshot_file_id')]
        
        # Build numbered channel list with clean formatting
        channel_lines = []
        for idx, ch in enumerate(channels, 1):
            ch_name = escape(ch.get('name', f'Channel {idx}'))
            desc = ch.get('description')
            if desc:
                channel_lines.append(f"<b>{idx}. {ch_name}</b>\n<i>Description: {escape(desc)}</i>")
            else:
                channel_lines.append(f"<b>{idx}. {ch_name}</b>")
            if idx < len(channels):
                channel_lines.append("━━━━━━━━━━━━━━━━━━━━")
        
        # Build attractive UI with bundle details first
        text = (
            f"╭━━━ 🎉 <b>{escape(bundle.get('title', 'Offer'))}</b> ━━━╮\n\n"
            f"💰 <b>𝙁𝙄𝙓𝙀𝘿 𝙋𝙍𝙄𝘾𝙀</b>\n"
            f"   ₹<b>{int(bundle.get('price', 0))}</b>\n\n"
            f"⏱ <b>𝘿𝙐𝙍𝘼𝙏𝙄𝙊𝙉</b>\n"
            f"   {escape(_bundle_duration_label(bundle))}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📺 <b>𝙊𝙁𝙁𝙀𝙍 𝘾𝙊𝙉𝙏𝘼𝙄𝙉𝙎:</b>\n"
            f"{chr(10).join(channel_lines)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Ready to lock it in?\n"
            f"Tap below to grab this offer 👇"
        )
        
        markup = InlineKeyboardMarkup(row_width=5)
        
        if screenshots:
            total = len(screenshots)
            markup.row(
                InlineKeyboardButton("◀️", callback_data=f"bundleprev_{bundle['bundle_id']}_0"),
                InlineKeyboardButton(f"{1}/{total}", callback_data="noop"),
                InlineKeyboardButton("▶️", callback_data=f"bundlenext_{bundle['bundle_id']}_0")
            )
        
        markup.add(
            InlineKeyboardButton("✅ 𝙂𝙀𝙏 𝙊𝙁𝙁𝙀𝙍", callback_data=f"obcheckout_{bundle['bundle_id']}"),
            InlineKeyboardButton("⬅️ 𝘽𝘼𝘾𝙆", callback_data="main_obundles")
        )
        markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
        
        if screenshots:
            # Add initial channel preview
            first_channel = channels[0] if channels else None
            first_channel_name = escape(first_channel.get('name', 'Channel 1')) if first_channel else 'Channel 1'
            initial_text = text + f"\n\n👆 <b>Now viewing: 1. {first_channel_name}</b>"
            
            msg = bot.send_photo(chat_id, screenshots[0], caption=initial_text, reply_markup=markup, parse_mode="HTML")
            _bundle_nav_state[chat_id] = {
                'screenshots': screenshots,
                'current_index': 0,
                'bundle_id': bundle['bundle_id'],
                'message_id': msg.message_id,
                'text': text,
                'channels': channels,
            }
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            
    except Exception as e:
        print(f"[bundle_preview_media] error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('bundleprev_') or call.data.startswith('bundlenext_'))
def bundle_nav_handler(call):
    """Handle prev/next navigation for bundle screenshot preview."""
    chat_id = call.message.chat.id
    state = _bundle_nav_state.get(chat_id)
    if not state:
        bot.answer_callback_query(call.id, "Session expired. Please open the offer again.")
        return
    
    parts = call.data.split('_')
    direction = parts[0]
    bundle_id = parts[1]
    current_index = int(parts[2])
    
    if str(bundle_id) != str(state['bundle_id']):
        bot.answer_callback_query(call.id, "Invalid session.")
        return
    
    screenshots = state['screenshots']
    total = len(screenshots)
    if total == 0:
        bot.answer_callback_query(call.id)
        return
    
    if direction == 'bundleprev':
        new_index = (current_index - 1) % total
    else:
        new_index = (current_index + 1) % total
    
    state['current_index'] = new_index
    
    # Get channel name for current screenshot
    channels = state.get('channels', [])
    current_channel = channels[new_index] if new_index < len(channels) else None
    channel_name = escape(current_channel.get('name', f'Channel {new_index + 1}')) if current_channel else f'Channel {new_index + 1}'
    
    # Build caption with channel preview
    base_text = state.get('text', '')
    preview_text = f"\n\n👆 <b>Now viewing: {new_index + 1}. {channel_name}</b>"
    
    markup = InlineKeyboardMarkup(row_width=5)
    markup.row(
        InlineKeyboardButton("◀️", callback_data=f"bundleprev_{bundle_id}_{new_index}"),
        InlineKeyboardButton(f"{new_index + 1}/{total}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"bundlenext_{bundle_id}_{new_index}")
    )
    markup.add(
        InlineKeyboardButton("✅ 𝙂𝙀𝙏 𝙊𝙁𝙁𝙀𝙍", callback_data=f"obcheckout_{bundle_id}"),
        InlineKeyboardButton("⬅️ 𝘽𝘼𝘾𝙆", callback_data="main_obundles")
    )
    markup.add(InlineKeyboardButton("Home", callback_data="main_menu_back"))
    
    try:
        # Include caption when editing media to preserve offer text
        media = InputMediaPhoto(
            screenshots[new_index],
            caption=base_text + preview_text,
            parse_mode="HTML"
        )
        bot.edit_message_media(
            chat_id=chat_id,
            message_id=state['message_id'],
            media=media,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[bundle_nav] error: {e}")
        bot.answer_callback_query(call.id, "Couldn't update preview. Please try again.")

def _render_bundle_detail(chat_id, message_id, bundle_id, user_view=False):
    bundle = _bundle_doc(bundle_id)
    if not bundle or (user_view and not bundle.get('enabled')):
        edit_menu(chat_id, message_id, "❌ This offer is no longer available.", reply_markup=None)
        return
    channels = _bundle_channels(bundle)
    
    if user_view:
        _send_bundle_preview_media(chat_id, bundle, channels)
    else:
        names = "\n".join(f"• {escape(ch.get('name', 'Channel'))}" for ch in channels) or "• No valid channels"
        description = escape(bundle.get('description') or 'No description provided.')
        text = (f"🎉 <b>{escape(bundle.get('title', 'Unnamed'))}</b>\n\n{description}\n\n"
                f"<b>Included channels:</b>\n{names}\n\n"
                f"⏱ Duration: <b>{escape(_bundle_duration_label(bundle))}</b>\n"
                f"💰 <b>Fixed price: ₹{int(bundle.get('price', 0))}</b>")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✏️ Edit Title", callback_data=f"obtitle_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("📝 Edit Description", callback_data=f"obdesc_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("💰 Edit Price", callback_data=f"obprice_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("⏱ Edit Duration", callback_data=f"obduration_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("😍 Select Channels", callback_data=f"obchannels_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("🔴 Disable" if bundle.get('enabled') else "🟢 Enable", callback_data=f"obtoggle_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("🗑 Delete", callback_data=f"obdelete_{bundle['bundle_id']}"))
        markup.add(InlineKeyboardButton("⬅️ Back", callback_data="oblist"))
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('obdetail_'))
def cb_bundle_detail(call):
    if _require_admin(call):
        bot.answer_callback_query(call.id)
        _render_bundle_detail(call.message.chat.id, call.message.message_id, call.data.split('_', 1)[1])

@bot.callback_query_handler(func=lambda call: call.data.startswith('obuy_'))
def cb_bundle_buy(call):
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    _render_bundle_detail(call.message.chat.id, call.message.message_id, call.data.split('_', 1)[1], True)

def _bundle_prompt(message, prompt, handler, *args):
    msg = send_prompt(ADMIN_ID, prompt)
    bot.register_next_step_handler(msg, handler, *args)

@bot.callback_query_handler(func=lambda call: call.data == 'obadd')
def cb_bundle_add(call):
    if _require_admin(call):
        bot.answer_callback_query(call.id)
        _bundle_prompt(call.message, "Send the offer title, or /cancel.", _bundle_add_title)

def _bundle_add_title(message):
    title = (message.text or '').strip()
    if title.lower() in ('/cancel', 'cancel'):
        send_admin_reply("❌ Offer creation cancelled."); return
    if not title:
        _bundle_prompt(message, "Title cannot be empty. Send the offer title.", _bundle_add_title); return
    _bundle_prompt(message, "Send the offer description, or /skip for none.", _bundle_add_description, title)

def _bundle_add_description(message, title):
    description = (message.text or '').strip()
    if description.lower() in ('/cancel', 'cancel'):
        send_admin_reply("❌ Offer creation cancelled."); return
    _bundle_prompt(message, "Send the exact fixed price in rupees (numbers only).", _bundle_add_price, title, '' if description.lower() == '/skip' else description)

def _bundle_add_price(message, title, description):
    try:
        price = int((message.text or '').strip())
        if price <= 0: raise ValueError
    except ValueError:
        _bundle_prompt(message, "Invalid price. Send a positive whole number, e.g. 199.", _bundle_add_price, title, description); return
    _bundle_prompt(message, "Send duration as Days:Hours:Mins, or `lifetime`.", _bundle_add_duration, title, description, price)

def _bundle_add_duration(message, title, description, price):
    try:
        duration = parse_duration_only(message.text)
    except Exception:
        _bundle_prompt(message, "Invalid duration. Use Days:Hours:Mins or lifetime.", _bundle_add_duration, title, description, price); return
    bundle_id = secrets.token_hex(4)
    bundle_selection_state[ADMIN_ID] = {"bundle_id": bundle_id, "title": title, "description": description, "price": price, "duration_minutes": duration, "new": True, "channel_ids": []}
    _render_bundle_channel_picker(ADMIN_ID, None)

def _render_bundle_channel_picker(chat_id, message_id, bundle_id=None):
    state = bundle_selection_state.get(ADMIN_ID)
    if bundle_id:
        bundle = _bundle_doc(bundle_id)
        if not bundle: return
        state = dict(bundle)
        state['channel_ids'] = [int(x) for x in bundle.get('channel_ids', [])]
        state['new'] = False
        bundle_selection_state[ADMIN_ID] = state
    if not state: return
    selected = {int(x) for x in state.get('channel_ids', [])}
    markup = InlineKeyboardMarkup()
    for ch in get_sorted_channels(ADMIN_ID):
        mark = '✅' if ch['channel_id'] in selected else '⬜'
        markup.add(InlineKeyboardButton(f"{mark} {ch['name']}", callback_data=f"obpick_{ch['channel_id']}"))
    markup.add(InlineKeyboardButton("💾 Save Offer", callback_data="obsave"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="oblist"))
    text = f"😍 Select channels for <b>{escape(state.get('title', 'offer'))}</b>.\nSelected: {len(selected)}"
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('obchannels_'))
def cb_bundle_channels(call):
    if _require_admin(call):
        bot.answer_callback_query(call.id)
        _render_bundle_channel_picker(call.message.chat.id, call.message.message_id, call.data.split('_', 1)[1])

@bot.callback_query_handler(func=lambda call: call.data.startswith('obpick_'))
def cb_bundle_pick(call):
    if not _require_admin(call): return
    state = bundle_selection_state.get(ADMIN_ID)
    if not state: return
    ch_id = int(call.data.split('_', 1)[1])
    selected = {int(x) for x in state.get('channel_ids', [])}
    selected.remove(ch_id) if ch_id in selected else selected.add(ch_id)
    state['channel_ids'] = list(selected)
    bot.answer_callback_query(call.id)
    _render_bundle_channel_picker(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'obsave')
def cb_bundle_save(call):
    if not _require_admin(call): return
    state = bundle_selection_state.pop(ADMIN_ID, None)
    if not state or not state.get('channel_ids'):
        bot.answer_callback_query(call.id, "Select at least one channel.", show_alert=True); return
    now = datetime.now()
    fields = {k: state[k] for k in ('title', 'description', 'price', 'duration_minutes', 'channel_ids')}
    if state.get('new'):
        fields.update({'admin_id': ADMIN_ID, 'enabled': True, 'created_at': now, 'updated_at': now})
        fields['bundle_id'] = state['bundle_id']
        offer_bundles_col.insert_one(fields)
    else:
        fields.update({'admin_id': ADMIN_ID, 'enabled': state.get('enabled', True), 'updated_at': now})
        offer_bundles_col.update_one({'bundle_id': state['bundle_id'], 'admin_id': ADMIN_ID}, {'$set': fields})
    bot.answer_callback_query(call.id, "Offer saved.")
    _render_bundle_list(call.message.chat.id, call.message.message_id)

def _bundle_edit_text(call, field, prompt, validator=None):
    if not _require_admin(call): return
    bundle_id = call.data.split('_', 1)[1]
    bundle = _bundle_doc(bundle_id)
    if not bundle: return
    bot.answer_callback_query(call.id)
    msg = send_prompt(ADMIN_ID, prompt)
    bot.register_next_step_handler(msg, _bundle_save_field, bundle_id, field, validator)

def _bundle_save_field(message, bundle_id, field, validator):
    value = (message.text or '').strip()
    if value.lower() in ('/cancel', 'cancel'): return
    try:
        value = validator(value) if validator else value
    except Exception:
        send_admin_reply("❌ Invalid value. Please try again."); return
    offer_bundles_col.update_one({'bundle_id': bundle_id, 'admin_id': ADMIN_ID}, {'$set': {field: value, 'updated_at': datetime.now()}})
    send_admin_reply("✅ Offer updated.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('obtitle_'))
def cb_bundle_title(call): _bundle_edit_text(call, 'title', 'Send the new offer title.')

@bot.callback_query_handler(func=lambda call: call.data.startswith('obdesc_'))
def cb_bundle_desc(call): _bundle_edit_text(call, 'description', 'Send the new description, or /skip.', lambda x: '' if x.lower() == '/skip' else x)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obprice_'))
def cb_bundle_price(call): _bundle_edit_text(call, 'price', 'Send the exact fixed price in rupees.', lambda x: int(x) if int(x) > 0 else (_ for _ in ()).throw(ValueError()))

@bot.callback_query_handler(func=lambda call: call.data.startswith('obduration_'))
def cb_bundle_duration(call): _bundle_edit_text(call, 'duration_minutes', 'Send duration as Days:Hours:Mins, or lifetime.', parse_duration_only)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obtogglelist_'))
def cb_bundle_toggle_list(call):
    if _require_admin(call):
        bundle_id = call.data.split('_', 1)[1]; bundle = _bundle_doc(bundle_id)
        if bundle: offer_bundles_col.update_one({'bundle_id': bundle_id}, {'$set': {'enabled': not bundle.get('enabled'), 'updated_at': datetime.now()}})
        bot.answer_callback_query(call.id); _render_bundle_list(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obtoggle_'))
def cb_bundle_toggle(call):
    if _require_admin(call):
        bundle_id = call.data.split('_', 1)[1]; bundle = _bundle_doc(bundle_id)
        if bundle: offer_bundles_col.update_one({'bundle_id': bundle_id}, {'$set': {'enabled': not bundle.get('enabled'), 'updated_at': datetime.now()}})
        bot.answer_callback_query(call.id); _render_bundle_detail(call.message.chat.id, call.message.message_id, bundle_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obdelete_'))
def cb_bundle_delete(call):
    if _require_admin(call):
        bundle_id = call.data.split('_', 1)[1]; offer_bundles_col.delete_one({'bundle_id': bundle_id, 'admin_id': ADMIN_ID})
        bot.answer_callback_query(call.id, "Deleted."); _render_bundle_list(call.message.chat.id, call.message.message_id)

# --- USER: SEARCH / FILTER CHANNELS ---

SEARCH_PER_PAGE = 15  # results per page so a long channel list stays scrollable
search_state = {}      # user_id -> {'keyword': str, 'page': int}

def search_channels_by_keyword(keyword):
    """Case-insensitive search across each channel's name and description."""
    kw = (keyword or '').strip().lower()
    if not kw:
        return []
    results = []
    for ch in get_sorted_channels(ADMIN_ID):
        hay = ch.get('name') or ''
        if ch.get('description'):
            hay += ' ' + ch['description']
        if kw in hay.lower():
            results.append(ch)
    return results

def render_search_results(chat_id, message_id, user_id, keyword, page=0, message_obj=None):
    """Renders a paginated list of channels matching `keyword`. Edits an existing
    message when message_id is given, otherwise sends a fresh one."""
    state = search_state.setdefault(user_id, {'keyword': keyword, 'page': 0})
    state['keyword'] = keyword
    state['page'] = page
    results = search_channels_by_keyword(keyword)

    if not results:
        text = f"🔍 No channels found for \"{escape(keyword)}\"\n\nTry a different keyword."
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔍 New Search", callback_data="search_prompt"))
        markup.add(InlineKeyboardButton("😍 All Channels", callback_data="cart_browse"))
        if message_id:
            edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=message_obj)
        else:
            dismiss_previous(chat_id, user_id)
            reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
            track_msg(user_id, reply)
        return

    total = len(results)
    start = page * SEARCH_PER_PAGE
    end = min(start + SEARCH_PER_PAGE, total)
    page_items = results[start:end]

    # Real position in the full channel list, so top-3 medals still apply
    all_channels = get_sorted_channels(ADMIN_ID)
    position_map = {ch['channel_id']: i for i, ch in enumerate(all_channels, start=1)}

    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(page_items, start=start + 1):
        real_pos = position_map.get(ch['channel_id'])
        label = channel_button_label(ch, real_pos) if real_pos else ch['name']
        markup.add(InlineKeyboardButton(f"{i}. {label}", callback_data=f"browse_{ch['channel_id']}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"searchpage_{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"searchpage_{page + 1}"))
    if nav:
        markup.row(*nav)
    markup.add(InlineKeyboardButton("🔍 New Search", callback_data="search_prompt"))
    markup.add(InlineKeyboardButton("😍 All Channels", callback_data="cart_browse"))

    text = f"🔍 <b>Search results</b> for \"{escape(keyword)}\"\n\nShowing {start + 1}-{end} of {total}:"
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=message_obj)
    else:
        dismiss_previous(chat_id, user_id)
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(user_id, reply)

@bot.message_handler(commands=['search'])
def search_handler(message):
    record_seen_user(message.from_user)
    parts = message.text.split(None, 1)
    keyword = parts[1].strip() if len(parts) > 1 else ''
    if not keyword:
        msg = send_prompt(message.chat.id, "🔍 Enter a keyword to search channels by name or description:")
        bot.register_next_step_handler(msg, search_next_step, message.from_user.id)
        return
    render_search_results(message.chat.id, None, message.from_user.id, keyword)

def search_next_step(message, user_id):
    if not getattr(message, 'text', None):
        msg = send_prompt(message.chat.id, "🔍 Please send a search keyword as text:")
        bot.register_next_step_handler(msg, search_next_step, user_id)
        return
    keyword = message.text.strip()
    render_search_results(message.chat.id, None, user_id, keyword)

@bot.callback_query_handler(func=lambda call: call.data == "search_prompt")
def cb_search_prompt(call):
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    cancel_delete(call.message.chat.id, call.message.message_id)
    msg = send_prompt(call.message.chat.id, "🔍 Enter a keyword to search channels by name or description:")
    bot.register_next_step_handler(msg, search_next_step, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('searchpage_'))
def cb_search_page(call):
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    try:
        page = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError, IndexError):
        page = 0
    state = search_state.get(call.from_user.id, {})
    keyword = state.get('keyword') or ''
    if not keyword:
        edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message, back_to_menu=True)
        return
    render_search_results(call.message.chat.id, call.message.message_id, call.from_user.id, keyword, page=page, message_obj=call.message)


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
    """Kick a user from a channel/group so they lose access immediately.
    Returns (removed: bool, detail: str). The user is unbanned right after the
    kick so they are not kept on a permanent block list — they can rejoin the
    group/channel again in the future."""
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
            # Kick is done. Immediately unban so the user is NOT kept on a permanent
            # block list — they can rejoin the group/channel again in the future.
            try:
                bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            except Exception:
                pass
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
    / 123456789 Name, etc. CSV lines like "ID,username,First,Last" are parsed
    too. Lines starting with '#' or '//' are skipped as comments."""
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('//'):
        return None, None
    if ',' in line:
        return _parse_csv_member_line(line)
    ids = [int(x) for x in _MEMBER_LINE_RE_ID.findall(line)]
    unames = [u for u in _MEMBER_LINE_RE_USERNAME.findall(line)]
    user_id = ids[0] if ids else None
    username = unames[0] if unames else None
    return user_id, username

def _parse_csv_member_line(line):
    """Parse a comma-separated row: User ID,Username,First Name,Last Name.
    The header row is skipped. Only the ID and username columns are kept."""
    fields = [f.strip() for f in line.split(',')]
    if fields and fields[0].lower().startswith('user id'):
        return None, None
    user_id = None
    username = None
    for i, field in enumerate(fields):
        if not field:
            continue
        if i == 0:
            try:
                user_id = int(field)
                continue
            except ValueError:
                pass
        if username is None:
            candidate = field.lstrip('@')
            if len(candidate) >= 5 and re.fullmatch(r'[A-Za-z0-9_]+', candidate):
                username = candidate
        if user_id is not None and username is not None:
            break
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
    """Remove user from this group/channel. The user is unbanned right after the
    kick (see remove_user_from_chat) so they are not kept on a block list and
    can rejoin the group/channel again in the future."""
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
        BotCommand("search", "Search channels by keyword"),
        BotCommand("myplans", "Check your active subscriptions"),
        BotCommand("cancel", "Cancel a pending payment"),
        BotCommand("help", "Help & contact admin"),
    ]
    admin_commands =  user_commands + [
        BotCommand("add", "Add a new channel"),
        BotCommand("channels", "Manage channels (edit/delete)"),
        BotCommand("bundles", "Manage offers (custom fixed-price offers)"),
        BotCommand("setmenuimage", "Set/remove the main menu image"),
        BotCommand("forcejoin", "Require users to join a channel (Force Join settings)"),
        BotCommand("removeuser", "Remove a subscriber early"),
        BotCommand("stats", "View bot stats & revenue"),
        BotCommand("broadcast", "Message everyone who has used the bot"),
        BotCommand("listbroadcasts", "List scheduled broadcasts"),
        BotCommand("dbstats", "Check database storage usage"),
        BotCommand("cleanup", "Free up database space"),
        BotCommand("import", "Import member list for a group/channel"),
        BotCommand("sync", "Tracked chats & re-sync members"),
        BotCommand("pending", "Review pending payment checkouts"),
        BotCommand("reactcachestatus", "Check auto-react backlog size"),
        BotCommand("clearreactcache", "Clear the auto-react backlog"),
        BotCommand("users", "List all active subscribers & plans"),
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

# =====================================================================
# FREE TRIAL PLANS (admin-managed, logically separate from paid plans)
# ---------------------------------------------------------------------
# Business rule: ONE USER + ONE CHANNEL = ONE FREE TRIAL EVER.
# Eligibility is based ONLY on (user_id, channel_id). trial_plan_id is
# retained purely for audit/history and must NEVER allow the same user to
# claim a second trial for the same channel — even if the admin deletes,
# recreates, renames, or changes the duration of a trial.
# Claims live in free_trial_claims_col and are NEVER deleted, even after the
# trial expires. Telegram numeric user_id is the identity (never a username).
# Free trials never create payment records, never bump revenue/sales counters,
# and never enter the paid checkout/cart flow.
# =====================================================================

# Lightweight per-user debounce for claim taps. MongoDB (unique index + atomic
# status transition) remains the real source of truth; this just stops accidental
# double-taps from spamming Telegram.
TRIAL_CLAIM_DEBOUNCE_SECONDS = 3
_trial_claim_debounce = {}

# A claim stuck in 'granting' for longer than this is assumed to be from a
# crashed process and becomes recoverable again (retry reuses the SAME claim).
TRIAL_GRANT_STALE_SECONDS = 60

_TRIAL_ID_RE = re.compile(r'^[A-Za-z0-9]+$')

def _safe_trial_id(raw):
    """Validate an opaque trial identifier from callback_data before use."""
    raw = (raw or '').strip()
    if not raw or not _TRIAL_ID_RE.match(raw):
        return None
    return raw

def _require_admin(call):
    """Every admin free-trial callback must pass through here. Only ADMIN_ID may
    run admin actions; callback_data is never trusted (never rely on a button
    having merely been shown to an admin)."""
    if not getattr(call, 'from_user', None) or not ADMIN_ID or call.from_user.id != ADMIN_ID:
        try:
            bot.answer_callback_query(call.id, "Access denied.")
        except Exception:
            pass
        return False
    return True

def _trial_channel_ok(trial):
    """Re-validate that this trial's channel exists and is managed by ADMIN_ID."""
    if not trial:
        return False, None
    try:
        ch_id = int(trial['channel_id'])
    except (TypeError, ValueError, KeyError):
        return False, None
    channel = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not channel:
        return False, None
    return True, ch_id

# ---- Admin: free-trial list for a channel ----

def _render_trials_list(chat_id, message_id, ch_id):
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        return
    trials = list(free_trials_col.find({"channel_id": ch_id}).sort("created_at", 1))
    markup = InlineKeyboardMarkup()
    for tr in trials:
        status = "🟢" if tr.get('enabled') else "🔴"
        label = tr.get('name') or 'Unnamed'
        markup.add(InlineKeyboardButton(f"{status} {label} ({format_label(tr.get('duration_minutes'))})",
                                        callback_data=f"trial_{tr['trial_id']}"))
    markup.add(InlineKeyboardButton("➕ Add Free Trial", callback_data=f"trialadd_{ch_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"manage_{ch_id}"))
    edit_menu(chat_id, message_id,
              f"🎁 *Free Trials* for *{ch_data['name']}*\n\n"
              f"Tap a trial to edit it, or add a new one. Trials are one-time offers per "
              f"user (never part of the paid cart/payment flow).",
              reply_markup=markup, parse_mode="Markdown")

def _render_trial_detail(chat_id, message_id, trial_id):
    trial = free_trials_col.find_one({"trial_id": trial_id})
    if not trial:
        return
    ok, ch_id = _trial_channel_ok(trial)
    if not ok:
        send_admin_reply("❌ Free trial not found or its channel is no longer managed by you.")
        return
    ch = channels_col.find_one({"channel_id": ch_id})
    ch_name = ch['name'] if ch else str(ch_id)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✏️ Edit Name", callback_data=f"trialname_{trial_id}"))
    markup.add(InlineKeyboardButton("⏱ Edit Duration", callback_data=f"trialdur_{trial_id}"))
    if trial.get('enabled'):
        markup.add(InlineKeyboardButton("🔴 Disable", callback_data=f"trialoff_{trial_id}"))
    else:
        markup.add(InlineKeyboardButton("🟢 Enable", callback_data=f"trialon_{trial_id}"))
    markup.add(InlineKeyboardButton("📊 Stats", callback_data=f"trialstats_{trial_id}"))
    markup.add(InlineKeyboardButton("🗑 Delete", callback_data=f"trialdel_{trial_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"trials_{ch_id}"))
    status = "🟢 Enabled" if trial.get('enabled') else "🔴 Disabled"
    created = trial.get('created_at')
    created_str = created.strftime("%Y-%m-%d") if created else "?"
    updated = trial.get('updated_at')
    updated_str = updated.strftime("%Y-%m-%d") if updated else "?"
    edit_menu(chat_id, message_id,
              f"🎁 *{escape_markdown(trial.get('name') or 'Unnamed')}*\n\n"
              f"😍 Channel: {escape_markdown(ch_name)}\n"
              f"⏱ Duration: {format_label(trial.get('duration_minutes'))}\n"
              f"Status: {status}\n"
              f"Created: {created_str}\n"
              f"Updated: {updated_str}\n"
              f"Trial ID: `{trial_id}`",
              reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trials_'))
def cb_trials_menu(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError):
        return
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        send_admin_reply("❌ Channel not found (or not managed by you).")
        return
    _render_trials_list(call.message.chat.id, call.message.message_id, ch_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('trial_'))
def cb_trial_detail(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    if trial_id:
        _render_trial_detail(call.message.chat.id, call.message.message_id, trial_id)
    else:
        send_admin_reply("❌ Invalid trial identifier.")

# ---- Admin: add / edit / enable / disable / delete ----

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialadd_'))
def cb_trial_add(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError):
        return
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        send_admin_reply("❌ Channel not found.")
        return
    msg = send_prompt(ADMIN_ID,
        f"Enter the free-trial duration for *{escape_markdown(ch_data['name'])}* as `Days:Hours:Mins`.\n\n"
        f"Example: `3:0:0` for 3 days.\n\nSend /cancel to abort.",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, _trial_add_duration, ch_id)

def _trial_add_duration(message, ch_id):
    if message.text and message.text.strip().lower() in ('/cancel', 'cancel', '/stop', 'stop'):
        send_admin_reply("❌ Free trial creation cancelled.")
        return
    try:
        t = parse_duration_only(message.text)
    except Exception:
        msg = send_prompt(ADMIN_ID, "❌ Invalid duration. Use `Days:Hours:Mins`, e.g. `3:0:0`. Send /cancel to abort.", parse_mode="Markdown")
        bot.register_next_step_handler(msg, _trial_add_duration, ch_id)
        return
    if t == "lifetime":
        msg = send_prompt(ADMIN_ID, "❌ Free trials must have a finite duration. Use `Days:Hours:Mins`, e.g. `3:0:0`.", parse_mode="Markdown")
        bot.register_next_step_handler(msg, _trial_add_duration, ch_id)
        return
    msg = send_prompt(ADMIN_ID, "Now send the display *name* for this free trial, e.g. `Free 3 Days`.", parse_mode="Markdown")
    bot.register_next_step_handler(msg, _trial_add_name, ch_id, int(t))

def _trial_add_name(message, ch_id, duration_minutes):
    if message.text and message.text.strip().lower() in ('/cancel', 'cancel', '/stop', 'stop'):
        send_admin_reply("❌ Free trial creation cancelled.")
        return
    name = (message.text or '').strip()
    if not name:
        msg = send_prompt(ADMIN_ID, "❌ Name can't be empty. Send the display name, e.g. `Free 3 Days`.")
        bot.register_next_step_handler(msg, _trial_add_name, ch_id, duration_minutes)
        return
    trial_id = secrets.token_hex(4)
    now = datetime.now()
    try:
        free_trials_col.insert_one({
            "trial_id": trial_id,
            "channel_id": ch_id,
            "name": name,
            "duration_minutes": duration_minutes,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        })
    except DuplicateKeyError:
        send_admin_reply("❌ Could not create the trial (ID collision). Please try again.")
        return
    send_admin_reply(f"✅ Free trial created: *{escape_markdown(name)}* — {format_label(duration_minutes)}\n\n"
                     f"It is enabled by default. Manage it from the channel's 🎁 Free Trials menu.",
                     parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialname_'))
def cb_trial_edit_name(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    trial = free_trials_col.find_one({"trial_id": trial_id}) if trial_id else None
    if not trial:
        send_admin_reply("❌ Free trial not found.")
        return
    msg = send_prompt(ADMIN_ID,
        f"Send the new display name for this free trial (currently *{escape_markdown(trial.get('name') or 'Unnamed')}*).",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, _trial_save_name, trial_id)

def _trial_save_name(message, trial_id):
    name = (message.text or '').strip()
    if not name:
        send_admin_reply("❌ Name can't be empty.")
        return
    free_trials_col.update_one({"trial_id": trial_id}, {"$set": {"name": name, "updated_at": datetime.now()}})
    send_admin_reply(f"✅ Trial name updated to *{escape_markdown(name)}*.", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialdur_'))
def cb_trial_edit_duration(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    trial = free_trials_col.find_one({"trial_id": trial_id}) if trial_id else None
    if not trial:
        send_admin_reply("❌ Free trial not found.")
        return
    msg = send_prompt(ADMIN_ID,
        f"Send the new duration as `Days:Hours:Mins` (currently {format_label(trial.get('duration_minutes'))}).\n\n"
        f"Example: `3:0:0` for 3 days.",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, _trial_save_duration, trial_id)

def _trial_save_duration(message, trial_id):
    try:
        t = parse_duration_only(message.text)
    except Exception:
        send_admin_reply("❌ Invalid duration. Use `Days:Hours:Mins`, e.g. `3:0:0`.")
        return
    if t == "lifetime":
        send_admin_reply("❌ Free trials must have a finite duration.")
        return
    free_trials_col.update_one({"trial_id": trial_id},
                               {"$set": {"duration_minutes": int(t), "updated_at": datetime.now()}})
    send_admin_reply(f"✅ Trial duration updated to {format_label(int(t))}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialon_'))
def cb_trial_enable(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id, "Enabled.")
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    if trial_id:
        free_trials_col.update_one({"trial_id": trial_id}, {"$set": {"enabled": True, "updated_at": datetime.now()}})
        _render_trial_detail(call.message.chat.id, call.message.message_id, trial_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialoff_'))
def cb_trial_disable(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id, "Disabled.")
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    if trial_id:
        free_trials_col.update_one({"trial_id": trial_id}, {"$set": {"enabled": False, "updated_at": datetime.now()}})
        _render_trial_detail(call.message.chat.id, call.message.message_id, trial_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialstats_'))
def cb_trial_stats(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    trial = free_trials_col.find_one({"trial_id": trial_id}) if trial_id else None
    if not trial:
        send_admin_reply("❌ Free trial not found.")
        return
    total = free_trial_claims_col.count_documents({"trial_plan_id": trial_id})
    active = free_trial_claims_col.count_documents({"trial_plan_id": trial_id, "status": "active"})
    expired = free_trial_claims_col.count_documents({"trial_plan_id": trial_id, "status": "expired"})
    pending = free_trial_claims_col.count_documents(
        {"trial_plan_id": trial_id, "status": {"$in": ["pending", "granting", "grant_failed"]}})
    send_admin_reply(
        f"📊 *Trial Stats: {escape_markdown(trial.get('name') or 'Unnamed')}*\n\n"
        f"🧾 Total claimed (ever): {total}\n"
        f"🟢 Currently active: {active}\n"
        f"⏳ Pending / activating: {pending}\n"
        f"⌛ Expired: {expired}\n\n"
        f"_(Claims are permanent and are never deleted on expiry.)_",
        parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialdel_'))
def cb_trial_delete(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    trial = free_trials_col.find_one({"trial_id": trial_id}) if trial_id else None
    if not trial:
        send_admin_reply("❌ Free trial not found.")
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Delete", callback_data=f"trialdelconf_{trial_id}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data=f"trial_{trial_id}"))
    edit_menu(call.message.chat.id, call.message.message_id,
              f"⚠️ Delete free trial *{escape_markdown(trial.get('name') or 'Unnamed')}*?\n\n"
              f"Existing claims will be kept permanently (one-trial-ever rule). Active trial "
              f"subscribers keep access until their trial naturally expires.",
              reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialdelconf_'))
def cb_trial_delete_confirm(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id, "Deleted.")
    trial_id = _safe_trial_id(call.data.split('_', 1)[1])
    trial = free_trials_col.find_one({"trial_id": trial_id}) if trial_id else None
    ch_id = None
    if trial:
        try:
            ch_id = int(trial['channel_id'])
        except (TypeError, ValueError, KeyError):
            ch_id = None
        free_trials_col.delete_one({"trial_id": trial_id})
    if ch_id is not None:
        _render_trials_list(call.message.chat.id, call.message.message_id, ch_id)
    else:
        send_admin_reply("❌ Free trial not found.")

# ---- User: claim a free trial ----

def _active_paid_sub_for(user_id, ch_id):
    """Return the user's active NON-trial subscription for this channel, or None.
    Used to ensure a free trial never overwrites/downgrades a paid subscription."""
    now = datetime.now().timestamp()
    sub = users_col.find_one({"user_id": int(user_id), "channel_id": int(ch_id)})
    if not sub or sub.get('subscription_type') == 'free_trial':
        return None
    if is_active_subscription(sub, now):
        return sub
    return None

def _create_trial_invite(ch_id, expiry_ts):
    """Create a SINGLE-USE invite link (member_limit=1) that expires at the trial
    end. Retries on Telegram 429 rate limits. Returns the link or None if it kept
    failing — never a permanent/public channel link."""
    for attempt in range(3):
        try:
            return bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                _sleep_retry_after(e)
                continue
            raise
    return None

def _ts_of(x):
    """Normalize a stored datetime or numeric timestamp to a unix timestamp."""
    if hasattr(x, 'timestamp'):
        return x.timestamp()
    return float(x or 0)

def _dt_of(x):
    """Normalize a stored datetime or numeric timestamp to a datetime."""
    if hasattr(x, 'strftime'):
        return x
    return datetime.fromtimestamp(float(x or 0))

def _grant_trial_subscription(user_id, trial, claim, ch_id):
    """Create (or reuse) the single-use invite and write the active trial
    subscription. Returns (invite_link_str, expiry_dt). Raises on failure so the
    caller can leave the claim in a recoverable state (no fake success ever).

    Recovery-aware: if this claim already has a persisted, still-valid invite
    (from a previous attempt where the users_col write failed or the process
    crashed mid-grant), it is REUSED — never a second invite for the same claim.
    The invite's stored expiry defines the trial window, so editing a trial's
    duration never extends a claim that was already in flight."""
    now = datetime.now()
    now_ts = now.timestamp()

    stored_link = claim.get('invite_link')
    stored_exp = claim.get('invite_expires_at')
    if stored_link and stored_exp and _ts_of(stored_exp) > now_ts:
        # Reuse the invite already persisted for this claim.
        link_str = stored_link
        expiry_dt = _dt_of(stored_exp)
    else:
        expiry_dt = now + timedelta(minutes=int(trial['duration_minutes']))
        expiry_ts = int(expiry_dt.timestamp())

        # Safety net: make sure a previously kicked user is not banned from joining.
        try:
            bot.unban_chat_member(ch_id, user_id, only_if_banned=True)
        except Exception:
            pass

        link = _create_trial_invite(ch_id, expiry_ts)
        if link is None:
            raise RuntimeError("Telegram kept rate-limiting the invite request (429).")
        link_str = link.invite_link

        # Persist the invite BEFORE writing users_col, so a crash in between is
        # recovered on retry by reusing this same single-use invite.
        free_trial_claims_col.update_one(
            {"_id": claim['_id']},
            {"$set": {
                "status": "invite_created",
                "invite_link": link_str,
                "invite_expires_at": expiry_dt,
                "invite_created_at": now,
            }}
        )

    users_col.update_one(
        {"user_id": int(user_id), "channel_id": int(ch_id)},
        {"$set": {
            "expiry": expiry_dt.timestamp(),
            "lifetime": False,
            "reminded_24h": False,
            "reminded_1h": False,
            "subscription_type": "free_trial",
            "trial_plan_id": trial['trial_id'],
            "trial_claim_id": str(claim['_id']),
        }},
        upsert=True
    )
    return link_str, expiry_dt

def _trial_already_claimed_markup(ch_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💳 Buy Paid Plan", callback_data=f"buypaid_{ch_id}"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    return markup

def _show_trial_already_claimed(call, ch_id):
    text = ("⚠️ <b>Free trial already claimed</b>\n\n"
            "You've already used your one-time free trial for this channel.\n\n"
            "Grab a paid plan below to keep going 💳")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=_trial_already_claimed_markup(ch_id))
        schedule_delete(call.message.chat.id, call.message.message_id, MENU_VANISH_SECONDS)
    except Exception:
        edit_menu(call.message.chat.id, call.message.message_id, text,
                  reply_markup=_trial_already_claimed_markup(ch_id),
                  message_obj=call.message)

def _show_trial_active_paid(call, ch_id):
    text = ("ℹ️ You're already subscribed here.\n\n"
            "Free trial can't replace or downgrade an active plan.\n\n"
            "Use /myplans to check your subscriptions.")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💳 Browse Paid Plans", callback_data=f"buypaid_{ch_id}"))
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, message_obj=call.message)

def _show_trial_retry(call, trial_id, ch_id):
    """Recoverable failure state: the SAME claim can be retried, never a new trial."""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Try Again", callback_data=f"trialretry_{trial_id}"))
    markup.add(InlineKeyboardButton("💳 Buy Paid Plan", callback_data=f"buypaid_{ch_id}"))
    edit_menu(call.message.chat.id, call.message.message_id,
              "❌ Couldn't activate your free trial right now.\n\nGive it another go 🔄",
              reply_markup=markup, message_obj=call.message)

def _process_trial_claim(call, raw_trial_id):
    """Core free-trial claim handler.

    Order of operations (all identifiers reloaded & validated from MongoDB):
      1. trial exists & is enabled
      2. channel exists & is managed by ADMIN_ID
      3. no active paid subscription to downgrade
      4. atomic one-trial-ever claim on (user_id, channel_id) — trial_plan_id is
         audit-only and never grants a second trial for the same channel
      5. single-use, expiring invite link (reused if a previous attempt already
         persisted one, so a retry never creates a second invite)
      6. active subscription in users_col (free_trial type)
    A failed invite never reports fake success — the claim is left in a
    recoverable 'grant_failed'/'invite_created' state so the SAME claim can be
    retried."""
    user_id = call.from_user.id
    trial_id = _safe_trial_id(raw_trial_id)
    if not trial_id:
        try:
            bot.answer_callback_query(call.id, "Invalid request.")
        except Exception:
            pass
        return

    now_ts = time.time()
    if _trial_claim_debounce.get(user_id, 0) > now_ts - TRIAL_CLAIM_DEBOUNCE_SECONDS:
        try:
            bot.answer_callback_query(call.id, "⏳ Please wait a moment and try again.")
        except Exception:
            pass
        return
    _trial_claim_debounce[user_id] = now_ts

    trial = free_trials_col.find_one({"trial_id": trial_id})
    if not trial or not trial.get('enabled'):
        try:
            bot.answer_callback_query(call.id, "This trial is no longer available.")
        except Exception:
            pass
        edit_menu(call.message.chat.id, call.message.message_id,
                  "❌ This free trial is no longer available.\n\nYou can browse the paid plans instead.",
                  reply_markup=None, message_obj=call.message)
        return

    try:
        ch_id = int(trial['channel_id'])
    except (TypeError, ValueError, KeyError):
        try:
            bot.answer_callback_query(call.id, "Invalid request.")
        except Exception:
            pass
        return

    channel = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not channel:
        try:
            bot.answer_callback_query(call.id, "This channel is no longer available.")
        except Exception:
            pass
        edit_menu(call.message.chat.id, call.message.message_id,
                  "❌ This channel is no longer available.",
                  reply_markup=None, message_obj=call.message)
        return

    now = datetime.now()

    # ---- 1) Check existing permanent claim (eligibility = user + channel ONLY) ----
    existing = free_trial_claims_col.find_one(
        {"user_id": int(user_id), "channel_id": ch_id})

    if existing is not None:
        status = existing.get('status')
        if status in ('active', 'expired', 'archived'):
            try:
                bot.answer_callback_query(call.id, "Free trial already claimed.")
            except Exception:
                pass
            _show_trial_already_claimed(call, ch_id)
            return
        if status == 'granting':
            # A fresh grant in progress -> don't double-trigger. A STALE one
            # (crashed process) is recoverable and retried below.
            started = existing.get('grant_started_at')
            stale = (started is None) or ((now - started).total_seconds() > TRIAL_GRANT_STALE_SECONDS)
            if not stale:
                try:
                    bot.answer_callback_query(call.id, "Activation is in progress, please try again in a few seconds.")
                except Exception:
                    pass
                return
        # status in ('pending', 'grant_failed', 'invite_created') or a stale
        # 'granting' -> safe retry/recovery of the SAME claim (never a new one).
        claim = existing
        # Keep the audit trail in sync with the trial actually being granted
        # (covers admin delete/recreate + retry scenarios).
        if existing.get('trial_plan_id') != trial_id:
            free_trial_claims_col.update_one(
                {"_id": existing['_id']},
                {"$set": {"trial_plan_id": trial_id}})
    else:
        # Do not replace/downgrade an active paid subscription.
        if _active_paid_sub_for(user_id, ch_id) is not None:
            try:
                bot.answer_callback_query(call.id, "You already have an active subscription for this channel.")
            except Exception:
                pass
            _show_trial_active_paid(call, ch_id)
            return

        try:
            claim_id = free_trial_claims_col.insert_one({
                "user_id": int(user_id),
                "channel_id": int(ch_id),
                "trial_plan_id": trial_id,
                "claimed_at": now,
                "trial_expiry": None,
                "status": "pending",
            }).inserted_id
            claim = {"_id": claim_id, "status": "pending"}
        except DuplicateKeyError:
            # Two simultaneous requests: we lost the race, the other one already
            # created the claim. Re-read it (by user+channel) and fall through to
            # the retry/recovery logic.
            existing = free_trial_claims_col.find_one(
                {"user_id": int(user_id), "channel_id": ch_id})
            if not existing:
                try:
                    bot.answer_callback_query(call.id, "Could not claim the trial right now. Please try again.")
                except Exception:
                    pass
                return
            if existing.get('status') in ('active', 'expired', 'archived'):
                try:
                    bot.answer_callback_query(call.id, "Free trial already claimed.")
                except Exception:
                    pass
                _show_trial_already_claimed(call, ch_id)
                return
            claim = existing
            if existing.get('trial_plan_id') != trial_id:
                free_trial_claims_col.update_one(
                    {"_id": existing['_id']},
                    {"$set": {"trial_plan_id": trial_id}})

    # ---- 2) Atomically reserve the claim for granting (blocks duplicate grant) ----
    # Reclaimable states: pending (never granted), grant_failed / invite_created
    # (a previous attempt crashed) and a stale 'granting' (process died mid-grant).
    if claim.get('status') == 'granting':
        # Stale grant from a crashed process: mark it reclaimable so the
        # reservation below can pick it up. The status filter guarantees only one
        # concurrent request can win the transition.
        free_trial_claims_col.update_one(
            {"_id": claim['_id'], "status": "granting"},
            {"$set": {"status": "grant_failed", "grant_fail_reason": "stale_grant"}})
    reserved = free_trial_claims_col.find_one_and_update(
        {"_id": claim['_id'], "status": {"$in": ["pending", "grant_failed", "invite_created"]}},
        {"$set": {"status": "granting", "grant_started_at": now, "last_grant_attempt_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not reserved:
        try:
            bot.answer_callback_query(call.id, "Activation is in progress, please try again in a few seconds.")
        except Exception:
            pass
        return
    claim = reserved

    # Re-check inside the grant window (Mongo is the source of truth).
    if _active_paid_sub_for(user_id, ch_id) is not None:
        free_trial_claims_col.update_one({"_id": claim['_id']},
                                         {"$set": {"status": "grant_failed", "grant_fail_reason": "paid_subscription_active"}})
        try:
            bot.answer_callback_query(call.id, "You already have an active subscription for this channel.")
        except Exception:
            pass
        _show_trial_active_paid(call, ch_id)
        return

    # ---- 3) Create (or reuse) single-use invite + active subscription ----
    try:
        link_str, expiry_dt = _grant_trial_subscription(int(user_id), trial, claim, ch_id)
    except telebot.apihelper.ApiTelegramException as e:
        free_trial_claims_col.update_one({"_id": claim['_id']},
                                         {"$set": {"status": "grant_failed", "grant_fail_reason": "telegram_error",
                                                   "last_error": str(e)[:200]}})
        print(f"[free-trial] invite failed for claim {claim['_id']}: {e}")
        try:
            bot.answer_callback_query(call.id, "Could not activate your free trial right now.")
        except Exception:
            pass
        _show_trial_retry(call, trial_id, ch_id)
        return
    except Exception as e:
        free_trial_claims_col.update_one({"_id": claim['_id']},
                                         {"$set": {"status": "grant_failed", "grant_fail_reason": "unknown",
                                                   "last_error": str(e)[:200]}})
        print(f"[free-trial] unexpected error for claim {claim['_id']}: {e}")
        try:
            bot.answer_callback_query(call.id, "Could not activate your free trial right now.")
        except Exception:
            pass
        _show_trial_retry(call, trial_id, ch_id)
        return

    # ---- 4) Success: permanent claim becomes active ----
    free_trial_claims_col.update_one(
        {"_id": claim['_id']},
        {"$set": {"status": "active", "trial_expiry": expiry_dt, "activated_at": datetime.now(),
                  "grant_fail_reason": None, "last_error": None}}
    )
    try:
        bot.answer_callback_query(call.id, "Free trial activated! 🎁")
    except Exception:
        pass

    # Replace the menu with a clean confirmation + the single-use invite link.
    try:
        cancel_delete(call.message.chat.id, call.message.message_id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    ch_name = channel.get('name') or f"Channel {ch_id}"
    text = (
        f"🎁 <b>Free trial activated!</b>\n\n"
        f"😍 Channel: {escape_markdown(ch_name)}\n"
        f"⏱ Duration: {format_label(trial['duration_minutes'])}\n"
        f"⌛ Expires: {expiry_dt.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"🔗 Your invite link (single use):\n{link_str}\n\n"
        f"⚠️ This link works only once and expires at the trial end."
    )
    try:
        inv_msg = bot.send_message(call.message.chat.id, text, parse_mode="Markdown", vanish_delay=None)
    except Exception:
        try:
            inv_msg = bot.send_message(call.message.chat.id, text, vanish_delay=None)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialclaim_'))
def cb_trial_claim(call):
    if not getattr(call, 'from_user', None) or not getattr(call, 'message', None):
        try:
            bot.answer_callback_query(call.id, "Invalid request.")
        except Exception:
            pass
        return
    record_seen_user(call.from_user)
    _process_trial_claim(call, call.data.split('_', 1)[1])

@bot.callback_query_handler(func=lambda call: call.data.startswith('trialretry_'))
def cb_trial_retry(call):
    if not getattr(call, 'from_user', None) or not getattr(call, 'message', None):
        try:
            bot.answer_callback_query(call.id, "Invalid request.")
        except Exception:
            pass
        return
    record_seen_user(call.from_user)
    _process_trial_claim(call, call.data.split('_', 1)[1])

@bot.callback_query_handler(func=lambda call: call.data.startswith('buypaid_'))
def cb_buy_paid(call):
    if not getattr(call, 'from_user', None):
        return
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError):
        return
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        edit_menu(call.message.chat.id, call.message.message_id,
                  "❌ This channel is no longer available.",
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse")),
                  message_obj=call.message)
        return
    edit_plan_selection(call.message.chat.id, call.message.message_id, ch_data, call.from_user.id)

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
                send_plan_selection(message.chat.id, ch_data, user_id)
                return
        except Exception:
            pass

    # Admin Panel Greeting
    if user_id == ADMIN_ID:
        dismiss_previous(message.chat.id, user_id)
        reply = bot.send_message(message.chat.id,
            "╭━━━ 👑 𝘼𝘿𝙈𝙄𝙉 𝙋𝘼𝙉𝙀𝙇 ━━━╮\n\n"
            "You're in control here 👇\n\n"
            "/add — add a new channel & prices\n"
            "/channels — manage existing channels\n"
            "/bundles — create & manage offers\n"
            "/removeuser — remove a subscriber\n"
            "/stats — view bot stats & revenue\n"
            "/broadcast — message everyone\n"
            "/dbstats — check DB storage\n"
            "/cleanup — free up space\n"
            "/import — bulk-import members\n"
            "/setmenuimage — set main menu image\n"
            "/removemenuimage — remove menu image\n"
            "/forcejoin — force join settings\n"
            "/buy — preview buyer flow\n\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯", parse_mode="Markdown")
        schedule_delete(message.chat.id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(user_id, reply)
    else:
        # No deep link, not the admin -> show the main hub (Premium Groups, Offers,
        # Contact) instead of dumping the full channel list on them immediately.
        show_main_menu(message.chat.id, user_id)

# --- USER: EXTRA COMMANDS (/buy, /myplans, /help) ---

@bot.message_handler(commands=['buy'])
def buy_handler(message):
    try:
        record_seen_user(message.from_user)
        user_id = getattr(message.from_user, 'id', None)
        chat_id = getattr(message.chat, 'id', None)
        print(f"[buy] chat={chat_id} user={user_id}")
        if not user_id or not chat_id:
            print("[buy] missing ids; aborting")
            return
        dismiss_previous(chat_id, user_id)
        show_all_channels(chat_id, user_id)
    except Exception as e:
        print(f"[buy] handler error: {e}")
        traceback.print_exc()
        try:
            bot.send_message(message.chat.id, "⚠️ Something went wrong. Please try again.")
        except Exception:
            pass

@bot.message_handler(commands=['myplans'])
def myplans_handler(message):
    user_id = message.from_user.id
    record_seen_user(message.from_user)
    subs = list(users_col.find({"user_id": user_id}))
    now = datetime.now().timestamp()

    markup = InlineKeyboardMarkup()
    lines = []
    for s in subs:
        ch = channels_col.find_one({"channel_id": s['channel_id']})
        ch_name = ch['name'] if ch else "Unknown Channel"
        if not ch:
            continue
        if s.get('lifetime'):
            lines.append(f"• *{ch_name}* — Lifetime access ♾️")
        else:
            remaining = s['expiry'] - now
            if remaining <= 0:
                continue
            if s.get('subscription_type') == 'free_trial':
                lines.append(f"• *{ch_name}* — Free Trial — expires in {format_time_left(remaining)}")
            else:
                lines.append(f"• *{ch_name}* — expires in {format_time_left(remaining)}")
        markup.add(InlineKeyboardButton(f"🔄 Renew — {ch_name}", callback_data=f"renew_{s['channel_id']}"))

    if not lines:
        send_command_reply(message, "🤷‍♂️ No active subscriptions right now.\n\nUse /buy to browse channels and grab one 🔥")
        return

    send_command_reply(message, "╭━━━ 📋 𝙔𝙊𝙐𝙍 𝙋𝙇𝘼𝙉𝙎 ━━━╮\n\n" + "\n".join(lines) + "\n\n╰━━━━━━━━━━━━━━━━━━━━╯", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('renew_'))
def cb_renew(call):
    """Renew button from /myplans — jumps straight to the plan picker for that channel."""
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        edit_menu(call.message.chat.id, call.message.message_id,
                  "❌ This channel is no longer available.",
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse")),
                  message_obj=call.message)
        return
    edit_plan_selection(call.message.chat.id, call.message.message_id, ch_data, call.from_user.id)

@bot.message_handler(commands=['users'])
def users_handler(message):
    """Admin-only /users command: displays a detailed list of all currently active
    subscribers from the database, including Telegram User ID, @username, first name,
    last name, subscribed plan/offer name, subscription start date, and expiry date/time."""
    if not message.from_user:
        return
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        send_command_reply(message, "⛔ *Access Denied.*\n\nThe `/users` command is restricted to configured bot administrators.", parse_mode="Markdown")
        return

    show_users_list(message.chat.id, user_id=message.from_user.id, message=message)


def show_users_list(chat_id, user_id=None, message=None, message_id=None, page=0, per_page=10):
    """Show active subscribers as a paginated inline-keyboard list for the /users command."""
    now_ts = datetime.now().timestamp()

    try:
        all_subs = list(users_col.find({}))
        active_subs = [s for s in all_subs if is_active_subscription(s, now_ts)]

        if not active_subs:
            text = "ℹ️ *No active subscribers found.*"
            if message:
                send_command_reply(message, text, parse_mode="Markdown")
            elif message_id:
                edit_menu(chat_id, message_id, text, parse_mode="Markdown")
            else:
                bot.send_message(chat_id, text, parse_mode="Markdown")
            return

        channel_map = {ch['channel_id']: ch for ch in channels_col.find({})}
        bundle_map = {b['bundle_id']: b for b in offer_bundles_col.find({})}

        unique_user_ids = {s['user_id'] for s in active_subs if s.get('user_id')}
        unique_active_users = len(unique_user_ids)
        total_active_subs = len(active_subs)

        active_subs.sort(key=lambda s: (s.get('user_id', 0), str(s.get('channel_id', ''))))

        entries = []
        for idx, sub in enumerate(active_subs, 1):
            uid = sub.get('user_id')
            seen = seen_users_col.find_one({"user_id": uid}) or {}

            raw_fname = seen.get('first_name') or sub.get('first_name')
            first_name = escape_markdown(raw_fname) if raw_fname else "N/A"

            raw_lname = seen.get('last_name') or sub.get('last_name')
            last_name = escape_markdown(raw_lname) if raw_lname else "N/A"

            raw_username = seen.get('username') or sub.get('username')
            username = f"@{escape_markdown(raw_username)}" if raw_username else "N/A"

            ch_id = sub.get('channel_id')
            ch_doc = channel_map.get(ch_id)
            if not ch_doc and ch_id is not None:
                try:
                    ch_doc = channels_col.find_one({"channel_id": int(ch_id)})
                except Exception:
                    pass
            ch_name = ch_doc.get('name') if ch_doc else (f"Channel {ch_id}" if ch_id else "General Access")

            sub_type = sub.get('subscription_type')
            if sub_type == 'bundle' and sub.get('bundle_id'):
                b_doc = bundle_map.get(sub.get('bundle_id')) or offer_bundles_col.find_one({"bundle_id": sub.get('bundle_id')})
                b_title = b_doc.get('title') if b_doc else 'Offer'
                plan_display = f"{b_title} ({ch_name})"
            elif sub_type == 'free_trial':
                plan_display = f"Free Trial ({ch_name})"
            else:
                plan_display = ch_name

            plan_display = escape_markdown(plan_display)

            start_dt = sub.get('start_date') or sub.get('created_at')
            if not start_dt:
                pay_doc = payments_col.find_one({"user_id": uid, "channel_id": ch_id}, sort=[("timestamp", -1)])
                if not pay_doc:
                    pay_doc = payments_col.find_one({"user_id": uid}, sort=[("timestamp", -1)])
                if pay_doc and pay_doc.get('timestamp'):
                    start_dt = pay_doc['timestamp']
                elif isinstance(sub.get('_id'), ObjectId):
                    start_dt = sub['_id'].generation_time

            if isinstance(start_dt, datetime):
                start_str = start_dt.strftime("%d %b %Y, %H:%M")
            elif isinstance(start_dt, (int, float)):
                start_str = datetime.fromtimestamp(start_dt).strftime("%d %b %Y, %H:%M")
            else:
                start_str = "N/A"

            if sub.get('lifetime'):
                expiry_str = "Lifetime ♾️"
            elif sub.get('expiry'):
                exp_dt = datetime.fromtimestamp(sub['expiry'])
                diff = sub['expiry'] - now_ts
                if diff > 0:
                    mins = int(diff // 60)
                    d, rem = divmod(mins, 1440)
                    h, m = divmod(rem, 60)
                    left = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
                    expiry_str = f"{exp_dt.strftime('%d %b %Y, %H:%M')} (in {left})"
                else:
                    expiry_str = f"{exp_dt.strftime('%d %b %Y, %H:%M')} (Expired)"
            else:
                expiry_str = "N/A"

            name_header = f"{first_name} {last_name}" if last_name != "N/A" else first_name
            entry = (
                f"👤 *{idx}. {name_header}* ({username})\n"
                f"  • 🆔 *User ID:* `{uid}`\n"
                f"  • 📛 *First Name:* {first_name} | *Last Name:* {last_name}\n"
                f"  • 🏷 *Username:* {username}\n"
                f"  • 😍 *Plan/Offer:* {plan_display}\n"
                f"  • 📅 *Start Date:* {start_str}\n"
                f"  • ⏳ *Expiry:* {expiry_str}\n\n"
            )
            entries.append(entry)

        total = len(entries)
        start = page * per_page
        end = min(start + per_page, total)
        page_entries = entries[start:end]

        header = (
            f"👥 *Active Subscribers Report*\n"
            f"📊 *Total Active Users:* {unique_active_users}\n"
            f"📋 *Total Active Subscriptions:* {total_active_subs}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        ) if page == 0 else (
            f"👥 *Active Subscribers Report (Page {page + 1})*\n"
            f"📊 *Total Active Users:* {unique_active_users}\n"
            f"📋 *Total Active Subscriptions:* {total_active_subs}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        text = header + "".join(page_entries)

        markup = InlineKeyboardMarkup()
        if total > per_page:
            markup.add(
                InlineKeyboardButton("⬅️ Prev", callback_data=f"userspage_{page-1}" if page > 0 else "noop"),
                InlineKeyboardButton("➡️ Next", callback_data=f"userspage_{page+1}" if end < total else "noop")
            )

        if message_id:
            edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
        elif message:
            send_command_reply(message, text, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        print(f"[users] error generating active subscribers report: {e}")
        traceback.print_exc()
        text = f"❌ An error occurred while fetching active subscribers: {e}"
        if message_id:
            edit_menu(chat_id, message_id, text, parse_mode=None)
        elif message:
            send_command_reply(message, text, parse_mode=None)
        else:
            bot.send_message(chat_id, text, parse_mode=None)


@bot.callback_query_handler(func=lambda call: call.data.startswith('userspage_'))
def cb_users_page(call):
    page = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    show_users_list(call.message.chat.id, user_id=call.from_user.id, message_id=call.message.message_id, page=page)


@bot.message_handler(commands=['help'])
def help_handler(message):
    record_seen_user(message.from_user)
    contact_url = contact_admin_url()
    markup = InlineKeyboardMarkup()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    send_command_reply(message,
        "╭━━━ ℹ️ 𝙃𝙊𝙒 𝙄𝙏 𝙒𝙊𝙍𝙆𝙎 ━━━╮\n\n"
        "1. Use /buy to see available channels\n"
        "2. Pick a channel and a plan\n"
        "3. Pay via UPI and tap 'I Have Paid'\n"
        "4. Send a screenshot of your receipt\n"
        "5. Wait for admin approval, then use your join link\n\n"
        "Use /myplans anytime to check your active subscriptions.\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯",
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
    edit_plan_selection(call.message.chat.id, call.message.message_id, ch_data, call.from_user.id)

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
    if count > 1:
        markup.add(InlineKeyboardButton("🔀 Reorder Channels", callback_data="chorder_menu"))

    text = "No channels found. Add one below 👇" if count == 0 else "Your Managed Channels:"
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
    msg = send_prompt(ADMIN_ID, "Make sure the bot is Admin in your channel, then FORWARD any message from that channel here.")
    bot.register_next_step_handler(msg, get_plans)

@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    # Reached via button, but this is now a prompt awaiting a forward -> never auto-vanish
    msg = send_prompt(ADMIN_ID, "FORWARD any message from your channel here.")
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

        existing_max = channels_col.find({"admin_id": ADMIN_ID}).sort("order", -1).limit(1)
        existing_max = list(existing_max)
        next_order = (existing_max[0].get('order', 0) + 1) if existing_max else 1
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"name": ch_name, "plans": plans_dict, "admin_id": ADMIN_ID, "order": next_order}}, upsert=True)
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
    markup.add(InlineKeyboardButton("✏️ Rename Channel", callback_data=f"renamech_{ch_id}"))
    markup.add(InlineKeyboardButton("✏️ Edit Plans", callback_data=f"editplans_{ch_id}"))
    markup.add(InlineKeyboardButton("🎁 Free Trials", callback_data=f"trials_{ch_id}"))
    markup.add(InlineKeyboardButton("📝 Edit About/Description", callback_data=f"editdesc_{ch_id}"))
    markup.add(InlineKeyboardButton("📸 Update Screenshot", callback_data=f"editss_{ch_id}"))
    markup.add(InlineKeyboardButton("🔀 Reorder Channels", callback_data="chorder_menu"))
    markup.add(InlineKeyboardButton(("▶️ Resume Channel" if ch_data.get('paused') else "⏸ Pause Channel"),
                                    callback_data=f"pausech_{ch_id}"))
    markup.add(InlineKeyboardButton("🗑 Delete Channel", callback_data=f"delch_{ch_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back to Channels", callback_data="back_channels"))

    ss_status = "✅ Screenshot set" if ch_data.get('screenshot_file_id') else "❌ No screenshot yet"
    desc_status = ch_data.get('description', 'None set')
    position = next((i for i, c in enumerate(get_sorted_channels(ADMIN_ID), start=1) if c['channel_id'] == ch_id), None)
    position_status = f"{RANK_BADGES.get(position, '')} #{position}".strip() if position else "Unset"
    pause_status = "⏸ Paused (waitlist open)" if ch_data.get('paused') else "▶️ Active"
    try:
        waitlist_count = waitlist_col.count_documents({"channel_id": ch_id})
    except Exception:
        waitlist_count = 0
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚙️ Settings for: *{ch_data['name']}*\n\n"
        f"🔗 Invite Link:\n`{link}`\n\n"
        f"📝 Description:\n_{desc_status}_\n\n"
        f"💰 Current Plans:\n{format_plans_text(ch_data)}\n\n"
        f"🔀 Position: {position_status}\n\n"
        f"⏯ Status: {pause_status}\n"
        f"👥 On waitlist: {waitlist_count}\n\n"
        f"🖼 {ss_status}",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pausech_'))
def cb_toggle_pause(call):
    """Admin pauses/resumes a channel. Pausing stops sales and lets users join a waitlist;
    resuming notifies everyone on the waitlist that the channel is back."""
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        send_admin_reply("❌ Channel not found.")
        return
    now_paused = not ch_data.get('paused')
    try:
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"paused": now_paused}})
    except Exception:
        send_admin_reply("❌ Couldn't update the channel.")
        return
    if now_paused:
        try:
            waitlist_count = waitlist_col.count_documents({"channel_id": ch_id})
        except Exception:
            waitlist_count = 0
        send_admin_reply(
            f"⏸ *{ch_data['name']}* is now paused. Users can no longer buy it — they can join the waitlist instead.\n\n"
            f"👥 Currently on the waitlist: {waitlist_count}",
            parse_mode="Markdown")
    else:
        notified = _notify_waitlist(ch_id, ch_data['name'])
        try:
            waitlist_col.delete_many({"channel_id": ch_id})
        except Exception:
            pass
        send_admin_reply(
            f"▶️ *{ch_data['name']}* is back! Notified {notified} user(s) on the waitlist.",
            parse_mode="Markdown")

def _notify_waitlist(ch_id, ch_name):
    """DMs every waitlisted user that a paused channel is available again. Returns the
    number of users successfully notified."""
    notified = 0
    try:
        bot_username = bot.get_me().username
    except Exception:
        bot_username = None
    link = f"https://t.me/{bot_username}?start={ch_id}" if bot_username else None
    for doc in waitlist_col.find({"channel_id": ch_id}):
        try:
            markup = InlineKeyboardMarkup()
            if link:
                markup.add(InlineKeyboardButton("😍 View Channel", url=link))
            bot.send_message(
                doc['user_id'],
                f"🔔 Good news! *{ch_name}* is back!\n\nTap below to check out the plans and grab a subscription.",
                reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
            notified += 1
        except Exception:
            pass
    return notified

@bot.callback_query_handler(func=lambda call: call.data.startswith('waitlist_'))
def cb_waitlist(call):
    """User taps 'Notify me when it's back' on a paused channel."""
    record_seen_user(call.from_user)
    try:
        ch_id = int(call.data.split('_', 1)[1])
    except (TypeError, ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid channel.")
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.")
        return
    try:
        waitlist_col.update_one(
            {"user_id": int(call.from_user.id), "channel_id": ch_id},
            {"$set": {"user_id": int(call.from_user.id), "channel_id": ch_id, "created_at": datetime.now()}},
            upsert=True
        )
        bot.answer_callback_query(call.id, "🔔 You'll be notified when it's back!")
    except Exception:
        bot.answer_callback_query(call.id, "Couldn't add you to the waitlist.")

@bot.callback_query_handler(func=lambda call: call.data == "chorder_menu")
def cb_channel_order_menu(call):
    """Entry point for the general channel reorder menu (reached from the main
    /channels list, since reordering needs the whole list in view)."""
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    _render_channel_order_menu(call)

def _render_channel_order_menu(call):
    """Shows every channel with ⬆️/⬇️ to move it. Top 3 by position always get a medal
    and appear first in the list users see — no separate ranking step needed."""
    channels = _ensure_channel_order(ADMIN_ID)
    n = len(channels)
    markup = InlineKeyboardMarkup()
    for i, ch in enumerate(channels, start=1):
        markup.row(InlineKeyboardButton(channel_button_label(ch, i), callback_data=f"manage_{ch['channel_id']}"))
        row = []
        if i > 1:
            row.append(InlineKeyboardButton("⬆️", callback_data=f"chmove_{ch['channel_id']}_up"))
        if i < n:
            row.append(InlineKeyboardButton("⬇️", callback_data=f"chmove_{ch['channel_id']}_down"))
        if row:
            markup.row(*row)
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_channels"))

    text = "🔀 *Reorder Channels*\n\nUse ⬆️/⬇️ to move a channel. Top 3 always get a medal and show first to users." if channels else "No channels found yet."
    edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('chmove_'))
def cb_channel_move(call):
    if not _require_admin(call):
        return
    _, ch_id_s, direction = call.data.split('_')
    ch_id = int(ch_id_s)
    channels = _ensure_channel_order(ADMIN_ID)
    idx = next((i for i, c in enumerate(channels) if c['channel_id'] == ch_id), None)
    if idx is not None:
        swap_idx = idx - 1 if direction == 'up' else idx + 1
        if 0 <= swap_idx < len(channels):
            a, b = channels[idx], channels[swap_idx]
            channels_col.update_one({"channel_id": a['channel_id']}, {"$set": {"order": b['order']}})
            channels_col.update_one({"channel_id": b['channel_id']}, {"$set": {"order": a['order']}})
    bot.answer_callback_query(call.id)
    _render_channel_order_menu(call)

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

@bot.callback_query_handler(func=lambda call: call.data.startswith('renamech_'))
def rename_channel_prompt(call):
    """Admin tapped 'Rename Channel' from the manage menu. This only changes the display
    name shown inside the bot (buttons, menus, messages) — it does NOT rename the actual
    Telegram channel/group itself."""
    ch_id = int(call.data.split('_', 1)[1])
    bot.answer_callback_query(call.id)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        send_admin_reply("❌ Channel not found (it may have been deleted.)")
        return
    msg = send_prompt(ADMIN_ID,
        f"✏️ Send the new display name for *{escape_markdown(ch_data['name'])}*.\n\n"
        f"_(This only changes the name shown inside the bot — not the actual Telegram channel name.)_",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_channel_rename, ch_id)

def save_channel_rename(message, ch_id):
    new_name = (message.text or "").strip()
    if not new_name:
        msg = send_prompt(ADMIN_ID, "❌ Please send a valid name (text only). Use /channels to try again.")
        bot.register_next_step_handler(msg, save_channel_rename, ch_id)
        return
    if len(new_name) > 128:
        msg = send_prompt(ADMIN_ID, "❌ That name is too long (max 128 characters). Please send a shorter one:")
        bot.register_next_step_handler(msg, save_channel_rename, ch_id)
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        send_admin_reply("❌ That channel no longer exists.")
        return
    old_name = ch_data['name']
    channels_col.update_one({"channel_id": ch_id}, {"$set": {"name": new_name}})
    send_admin_reply(f"✅ Renamed \"{old_name}\" to \"{new_name}\".")

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
    for t, pr in get_ordered_plan_items(ch_data):
        markup.add(InlineKeyboardButton(f"{format_label(t)} - ₹{pr}", callback_data=f"editplan_{ch_id}_{t}"))
    markup.add(InlineKeyboardButton("➕ Add New Plan", callback_data=f"addplan_{ch_id}"))
    if len(ch_data.get('plans') or {}) > 1:
        markup.add(InlineKeyboardButton("🔀 Reorder Plans", callback_data=f"planorder_{ch_id}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"manage_{ch_id}"))

    edit_menu(call.message.chat.id, call.message.message_id,
        f"✏️ Edit Plans for *{ch_data['name']}*\n\nTap a plan below to edit its price/duration, or add a new one:",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('planorder_'))
def cb_plan_order_menu(call):
    if not _require_admin(call):
        return
    ch_id = int(call.data.split('_', 1)[1])
    bot.answer_callback_query(call.id)
    _render_plan_order_menu(call, ch_id)

def _render_plan_order_menu(call, ch_id):
    """Shows every plan with ⬆️/⬇️ to move it. This order is what buyers see (free
    trials still always render above all of these, regardless of this order)."""
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        send_admin_reply("❌ Channel not found (it may have been deleted.)")
        return
    items = get_ordered_plan_items(ch_data)
    n = len(items)
    markup = InlineKeyboardMarkup()
    for i, (t, pr) in enumerate(items):
        markup.row(InlineKeyboardButton(f"{i+1}. {format_label(t)} - ₹{pr}", callback_data=f"editplan_{ch_id}_{t}"))
        row = []
        if i > 0:
            row.append(InlineKeyboardButton("⬆️", callback_data=f"planmove_{ch_id}_{t}_up"))
        if i < n - 1:
            row.append(InlineKeyboardButton("⬇️", callback_data=f"planmove_{ch_id}_{t}_down"))
        if row:
            markup.row(*row)
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"editplans_{ch_id}"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"🔀 *Reorder Plans* — {ch_data['name']}\n\nThis is the order buyers see (free trials always show above all plans, regardless of this order).",
        reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('planmove_'))
def cb_plan_move(call):
    if not _require_admin(call):
        return
    _, ch_id_s, t, direction = call.data.split('_')
    ch_id = int(ch_id_s)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.")
        return
    keys = [k for k, _ in get_ordered_plan_items(ch_data)]
    if t not in keys:
        bot.answer_callback_query(call.id, "Plan not found.")
        return
    idx = keys.index(t)
    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(keys):
        keys[idx], keys[swap_idx] = keys[swap_idx], keys[idx]
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"plan_order": keys}})
    bot.answer_callback_query(call.id)
    _render_plan_order_menu(call, ch_id)

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
    except Exception:
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
    # Keep its position in plan_order (rename shouldn't silently move it to the end)
    order = ch_data.get('plan_order') or []
    if old_t in order:
        order = [new_t if k == old_t else k for k in order]
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"plan_order": order}})
    send_admin_reply(f"✅ Duration updated to {format_label(new_t)} (price stays ₹{price}).")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delplan_'))
def delete_plan(call):
    _, ch_id, t = call.data.split('_')
    ch_id = int(ch_id)
    channels_col.update_one({"channel_id": ch_id}, {"$unset": {f"plans.{t}": ""}})
    channels_col.update_one({"channel_id": ch_id}, {"$pull": {"plan_order": t}})
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
        # New plans are appended at the end of the display order automatically since
        # get_ordered_plan_items() appends anything not yet in plan_order — no explicit
        # push needed here, this just keeps plan_order from drifting if it already exists.
        send_admin_reply(f"✅ New plan added: {format_label(total_minutes)} — ₹{price}")
    except Exception:
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
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse")),
                  message_obj=call.message)
        return
    # Option selected -> the plan-picker message instantly becomes the cart summary (no lingering message)
    text, markup = build_cart_summary(call.from_user.id)
    try:
        edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=call.message)
    except Exception as e:
        print(f"[cart_add] edit_menu failed: {e}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "cart_view")
def cart_view_handler(call):
    bot.answer_callback_query(call.id)
    text, markup = build_cart_summary(call.from_user.id)
    try:
        edit_menu(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="HTML", message_obj=call.message)
    except Exception as e:
        print(f"[cart_view] edit_menu failed: {e}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "cart_browse")
def cart_browse_handler(call):
    bot.answer_callback_query(call.id)
    try:
        edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message, back_to_menu=True)
    except Exception as e:
        print(f"[cart_browse] edit_all_channels failed: {e}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        show_all_channels(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "cart_clear_ask")
def cart_clear_ask_handler(call):
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Clear Cart", callback_data="cart_clear_confirm"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="cart_view"))
    try:
        edit_menu(call.message.chat.id, call.message.message_id, "⚠️ Clear your entire cart?", reply_markup=markup, message_obj=call.message)
    except Exception as e:
        print(f"[cart_clear_ask] edit_menu failed: {e}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "⚠️ Clear your entire cart?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "cart_clear_confirm")
def cart_clear_confirm_handler(call):
    user_carts[call.from_user.id] = []
    _clear_persisted_cart(call.from_user.id)
    bot.answer_callback_query(call.id, "Cart cleared.")
    try:
        edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message, back_to_menu=True)
    except Exception as e:
        print(f"[cart_clear_confirm] edit_all_channels failed: {e}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        show_all_channels(call.message.chat.id, call.from_user.id)


# --- USER: CHECKOUT & PAYMENT ---

QR_BRAND_HEX = (11, 83, 148)    # deep blue — the QR's data modules
QR_ACCENT_HEX = (5, 51, 102)    # darker navy — the three corner finder patterns
QR_BRAND_CSS = '#0B5394'
QR_ACCENT_CSS = '#053366'
QR_LOGO_TEXT = 'AB'
_BASE_DIR = os.path.dirname(os.path.abspath(globals()['__file__'])) if '__file__' in globals() else os.getcwd()
_BUNDLED_FONT = os.path.join(_BASE_DIR, 'assets', 'fonts', 'Poppins-Bold.ttf')

# Curated dark palettes for the payment QR. A different colour is picked at random
# on every QR generation so each invoice looks distinct while staying scannable.
QR_COLOR_PALETTES = [
    ((11, 83, 148), (5, 51, 102), '#0B5394', '#053366'),  # deep blue
    ((11, 100, 60), (4, 62, 36), '#0B643C', '#043E24'),  # forest green
    ((132, 20, 66), (82, 8, 38), '#841442', '#520826'),  # crimson
    ((90, 24, 154), (54, 12, 96), '#5A189A', '#360C60'),  # royal purple
    ((146, 58, 0), (92, 34, 0), '#923A00', '#5C2200'),   # burnt orange
    ((0, 77, 105), (0, 46, 64), '#004D69', '#002E40'),   # teal
    ((72, 45, 120), (42, 26, 72), '#482D78', '#2A1A48'), # violet
    ((104, 72, 0), (62, 42, 0), '#684800', '#3E2A00'),   # amber-brown
]

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

def _make_ab_logo(size, brand_hex=QR_BRAND_HEX):
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
        fill=(255, 255, 255, 255), outline=brand_hex + (255,), width=ring)

    # 3) Bold 'AB' in brand blue, optically centered
    font = _qr_logo_font(int(size * 0.46))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), QR_LOGO_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
              QR_LOGO_TEXT, fill=brand_hex + (255,), font=font)
    return img

def _make_payment_qr(upi_id, amount):
    """Generate a styled, logo-embedded payment QR (UPI deep link) as a BytesIO
    PNG stream ready for send_photo. High error correction keeps the centered
    'AB' logo from breaking scanning; a crisp scale + two-tone finder patterns
    give it a sharp, modern look. A random colour palette is picked on every
    call so each generated QR looks different."""
    data = f"upi://pay?pa={upi_id}&am={amount}&cu=INR"
    brand_hex, accent_hex, brand_css, accent_css = random.choice(QR_COLOR_PALETTES)
    buf = io.BytesIO()
    if segno is not None:
        segno.make(data, error='h').save(
            buf, kind='png', scale=14, border=3,
            dark=brand_css, light='#FFFFFF',
            finder_dark=accent_css, finder_light='#FFFFFF')
        buf.seek(0)
        img = Image.open(buf).convert('RGB')
    elif qrcode is not None:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=3)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=brand_css, back_color="white").convert('RGB')
    else:
        raise RuntimeError("No QR code library (segno or qrcode) available.")
    logo = _make_ab_logo(int(img.size[0] * 0.23), brand_hex=brand_hex)
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
        edit_all_channels(call.message.chat.id, call.message.message_id, user_id, back_to_menu=True)
        return

    total = cart_total(items)
    discount, grand_total = bundle_discount(items)
    cancel_delete(call.message.chat.id, call.message.message_id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    result = pending_checkouts_col.insert_one({
        "user_id": user_id, "items": items, "total": total,
        "discount": discount, "grand_total": grand_total,
        "created_at": datetime.now()
    })
    token = str(result.inserted_id)
    _clear_persisted_cart(user_id)
    lines = [f"• {escape(i['name'])} — {escape(format_label(i['t']))} — ₹{i['price']}" for i in items]
    caption = ("🧾 <b>Checkout Summary</b>\n" + "\n".join(lines) +
               f"\n\n💰 <b>Total to pay: ₹{grand_total}</b>\nUPI ID: <code>{UPI_ID}</code>\n\n"
               "<b><i>Complete the payment, then tap 'I Have Paid' and send your receipt screenshot 👇</i></b>")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"coutpaid_{token}"))
    markup.add(InlineKeyboardButton("❌ Cancel Payment", callback_data=f"coutcancel_{token}"))
    contact_url = contact_admin_url()
    if contact_url:
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    try:
        qr_file = _make_payment_qr(UPI_ID, grand_total)
        msg = bot.send_photo(call.message.chat.id, InputFile(qr_file, 'payment_qr.png'),
                             caption=caption, reply_markup=markup, parse_mode="HTML")
        schedule_delete(call.message.chat.id, msg.message_id, QR_SHOW_SECONDS)
    except Exception:
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
        _persist_cart(user_id)
        edit_menu(call.message.chat.id, call.message.message_id,
                  "❌ Couldn't show the payment QR right now. Please try again.",
                  reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("😍 Browse Channels", callback_data="cart_browse")),
                  message_obj=call.message)
        return

@bot.callback_query_handler(func=lambda call: call.data.startswith('obcheckout_'))
def bundle_checkout_handler(call):
    bundle_id = _safe_bundle_id(call.data.split('_', 1)[1])
    bundle = _bundle_doc(bundle_id)
    channels = _bundle_channels(bundle) if bundle and bundle.get('enabled') else []
    if not bundle or not channels or int(bundle.get('price', 0)) <= 0:
        bot.answer_callback_query(call.id, "This offer is no longer available.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _proceed_bundle_payment(call.message.chat.id, call.message.message_id, bundle)

def _send_bundle_preview(chat_id, message_id, bundle, channels):
    """Send a preview of the bundle with screenshots and descriptions before payment."""
    screenshot_file_ids = []
    description_parts = []
    
    for ch in channels:
        screenshot = ch.get('screenshot_file_id')
        if screenshot:
            screenshot_file_ids.append(screenshot)
        else:
            desc = ch.get('description')
            if desc:
                description_parts.append(f"😍 <b>{escape(ch.get('name', 'Channel'))}</b>\n{escape(desc)}")
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Proceed to Payment", callback_data=f"obpay_{bundle['bundle_id']}"))
    markup.add(InlineKeyboardButton("⬅️ Back to Offers", callback_data="main_obundles"))
    
    try:
        if screenshot_file_ids:
            # Send screenshots as media group (album)
            media_group = []
            for idx, file_id in enumerate(screenshot_file_ids):
                if idx == 0:
                    # First image gets the bundle info as caption
                    caption = (f"🎉 <b>{escape(bundle.get('title', 'Offer Preview'))}</b>\n\n"
                               f"<b>Included channels:</b> {len(channels)}\n\n"
                               "Swipe to see all channels 👇")
                    media_group.append(InputMediaPhoto(file_id, caption=caption, parse_mode="HTML"))
                else:
                    media_group.append(InputMediaPhoto(file_id))
            
            bot.send_media_group(chat_id, media_group)
            
            # Send descriptions for channels without screenshots
            if description_parts:
                desc_text = "\n\n".join(description_parts)
                bot.send_message(chat_id, desc_text, parse_mode="HTML")
            
            # Send the proceed button
            preview_msg = bot.send_message(chat_id, 
                f"💰 <b>Fixed price: ₹{int(bundle.get('price', 0))}</b>\n\n"
                "Tap below to proceed to payment 👇",
                reply_markup=markup, parse_mode="HTML")
        else:
            # No screenshots, just send descriptions
            if description_parts:
                desc_text = "\n\n".join(description_parts)
            else:
                desc_text = f"🎉 <b>{escape(bundle.get('title', 'Offer'))}</b>\n\nNo channel previews available."
            
            preview_msg = bot.send_message(chat_id, desc_text, reply_markup=markup, parse_mode="HTML")
        
        schedule_delete(chat_id, preview_msg.message_id, MENU_VANISH_SECONDS)
    except Exception as e:
        print(f"[bundle_preview] error: {e}")
        # Fallback: proceed directly to payment
        try:
            _proceed_bundle_payment(chat_id, message_id, bundle)
        except Exception as inner_e:
            print(f"[bundle_preview] fallback payment also failed: {inner_e}")
            edit_menu(chat_id, message_id, "❌ Couldn't load the offer preview. Please try again.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('obpay_'))
def bundle_proceed_payment_handler(call):
    bundle_id = _safe_bundle_id(call.data.split('_', 1)[1])
    bundle = _bundle_doc(bundle_id)
    channels = _bundle_channels(bundle) if bundle and bundle.get('enabled') else []
    if not bundle or not channels or int(bundle.get('price', 0)) <= 0:
        bot.answer_callback_query(call.id, "This offer is no longer available.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _proceed_bundle_payment(call.message.chat.id, call.message.message_id, bundle)

def _proceed_bundle_payment(chat_id, message_id, bundle):
    """Show the payment QR for the bundle."""
    channels = _bundle_channels(bundle)
    snapshot = [{'channel_id': int(ch['channel_id']), 'name': ch['name']} for ch in channels]
    doc = {
        'user_id': chat_id, 'bundle_id': bundle['bundle_id'],
        'bundle_title': bundle.get('title', 'Offer'), 'items': snapshot,
        'duration_minutes': bundle.get('duration_minutes'), 'amount': int(bundle['price']),
        'created_at': datetime.now()
    }
    token = str(pending_offer_bundle_checkouts_col.insert_one(doc).inserted_id)
    lines = '\n'.join(f"• {escape(i['name'])}" for i in snapshot)
    caption = (f"🧾 <b>{escape(bundle.get('title', 'Offer'))}</b>\n\n{escape(bundle.get('description') or '')}\n\n"
               f"<b>Included channels:</b>\n{lines}\n\n"
               f"⏱ Duration: <b>{escape(_bundle_duration_label(bundle))}</b>\n"
               f"💰 <b>Fixed price: ₹{int(bundle['price'])}</b>\nUPI ID: <code>{UPI_ID}</code>\n\n"
               "Complete the payment, tap 'I Have Paid', then send the receipt screenshot.")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"obpaid_{token}"))
    markup.add(InlineKeyboardButton("❌ Cancel Payment", callback_data=f"obcancel_{token}"))
    try:
        qr_file = _make_payment_qr(UPI_ID, int(bundle['price']))
        msg = bot.send_photo(chat_id, InputFile(qr_file, 'offer_payment_qr.png'),
                             caption=caption, reply_markup=markup, parse_mode='HTML')
        schedule_delete(chat_id, msg.message_id, QR_SHOW_SECONDS)
    except Exception:
        pending_offer_bundle_checkouts_col.delete_one({'_id': ObjectId(token)})
        edit_menu(chat_id, message_id, "❌ Couldn't show the payment QR. Please try again.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('obcancel_'))
def bundle_cancel_handler(call):
    token = call.data.split('_', 1)[1]
    pending_offer_bundle_checkouts_col.delete_one({'_id': ObjectId(token)})
    bot.answer_callback_query(call.id, "Payment cancelled.")
    _render_user_bundles(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obpaid_'))
def bundle_paid_handler(call):
    token = call.data.split('_', 1)[1]
    bot.answer_callback_query(call.id, "Send your payment screenshot now.")
    schedule_delete(call.message.chat.id, call.message.message_id, PAYMENT_VANISH_SECONDS)
    msg = send_prompt(call.message.chat.id, "📸 Drop the payment receipt screenshot now, or type /cancel.")
    bot.register_next_step_handler(msg, receive_bundle_screenshot, token)

def receive_bundle_screenshot(message, token):
    if message.text and message.text.strip().lower() in ('/cancel', 'cancel', '/stop', 'stop'):
        pending_offer_bundle_checkouts_col.delete_one({'_id': ObjectId(token), 'user_id': message.from_user.id})
        send_command_reply(message, "✅ Offer payment cancelled.")
        return
    doc = pending_offer_bundle_checkouts_col.find_one({'_id': ObjectId(token), 'user_id': message.from_user.id})
    if not doc:
        bot.send_message(message.chat.id, "❌ This offer checkout has expired or was already processed.")
        return
    if not message.photo:
        msg = send_prompt(message.chat.id, "❌ Please send a receipt screenshot, or type /cancel.")
        bot.register_next_step_handler(msg, receive_bundle_screenshot, token)
        return
    pending_offer_bundle_checkouts_col.update_one({'_id': ObjectId(token)}, {'$set': {
        'screenshot_file_id': message.photo[-1].file_id, 'user_chat_id': message.chat.id,
        'user_name': message.from_user.first_name, 'user_username': message.from_user.username,
    }})
    lines = '\n'.join(f"• {escape_markdown(i['name'])}" for i in doc.get('items', []))
    caption = ("🔔 *Offer Payment Verification Required!*\n\n"
               f"User: {escape_markdown(message.from_user.first_name)}\n"
               f"User ID: `{message.from_user.id}`\n\n{lines}\n\n"
               f"Offer: *{escape_markdown(doc.get('bundle_title', 'Offer'))}*\n"
               f"Fixed total: *₹{doc.get('amount', 0)}*")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve Offer", callback_data=f"obapp_{token}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"obrej_{token}"))
    try:
        admin_msg = bot.send_photo(ADMIN_ID, doc['screenshot_file_id'], caption=caption, reply_markup=markup, parse_mode='Markdown', vanish_delay=None)
    except Exception:
        admin_msg = bot.send_message(ADMIN_ID, caption, reply_markup=markup, parse_mode='Markdown', vanish_delay=None)
    conf = bot.send_message(message.chat.id, "✅ Offer receipt sent for admin approval. Please wait.", vanish_delay=None)
    pending_review_messages[token] = {'user_chat_id': message.chat.id, 'user_msg_id': conf.message_id,
                                      'admin_chat_id': ADMIN_ID, 'admin_msg_id': getattr(admin_msg, 'message_id', None)}

@bot.callback_query_handler(func=lambda call: call.data.startswith('obrej_'))
def bundle_reject_handler(call):
    token = call.data.split('_', 1)[1]
    doc = pending_offer_bundle_checkouts_col.find_one({'_id': ObjectId(token)})
    if doc:
        try: bot.send_message(doc['user_id'], "❌ Your offer payment could not be verified. Please contact the admin.")
        except Exception: pass
        pending_offer_bundle_checkouts_col.delete_one({'_id': ObjectId(token)})
    _clear_pending_review_messages(token)
    bot.answer_callback_query(call.id, "Rejected.")
    edit_caption_menu(call.message.chat.id, call.message.message_id, "❌ Rejected this offer checkout.", delay=None)

@bot.callback_query_handler(func=lambda call: call.data.startswith('obapp_'))
def bundle_approve_handler(call):
    token = call.data.split('_', 1)[1]
    doc = pending_offer_bundle_checkouts_col.find_one({'_id': ObjectId(token)})
    if not doc:
        bot.answer_callback_query(call.id, "Already processed or expired."); return
    bot.answer_callback_query(call.id, "Approving offer...")
    user_id = int(doc['user_id']); result_lines = []; errors = []
    duration = doc.get('duration_minutes')
    for item in doc.get('items', []):
        ch_id = int(item['channel_id']); name = item.get('name', str(ch_id))
        if not channels_col.find_one({'channel_id': ch_id, 'admin_id': ADMIN_ID}):
            errors.append(f"• {escape_markdown(name)}: channel is no longer managed"); continue
        try:
            try: bot.unban_chat_member(ch_id, user_id, only_if_banned=True)
            except Exception: pass
            if duration == 'lifetime':
                link = bot.create_chat_invite_link(ch_id, member_limit=1)
                users_col.update_one({'user_id': user_id, 'channel_id': ch_id}, {'$set': {
                    'expiry': None, 'lifetime': True, 'subscription_type': 'bundle',
                    'bundle_id': doc['bundle_id'], 'reminded_24h': True, 'reminded_1h': True}}, upsert=True)
                result_lines.append(f"• {escape_markdown(name)} — Lifetime\nJoin Link: {link.invite_link}")
            else:
                expiry = datetime.now() + timedelta(minutes=int(duration)); link = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=int(expiry.timestamp()))
                users_col.update_one({'user_id': user_id, 'channel_id': ch_id}, {'$set': {
                    'expiry': expiry.timestamp(), 'lifetime': False, 'subscription_type': 'bundle',
                    'bundle_id': doc['bundle_id'], 'reminded_24h': False, 'reminded_1h': False}}, upsert=True)
                result_lines.append(f"• {escape_markdown(name)} — {format_label(duration)}\nJoin Link: {link.invite_link}")
        except Exception as e:
            errors.append(f"• {escape_markdown(name)}: {e}")
    payments_col.insert_one({'user_id': user_id, 'channel_id': None, 'minutes': duration,
                             'amount': int(doc['amount']), 'bundle_id': doc['bundle_id'],
                             'purchase_type': 'bundle', 'timestamp': datetime.now()})
    counters_col.update_one({'_id': 'stats'}, {'$inc': {'total_sales': 1, 'total_revenue': int(doc['amount'])}}, upsert=True)
    pending_offer_bundle_checkouts_col.delete_one({'_id': ObjectId(token)})
    _clear_pending_review_messages(token)
    if result_lines:
        bot.send_message(user_id, "🥳 *Offer Payment Approved!*\n\n" + "\n\n".join(result_lines), parse_mode='Markdown', vanish_delay=None)
    if errors:
        bot.send_message(user_id, "⚠️ Some offer channels could not be activated:\n" + '\n'.join(errors))
    edit_caption_menu(call.message.chat.id, call.message.message_id, f"✅ Approved offer for user {user_id}.", delay=None)
@bot.callback_query_handler(func=lambda call: call.data.startswith('coutpaid_'))
def cout_paid_handler(call):
    token = call.data.split('_', 1)[1]
    record_seen_user(call.from_user)
    bot.answer_callback_query(call.id, "✅ Got it! Please send your payment screenshot now.")

    # Keep QR alive for PAYMENT_VANISH_SECONDS (2 min) after 'I Have Paid' is tapped so the user
    # can still scan it if they need to go back and complete the payment.
    schedule_delete(call.message.chat.id, call.message.message_id, PAYMENT_VANISH_SECONDS)

    # Awaiting the screenshot -> auto-vanish after 60 seconds
    msg = send_prompt(call.message.chat.id,
        "📸 Drop your payment receipt screenshot now.\n\n"
        "If you tapped 'I Have Paid' by mistake, type /cancel.")
    schedule_delete(call.message.chat.id, msg.message_id, DEFAULT_VANISH_SECONDS)
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
            "❌ That doesn't look like a photo. Send a screenshot image of your payment receipt.\n\n"
            "If you tapped 'I Have Paid' by mistake, type /cancel.")
        schedule_delete(message.chat.id, msg.message_id, DEFAULT_VANISH_SECONDS)
        bot.register_next_step_handler(msg, receive_cart_screenshot, token)
        return

    try:
        doc = pending_checkouts_col.find_one({"_id": ObjectId(token)})
    except Exception:
        doc = None
    if not doc:
        bot.send_message(message.chat.id, "❌ This checkout has expired or was already processed. Use /buy to start again.")
        return

    user = message.from_user
    file_id = message.photo[-1].file_id  # highest resolution version

    try:
        pending_checkouts_col.update_one(
            {"_id": ObjectId(token)},
            {"$set": {
                "screenshot_file_id": file_id,
                "user_chat_id": message.chat.id,
                "user_name": user.first_name,
                "user_username": user.username,
            }}
        )
    except Exception:
        pass

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve All", callback_data=f"coutapp_{token}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"coutrej_{token}"))

    username_tag = f"@{escape_markdown(user.username)}" if user.username else "No username"
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
        admin_msg = bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
    except Exception as e:
        # Photo delivery failed — fall back to a text summary for the admin instead of aborting
        try:
            text_caption = (
                "🔔 *Payment Verification Required!* (screenshot delivery failed)\n\n"
                f"User: {escape_markdown(user.first_name)} ({username_tag})\n"
                f"User ID: `{user.id}`\n\n"
                + "\n".join(lines) +
                f"\n\n💰 *Total: ₹{doc['total']}*"
            )
            admin_msg = bot.send_message(ADMIN_ID, text_caption, reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
        except Exception:
            admin_msg = None

    contact_url = contact_admin_url()
    u_markup = InlineKeyboardMarkup()
    if contact_url:
        u_markup.add(InlineKeyboardButton("📞 Contact Admin", url=contact_url))
    conf_msg = bot.send_message(message.chat.id, "✅ Receipt sent for verification!\n\nSit tight — admin will approve it in 5-10 mins ⏳", reply_markup=u_markup, vanish_delay=None)
    pending_review_messages[token] = {
        'user_chat_id': message.chat.id,
        'user_msg_id': conf_msg.message_id,
        'admin_chat_id': ADMIN_ID,
        'admin_msg_id': getattr(admin_msg, 'message_id', None),
    }

    # Cart has now moved into the pending checkout — clear it so a fresh /buy starts empty
    user_carts.pop(message.from_user.id, None)
    _clear_persisted_cart(message.from_user.id)

def _restore_cart_items(user_id, items):
    """Put cancelled checkout items back into the user's cart, without creating duplicates."""
    cart = get_cart(user_id)
    existing = {(i['channel_id'], i['t']) for i in cart}
    for i in items:
        key = (i['channel_id'], i['t'])
        if key not in existing:
            cart.append(i)
            existing.add(key)
    _persist_cart(user_id)

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
    edit_all_channels(call.message.chat.id, call.message.message_id, call.from_user.id, message_obj=call.message, back_to_menu=True)

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
    try:
        cancelled += pending_offer_bundle_checkouts_col.delete_many({"user_id": user_id}).deleted_count
    except Exception:
        pass
    user_carts.pop(user_id, None)
    _clear_persisted_cart(user_id)
    if cancelled:
        send_command_reply(message, "✅ Payment cancelled. Your pending checkout has been removed. Use /buy to start again.")
    else:
        send_command_reply(message, "🤷‍♂️ No pending payment right now.")

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
            bot.send_message(doc['user_id'], "❌ Your payment could not be verified. Please contact the admin for help.")
        except Exception:
            pass
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
    _clear_pending_review_messages(token)
    # Admin's decision confirmation is permanent — a record of what was rejected.
    edit_caption_menu(call.message.chat.id, call.message.message_id,
        "❌ Rejected this checkout.",
        delay=None)

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
            # remove_user_from_chat already unbans right after a kick, so this is just a safety
            # net for any user who was banned before that change.
            try:
                bot.unban_chat_member(ch_id, u_id, only_if_banned=True)
            except Exception:
                pass  # Not in channel / already not banned — safe to ignore

            if t == "lifetime":
                link = bot.create_chat_invite_link(ch_id, member_limit=1)  # single-use invite; expires on first join
                users_col.update_one(
                    {"user_id": u_id, "channel_id": ch_id},
                    {"$set": {"expiry": None, "lifetime": True, "reminded_24h": True, "reminded_1h": True}},
                    upsert=True
                )
                result_lines.append(f"• {escape_markdown(name)} — Lifetime ♾️\nJoin Link: {link.invite_link}")
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
                result_lines.append(f"• {escape_markdown(name)} — {format_label(t)}\nJoin Link: {link.invite_link}")

            # Log the sale for /stats (itemized, prunable via /cleanup)
            payments_col.insert_one({
                "user_id": u_id, "channel_id": ch_id, "minutes": t,
                "amount": price, "timestamp": datetime.now()
            })
            # Also bump lifetime counters — these stay accurate even after old payment logs are pruned
            counters_col.update_one({"_id": "stats"}, {"$inc": {"total_sales": 1, "total_revenue": price}}, upsert=True)
        except Exception as e:
            error_lines.append(f"• {escape_markdown(name)}: {e}")

    # Always close out the checkout so it can never stay stuck half-processed.
    user_carts.pop(u_id, None)
    _clear_persisted_cart(u_id)

    # Bundle discount: keep itemized logs (and lifetime counters) honest. Each item was
    # logged at full price; record the bundle discount as a negative adjustment row and
    # pull it out of the lifetime revenue total.
    discount = int(doc.get('discount') or 0)
    if discount:
        try:
            payments_col.insert_one({
                "user_id": u_id, "channel_id": None, "minutes": "bundle_discount",
                "amount": -discount, "timestamp": datetime.now()
            })
        except Exception:
            pass
        try:
            counters_col.update_one({"_id": "stats"}, {"$inc": {"total_revenue": -discount, "total_discount": discount}}, upsert=True)
        except Exception:
            pass

    try:
        pending_checkouts_col.delete_one({"_id": ObjectId(token)})
    except Exception:
        pass

    if result_lines:
        try:
            bot.send_message(u_id,
                "🥳 *Payment Approved!*\n\n" + "\n\n".join(result_lines) +
                "\n\n⚠️ Note: Each link/access expires per its own plan (unless marked Lifetime).\n\nEnjoyyy!!!",
                parse_mode="Markdown", vanish_delay=None)
        except Exception:
            try:
                bot.send_message(u_id,
                    "🥳 Payment Approved!\n\n" + "\n\n".join(result_lines) +
                    "\n\nNote: Each link/access expires per its own plan (unless marked Lifetime).", vanish_delay=None)
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
            bot.send_message(ADMIN_ID, "⚠️ Partial approval errors:\n" + "\n".join(error_lines))
        except Exception:
            pass

    _clear_pending_review_messages(token)

    # Admin's decision confirmation is permanent — a record of who/what was approved.
    try:
        edit_caption_menu(call.message.chat.id, call.message.message_id,
            f"✅ Approved checkout for user {u_id} ({len(doc['items'])} channel(s)).",
            delay=None)
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
    total_discount = counters.get("total_discount", 0)

    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    month_revenue = sum(p.get('amount', 0) for p in payments_col.find({"timestamp": {"$gte": month_start}}))

    text = (
        "📊 *Bot Stats*\n\n"
        f"😍 Channels: {total_channels}\n"
        f"👥 Active Subscriptions: {active_subs}\n"
        f"🧾 Total Sales: {total_sales}\n"
        f"💰 Total Revenue: ₹{total_revenue}\n"
        + (f"🎁 Bundle Discounts Given: ₹{total_discount}\n" if total_discount else "") +
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
        for name in ["channels", "users", "payments", "seen_users", "counters", "pending_checkouts",
                     "offer_bundles", "pending_offer_bundle_checkouts"]:
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

@bot.message_handler(commands=['clearreactcache'], func=lambda m: m.from_user.id == ADMIN_ID)
def clear_react_cache_handler(message):
    """Admin escape hatch: wipe the auto-react backlog (in-memory + persisted) so the
    bot stops slowly working through a large/stale queue and reacts promptly to new
    messages again. Anything cleared here simply never gets a reaction — it trades
    completeness for staying current, which is the right trade once a backlog has
    grown large enough that reacting to it late isn't useful anymore.

    Usage:
      /clearreactcache            -> clear the backlog for every chat
      /clearreactcache <chat_id>  -> clear the backlog for just one chat
      /reactcachestatus           -> check the current backlog size first (see below)"""
    dismiss_previous(message.chat.id, message.from_user.id)
    args = message.text.split()[1:]
    if args:
        try:
            chat_id = int(args[0].strip())
        except ValueError:
            send_command_reply(message,
                "❌ Invalid chat id. Usage: `/clearreactcache` (clears every chat) or "
                "`/clearreactcache <chat_id>` (clears just one chat).", parse_mode="Markdown")
            return
        mem, persisted = clear_reaction_cache(chat_id)
        send_command_reply(message,
            f"✅ Cleared reaction backlog for chat `{chat_id}`.\n"
            f"In-memory: {mem} | Persisted: {persisted}", parse_mode="Markdown")
    else:
        mem, persisted = clear_reaction_cache()
        send_command_reply(message,
            f"✅ Cleared the entire reaction backlog across all chats.\n"
            f"In-memory: {mem} | Persisted: {persisted}\n\n"
            f"New messages will start getting reactions promptly again.")

@bot.message_handler(commands=['reactcachestatus'], func=lambda m: m.from_user.id == ADMIN_ID)
def react_cache_status_handler(message):
    """Quick check of how backed-up the auto-react queue is before deciding whether
    to /clearreactcache."""
    dismiss_previous(message.chat.id, message.from_user.id)
    args = message.text.split()[1:]
    if args:
        try:
            chat_id = int(args[0].strip())
        except ValueError:
            send_command_reply(message, "❌ Invalid chat id. Usage: `/reactcachestatus <chat_id>`.", parse_mode="Markdown")
            return
        size = reaction_backlog_size(chat_id)
        send_command_reply(message, f"📊 Reaction backlog for chat `{chat_id}`: {size} message(s) waiting.", parse_mode="Markdown")
    else:
        with _reaction_queues_lock:
            per_chat = {cid: q.qsize() for cid, q in reaction_queues.items() if q.qsize() > 0}
        total = sum(per_chat.values())
        if not per_chat:
            send_command_reply(message, "📊 Reaction backlog: empty across all chats.")
            return
        lines = [f"📊 Reaction backlog: {total} message(s) waiting across {len(per_chat)} chat(s):"]
        for cid, size in sorted(per_chat.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"• `{cid}`: {size}")
        lines.append("\nUse `/clearreactcache <chat_id>` to clear one, or `/clearreactcache` to clear all.")
        send_command_reply(message, "\n".join(lines), parse_mode="Markdown")

def show_cleanup_menu(chat_id, message_id=None, user_id=None):
    now = datetime.now()
    payments_cutoff = now - timedelta(days=CLEANUP_PAYMENTS_DAYS)
    seenusers_cutoff = now - timedelta(days=CLEANUP_SEENUSERS_DAYS)

    old_payments_count = payments_col.count_documents({"timestamp": {"$lt": payments_cutoff}})
    old_seenusers_count = seen_users_col.count_documents({"last_seen": {"$lt": seenusers_cutoff}})

    text = (
        "╭━━━ 🧹 𝙁𝙍𝙀𝙀 𝙐𝙋 𝙎𝙋𝘼𝘾𝙀 ━━━╮\n\n"
        f"🧾 Old payment logs (older than {CLEANUP_PAYMENTS_DAYS} days): *{old_payments_count}* records\n"
        "   _(Total Sales/Revenue in /stats stay intact — tracked separately and won't change.)_\n\n"
        f"👤 Inactive users (not seen in {CLEANUP_SEENUSERS_DAYS}+ days): *{old_seenusers_count}* records\n"
        "   _(Only affects /broadcast reach — active subscribers are never touched.)_\n\n"
        "Tap below to delete a category. Can't be undone.\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
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

@bot.message_handler(commands=['pending'], func=lambda m: m.from_user.id == ADMIN_ID)
def pending_checkouts_handler(message):
    """Show all pending checkouts awaiting admin approval, with screenshot if available."""
    try:
        pending = list(pending_checkouts_col.find({}).sort("created_at", -1))
    except Exception:
        pending = []

    if not pending:
        send_command_reply(message, "✅ No pending checkouts.")
        return

    for doc in pending:
        token = str(doc['_id'])
        user_id = doc.get('user_id')
        user_chat_id = doc.get('user_chat_id', user_id)
        user_name = doc.get('user_name', 'User')
        user_username = doc.get('user_username')
        items = doc.get('items', [])
        total = doc.get('total', 0)
        screenshot_file_id = doc.get('screenshot_file_id')

        username_tag = f"@{escape_markdown(user_username)}" if user_username else "No username"
        lines = [f"• {escape_markdown(i['name'])} — {format_label(i['t'])} — ₹{i['price']}" for i in items]
        caption = (
            f"🔔 *Pending Checkout*\n\n"
            f"User: {escape_markdown(user_name)} ({username_tag})\n"
            f"User ID: `{user_id}`\n\n"
            + "\n".join(lines) +
            f"\n\n💰 *Total: ₹{total}*"
        )

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"coutapp_{token}"))
        markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"coutrej_{token}"))

        try:
            if screenshot_file_id:
                msg = bot.send_photo(ADMIN_ID, screenshot_file_id, caption=caption, reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
            else:
                msg = bot.send_message(ADMIN_ID, caption, reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
            pending_review_messages[token] = {
                'user_chat_id': user_chat_id,
                'user_msg_id': None,
                'admin_chat_id': ADMIN_ID,
                'admin_msg_id': msg.message_id,
            }
        except Exception as e:
            try:
                bot.send_message(ADMIN_ID, f"❌ Failed to show pending checkout {token}: {e}")
            except Exception:
                pass

_bc_pending_text = {}  # ADMIN_ID -> text typed before asking for the schedule time

def _show_broadcast_menu(chat_id, message_id=None):
    """Admin menu for /broadcast: send now, schedule later, or list scheduled ones."""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📨 Broadcast Now", callback_data="bcnow"))
    markup.add(InlineKeyboardButton("⏰ Schedule for Later", callback_data="bcsched"))
    markup.add(InlineKeyboardButton("📋 Scheduled Broadcasts", callback_data="bclist"))
    text = ("╭━━━ 📣 𝘽𝙍𝙊𝘼𝘿𝘾𝘼𝙎𝙏 ━━━╮\n\n"
            "Shoot a message to everyone who's ever used this bot 👇\n\n"
            "Send it now, or schedule it for later ⏰\n\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯")
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        dismiss_previous(chat_id, ADMIN_ID)
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        schedule_delete(chat_id, reply.message_id, MENU_VANISH_SECONDS)
        track_msg(ADMIN_ID, reply)

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast_start(message):
    _show_broadcast_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "bcnow")
def cb_bc_now(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    # Awaiting the broadcast text -> prompt never auto-vanishes
    msg = send_prompt(ADMIN_ID, "Drop the broadcast message you want to send to everyone:")
    bot.register_next_step_handler(msg, do_broadcast)

@bot.callback_query_handler(func=lambda call: call.data == "bcsched")
def cb_bc_sched(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    msg = send_prompt(ADMIN_ID, "Send the broadcast text first (plain text only):")
    bot.register_next_step_handler(msg, _bc_sched_time)

def _bc_sched_time(message):
    if not message.text:
        send_admin_reply("❌ Please send plain text for the scheduled broadcast. Use /broadcast to try again.")
        return
    _bc_pending_text[ADMIN_ID] = message.text
    msg = send_prompt(ADMIN_ID,
        "⏰ <b>When should this drop?</b>\n\n"
        "Accepted formats:\n"
        "• `18:00` — today at that time (or tomorrow if already past)\n"
        "• `2026-08-20 18:00` — exact date & time\n"
        "• `30` — minutes from now\n\n"
        "Send the time now:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, _bc_sched_save)

def _bc_parse_time(raw):
    """Parse a user-typed broadcast time into an epoch timestamp, or None if invalid."""
    raw = (raw or '').strip()
    if not raw:
        return None
    now = datetime.now()
    # plain minutes from now: "30"
    if raw.isdigit():
        return int(time.time()) + int(raw) * 60
    # "in N minutes" / "now + N h" / "N hours" / "10m" ...
    m = re.match(r'^(?:in\s+|now\s*\+\s*|\+\s*)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$', raw, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * 60 if unit.startswith('m') else n * 3600
        return int(time.time()) + seconds
    # HH:MM (today, or tomorrow if already past)
    m = re.match(r'^(\d{1,2}):(\d{2})$', raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return int(when.timestamp())
    # YYYY-MM-DD HH:MM
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$', raw)
    if m:
        try:
            when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)))
        except ValueError:
            return None
        return int(when.timestamp())
    return None

def _bc_sched_save(message):
    text = _bc_pending_text.pop(ADMIN_ID, None)
    if text is None:
        send_admin_reply("❌ Broadcast flow expired — start again with /broadcast.")
        return
    if not message.text:
        send_admin_reply("❌ Please send a time. Use /broadcast to try again.")
        return
    when_ts = _bc_parse_time(message.text)
    if when_ts is None:
        send_admin_reply("❌ Couldn't parse that time.\n\nFormats: `18:00`, `2026-08-20 18:00`, or `30` (minutes). Use /broadcast to try again.", parse_mode="Markdown")
        return
    if when_ts <= int(time.time()):
        send_admin_reply("❌ That time is in the past — pick a future time. Use /broadcast to try again.")
        return
    try:
        res = scheduled_broadcasts_col.insert_one({
            "text": text,
            "when_ts": int(when_ts),
            "created_at": datetime.now(),
            "status": "pending",
        })
    except Exception as e:
        send_admin_reply(f"❌ Couldn't save the scheduled broadcast: {e}")
        return
    when_dt = datetime.fromtimestamp(when_ts)
    preview = text if len(text) <= 100 else text[:97] + "..."
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel Broadcast", callback_data=f"bcdel_{res.inserted_id}"))
    send_admin_reply(
        f"✅ *Broadcast scheduled* for {when_dt.strftime('%Y-%m-%d %H:%M')}.\n\n"
        f"Preview:\n{escape_markdown(preview)}",
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "bclist")
def cb_bc_list(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    show_scheduled_broadcasts(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "bc_back")
def cb_bc_back(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    _show_broadcast_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('bcdel_'))
def cb_bc_del(call):
    if not _require_admin(call):
        return
    bot.answer_callback_query(call.id)
    oid = call.data.split('_', 1)[1]
    try:
        doc = scheduled_broadcasts_col.find_one({"_id": ObjectId(oid)})
        scheduled_broadcasts_col.delete_one({"_id": ObjectId(oid)})
    except Exception:
        doc = None
    if doc and doc.get('status') == 'pending':
        send_admin_reply("✅ Scheduled broadcast cancelled.")
    else:
        send_admin_reply("❌ That broadcast is already sent or doesn't exist.")

@bot.message_handler(commands=['listbroadcasts'], func=lambda m: m.from_user.id == ADMIN_ID)
def listbroadcasts_handler(message):
    dismiss_previous(message.chat.id, message.from_user.id)
    show_scheduled_broadcasts(message.chat.id)

def show_scheduled_broadcasts(chat_id, message_id=None):
    """Lists pending scheduled broadcasts (with a cancel button each) plus a few of the
    most recent ones already sent."""
    pending = list(scheduled_broadcasts_col.find({"status": "pending"}).sort("when_ts", 1))
    recent = list(scheduled_broadcasts_col.find({"status": "sent"}).sort("sent_at", -1).limit(5))

    markup = InlineKeyboardMarkup()
    if pending:
        lines = ["⏰ *Scheduled Broadcasts*\n"]
        for d in pending:
            when_dt = datetime.fromtimestamp(d['when_ts'])
            preview = (d.get('text') or '').replace('\n', ' ')[:45]
            lines.append(f"• {when_dt.strftime('%m-%d %H:%M')} — _{escape_markdown(preview)}_")
            markup.add(InlineKeyboardButton(f"❌ Cancel — {when_dt.strftime('%m-%d %H:%M')}", callback_data=f"bcdel_{d['_id']}"))
        text = "\n".join(lines)
    else:
        text = "📭 No scheduled broadcasts pending."

    if recent:
        text += "\n\n_Recently sent:_"
        for d in recent:
            when_dt = datetime.fromtimestamp(d.get('when_ts') or 0)
            preview = (d.get('text') or '').replace('\n', ' ')[:40]
            text += f"\n• {when_dt.strftime('%m-%d %H:%M')} — {escape_markdown(preview)}"

    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="bc_back"))
    if message_id:
        edit_menu(chat_id, message_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        reply = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        schedule_delete(chat_id, reply.message_id, COMMAND_VANISH_SECONDS)
        track_msg(ADMIN_ID, reply)

def run_scheduled_broadcasts():
    """Scheduler job: sends every scheduled broadcast whose time has come. Marks each as
    sent atomically so a crash/re-run can never double-send the same message."""
    now_ts = int(time.time())
    try:
        due = list(scheduled_broadcasts_col.find({"status": "pending", "when_ts": {"$lte": now_ts}}))
    except Exception:
        return
    for doc in due:
        try:
            res = scheduled_broadcasts_col.update_one(
                {"_id": doc["_id"], "status": "pending"},
                {"$set": {"status": "sent", "sent_at": datetime.now()}}
            )
            if res.modified_count == 0:
                continue  # already sent by a concurrent run
        except Exception:
            continue
        text = doc.get('text', '')
        sent, failed = 0, 0
        errors = []
        try:
            user_ids = seen_users_col.distinct("user_id")
        except Exception:
            user_ids = []
        for uid in user_ids:
            try:
                bot.send_message(uid, text, vanish_delay=None)
                sent += 1
            except Exception as e:
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{uid}: {e}")
        when_dt = datetime.fromtimestamp(doc.get('when_ts') or now_ts)
        result = f"✅ Scheduled broadcast ({when_dt.strftime('%Y-%m-%d %H:%M')}) sent to {sent} users. Failed: {failed}."
        if errors:
            result += "\n\nFailure details (first 5):\n" + "\n".join(errors)
        try:
            bot.send_message(ADMIN_ID, result, vanish_delay=None)
        except Exception:
            pass

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
            # vanish_delay=None: a broadcast is content the recipient should keep, not a
            # transient bot menu/prompt — without this it silently inherits the global
            # 90s auto-delete default from the send_message wrapper.
            bot.send_message(uid, message.text, vanish_delay=None)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(f"{uid}: {e}")

    result = f"✅ Broadcast sent to {sent} users. Failed: {failed}."
    if errors:
        # Show up to 5 concrete error reasons so you can see WHY sends failed (e.g. blocked bot)
        result += "\n\nFailure details (first 5):\n" + "\n".join(errors[:5])
    # Broadcast result is a permanent record — never auto-vanishes. bot.send_message is
    # globally wrapped (see _vanishing_send_message) to auto-schedule deletion after
    # DEFAULT_VANISH_SECONDS unless vanish_delay=None is passed explicitly — a plain call
    # here still gets swept up by that default, so this MUST be passed every time.
    try:
        bot.send_message(ADMIN_ID, result, vanish_delay=None)
    except Exception:
        pass

def show_active_users(chat_id, user_id=None, message=None, page=0, per_page=20):
    """Show active subscribers as inline buttons for the admin to remove.
    This is the fallback when /removeuser is used without arguments."""
    now = datetime.now().timestamp()
    admin_channel_ids = _admin_channel_ids()
    if not admin_channel_ids:
        text = "ℹ️ No channels are registered for this admin yet."
        if message:
            send_command_reply(message, text)
        else:
            bot.send_message(chat_id, text)
        return

    subs = []
    for s in users_col.find({}):
        if not is_active_subscription(s, now):
            continue
        try:
            ch_id = int(s['channel_id'])
            if ch_id in admin_channel_ids:
                subs.append(s)
        except (TypeError, ValueError):
            continue

    if not subs:
        text = "ℹ️ No active subscribers found to remove."
        if message:
            send_command_reply(message, text)
        else:
            bot.send_message(chat_id, text)
        return

    subs.sort(key=lambda x: (x.get('channel_id', ''), x.get('user_id', 0)))
    total = len(subs)
    start = page * per_page
    end = min(start + per_page, total)
    page_subs = subs[start:end]

    lines = [f"👤 Active subscribers (showing {start+1}-{end} of {total}):"]
    for s in page_subs:
        ch_name = "Unknown channel"
        try:
            ch = channels_col.find_one({"channel_id": int(s['channel_id'])})
            if ch:
                ch_name = ch.get('name') or f"Channel {s['channel_id']}"
        except Exception:
            pass

        uid = s.get('user_id')
        uname = s.get('username') or ""
        name = escape_markdown(uname or str(uid))
        ch_label = escape_markdown(ch_name)
        display = f"@{uname}" if uname else f"`{uid}`"
        lines.append(f"• {display} — {ch_label}")

    text = "\n".join(lines)

    markup = InlineKeyboardMarkup()
    for s in page_subs:
        uid = s.get('user_id')
        uname = s.get('username')
        ch_id = s.get('channel_id')
        ch_name = "?"
        try:
            ch = channels_col.find_one({"channel_id": int(ch_id)})
            if ch:
                ch_name = ch.get('name') or "?"
        except Exception:
            pass
        label = f"@{uname}" if uname else f"{uid}"
        sublabel = f"{label} ({ch_name})"
        if len(sublabel) > 30:
            sublabel = label
        markup.add(InlineKeyboardButton(f"❌ {sublabel}", callback_data=f"rmuser_{uid}"))

    if total > per_page:
        markup.add(InlineKeyboardButton("⬅️ Prev", callback_data=f"rmuserpage_{page-1}" if page > 0 else "noop"),
                   InlineKeyboardButton("➡️ Next", callback_data=f"rmuserpage_{page+1}" if end < total else "noop"))

    if message:
        send_command_reply(message, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


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

    show_active_users(message.chat.id, user_id=message.from_user.id, message=message)


@bot.callback_query_handler(func=lambda call: call.data == "noop")
def cb_noop(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('rmuserpage_'))
def cb_rmuser_page(call):
    page = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    show_active_users(call.message.chat.id, user_id=call.from_user.id, message=None, page=page)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith('rmuser_'))
def cb_rmuser_confirm(call):
    user_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Remove", callback_data=f"rmuserconfirm_{user_id}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="rmuser_cancel"))
    edit_menu(call.message.chat.id, call.message.message_id,
        f"⚠️ Are you sure you want to remove user `{user_id}`? This will kick them from all subscribed channels.",
        reply_markup=markup, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: call.data == "rmuser_cancel")
def cb_rmuser_cancel(call):
    bot.answer_callback_query(call.id)
    show_active_users(call.message.chat.id, user_id=call.from_user.id, message=None, page=0)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith('rmuserconfirm_'))
def cb_rmuser_do(call):
    user_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id, "Removing...")
    admin_channel_ids = _admin_channel_ids()
    subs = []
    for s in users_col.find({"user_id": user_id}):
        try:
            if int(s['channel_id']) in admin_channel_ids:
                subs.append(s)
        except (TypeError, ValueError):
            continue

    if not subs:
        edit_menu(call.message.chat.id, call.message.message_id,
            f"⚠️ No active subscriptions found for user `{user_id}`.",
            reply_markup=None, parse_mode="Markdown")
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

    try:
        rev_msg = bot.send_message(user_id, "⚠️ Your subscription access has been revoked by the admin.")
        schedule_delete(user_id, rev_msg.message_id, COMMAND_VANISH_SECONDS)
    except Exception:
        pass

    msg = f"✅ Removed subscription for user `{user_id}` ({count} channel subscription(s) cleared)."
    if failed:
        msg += "\n\n⚠️ Some bans failed:\n" + "\n".join(failed[:5])

    edit_menu(call.message.chat.id, call.message.message_id, msg, reply_markup=None, parse_mode="Markdown")


@bot.message_handler(commands=['remove'])
@bot.channel_post_handler(commands=['remove'])
def group_remove_handler(message):
    """In groups/channels: any chat admin can kick members. In private DM: same as /removeuser for bot owner."""
    if getattr(message.chat, 'type', None) == 'private':
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

    unresolved = total - saved
    summary = (
        f"✅ *Import Complete!*\n"
        f"📊 Stored *{saved}* entr{'y' if saved == 1 else 'ies'} for chat `{chat_id}`"
        f"{f' ({unresolved} pending username resolution)' if unresolved else ''}."
    )
    if is_chat:
        _safe_reply(message, summary, parse_mode="Markdown")
    else:
        send_command_reply(message, summary, parse_mode="Markdown")

# --- ABANDONED CART NUDGE ---
def send_abandoned_cart_nudges():
    """One gentle reminder to users who added items to their cart but never checked out
    within CART_NUDGE_MINUTES of the last cart update. Fires once per cart; carts stale
    for CART_MAX_AGE_DAYS get pruned."""
    now = datetime.now()
    cutoff = now - timedelta(minutes=CART_NUDGE_MINUTES)
    stale_cutoff = now - timedelta(days=CART_MAX_AGE_DAYS)
    try:
        for doc in carts_col.find({"updated_at": {"$lte": cutoff}, "nudged": {"$ne": True}}):
            try:
                items = doc.get('items') or []
                if not items:
                    continue
                names = ", ".join(i.get('name', '?') for i in items[:3])
                total = cart_total(items)
                discount, grand_total = bundle_discount(items)
                extra = f"\n🎁 Bundle deal: you save ₹{discount} with 3+ channels!" if discount else ""
                text = (
                    "Hey, you left some channels in your cart and never finished checkout. 🛒\n\n"
                    f"{names}{'…' if len(items) > 3 else ''}\n\n"
                    f"💰 Total: ₹{grand_total}{extra}\n\n"
                    "Tap below to review your cart and complete your purchase."
                )
                markup = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🛒 View Cart", callback_data="cart_view")
                )
                try:
                    bot.send_message(doc['user_id'], text, reply_markup=markup, vanish_delay=None)
                except Exception:
                    pass
                carts_col.update_one({"_id": doc['_id']}, {"$set": {"nudged": True, "nudge_sent_at": datetime.now()}})
            except Exception:
                continue
        carts_col.delete_many({"updated_at": {"$lte": stale_cutoff}})
    except Exception:
        pass

# --- WIN-BACK FOR EXPIRED USERS ---
def backfill_expired_subs():
    """One-time: derive lapsed subscriptions from historical payment logs so the win-back
    DM can reach users whose subs expired before this feature shipped. Only fills records
    that lapsed within the recent WINBACK_LOOKBACK_DAYS window (and more than
    WINBACK_AFTER_DAYS ago), to avoid DMing stale contacts from months back."""
    try:
        if expired_subs_col.count_documents({}) > 0:
            return
    except Exception:
        return
    lookback = datetime.now() - timedelta(days=WINBACK_LOOKBACK_DAYS)
    min_lapse = datetime.now() - timedelta(days=WINBACK_AFTER_DAYS)
    for p in payments_col.find({}):
        try:
            minutes = p.get('minutes')
            if not minutes or str(minutes).lower() in ('lifetime', 'bundle_discount'):
                continue
            ts = p.get('timestamp')
            if not ts:
                continue
            expiry_ts = int(ts.timestamp()) + int(minutes) * 60
            if expiry_ts <= lookback.timestamp() or expiry_ts > min_lapse.timestamp():
                continue
            ch_id = p.get('channel_id')
            u_id = p.get('user_id')
            if ch_id is None or u_id is None:
                continue
            expired_subs_col.update_one(
                {"user_id": int(u_id), "channel_id": int(ch_id)},
                {"$set": {"user_id": int(u_id), "channel_id": int(ch_id),
                          "expired_at": datetime.fromtimestamp(expiry_ts)},
                 "$setOnInsert": {"winback_sent": False}},
                upsert=True
            )
        except Exception:
            continue

def send_winback_offers():
    """Auto-DM users whose paid subscription lapsed WINBACK_AFTER_DAYS+ days ago with a
    gentle 'come back' offer. Fires once per (user, channel)."""
    try:
        bot_username = bot.get_me().username
    except Exception:
        bot_username = None
    cutoff = datetime.now() - timedelta(days=WINBACK_AFTER_DAYS)
    try:
        for doc in expired_subs_col.find({"expired_at": {"$lte": cutoff}, "winback_sent": {"$ne": True}}):
            try:
                ch = channels_col.find_one({"channel_id": int(doc.get('channel_id'))})
            except Exception:
                ch = None
            ch_name = ch['name'] if ch else "your subscription"
            ch_id = doc.get('channel_id')
            if ch_id is None:
                continue
            markup = InlineKeyboardMarkup()
            if bot_username:
                rejoin_url = f"https://t.me/{bot_username}?start={ch_id}"
                markup.add(InlineKeyboardButton("🔄 Come Back", url=rejoin_url))
            body = (
                f"👋 We miss you!\n\n"
                f"Your subscription to *{ch_name}* lapsed a few days ago. "
                f"Come back and jump right in — plans are still available.\n\n"
                f"*See you there!*"
            )
            try:
                bot.send_message(doc['user_id'], body, reply_markup=markup, parse_mode="Markdown", vanish_delay=None)
            except Exception:
                pass
            # Mark as sent even if the DM failed (blocked bot / unreachable), so it's never retried.
            try:
                expired_subs_col.update_one({"_id": doc['_id']}, {"$set": {"winback_sent": True, "winback_sent_at": datetime.now()}})
            except Exception:
                pass
    except Exception:
        pass

# Automate Kicking
def kick_expired_users():
    now = datetime.now().timestamp()
    # Free trials: mark claims whose access window has ended as 'expired'.
    # Claims are PERMANENT — they are never deleted, so the one-trial-ever rule
    # holds even after the trial ends, the user leaves, or the bot restarts.
    try:
        free_trial_claims_col.update_many(
            {"status": "active", "trial_expiry": {"$lte": datetime.now()}},
            {"$set": {"status": "expired", "expired_at": datetime.now()}}
        )
    except Exception:
        pass
    # Lifetime subscribers have expiry=None and are never kicked
    expired_users = list(users_col.find({"expiry": {"$lte": now}, "lifetime": {"$ne": True}}))
    try:
        bot_username = bot.get_me().username
    except Exception:
        bot_username = None

    for user in expired_users:
        try:
            # Record the lapse first (for the 3-day win-back DM) — before the users_col
            # record is removed below.
            try:
                ch_id = user.get('channel_id')
                if ch_id is not None:
                    expired_subs_col.update_one(
                        {"user_id": int(user['user_id']), "channel_id": int(ch_id)},
                        {"$set": {"user_id": int(user['user_id']), "channel_id": int(ch_id),
                                  "expired_at": datetime.fromtimestamp(user.get('expiry') or now)},
                         "$setOnInsert": {"winback_sent": False}},
                        upsert=True
                    )
            except Exception:
                pass
            removed, detail = _kick_from_group(user['channel_id'], user['user_id'])

            rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}" if bot_username else f"https://t.me/{bot_username}"
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Re-join / Renew", url=rejoin_url))
            try:
                bot.send_message(user['user_id'], "⚠️ Your subscription has expired.\n\nTo join again or renew, please click the button below:", reply_markup=markup)
            except Exception:
                pass

            # _kick_from_group already deletes the users_col record when removal
            # succeeds; if it failed (e.g. bot lost admin rights), still drop the
            # record so an un-kickable stale entry doesn't get retried forever.
            if not removed:
                users_col.delete_one({"_id": user['_id']})
        except Exception:
            pass

# Automate Expiry Reminders (24h and 1h before a plan expires)
def send_expiry_reminders():
    now = datetime.now().timestamp()
    try:
        bot_username = bot.get_me().username
    except Exception:
        bot_username = None

    def _notify(user, remaining, flag_field):
        ch = channels_col.find_one({"channel_id": user['channel_id']})
        ch_name = ch['name'] if ch else "your channel"
        rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}" if bot_username else f"https://t.me/{bot_username}"
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Renew Now", url=rejoin_url))
        if user.get('subscription_type') == 'free_trial':
            body = (f"⏰ *Reminder:* your free trial to *{ch_name}* expires in {format_time_left(remaining)}.\n\n"
                    f"Upgrade to a paid plan so you don't lose access!")
        else:
            body = (f"⏰ *Reminder:* your subscription to *{ch_name}* expires in {format_time_left(remaining)}.\n\n"
                    f"Renew now so you don't lose access!")
        try:
            bot.send_message(
                user['user_id'],
                body,
                reply_markup=markup, parse_mode="Markdown"
            )
        except Exception:
            pass
        # Mark as sent even if the DM failed (e.g. user blocked the bot), so we don't retry forever
        try:
            users_col.update_one({"_id": user['_id']}, {"$set": {flag_field: True}})
        except Exception:
            pass

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
def _migrate_trial_claim_unique_index():
    """Migrate the one-trial-ever guarantee from the legacy 3-field unique index
    (user_id, channel_id, trial_plan_id) to the new 2-field unique index
    (user_id, channel_id).

    Restart-safe and idempotent: creating an index that already exists and
    dropping one that doesn't are both no-ops. Before building the new unique
    index we DETECT legacy duplicates (a user who claimed two different trial
    plans on the same channel under the old rule). Duplicate records are NEVER
    deleted: each older duplicate is copied intact (same _id) into the
    free_trial_claims_history archive collection, then removed from the live
    claims collection so the unique index can be created. The most recent claim
    per key stays in the live collection as the canonical record."""
    new_key = "user_id_1_channel_id_1"
    legacy_keys = ["user_id_1_channel_id_1_trial_plan_id_1"]

    try:
        existing = set(free_trial_claims_col.index_information().keys())
    except Exception:
        existing = set()

    if new_key not in existing:
        # New unique index not built yet -> detect legacy duplicates first.
        groups = {}
        try:
            for c in free_trial_claims_col.find({}):
                k = (int(c.get('user_id') or 0), int(c.get('channel_id') or 0))
                groups.setdefault(k, []).append(c)
        except Exception:
            groups = {}
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        if dups:
            print(f"[free-trial] WARNING: {len(dups)} (user, channel) keys have more "
                  f"than one claim. Archiving all but the newest per key into the "
                  f"history collection (claim records are never deleted).")
            for k, claims in dups.items():
                claims.sort(key=lambda c: c.get('claimed_at') or datetime.min)
                for c in claims[:-1]:
                    # Preserve history first (keyed by the same _id so a re-run of
                    # this migration is idempotent), then remove from live claims.
                    try:
                        free_trial_claims_history_col.insert_one(dict(c))
                    except DuplicateKeyError:
                        pass  # already archived on a previous run
                    except Exception:
                        pass
                    try:
                        free_trial_claims_col.delete_one({"_id": c['_id']})
                    except Exception:
                        pass

    try:
        # The one-trial-ever guarantee now lives in this unique index.
        free_trial_claims_col.create_index([("user_id", 1), ("channel_id", 1)], unique=True)
    except Exception as e:
        print(f"[free-trial] could not create unique (user_id, channel_id) index: {e}")

    for name in legacy_keys:
        try:
            free_trial_claims_col.drop_index(name)
        except Exception:
            pass

def setup_indexes():
    """Create/ensure free-trial indexes safely at startup. Idempotent: it does not
    crash if an index already exists (MongoDB create_index is a no-op then)."""
    try:
        free_trials_col.create_index([("trial_id", 1)], unique=True)
        free_trials_col.create_index([("channel_id", 1)])
    except Exception:
        pass
    try:
        # One-trial-ever unique index + safe migration of the legacy 3-field index.
        _migrate_trial_claim_unique_index()
    except Exception:
        pass
    try:
        free_trial_claims_col.create_index([("user_id", 1)])
        free_trial_claims_col.create_index([("channel_id", 1)])
        free_trial_claims_col.create_index([("status", 1)])
    except Exception:
        pass
    try:
        pending_reactions_col.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
    except Exception:
        pass
    try:
        carts_col.create_index([("user_id", 1)], unique=True)
    except Exception:
        pass
    try:
        scheduled_broadcasts_col.create_index([("status", 1), ("when_ts", 1)])
    except Exception:
        pass
    try:
        waitlist_col.create_index([("user_id", 1), ("channel_id", 1)], unique=True)
        waitlist_col.create_index([("channel_id", 1)])
    except Exception:
        pass
    try:
        expired_subs_col.create_index([("user_id", 1), ("channel_id", 1)], unique=True)
        expired_subs_col.create_index([("expired_at", 1)])
    except Exception:
        pass
    try:
        offer_bundles_col.create_index([("bundle_id", 1)], unique=True)
        offer_bundles_col.create_index([("enabled", 1)])
    except Exception:
        pass
    try:
        pending_offer_bundle_checkouts_col.create_index([("user_id", 1)])
    except Exception:
        pass
    try:
        fj_pending_requests_col.create_index([("channel_id", 1), ("user_id", 1)], unique=True)
        fj_pending_requests_col.create_index([("active", 1)])
        fj_pending_requests_col.create_index([("requested_at", 1)])
    except Exception:
        pass

def bootstrap_counters():
    """One-time migration: if counters_col doesn't exist yet, seed it from whatever
    payment history already exists, so /stats totals don't reset to zero after this update."""
    if counters_col.count_documents({"_id": "stats"}) == 0:
        existing_sales = payments_col.count_documents({})
        existing_revenue = sum(p.get('amount', 0) for p in payments_col.find({}))
        counters_col.insert_one({"_id": "stats", "total_sales": existing_sales, "total_revenue": existing_revenue})
        print(f"Bootstrapped counters from existing history: {existing_sales} sales, ₹{existing_revenue} revenue.")

def cleanup_stale_fj_pending_requests():
    """Clean up stale pending join requests that are no longer valid.
    This runs periodically to remove requests that were never approved/rejected
    but the user is no longer pending (e.g. request expired, was cancelled, etc.)."""
    try:
        cutoff = datetime.now() - timedelta(days=7)  # remove requests older than 7 days
        result = fj_pending_requests_col.delete_many({
            "active": True,
            "requested_at": {"$lt": cutoff}
        })
        if result.deleted_count > 0:
            print(f"[fj_cleanup] removed {result.deleted_count} stale pending join requests")
    except Exception as e:
        print(f"[fj_cleanup] error: {e}")

if __name__ == '__main__':
    keep_alive()
    setup_indexes()
    bootstrap_counters()
    backfill_expired_subs()
    resume_pending_reactions()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users, 'interval', minutes=1)
    scheduler.add_job(send_expiry_reminders, 'interval', minutes=5)
    scheduler.add_job(run_scheduled_broadcasts, 'interval', minutes=1)
    scheduler.add_job(send_abandoned_cart_nudges, 'interval', minutes=5)
    scheduler.add_job(send_winback_offers, 'interval', minutes=15)
    scheduler.add_job(sync_all_tracked_chats, 'interval', minutes=30)
    scheduler.add_job(cleanup_stale_fj_pending_requests, 'interval', hours=1)
    scheduler.start()
    bot.remove_webhook()
    # Small pause so that if a previous deploy's instance is still shutting down
    # (common on Render during a redeploy), it has time to release the getUpdates
    # connection before this instance starts polling — avoids a 409 Conflict.
    import time
    time.sleep(2)
    setup_commands()
    print("Bot is running...")
    
    # --- Force Join startup diagnostics ---
    try:
        fj_settings = get_force_join_settings()
        if fj_settings.get('enabled') and fj_settings.get('channels'):
            print("[fj_startup] Force Join is enabled. Running diagnostics...")
            has_chat_join_handler = hasattr(bot, 'chat_join_request_handler') or hasattr(bot, 'register_chat_join_request_handler')
            print(f"[fj_startup] chat_join_request handler support: {has_chat_join_handler}")
            if not has_chat_join_handler:
                print("[fj_startup] WARNING: chat_join_request handler may not be available. Event-based tracking will not work.")
            for idx, ch in enumerate(fj_settings['channels'], 1):
                chat_id = _fj_resolve_chat_id(ch.get('channel'))
                title = ch.get('title') or ch.get('channel') or f'Channel {idx}'
                is_private = ch.get('is_private', False)
                print(f"[fj_startup] Channel {idx}: {title} (id={chat_id}, private={is_private})")
                if chat_id is None:
                    print(f"[fj_startup]   WARNING: could not resolve chat_id for {title}")
                    continue
                try:
                    chat_obj = bot.get_chat(chat_id)
                    print(f"[fj_startup]   get_chat OK: {getattr(chat_obj, 'title', '?')}")
                except Exception as e:
                    print(f"[fj_startup]   get_chat FAILED: {e}")
                    continue
                try:
                    bot_member = bot.get_chat_member(chat_id, bot.user.id)
                    bstatus = getattr(bot_member, 'status', None)
                    print(f"[fj_startup]   bot membership status: {bstatus}")
                except Exception as e:
                    print(f"[fj_startup]   bot get_chat_member FAILED: {e}")
                
                # Check MongoDB for pending requests
                try:
                    pending_count = fj_pending_requests_col.count_documents({
                        "channel_id": int(chat_id),
                        "active": True
                    })
                    print(f"[fj_startup]   pending join requests in DB: {pending_count}")
                except Exception as e:
                    print(f"[fj_startup]   could not query pending requests: {e}")
                
                if is_private:
                    print(f"[fj_startup]   -> Private channel. Event-based tracking via chat_join_request handler is configured.")
                    print(f"[fj_startup]   -> Ensure bot has 'Invite Users' permission to receive join request updates.")
            print("[fj_startup] Force Join diagnostics complete.")
        else:
            print("[fj_startup] Force Join is disabled or no channels configured.")
    except Exception as e:
        print(f"[fj_startup] Force Join diagnostic error: {e}")
    # skip_pending=True: ignore any updates that piled up while no instance was polling
    # (e.g. during a redeploy), so old messages aren't reprocessed on restart.
    #
    # infinity_polling normally already retries on most errors, but a 409 Conflict
    # (another getUpdates poller still active — e.g. the previous Render deploy
    # hasn't fully stopped yet) can surface as an unhandled ApiTelegramException
    # that kills the whole process. Render then restarts the dyno immediately,
    # which just repeats the same race. Wrap it in a retry loop with backoff so a
    # transient overlap self-heals instead of crash-looping the service.
    backoff = 3
    while True:
        try:
            bot.infinity_polling(
                timeout=20,
                long_polling_timeout=10,
                skip_pending=True,
                allowed_updates=[
                    'message', 'edited_message', 'callback_query',
                    'channel_post', 'edited_channel_post',
                    'chat_member', 'my_chat_member', 'chat_join_request',
                ],
            )
            break  # infinity_polling only returns on a clean stop
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print(f"⚠️ 409 Conflict (another poller still active) — retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise
        except Exception as e:
            print(f"⚠️ Polling crashed unexpectedly: {e} — retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
