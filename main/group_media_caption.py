"""Group media caption enforcement and ordered reposting.

Architecture
------------
Every incoming admin media message — whether a standalone item or part of a
Telegram album — enters a single per-chat *collect window*.  After a quiet
gap (COLLECT_SETTLE_SECONDS) with no new arrivals for that chat, the window
closes and ALL collected messages are processed at once:

1. Messages are grouped by their media_group_id.
   - Messages with no media_group_id each form a 1-item group (single).
   - Messages sharing a media_group_id form a multi-item album group.

2. Groups are sorted by the LOWEST message_id in each group so the replay
   order exactly matches the order the admin originally sent the media.

3. Each group is handed to the sequential per-chat worker which processes
   them one at a time, with up to REPOST_MAX_RETRIES retries per unit.

This single-window design eliminates two separate settle timers (the old
2-second singles timer and 5-second album timer) that could fire at
different times and scramble the send order or misclassify a lone video
that Telegram tagged with a media_group_id as an "album".
"""

import time
from collections import defaultdict
from html.parser import HTMLParser
from threading import RLock, Thread, Timer

import telebot
from telebot.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

# ---------------------------------------------------------------------------
# Timing / retry constants
# ---------------------------------------------------------------------------

# How long to wait after the LAST media message arrives for a chat before
# closing the collect window and starting to process.  5 s comfortably covers
# the worst-case Telegram album delivery spread while still feeling snappy.
COLLECT_SETTLE_SECONDS = 5.0

GROUP_ADMIN_CACHE_SECONDS = 60

# Retries per unit.  The queue is BLOCKED during retries to preserve order.
REPOST_MAX_RETRIES = 3
REPOST_RETRY_DELAY = 2.0   # seconds between attempts

# ---------------------------------------------------------------------------
# Module-level shared state
# ---------------------------------------------------------------------------

# Per-chat collect windows: chat_id -> {'messages': [...], 'timer': Timer|None}
_collect_windows = {}
_collect_lock = RLock()

# Per-chat sequential worker queues: chat_id -> {'units': [...], 'active': bool}
_repost_queues = {}
_repost_queue_lock = RLock()

_group_admin_status_cache = {}
_group_admin_status_lock = RLock()
_bot_delete_permission_cache = {}

bot = None
GROUP_ANONYMOUS_BOT_ID = 1087968824
caption_getter = lambda: ""
schedule_delete_fn = lambda chat_id, message_id, delay: None
admin_reply_vanish_seconds = 90
_saved_caption_enabled_fn = None   # optional callable(chat_id) -> bool


def configure(bot_client, anonymous_bot_id, get_caption, schedule_delete,
              reply_vanish_seconds=90, saved_caption_enabled_fn=None):
    global bot, GROUP_ANONYMOUS_BOT_ID, caption_getter, schedule_delete_fn
    global admin_reply_vanish_seconds, _saved_caption_enabled_fn
    bot = bot_client
    GROUP_ANONYMOUS_BOT_ID = anonymous_bot_id
    caption_getter = get_caption
    schedule_delete_fn = schedule_delete
    admin_reply_vanish_seconds = reply_vanish_seconds
    _saved_caption_enabled_fn = saved_caption_enabled_fn


def register_handlers():
    bot.message_handler(
        content_types=[
            'photo', 'video', 'document', 'audio', 'animation',
            'voice', 'sticker', 'video_note',
        ],
        func=lambda message: (
            _is_group_admin_media_sender(message)
            and _message_has_supported_media(message)
        ),
    )(handle_configured_admin_group_media)


# ---------------------------------------------------------------------------
# Admin / permission helpers
# ---------------------------------------------------------------------------

