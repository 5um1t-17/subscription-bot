"""Environment configuration shared by the bot entry point and features."""

import os
import re
from threading import local


def _parse_admin_ids(*values):
    parsed = []
    for value in values:
        for raw_id in re.split(r"[,;\s]+", value or ""):
            raw_id = raw_id.strip()
            if raw_id.isdigit():
                numeric_id = int(raw_id)
                if numeric_id not in parsed:
                    parsed.append(numeric_id)
    return parsed


BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
admin_id_env = os.getenv("ADMIN_ID")
admin_ids_env = os.getenv("ADMIN_IDS", "")

_primary_admin_candidates = _parse_admin_ids(admin_id_env)
_additional_admin_candidates = _parse_admin_ids(admin_ids_env)
CONFIGURED_ADMIN_ID = (_primary_admin_candidates or _additional_admin_candidates or [0])[0]
ADMIN_ID = CONFIGURED_ADMIN_ID
ADMIN_USER_IDS = set(_primary_admin_candidates + _additional_admin_candidates)
_admin_request_context = local()


def is_admin_user_id(user_id):
    try:
        return int(user_id) in ADMIN_USER_IDS
    except (TypeError, ValueError):
        return False


def current_admin_id():
    user_id = getattr(_admin_request_context, "user_id", None)
    return user_id if is_admin_user_id(user_id) else ADMIN_ID


UPI_ID = os.getenv("UPI_ID")
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME")


def contact_admin_url():
    if CONTACT_USERNAME:
        return f"https://t.me/{CONTACT_USERNAME}"
    return None
