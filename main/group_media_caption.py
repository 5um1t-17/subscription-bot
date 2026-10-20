"""Group media caption enforcement and ordered reposting."""

import time
from html.parser import HTMLParser
from threading import RLock, Thread, Timer

import telebot
from telebot.types import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument

GROUP_MEDIA_ALBUM_SETTLE_SECONDS = 1.5
GROUP_MEDIA_REPOST_SETTLE_SECONDS = 2.0
GROUP_ADMIN_CACHE_SECONDS = 60

_group_media_album_batches = {}
_group_media_album_lock = RLock()
_group_media_repost_queues = {}
_group_media_repost_queue_lock = RLock()
_group_admin_status_cache = {}
_group_admin_status_lock = RLock()
_bot_delete_permission_cache = {}

bot = None
GROUP_ANONYMOUS_BOT_ID = 1087968824
caption_getter = lambda: ""
schedule_delete_fn = lambda chat_id, message_id, delay: None
admin_reply_vanish_seconds = 90

def configure(bot_client, anonymous_bot_id, get_caption, schedule_delete, reply_vanish_seconds=90):
    global bot, GROUP_ANONYMOUS_BOT_ID, caption_getter, schedule_delete_fn, admin_reply_vanish_seconds
    bot = bot_client
    GROUP_ANONYMOUS_BOT_ID = anonymous_bot_id
    caption_getter = get_caption
    schedule_delete_fn = schedule_delete
    admin_reply_vanish_seconds = reply_vanish_seconds

def register_handlers():
    bot.message_handler(
        content_types=['photo', 'video', 'document', 'audio', 'animation', 'voice'],
        func=lambda message: _is_group_admin_media_sender(message) and _message_has_supported_media(message),
    )(handle_configured_admin_group_media)

def _is_group_admin_media_sender(message):
    if not message.chat or message.chat.type not in ('group', 'supergroup'):
        return False
    # Telegram hides the real sender when an admin posts anonymously. In that
    # case the message is sent as the group itself, which is only available to
    # admins using the group's anonymous posting mode.
    sender_chat = getattr(message, 'sender_chat', None)
    if sender_chat and sender_chat.id == message.chat.id:
        return True
    if not message.from_user:
        return False
    # Some Telegram updates identify anonymous group admins through the
    # GroupAnonymousBot user instead of (or in addition to) sender_chat.
    if message.from_user.id == GROUP_ANONYMOUS_BOT_ID:
        return True
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
        print(f"[group-media-caption] couldn't verify sender admin status chat={message.chat.id} user={message.from_user.id}: {e}")
        return False
    with _group_admin_status_lock:
        _group_admin_status_cache[key] = (now + GROUP_ADMIN_CACHE_SECONDS, is_admin)
        if not is_admin:
            # Do not retain a negative result: a regular member may be promoted
            # moments later, and their next media must be checked against Telegram.
            _group_admin_status_cache.pop(key, None)
        expired = [cache_key for cache_key, value in _group_admin_status_cache.items() if value[0] <= now]
        for cache_key in expired:
            _group_admin_status_cache.pop(cache_key, None)
    if not is_admin:
        print(f"[group-media-caption] sender is not currently a group admin chat={message.chat.id} user={message.from_user.id} status={getattr(member, 'status', '?')}")
    return is_admin

def _message_has_supported_media(message):
    return any(getattr(message, attr, None) for attr in ('photo', 'video', 'document', 'audio', 'animation', 'voice'))

def _get_group_media_file(message):
    if getattr(message, 'photo', None):
        return 'photo', message.photo[-1].file_id
    for kind in ('video', 'document', 'audio', 'animation', 'voice'):
        media = getattr(message, kind, None)
        if media:
            return kind, media.file_id
    return None, None

class _CaptionTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

def _caption_visible_text(caption_html):
    """Return the visible text in the saved HTML caption for duplicate checks."""
    parser = _CaptionTextExtractor()
    try:
        parser.feed(caption_html or '')
        parser.close()
    except Exception:
        return ''
    return ''.join(parser.parts).strip()

def _caption_as_html(caption, entities=None):
    """Escape existing caption text and retain its Telegram formatting entities."""
    return telebot.formatting.apply_html_entities(caption or '', entities or None)

def _append_saved_caption_html(original_caption, original_entities, saved_caption):
    original_html = _caption_as_html(original_caption, original_entities)
    return f"{original_html}\n\n{saved_caption}" if original_html else saved_caption