def _is_group_admin_media_sender(message):
    if not message.chat or message.chat.type not in ('group', 'supergroup'):
        return False
    sender_chat = getattr(message, 'sender_chat', None)
    if sender_chat and sender_chat.id == message.chat.id:
        return True          # anonymous admin posting as the group
    if not message.from_user:
        return False
    if message.from_user.id == GROUP_ANONYMOUS_BOT_ID:
        return True          # GroupAnonymousBot identity
    if message.from_user.is_bot:
        return False
    key = (message.chat.id, message.from_user.id)
    now = time.monotonic()
    with _group_admin_status_lock:
        cached = _group_admin_status_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    try:
        member = bot.get_chat_member(message.chat.id, message.from_user.id)
        is_admin = getattr(member, 'status', None) in ('administrator', 'creator')
    except Exception as e:
        print(f"[group-media-caption] couldn't verify admin status "
              f"chat={message.chat.id} user={message.from_user.id}: {e}")
        return False
    with _group_admin_status_lock:
        _group_admin_status_cache[key] = (now + GROUP_ADMIN_CACHE_SECONDS, is_admin)
        if not is_admin:
            _group_admin_status_cache.pop(key, None)
        for k in [k for k, v in _group_admin_status_cache.items() if v[0] <= now]:
            _group_admin_status_cache.pop(k, None)
    if not is_admin:
        print(f"[group-media-caption] sender is not a group admin "
              f"chat={message.chat.id} user={message.from_user.id}")
    return is_admin


def _message_has_supported_media(message):
    return any(getattr(message, attr, None) for attr in (
        'photo', 'video', 'document', 'audio', 'animation', 'voice',
        'sticker', 'video_note',
    ))


def _bot_can_delete_group_messages(chat_id):
    now = time.monotonic()
    with _group_admin_status_lock:
        cached = _bot_delete_permission_cache.get(chat_id)
        if cached and cached[0] > now:
            return cached[1]
    try:
        bot_id = getattr(getattr(bot, 'user', None), 'id', None) or bot.get_me().id
        member = bot.get_chat_member(chat_id, bot_id)
        can_delete = (
            getattr(member, 'status', None) == 'creator'
            or (
                getattr(member, 'status', None) == 'administrator'
                and bool(getattr(member, 'can_delete_messages', False))
            )
        )
    except Exception as e:
        print(f"[group-media-caption] couldn't verify bot delete permission "
              f"chat={chat_id}: {e}")
        return False
    with _group_admin_status_lock:
        _bot_delete_permission_cache[chat_id] = (now + GROUP_ADMIN_CACHE_SECONDS, can_delete)
    return can_delete


# ---------------------------------------------------------------------------
# Caption helpers
# ---------------------------------------------------------------------------

class _CaptionTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _caption_visible_text(caption_html):
    parser = _CaptionTextExtractor()
    try:
        parser.feed(caption_html or '')
        parser.close()
    except Exception:
        return ''
    return ''.join(parser.parts).strip()


def _caption_utf16_length(caption_html):
    return len(_caption_visible_text(caption_html).encode('utf-16-le')) // 2


def _caption_as_html(caption, entities=None):
    return telebot.formatting.apply_html_entities(caption or '', entities or None)


def _append_saved_caption_html(original_caption, original_entities, saved_caption):
    original_html = _caption_as_html(original_caption, original_entities)
    return f"{original_html}\n\n{saved_caption}" if original_html else saved_caption


def _caption_already_present(messages, saved_caption):
    saved_text = _caption_visible_text(saved_caption)
    return bool(saved_text) and any(
        saved_text in (getattr(m, 'caption', None) or '')
        for m in messages
    )


# ---------------------------------------------------------------------------
# Media send helpers
# ---------------------------------------------------------------------------

def _get_group_media_file(message):
    if getattr(message, 'photo', None):
        return 'photo', message.photo[-1].file_id
    for kind in ('video', 'document', 'audio', 'animation', 'voice'):
        media = getattr(message, kind, None)
        if media:
            return kind, media.file_id
    return None, None


def _keep_replacement_message(chat_id, message):
    message_id = getattr(message, 'message_id', None)
    if message_id is not None:
        schedule_delete_fn(chat_id, message_id, None)


def _send_one_group_media(message, caption_html):
    """Resend one media item with the saved caption appended."""
    kind, file_id = _get_group_media_file(message)
    send_method = {
        'photo':     bot.send_photo,
        'video':     bot.send_video,
        'document':  bot.send_document,
        'audio':     bot.send_audio,
        'animation': bot.send_animation,
        'voice':     bot.send_voice,
    }.get(kind)
    if not send_method or not file_id:
        raise ValueError(f"unsupported media type: {kind or 'unknown'}")
    kwargs = {"caption": caption_html, "parse_mode": "HTML"}
    thread_id = getattr(message, 'message_thread_id', None)
    if thread_id is not None:
        kwargs['message_thread_id'] = thread_id
    reply_target = getattr(message, 'reply_to_message', None)
    if reply_target is not None:
        kwargs['reply_to_message_id'] = reply_target.message_id
        kwargs['allow_sending_without_reply'] = True
    replacement = send_method(message.chat.id, file_id, **kwargs)
    _keep_replacement_message(message.chat.id, replacement)
    return replacement


