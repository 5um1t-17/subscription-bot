"""Admin caption settings and reply-based caption editing for bot media."""

import re
import secrets
import time
from html import escape
from html.parser import HTMLParser

import telebot
from telebot.types import InlineKeyboardMarkup

from main.config import is_admin_user_id
from main.ui import InlineKeyboardButton


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class AdminCaptionFeature:
    """Own saved global captions and the /cc per-message editing workflow."""

    setting_id = "group_media_caption"
    custom_setting_prefix = "group_media_custom_caption:"
    pending_preview_ttl = 900
    pending_cc_ttl = 900

    def __init__(self, bot, settings_collection, chat_admin_checker=None, sc_enabled_checker=None):
        self.bot = bot
        self.settings = settings_collection
        self.chat_admin_checker = chat_admin_checker or (lambda message: False)
        self.sc_enabled_checker = sc_enabled_checker or (lambda chat_id: True)
        self.pending_previews = {}
        self.pending_cc = {}
        self.pending_cc_tokens = {}

    def get_caption(self):
        setting = self.settings.find_one({"_id": self.setting_id})
        return (setting.get("caption") or "").strip() if setting else ""

    @staticmethod
    def _visible_text(caption_html):
        parser = _VisibleText()
        try:
            parser.feed(caption_html or "")
            parser.close()
        except Exception:
            return ""
        return "".join(parser.parts).strip()

    @staticmethod
    def _utf16_length(text):
        return len((text or "").encode("utf-16-le")) // 2

    @staticmethod
    def _cancel_text(text):
        value = (text or "").strip()
        return value.casefold() == "cancel" or bool(
            re.fullmatch(r"/cancel(?:@[A-Za-z0-9_]+)?", value, flags=re.IGNORECASE)
        )

    @staticmethod
    def _command_args(message):
        text = (getattr(message, "text", None) or "").strip()
        return text.partition(" ")[2].strip()

    def _is_admin_message(self, message):
        return bool(message.from_user and is_admin_user_id(message.from_user.id))

    def _can_edit_global(self, user_id):
        return bool(user_id and is_admin_user_id(user_id))

    def _show_settings_markup(self, can_edit):
        markup = InlineKeyboardMarkup()
        if can_edit:
            markup.row(
                InlineKeyboardButton("✏️ Edit Caption", callback_data="caption_edit"),
                InlineKeyboardButton("🗑 Delete Caption", callback_data="caption_delete"),
            )
        markup.add(InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_home"))
        markup.add(InlineKeyboardButton("✖️ Close", callback_data="ui_close"))
        return markup

    def show_settings(self, chat_id, message_id=None, admin_user_id=None):
        caption = self.get_caption()
        can_edit = self._can_edit_global(admin_user_id)
        markup = self._show_settings_markup(can_edit)
        text = "📝 <b>Saved Media Caption</b>\n\n" + (caption or "No caption is saved.")
        if not can_edit:
            text += "\n\nOnly a configured bot admin can edit or delete it."
        if message_id:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id,
                    reply_markup=markup, parse_mode="HTML",
                )
            except Exception as error:
                if "message is not modified" not in str(error).lower():
                    raise
        else:
            self.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    def _preview_markup(self, token, delete=False):
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("✓ Save", callback_data=f"caption_save_{token}"),
            InlineKeyboardButton("✖ Cancel", callback_data=f"caption_cancel_{token}"),
        )
        return markup

    def _render_caption_preview(self, chat_id, admin_user_id, caption, message_id=None):
        if not self._can_edit_global(admin_user_id):
            return
        now = time.monotonic()
        self.pending_previews = {
            token: pending for token, pending in self.pending_previews.items()
            if pending["expires_at"] > now
        }
        visible = self._visible_text(caption)
        if self._utf16_length(visible) > 1024:
            self.bot.send_message(chat_id, "Caption must fit Telegram's 1024-unit media caption limit.")
            return
        token = secrets.token_hex(6)
        self.pending_previews[token] = {
            "admin_id": admin_user_id,
            "caption": caption,
            "delete": caption == "",
            "expires_at": time.monotonic() + self.pending_preview_ttl,
        }
        action_text = (
            "<i>Saving this will clear the saved caption.</i>\n\n"
            if caption == "" else ""
        )
        preview = "📝 <b>Preview saved media caption</b>\n\n" + action_text + (caption or "<i>(empty caption)</i>")
        try:
            markup = self._preview_markup(token)
            if message_id:
                self.bot.edit_message_text(
                    preview, chat_id=chat_id, message_id=message_id,
                    reply_markup=markup, parse_mode="HTML",
                )
            else:
                self.bot.send_message(chat_id, preview, reply_markup=markup, parse_mode="HTML")
        except Exception as error:
            self.pending_previews.pop(token, None)
            self.bot.send_message(
                chat_id,
                "Telegram couldn't render that caption as HTML. Check the tags and try again.",
            )
            print(f"[caption] preview render failed admin={admin_user_id}: {error}")

    def _prompt_edit(self, chat_id, admin_user_id, message_id=None):
        text = (
            "Send the caption to append to group admin media. HTML formatting is supported.\n\n"
            "Send /cancel, /cancel@botname, or cancel to leave it unchanged."
        )
        prompt = self.bot.send_message(chat_id, text, reply_to_message_id=message_id)
        self.bot.register_next_step_handler(prompt, self._save_caption_step, admin_user_id)

    def _save_caption_step(self, message, admin_user_id):
        if not self._can_edit_global(admin_user_id) or not message.from_user or message.from_user.id != admin_user_id:
            return
        if message.chat.type != "private":
            return
        caption = (message.text or "").strip()
        if self._cancel_text(caption):
            self.bot.send_message(message.chat.id, "Caption change cancelled.")
            return
        if not caption:
            prompt = self.bot.send_message(message.chat.id, "Caption cannot be empty. Send it again, or /cancel.")
            self.bot.register_next_step_handler(prompt, self._save_caption_step, admin_user_id)
            return
        self._render_caption_preview(message.chat.id, admin_user_id, caption)

    def set_command(self, message):
        if not self._is_admin_message(message):
            return
        if message.chat.type != "private":
            self.bot.reply_to(message, "Please configure the saved media caption in a private chat with the bot.")
            return
        supplied_caption = self._command_args(message)
        if supplied_caption:
            self._render_caption_preview(message.chat.id, message.from_user.id, supplied_caption)
        else:
            self._prompt_edit(message.chat.id, message.from_user.id)

    def edit_callback(self, call):
        if not self._can_edit_global(call.from_user.id):
            self.bot.answer_callback_query(call.id, "Only configured bot admins can edit this setting.", show_alert=True)
            return
        if call.message.chat.type != "private":
            self.bot.answer_callback_query(call.id, "Open the admin panel in a private chat to edit the global caption.", show_alert=True)
            return
        self.bot.answer_callback_query(call.id)
        self._prompt_edit(call.message.chat.id, call.from_user.id)

    def delete_callback(self, call):
        if not self._can_edit_global(call.from_user.id):
            self.bot.answer_callback_query(call.id, "Only configured bot admins can edit this setting.", show_alert=True)
            return
        self.bot.answer_callback_query(call.id)
        self._render_caption_preview(
            call.message.chat.id, call.from_user.id, "", message_id=call.message.message_id
        )

    def preview_callback(self, call):
        action, token = call.data.split("_", 2)[1:]
        pending = self.pending_previews.get(token)
        if not pending or pending["expires_at"] <= time.monotonic():
            self.pending_previews.pop(token, None)
            self.bot.answer_callback_query(call.id, "This preview expired. Start the edit again.", show_alert=True)
            return
        if pending["admin_id"] != call.from_user.id or not self._can_edit_global(call.from_user.id):
            self.bot.answer_callback_query(call.id, "This preview belongs to another admin.", show_alert=True)
            return
        self.pending_previews.pop(token, None)
        if action == "save":
            if pending["delete"]:
                self.settings.delete_one({"_id": self.setting_id})
                answer = "Saved caption cleared."
            else:
                self.settings.update_one(
                    {"_id": self.setting_id},
                    {"$set": {"caption": pending["caption"]}},
                    upsert=True,
                )
                answer = "Saved media caption updated."
            self.bot.answer_callback_query(call.id, answer)
            self.show_settings(call.message.chat.id, call.message.message_id, call.from_user.id)
        else:
            self.bot.answer_callback_query(call.id, "Caption change cancelled.")
            try:
                self.bot.edit_message_text(
                    "Caption change cancelled.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
            except Exception:
                pass

    def _cc_key(self, message):
        sender_chat = getattr(message, "sender_chat", None)
        sender_id = sender_chat.id if sender_chat else (
            message.from_user.id if message.from_user else None
        )
        return (message.chat.id, sender_id) if sender_id is not None else None

    def _is_cc_chat_admin(self, message):
        chat = getattr(message, "chat", None)
        if not chat or getattr(chat, "type", None) not in ("group", "supergroup", "channel"):
            return False
        if chat.type in ("group", "supergroup"):
            try:
                if not self.sc_enabled_checker(chat.id):
                    return False
            except Exception:
                pass
        return self.chat_admin_checker(message)

    def _bot_can_delete_workflow(self, chat_id, chat_type):
        try:
            bot_id = self.bot.get_me().id
            member = self.bot.get_chat_member(chat_id, bot_id)
        except Exception as error:
            print(f"[caption] can't verify workflow deletion rights chat={chat_id}: {error}")
            return False
        status = getattr(member, "status", None)
        if status == "creator":
            return True
        if status != "administrator":
            return False
        if chat_type == "group":
            return True
        return bool(getattr(member, "can_delete_messages", False))

    def _bot_can_post_channel_prompt(self, chat_id):
        try:
            member = self.bot.get_chat_member(chat_id, self.bot.get_me().id)
        except Exception as error:
            print(f"[caption] can't verify channel posting rights chat={chat_id}: {error}")
            return False
        return bool(
            getattr(member, "status", None) == "creator"
            or (
                getattr(member, "status", None) == "administrator"
                and getattr(member, "can_post_messages", False)
            )
        )

    def _target_is_bot_media(self, target):
        if not target or not getattr(target, "from_user", None):
            return False
        try:
            if target.from_user.id != self.bot.get_me().id:
                return False
        except Exception:
            return False
        return any(getattr(target, name, None) for name in (
            "photo", "live_photo", "video", "document", "audio", "animation", "voice"
        ))

    def _custom_setting_id(self, chat_id, message_id):
        return f"{self.custom_setting_prefix}{chat_id}:{message_id}"

    def _html_caption(self, text, entities=None):
        return telebot.formatting.apply_html_entities(text or "", entities or None)

    def _caption_entity_prefix(self, message, global_caption):
        raw_text = getattr(message, "caption", None) or ""
        text = raw_text.strip()
        global_text = self._visible_text(global_caption).strip()
        if not text:
            return ""
        if global_text and text == global_text:
            return ""
        separator_suffix = "\n\n" + global_text if global_text else ""
        if separator_suffix and raw_text.endswith(separator_suffix):
            custom_text = raw_text[:-len(separator_suffix)]
            custom_units = self._utf16_length(custom_text)
            entities = [
                entity for entity in (getattr(message, "caption_entities", None) or [])
                if getattr(entity, "offset", 0) + getattr(entity, "length", 0) <= custom_units
            ]
            return self._html_caption(custom_text, entities)
        if global_text and text.endswith(global_text):
            prefix = text[:-len(global_text)].rstrip()
            if not prefix:
                return ""
        return self._html_caption(raw_text, getattr(message, "caption_entities", None))

    def _current_custom_caption(self, chat_id, target, global_caption):
        doc = self.settings.find_one({
            "_id": self._custom_setting_id(chat_id, target.message_id)
        })
        if doc is not None:
            return doc.get("caption") or ""
        return self._caption_entity_prefix(target, global_caption)

    def _compose_message_caption(self, custom_caption, global_caption):
        if custom_caption and global_caption:
            return f"{custom_caption}\n\n{global_caption}"
        return custom_caption or global_caption or ""

    def _delete_workflow_message(self, chat_id, message_id):
        if message_id is None:
            return
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception as error:
            print(f"[caption] couldn't delete workflow message chat={chat_id} message={message_id}: {error}")

    def _finish_cc_messages(self, state, submitted_message_id=None):
        chat_id = state["chat_id"]
        if submitted_message_id is not None:
            state.setdefault("workflow_message_ids", []).append(submitted_message_id)
        if state.get("cc_token"):
            self.pending_cc_tokens.pop(state["cc_token"], None)
        message_ids = [
            state.get("command_message_id"),
            state.get("prompt_message_id"),
            *state.get("workflow_message_ids", []),
        ]
        for message_id in dict.fromkeys(message_ids):
            self._delete_workflow_message(chat_id, message_id)

    @staticmethod
    def _cc_cancel_markup(token):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✖ Cancel", callback_data=f"cc_cancel_{token}"))
        return markup

    def _cc_cancel_callback(self, call):
        token = call.data.partition("cc_cancel_")[2]
        key = self.pending_cc_tokens.get(token)
        state = self.pending_cc.get(key) if key else None
        if not state or state["expires_at"] <= time.monotonic():
            if key:
                self.pending_cc.pop(key, None)
            self.pending_cc_tokens.pop(token, None)
            self.bot.answer_callback_query(call.id, "This caption edit expired.", show_alert=True)
            if state:
                self._finish_cc_messages(state)
            return
        if call.message.chat.id != state["chat_id"] or not self._is_admin_user_in_chat(
            state["chat_id"], call.from_user.id
        ):
            self.bot.answer_callback_query(call.id, "Only a chat admin can cancel this edit.", show_alert=True)
            return
        self.pending_cc.pop(key, None)
        self.bot.answer_callback_query(call.id, "Caption edit cancelled.")
        self._finish_cc_messages(state)

    def _is_admin_user_in_chat(self, chat_id, user_id):
        if is_admin_user_id(user_id):
            return True
        try:
            member = self.bot.get_chat_member(chat_id, user_id)
            return getattr(member, "status", None) in ("creator", "administrator")
        except Exception:
            return False

    def _start_cc(self, message):
        if not self._is_cc_chat_admin(message):
            return
        chat = message.chat
        target = getattr(message, "reply_to_message", None)
        if not self._target_is_bot_media(target):
            self.bot.reply_to(message, "Reply to media sent by this bot with /cc.")
            return
        if not self._bot_can_delete_workflow(chat.id, chat.type):
            self.bot.reply_to(
                message,
                "I need admin permission to delete messages in this chat so I can remove the /cc command and caption workflow messages.",
            )
            return

        args = self._command_args(message).casefold()
        if args in ("r", "reset"):
            global_caption = self.get_caption()
            current_custom = self._current_custom_caption(chat.id, target, global_caption)
            if not current_custom:
                self._delete_workflow_message(chat.id, message.message_id)
                self.bot.send_message(
                    chat.id,
                    "ℹ️ There is nothing to remove — this media doesn't have any custom caption.",
                )
                return
            try:
                self.bot.edit_message_caption(
                    caption=global_caption,
                    chat_id=chat.id,
                    message_id=target.message_id,
                    parse_mode="HTML" if global_caption else None,
                )
                self.settings.delete_one({
                    "_id": self._custom_setting_id(chat.id, target.message_id)
                })
                self._delete_workflow_message(chat.id, message.message_id)
            except Exception as error:
                self._delete_workflow_message(chat.id, message.message_id)
                err_text = str(error).lower()
                if "message is not modified" in err_text or "not modified" in err_text:
                    self.settings.delete_one({
                        "_id": self._custom_setting_id(chat.id, target.message_id)
                    })
                    self.bot.send_message(
                        chat.id,
                        "ℹ️ There is nothing to remove — this media doesn't have any custom caption.",
                    )
                    return
                print(f"[caption] reset failed chat={chat.id} target={target.message_id}: {error}")
                self.bot.send_message(
                    chat.id,
                    "Couldn't reset that media caption. Check the bot's edit permission and saved caption HTML.",
                )
            return
        if args:
            self.bot.reply_to(message, "Use /cc to replace the custom caption, or reply with /cc reset to restore the saved caption.")
            return
        if chat.type == "channel" and not self._bot_can_post_channel_prompt(chat.id):
            self.bot.reply_to(message, "I need the channel's Post Messages permission to show the caption prompt.")
            return

        key = self._cc_key(message)
        if key is None:
            return
        global_caption = self.get_caption()
        current_custom = self._current_custom_caption(chat.id, target, global_caption)
        prompt_text = (
            "✏️ <b>Current custom caption</b>\n\n"
            + (current_custom or "<i>(none)</i>")
            + "\n\nSend the replacement caption below."
        )
        token = secrets.token_hex(6)
        try:
            prompt_kwargs = {"parse_mode": "HTML"}
            if chat.type in ("group", "supergroup"):
                prompt_kwargs["reply_to_message_id"] = target.message_id
                thread_id = getattr(message, "message_thread_id", None)
                if thread_id is not None:
                    prompt_kwargs["message_thread_id"] = thread_id
            prompt = self.bot.send_message(
                chat.id, prompt_text,
                reply_markup=self._cc_cancel_markup(token),
                **prompt_kwargs,
            )
        except Exception as error:
            print(f"[caption] /cc prompt failed chat={chat.id}: {error}")
            return
        self.pending_cc[key] = {
            "chat_id": chat.id,
            "chat_type": chat.type,
            "admin_key": key[1],
            "command_message_id": message.message_id,
            "prompt_message_id": prompt.message_id,
            "target_message_id": target.message_id,
            "thread_id": getattr(message, "message_thread_id", None),
            "global_caption": global_caption,
            "cc_token": token,
            "workflow_message_ids": [],
            "expires_at": time.monotonic() + self.pending_cc_ttl,
        }
        self.pending_cc_tokens[token] = key
        self._delete_workflow_message(chat.id, message.message_id)

    def _has_pending_cc(self, message):
        key = self._cc_key(message)
        state = self.pending_cc.get(key) if key else None
        if not state:
            return False
        if state["expires_at"] <= time.monotonic():
            self.pending_cc.pop(key, None)
            self._finish_cc_messages(state)
            return False
        return self._is_cc_chat_admin(message)

    def _receive_cc_caption(self, message):
        key = self._cc_key(message)
        state = self.pending_cc.get(key) if key else None
        if not state:
            return
        state.setdefault("workflow_message_ids", []).append(message.message_id)
        submitted = (getattr(message, "text", None) or "").strip()
        if self._cancel_text(submitted):
            self.pending_cc.pop(key, None)
            self._finish_cc_messages(state, message.message_id)
            return
        if not submitted or submitted.startswith("/"):
            response = self.bot.send_message(state["chat_id"], "That command wasn't used as a caption. Send caption text or tap Cancel.")
            state["workflow_message_ids"].append(response.message_id)
            return
        caption = self._compose_message_caption(submitted, state["global_caption"])
        if self._utf16_length(self._visible_text(caption)) > 1024:
            response = self.bot.send_message(state["chat_id"], "The combined caption exceeds Telegram's media caption limit. Send a shorter caption or tap Cancel.")
            state["workflow_message_ids"].append(response.message_id)
            return
        try:
            # Render-check HTML before changing the target media message.
            preview = self.bot.send_message(
                state["chat_id"], caption or "<i>(caption will be cleared)</i>",
                parse_mode="HTML", message_thread_id=state.get("thread_id"),
            )
            state["workflow_message_ids"].append(preview.message_id)
            self.bot.edit_message_caption(
                caption=caption,
                chat_id=state["chat_id"],
                message_id=state["target_message_id"],
                parse_mode="HTML" if caption else None,
            )
            self.settings.update_one(
                {"_id": self._custom_setting_id(state["chat_id"], state["target_message_id"])},
                {"$set": {"caption": submitted}},
                upsert=True,
            )
            self.pending_cc.pop(key, None)
            self._finish_cc_messages(state, message.message_id)
        except Exception as error:
            self.pending_cc.pop(key, None)
            print(f"[caption] /cc edit failed chat={state['chat_id']} target={state['target_message_id']}: {error}")
            try:
                response = self.bot.send_message(
                    state["chat_id"],
                    "Couldn't update that media caption. Check the HTML, caption length, and bot permissions.",
                )
                state["workflow_message_ids"].append(response.message_id)
            except Exception as notice_error:
                print(f"[caption] couldn't report /cc failure chat={state['chat_id']}: {notice_error}")
            finally:
                self._finish_cc_messages(state)

    def register_handlers(self):
        self.bot.message_handler(
            commands=["setmediacaption", "setcap"], func=self._is_admin_message
        )(self.set_command)
        self.bot.message_handler(
            commands=["cc"], func=self._is_cc_chat_admin
        )(self._start_cc)
        self.bot.channel_post_handler(
            commands=["cc"], func=self._is_cc_chat_admin
        )(self._start_cc)
        self.bot.message_handler(
            content_types=["text"], func=self._has_pending_cc
        )(self._receive_cc_caption)
        self.bot.channel_post_handler(
            content_types=["text"], func=self._has_pending_cc
        )(self._receive_cc_caption)
        self.bot.callback_query_handler(func=lambda call: call.data.startswith("cc_cancel_"))(self._cc_cancel_callback)
        self.bot.callback_query_handler(func=lambda call: call.data == "caption_edit")(self.edit_callback)
        self.bot.callback_query_handler(func=lambda call: call.data == "caption_delete")(self.delete_callback)
        self.bot.callback_query_handler(func=lambda call: call.data.startswith(("caption_save_", "caption_cancel_")))(self.preview_callback)