def _caption_already_present(messages, saved_caption):
    saved_text = _caption_visible_text(saved_caption)
    return bool(saved_text) and any(
        saved_text in (getattr(message, 'caption', None) or '')
        for message in messages
    )

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
        print(f"[group-media-caption] couldn't verify bot delete permission chat={chat_id}: {e}")
        return False
    with _group_admin_status_lock:
        _bot_delete_permission_cache[chat_id] = (now + GROUP_ADMIN_CACHE_SECONDS, can_delete)
    return can_delete

def _repost_group_media(messages):
    messages = sorted(messages, key=lambda item: item.message_id)
    if not messages:
        return
    chat_id = messages[0].chat.id
    saved_caption = caption_getter()
    if not saved_caption:
        print(f"[group-media-caption] skipped chat={chat_id}: no saved caption is configured")
        try:
            bot.reply_to(messages[0], "ℹ️ No saved media caption is set yet. The configured bot admin must set one with /setmediacaption in a private chat.")
        except Exception as reply_error:
            print(f"[group-media-caption] couldn't notify sender chat={chat_id}: {reply_error}")
        return
    if _caption_already_present(messages, saved_caption):
        print(f"[group-media-caption] already present chat={chat_id}; leaving source unchanged")
        return
    if not _bot_can_delete_group_messages(chat_id):
        print(f"[group-media-caption] skipped chat={chat_id}: bot needs Delete Messages permission to replace the source")
        try:
            bot.reply_to(
                messages[0],
                "⚠️ I couldn't replace this media. Please grant me the Telegram admin permission to delete messages, then try again.",
            )
        except Exception as reply_error:
            print(f"[group-media-caption] couldn't notify sender chat={chat_id}: {reply_error}")
        return

    try:
        if len(messages) == 1:
            message = messages[0]
            caption_html = _append_saved_caption_html(
                message.caption,
                getattr(message, 'caption_entities', None),
                saved_caption,
            )
            if len(_caption_visible_text(caption_html)) > 1024:
                print(f"[group-media-caption] skipped chat={chat_id} message={message.message_id}: combined caption exceeds 1024 characters")
                bot.reply_to(message, "⚠️ The combined caption is too long for Telegram, so I left the original media unchanged.")
                return
            kind, file_id = _get_group_media_file(message)
            send_method = {
                'photo': bot.send_photo,
                'video': bot.send_video,
                'document': bot.send_document,
                'audio': bot.send_audio,
                'animation': bot.send_animation,
                'voice': bot.send_voice,
            }.get(kind)
            if not send_method or not file_id:
                print(f"[group-media-caption] skipped chat={chat_id} message={message.message_id}: unsupported media type")
                return
            kwargs = {"caption": caption_html, "parse_mode": "HTML"}
            thread_id = getattr(message, 'message_thread_id', None)
            if thread_id is not None:
                kwargs['message_thread_id'] = thread_id
            # Resend the received file_id so protected-content groups do not
            # block the replacement through copyMessage's forwarding rules.
            send_method(chat_id, file_id, **kwargs)
        else:
            media_items = []
            caption_index = next((i for i, item in enumerate(messages) if getattr(item, 'caption', None)), 0)
            caption_source = messages[caption_index]
            combined_caption = _append_saved_caption_html(
                getattr(caption_source, 'caption', None),
                getattr(caption_source, 'caption_entities', None),
                saved_caption,
            )
            if len(_caption_visible_text(combined_caption)) > 1024:
                print(f"[group-media-caption] skipped album chat={chat_id}: combined caption exceeds 1024 characters")
                bot.reply_to(messages[0], "⚠️ The combined album caption is too long for Telegram, so I left the original album unchanged.")
                return
            for index, item in enumerate(messages):
                kind, file_id = _get_group_media_file(item)
                if not file_id:
                    print(f"[group-media-caption] skipped album chat={chat_id}: unsupported media item {item.message_id}")
                    return
                if index == caption_index:
                    caption = combined_caption
                else:
                    original_caption = getattr(item, 'caption', None)
                    caption = _caption_as_html(original_caption, getattr(item, 'caption_entities', None)) if original_caption else None
                if kind == 'photo':
                    media = InputMediaPhoto(file_id, caption=caption, parse_mode='HTML')
                elif kind == 'video':
                    media = InputMediaVideo(file_id, caption=caption, parse_mode='HTML')
                elif kind == 'audio':
                    media = InputMediaAudio(file_id, caption=caption, parse_mode='HTML')
                else:
                    media = InputMediaDocument(file_id, caption=caption, parse_mode='HTML')
                media_items.append(media)
            kwargs = {}
            thread_id = getattr(messages[0], 'message_thread_id', None)
            if thread_id is not None:
                kwargs['message_thread_id'] = thread_id
            bot.send_media_group(chat_id, media_items, **kwargs)

        # Delete the source only after Telegram confirms the replacement was sent.
        for message in messages:
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception as delete_error:
                print(f"[group-media-caption] replacement sent but couldn't delete source chat={chat_id} message={message.message_id}: {delete_error}")
        print(f"[group-media-caption] reposted media chat={chat_id} source_messages={len(messages)}")
    except Exception as e:
        print(f"[group-media-caption] could not replace media in chat={chat_id}: {e}")
        try:
            bot.reply_to(messages[0], "⚠️ I couldn't append the saved caption to this media. Please check that the saved caption uses valid Telegram HTML.")
        except Exception as reply_error:
            print(f"[group-media-caption] couldn't notify sender chat={chat_id}: {reply_error}")