def _album_can_be_sent_together(kinds):
    """True if Telegram will accept these kinds as a single media group."""
    if len(kinds) < 2 or len(kinds) > 10:
        return False
    if all(k in ('photo', 'video') for k in kinds):
        return True
    return len(set(kinds)) == 1 and kinds[0] in ('audio', 'document')


def _send_media_album(messages, caption_index, combined_caption):
    media_data = [_get_group_media_file(item) for item in messages]
    kinds = [kind for kind, _ in media_data]
    if any(not kind or not file_id for kind, file_id in media_data):
        raise ValueError("album contains an unsupported media item")
    if not _album_can_be_sent_together(kinds):
        raise ValueError(
            f"Telegram cannot group {len(kinds)} items of types [{','.join(kinds)}] "
            "as one album"
        )
    media_items = []
    for idx, (item, (kind, file_id)) in enumerate(zip(messages, media_data)):
        caption = (
            combined_caption if idx == caption_index
            else (_caption_as_html(item.caption, getattr(item, 'caption_entities', None))
                  if item.caption else None)
        )
        cls_map = {
            'photo':    InputMediaPhoto,
            'video':    InputMediaVideo,
            'audio':    InputMediaAudio,
            'document': InputMediaDocument,
        }
        media_items.append(cls_map[kind](file_id, caption=caption, parse_mode='HTML'))

    kwargs = {}
    thread_id = getattr(messages[0], 'message_thread_id', None)
    if thread_id is not None:
        kwargs['message_thread_id'] = thread_id
    reply_target = next(
        (getattr(item, 'reply_to_message', None) for item in messages
         if getattr(item, 'reply_to_message', None) is not None),
        None,
    )
    if reply_target is not None:
        kwargs['reply_to_message_id'] = reply_target.message_id
        kwargs['allow_sending_without_reply'] = True
    replacements = bot.send_media_group(messages[0].chat.id, media_items, **kwargs)
    for r in replacements or []:
        _keep_replacement_message(messages[0].chat.id, r)


# ---------------------------------------------------------------------------
# Core repost logic — one unit (single OR album)
# ---------------------------------------------------------------------------

