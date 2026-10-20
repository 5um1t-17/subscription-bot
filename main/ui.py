"""Shared text-related icons for Telegram inline keyboard buttons."""

import unicodedata

from telebot.types import InlineKeyboardButton as _TelegramInlineKeyboardButton


# Remove the current leading icon before selecting a label-specific icon. The
# callbacks and button behavior are untouched.
_OLD_PREFIXES = (
    "✅", "☑️", "☑", "❌", "✖️", "✖", "❎", "🗑️", "🗑", "➕", "➖", "➗",
    "🔍", "🔎", "🔗", "📎", "📣", "📢", "📨", "📤", "📋", "📊", "🗄", "🗃",
    "🧹", "🔒", "🔔", "📞", "☎️", "☎", "🏠", "🏡", "⌂", "⬅️", "⬅", "🔙",
    "◀️", "◀", "➡️", "➡", "▶️", "▶", "⏳", "⏱", "⏰", "💳", "💰", "💲",
    "🎁", "🎉", "🆓", "😍", "📝", "✏️", "🖼", "📷", "📸", "🎬", "🔀",
    "🔄", "🔴", "🟢", "📚", "👥", "➜", "→", "¶", "▷", "⛶", "▣", "↗", "◩", "☰", "✧",
)


def _icon_and_label(text):
    if not isinstance(text, str):
        return text

    label = text.strip()
    for prefix in _OLD_PREFIXES:
        if label.startswith(prefix):
            label = label[len(prefix):].lstrip()
            break

    # Some legacy labels use mathematical bold/italic Unicode alphabets. NFKC
    # makes their words readable to the semantic matcher without changing them.
    lowered = unicodedata.normalize("NFKC", label).casefold()
    # Navigation and destructive actions take precedence over the subject word.
    if any(word in lowered for word in ("close", "cancel", "reject")):
        icon = "✖"
    elif any(word in lowered for word in ("back", "previous", "prev")):
        icon = "←"
    elif "next" in lowered:
        icon = "→"
    elif "home" in lowered:
        icon = "⌂"
    elif "admin panel" in lowered:
        icon = "⚙"
    elif lowered.startswith("add ") or "add another" in lowered:
        icon = "＋"
    elif lowered.startswith("create "):
        icon = "✚"
    elif lowered.startswith("replace "):
        icon = "⇄"
    elif lowered.startswith("edit "):
        icon = "✎"
    elif lowered.startswith("set "):
        icon = "⚙"
    elif lowered.startswith("delete "):
        icon = "🗑"
    elif lowered.startswith("remove "):
        icon = "−"
    elif lowered.startswith("clear "):
        icon = "🧹"
    elif lowered.startswith("verify "):
        icon = "✓"
    elif lowered.startswith("approve "):
        icon = "☑"
    elif lowered.startswith(("confirm ", "continue ", "proceed ", "done ")):
        icon = "✓"
    elif lowered.startswith("join "):
        icon = "🔗"
    elif lowered.startswith("start "):
        icon = "▶"
    elif lowered.startswith("save "):
        icon = "💾"
    elif lowered.startswith("pause "):
        icon = "⏸"
    elif lowered.startswith("resume "):
        icon = "▶"
    elif "screenshot" in lowered:
        icon = "⛶"
    elif "thumbnail" in lowered or "thumb" in lowered:
        icon = "▣"
    elif "cover" in lowered:
        icon = "◩"
    elif "media mode" in lowered:
        icon = "☰"
    elif "media info" in lowered or "mediainfo" in lowered:
        icon = "¶"
    elif "caption" in lowered:
        icon = "✍"
    elif "description" in lowered or "about" in lowered:
        icon = "≡"
    elif "sample" in lowered:
        icon = "▷"
    elif "preview" in lowered:
        icon = "◉"
    elif "search" in lowered:
        icon = "⌕"
    elif "contact" in lowered:
        icon = "☎"
    elif "broadcast" in lowered:
        icon = "📣"
    elif "schedule" in lowered:
        icon = "◷"
    elif "notify" in lowered or "notification" in lowered:
        icon = "🔔"
    elif "waitlist" in lowered:
        icon = "⏳"
    elif "force join" in lowered:
        icon = "🔒"
    elif "cleanup" in lowered:
        icon = "🧹"
    elif "database" in lowered:
        icon = "▤"
    elif "dashboard" in lowered:
        icon = "▦"
    elif "stats" in lowered:
        icon = "📈"
    elif "subscriber" in lowered:
        icon = "👥"
    elif "user" in lowered:
        icon = "👤"
    elif "free" in lowered:
        icon = "🆓"
    elif "trial" in lowered:
        icon = "🎟"
    elif "offer" in lowered:
        icon = "🎁"
    elif "cart" in lowered:
        icon = "🛒"
    elif "checkout" in lowered:
        icon = "🧾"
    elif "payment" in lowered or "paid" in lowered:
        icon = "💳"
    elif "price" in lowered:
        icon = "₹"
    elif "plan" in lowered or "duration" in lowered:
        icon = "🗓"
    elif "channel" in lowered:
        icon = "◉"
    elif "group" in lowered:
        icon = "👥"
    elif "link" in lowered:
        icon = "↗"
    elif "file" in lowered:
        icon = "📄"
    elif "photo" in lowered:
        icon = "▧"
    elif "image" in lowered or "banner" in lowered:
        icon = "🖼"
    elif "refresh" in lowered or "retry" in lowered:
        icon = "⟳"
    elif "enable" in lowered or "active" in lowered:
        icon = "🟢"
    elif "disable" in lowered or "paused" in lowered:
        icon = "🔴"
    else:
        return text

    return f"{icon} {label}"


def InlineKeyboardButton(text=None, *args, **kwargs):
    """Build a Telegram button with a compact icon chosen from its label."""
    if text is None and "text" in kwargs:
        kwargs["text"] = _icon_and_label(kwargs["text"])
    else:
        text = _icon_and_label(text)
    return _TelegramInlineKeyboardButton(text, *args, **kwargs)
