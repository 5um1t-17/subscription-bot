"""Shared typography cleanup for Telegram inline keyboard buttons."""

import re
import unicodedata

from telebot.types import InlineKeyboardButton as _TelegramInlineKeyboardButton


def _clean_button_label(text):
    if not isinstance(text, str):
        return text

    # Convert legacy decorative alphabets to plain text first, then apply one
    # consistent regular sans-serif Unicode style to button labels. Preserve
    # compact tags such as ⁽ⱽᴵᴾ⁾ because NFKC expands their superscript letters.
    small_tags = {}

    def protect_small_tag(match):
        marker = chr(0xE000 + len(small_tags))
        small_tags[marker] = match.group(0)
        return marker

    label = re.sub(r"⁽[^⁾]*⁾(?:˖✧)?", protect_small_tag, text.strip())
    label = unicodedata.normalize("NFKC", label).strip()
    if label in ("◀", "◀️", "⬅", "⬅️"):
        return f"◀️ {_to_sans_serif('Previous')}"
    if label in ("▶", "▶️", "➡", "➡️"):
        return f"▶️ {_to_sans_serif('Next')}"

    # Replace any existing decorative prefix with one consistent icon selected
    # from the button's purpose.
    while label and unicodedata.category(label[0]) in ("Mn", "Cf", "Sk", "So"):
        label = label[1:]
    label = label.lstrip()
    if not label:
        return text

    lowered = label.casefold()
    if lowered == "offers":
        return "🎁 𝒪𝒻𝒻𝑒𝓇𝓈"
    if "premium groups" in lowered:
        icon = "🔹"
    elif "free groups" in lowered:
        icon = "✨"
    elif "approved payment" in lowered:
        icon = "✅"
    elif "saved caption" in lowered:
        icon = "📝"
    elif "force join" in lowered:
        icon = "⚙️"
    elif "pending payment" in lowered or "notify" in lowered or "notification" in lowered:
        icon = "🔔"
    elif "dashboard" in lowered or "stats" in lowered:
        icon = "📊"
    elif "admin panel" in lowered:
        icon = "⚙️"
    elif "manage channel" in lowered or "manage channels" in lowered:
        icon = "🗂️"
    elif "contact" in lowered:
        icon = "💬"
    elif "home" in lowered:
        icon = "🏠"
    elif "close" in lowered or "cancel" in lowered or "reject" in lowered:
        icon = "✖️"
    elif "back" in lowered or "previous" in lowered or lowered.startswith("prev"):
        icon = "↩️"
    elif "next" in lowered:
        icon = "➡️"
    elif "checkout" in lowered or "payment" in lowered or "pay" in lowered or "purchase" in lowered:
        icon = "💳"
    elif "cart" in lowered:
        icon = "🛒"
    elif "join" in lowered:
        icon = "🔗"
    elif "delete" in lowered or "remove" in lowered:
        icon = "🗑️"
    elif "edit" in lowered or "rename" in lowered:
        icon = "✏️"
    elif "save" in lowered or "confirm" in lowered or "approve" in lowered or "continue" in lowered:
        icon = "✅"
    elif "caption" in lowered:
        icon = "📝"
    elif "add" in lowered or "create" in lowered:
        icon = "➕"
    elif "search" in lowered:
        icon = "🔍"
    elif "broadcast" in lowered:
        icon = "📣"
    elif "refresh" in lowered or "retry" in lowered:
        icon = "🔄"
    elif "trial" in lowered:
        icon = "🎟️"
    elif "free" in lowered or "offer" in lowered:
        icon = "🎁"
    elif "channel" in lowered or "group" in lowered:
        icon = "🔹"
    elif label[:1].isdigit() and ". " in label:
        icon = "🔹"
    elif re.fullmatch(r"\d+\s*/\s*\d+", label):
        icon = ""
    else:
        icon = "🔹"

    styled_label = _to_sans_serif(label)
    for marker, tag in small_tags.items():
        styled_label = styled_label.replace(marker, tag)
    return f"{icon} {styled_label}" if icon else styled_label


def _to_sans_serif(text):
    """Use the regular (non-bold) mathematical sans-serif Unicode alphabet."""
    styled = []
    for char in text:
        codepoint = ord(char)
        if "A" <= char <= "Z":
            codepoint = 0x1D5A0 + codepoint - ord("A")
        elif "a" <= char <= "z":
            codepoint = 0x1D5BA + codepoint - ord("a")
        elif "0" <= char <= "9":
            codepoint = 0x1D7E2 + codepoint - ord("0")
        styled.append(chr(codepoint))
    return "".join(styled)


def InlineKeyboardButton(text=None, *args, **kwargs):
    """Build a Telegram button with consistent regular sans-serif lettering."""
    if text is None and "text" in kwargs:
        kwargs["text"] = _clean_button_label(kwargs["text"])
    else:
        text = _clean_button_label(text)
    return _TelegramInlineKeyboardButton(text, *args, **kwargs)