def _repost_unit(messages):
    """
    Process one ordered unit.  Raises on failure so the caller can retry.

    A unit is ALWAYS a list sorted by message_id.  Whether it's treated as a
    single or an album is decided here by the actual list length — never by
    media_group_id alone, since Telegram sometimes stamps a lone forwarded
    video with a media_group_id.
    """
    messages = sorted(messages, key=lambda m: m.message_id)
    if not messages:
        return
    chat_id = messages[0].chat.id

    # Per-group saved-caption toggle
    if _saved_caption_enabled_fn is not None:
        try:
            if not _saved_caption_enabled_fn(chat_id):
                print(f"[group-media-caption] skipped chat={chat_id}: "
                      "saved caption disabled for this group")
                return
        except Exception as e:
            print(f"[group-media-caption] saved_caption_enabled_fn error "
                  f"chat={chat_id}: {e}")

    saved_caption = caption_getter()
    if not saved_caption:
        print(f"[group-media-caption] skipped chat={chat_id}: no saved caption")
        try:
            bot.reply_to(
                messages[0],
                "ℹ️ No saved media caption is set yet. "
                "A configured bot admin must set one with /setmediacaption "
                "in a private chat.",
            )
        except Exception:
            pass
        return

    if _caption_already_present(messages, saved_caption):
        print(f"[group-media-caption] caption already present chat={chat_id}; skipping")
        return

    if not _bot_can_delete_group_messages(chat_id):
        print(f"[group-media-caption] bot lacks Delete Messages permission chat={chat_id}")
        try:
            bot.reply_to(
                messages[0],
                "⚠️ I couldn't replace this media — please grant me the "
                "Telegram admin permission to delete messages, then try again.",
            )
        except Exception:
            pass
        return

    # ------------------------------------------------------------------
    # Single vs album decision — based on list length, NOT media_group_id
    # ------------------------------------------------------------------
    is_album = len(messages) >= 2   # 1-item list = single, always

    if not is_album:
        # ---- SINGLE media item ----------------------------------------
        msg = messages[0]
        caption_html = _append_saved_caption_html(
            msg.caption,
            getattr(msg, 'caption_entities', None),
            saved_caption,
        )
        if _caption_utf16_length(caption_html) > 1024:
            print(f"[group-media-caption] combined caption > 1024 UTF-16 units "
                  f"chat={chat_id} message={msg.message_id}")
            bot.reply_to(
                msg,
                "⚠️ The combined caption is too long for Telegram — "
                "original media left unchanged.",
            )
            return
        _send_one_group_media(msg, caption_html)   # raises on failure
    else:
        # ---- Album (≥2 real items) ------------------------------------
        caption_idx = next(
            (i for i, m in enumerate(messages) if getattr(m, 'caption', None)),
            0,
        )
        cap_src = messages[caption_idx]
        combined = _append_saved_caption_html(
            cap_src.caption,
            getattr(cap_src, 'caption_entities', None),
            saved_caption,
        )
        if _caption_utf16_length(combined) > 1024:
            print(f"[group-media-caption] album combined caption > 1024 UTF-16 units "
                  f"chat={chat_id}")
            bot.reply_to(
                messages[0],
                "⚠️ The combined album caption is too long for Telegram — "
                "originals left unchanged.",
            )
            return
        _send_media_album(messages, caption_idx, combined)   # raises on failure

    # Delete originals only after Telegram confirms the replacement was sent.
    for m in messages:
        try:
            bot.delete_message(chat_id, m.message_id)
        except Exception as de:
            print(f"[group-media-caption] replacement sent but couldn't delete "
                  f"source chat={chat_id} message={m.message_id}: {de}")
    print(f"[group-media-caption] done chat={chat_id} items={len(messages)} "
          f"album={is_album}", flush=True)


# ---------------------------------------------------------------------------
# Sequential per-chat worker with retry
# ---------------------------------------------------------------------------

def _run_repost_queue(chat_id):
    """
    Pop units from the per-chat queue one at a time.
    Each unit is retried up to REPOST_MAX_RETRIES times before giving up.
    The queue is BLOCKED during retries → strict send order is preserved.
    """
    while True:
        with _repost_queue_lock:
            state = _repost_queues.get(chat_id)
            if not state or not state['units']:
                if state:
                    state['active'] = False
                    if not state['units']:
                        _repost_queues.pop(chat_id, None)
                return
            unit = state['units'].pop(0)

        last_err = None
        for attempt in range(1, REPOST_MAX_RETRIES + 1):
            try:
                _repost_unit(unit)
                last_err = None
                break
            except Exception as e:
                last_err = e
                kinds = ','.join(
                    k or 'unknown'
                    for k, _ in (_get_group_media_file(m) for m in unit)
                )
                print(
                    f"[group-media-caption] attempt {attempt}/{REPOST_MAX_RETRIES} "
                    f"failed chat={chat_id} "
                    f"ids={[m.message_id for m in unit]} types={kinds}: {e}",
                    flush=True,
                )
                if attempt < REPOST_MAX_RETRIES:
                    time.sleep(REPOST_RETRY_DELAY)

        if last_err is not None:
            print(
                f"[group-media-caption] gave up after {REPOST_MAX_RETRIES} attempts "
                f"chat={chat_id} ids={[m.message_id for m in unit]}: {last_err}",
                flush=True,
            )
            is_album = len(unit) >= 2
            try:
                notice = (
                    f"⚠️ I couldn't replace this "
                    f"{'album' if is_album else 'media'} with its caption "
                    f"after {REPOST_MAX_RETRIES} attempts — original kept.\n"
                    + ("Albums need 2–10 photos/videos, or 2–10 audio files or "
                       "documents of the same type. Also check the saved caption HTML."
                       if is_album else
                       "Check the saved caption HTML and my admin permissions.")
                )
                bot.reply_to(unit[0], notice)
            except Exception as ne:
                print(f"[group-media-caption] couldn't notify admin "
                      f"chat={chat_id}: {ne}")