def _drain_group_media_repost_queue(chat_id):
    while True:
        with _group_media_repost_queue_lock:
            state = _group_media_repost_queues.get(chat_id)
            if not state:
                return
            if not state['ready_units']:
                state['worker_active'] = False
                if not state['pending_units']:
                    _group_media_repost_queues.pop(chat_id, None)
                return
            unit = state['ready_units'].pop(0)
        try:
            _repost_group_media(unit)
        except Exception as e:
            print(f"[group-media-caption] queued repost failed chat={chat_id}: {e}", flush=True)


def _flush_group_media_repost_batch(chat_id):
    start_worker = False
    with _group_media_repost_queue_lock:
        state = _group_media_repost_queues.get(chat_id)
        if not state:
            return
        state['timer'] = None
        if state['pending_units']:
            state['pending_units'].sort(key=lambda unit: min(item.message_id for item in unit))
            state['ready_units'].extend(state['pending_units'])
            state['pending_units'] = []
        if state['ready_units'] and not state['worker_active']:
            state['worker_active'] = True
            start_worker = True
    if start_worker:
        worker = Thread(target=_drain_group_media_repost_queue, args=(chat_id,), daemon=True)
        worker.start()


def _queue_group_media_repost(messages):
    messages = sorted(messages, key=lambda item: item.message_id)
    if not messages:
        return
    chat_id = messages[0].chat.id
    with _group_media_repost_queue_lock:
        state = _group_media_repost_queues.setdefault(chat_id, {
            'pending_units': [], 'ready_units': [], 'timer': None, 'worker_active': False,
        })
        state['pending_units'].append(messages)
        old_timer = state.get('timer')
        if old_timer:
            old_timer.cancel()
        timer = Timer(GROUP_MEDIA_REPOST_SETTLE_SECONDS, _flush_group_media_repost_batch, args=(chat_id,))
        timer.daemon = True
        state['timer'] = timer
        timer.start()


def _flush_group_media_album(album_key):
    with _group_media_album_lock:
        batch = _group_media_album_batches.pop(album_key, None)
    if batch:
        _queue_group_media_repost(batch['messages'])

def handle_configured_admin_group_media(message):
    print(
        f"[group-media-caption] received group media chat={message.chat.id} "
        f"sender={message.from_user.id if message.from_user else '?'} "
        f"message={message.message_id} album={getattr(message, 'media_group_id', None) or '-'}",
        flush=True,
    )
    media_group_id = getattr(message, 'media_group_id', None)
    if not media_group_id:
        _queue_group_media_repost([message])
        return

    album_key = (message.chat.id, str(media_group_id))
    with _group_media_album_lock:
        batch = _group_media_album_batches.setdefault(album_key, {"messages": [], "timer": None})
        if all(item.message_id != message.message_id for item in batch['messages']):
            batch['messages'].append(message)
        old_timer = batch.get('timer')
        if old_timer:
            old_timer.cancel()
        timer = Timer(GROUP_MEDIA_ALBUM_SETTLE_SECONDS, _flush_group_media_album, args=(album_key,))
        timer.daemon = True
        batch['timer'] = timer
        timer.start()

