"""Shared typography cleanup for Telegram inline keyboard buttons."""

import unicodedata

from telebot.types import InlineKeyboardButton as _TelegramInlineKeyboardButton


def _clean_button_label(text):
    if not isinstance(text, str):
        return text

    # Convert legacy decorative alphabets to plain text first, then apply one
    # consistent regular sans-serif Unicode style to button labels.
    label = unicodedata.normalize("NFKC", text).strip()
    if label in ("◀", "◀️", "⬅", "⬅️"):
        return "Previous"
    if label in ("▶", "▶️", "➡", "➡️"):
        return "Next"

    # Remove decorative leading emoji/icons while preserving the actual label.
    # Repeat to handle labels that accidentally have more than one prefix.
    while label and unicodedata.category(label[0]) in ("Mn", "Cf", "Sk", "So"):
        label = label[1:]
    label = label.lstrip()
    if label.casefold() == "offers":
        return "𝒪𝒻𝒻𝑒𝓇𝓈"
    return _to_sans_serif(label or text)


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