def _start_repost_queue(chat_id, units):
    """
    Push sorted units into the per-chat queue and ensure the worker is running.
    `units` is an already-sorted list of message-lists (each sorted internally).
    """
    start = False
    with _repost_queue_lock:
        state = _repost_queues.setdefault(chat_id, {'units': [], 'active': False})
        state['units'].extend(units)
        if not state['active']:
            state['active'] = True
            start = True
    if start:
        Thread(target=_run_repost_queue, args=(chat_id,), daemon=True).start()


# ---------------------------------------------------------------------------
# Unified collect window — single timer, all media types, per chat
# ---------------------------------------------------------------------------

def _flush_collect_window(chat_id):
    """
    Called when the per-chat collect window closes.

    1. Drain all buffered messages for this chat.
    2. Group them by media_group_id:
       - Each distinct media_group_id whose group has ≥2 members → album unit.
       - A group with only 1 member (lone video with spurious media_group_id,
         or genuinely standalone message) → single unit.
       - Messages with no media_group_id → each its own single unit.
    3. Sort all units by their lowest message_id (original send order).
    4. Hand the sorted list to the sequential worker.
    """
    with _collect_lock:
        window = _collect_windows.pop(chat_id, None)
    if not window or not window['messages']:
        return

    all_messages = sorted(window['messages'], key=lambda m: m.message_id)

    # Group by media_group_id.  None-keyed messages are always individual.
    groups_by_id = defaultdict(list)   # media_group_id -> [message, ...]
    singles = []                       # messages with no media_group_id

    for msg in all_messages:
        mgid = getattr(msg, 'media_group_id', None)
        if mgid:
            groups_by_id[mgid].append(msg)
        else:
            singles.append([msg])      # wrap immediately so it's a unit

    # Convert multi-item album groups; demote 1-item groups to singles.
    album_units = []
    for mgid, msgs in groups_by_id.items():
        msgs_sorted = sorted(msgs, key=lambda m: m.message_id)
        if len(msgs_sorted) >= 2:
            album_units.append(msgs_sorted)   # true album
        else:
            singles.append(msgs_sorted)       # lone video — treat as single

    # Merge and sort ALL units by their first (lowest) message_id so the
    # replay order matches the exact order the admin originally sent them.
    all_units = singles + album_units
    all_units.sort(key=lambda unit: unit[0].message_id)

    unit_summary = [(len(u), getattr(u[0], 'media_group_id', None)) for u in all_units]
    print(
        f"[group-media-caption] flushing chat={chat_id} "
        f"total_items={len(all_messages)} units={len(all_units)} "
        f"summary={unit_summary}",
        flush=True,
    )

    _start_repost_queue(chat_id, all_units)


def _add_to_collect_window(message):
    """
    Add a message to this chat's collect window, resetting the settle timer.
    All messages — singles and album parts alike — enter the same window.
    """
    chat_id = message.chat.id
    with _collect_lock:
        window = _collect_windows.setdefault(chat_id, {'messages': [], 'timer': None})

        # Deduplicate (Telegram can re-deliver the same update).
        if all(m.message_id != message.message_id for m in window['messages']):
            window['messages'].append(message)

        # Reset the settle timer.
        if window['timer']:
            window['timer'].cancel()
        t = Timer(COLLECT_SETTLE_SECONDS, _flush_collect_window, args=(chat_id,))
        t.daemon = True
        window['timer'] = t
        t.start()


# ---------------------------------------------------------------------------
# Entry point called by the telebot handler
# ---------------------------------------------------------------------------

def handle_configured_admin_group_media(message):
    print(
        f"[group-media-caption] incoming chat={message.chat.id} "
        f"sender={message.from_user.id if message.from_user else 'anon'} "
        f"msg={message.message_id} "
        f"mgid={getattr(message, 'media_group_id', None) or '-'}",
        flush=True,
    )

    # Stickers and video notes can never carry a caption.
    if getattr(message, 'sticker', None) or getattr(message, 'video_note', None):
        try:
            bot.reply_to(
                message,
                "ℹ️ Telegram doesn't support captions on stickers or video notes. "
                "I left the original media unchanged.",
            )
        except Exception as e:
            print(f"[group-media-caption] couldn't explain unsupported type "
                  f"chat={message.chat.id}: {e}")
        return

    # Everything else enters the unified collect window.
    _add_to_collect_window(message)
