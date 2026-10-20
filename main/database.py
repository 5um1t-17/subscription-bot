"""MongoDB client and collections used by the bot."""

from pymongo import MongoClient

from .config import MONGO_URI


client = MongoClient(MONGO_URI)
db = client["sub_management"]
channels_col = db["channels"]
users_col = db["users"]
payments_col = db["payments"]
seen_users_col = db["seen_users"]
counters_col = db["counters"]
pending_checkouts_col = db["pending_checkouts"]
chat_members_col = db["chat_members"]
tracked_chats_col = db["tracked_chats"]
free_trials_col = db["free_trials"]
free_trial_claims_col = db["free_trial_claims"]
free_trial_claims_history_col = db["free_trial_claims_history"]
pending_reactions_col = db["pending_reactions"]
carts_col = db["carts"]
scheduled_broadcasts_col = db["scheduled_broadcasts"]
waitlist_col = db["waitlist"]
expired_subs_col = db["expired_subs"]
settings_col = db["settings"]
offer_bundles_col = db["offer_bundles"]
pending_offer_bundle_checkouts_col = db["pending_offer_bundle_checkouts"]
fj_pending_requests_col = db["fj_pending_requests"]
approved_payments_col = db["approved_payments"]
