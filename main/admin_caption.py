"""Admin UI and persistence for the caption appended to group admin media."""

from html.parser import HTMLParser

from telebot.types import InlineKeyboardMarkup

from main.ui import InlineKeyboardButton


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class AdminCaptionFeature:
    """Own the saved-caption commands, callbacks, and settings card."""

    setting_id = "group_media_caption"

    def __init__(self, bot, settings_collection, configured_admin_id):
        self.bot = bot
        self.settings = settings_collection
        self.configured_admin_id = configured_admin_id

    def get_caption(self):
        setting = self.settings.find_one({"_id": self.setting_id})
        return (setting.get("caption") or "").strip() if setting else ""

    def _visible_text(self, caption_html):
        parser = _VisibleText()
        try:
            parser.feed(caption_html or "")
            parser.close()
        except Exception:
            return ""
        return "".join(parser.parts).strip()

    def _is_configured_admin_message(self, message):
        return bool(
            self.configured_admin_id
            and message.from_user
            and message.from_user.id == self.configured_admin_id
        )

    def _can_edit(self, user_id):
        return bool(self.configured_admin_id and user_id == self.configured_admin_id)

    def show_settings(self, chat_id, message_id=None, admin_user_id=None):
        caption = self.get_caption()
        can_edit = self._can_edit(admin_user_id)
        markup = InlineKeyboardMarkup()
        if can_edit:
            markup.row(
                InlineKeyboardButton("✏️ Edit Caption", callback_data="caption_edit"),
                InlineKeyboardButton("🗑 Delete Caption", callback_data="caption_delete"),
            )
        markup.add(InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_home"))
        markup.add(InlineKeyboardButton("✖️ Close", callback_data="ui_close"))
        text = "📝 <b>Saved Media Caption</b>\n\n" + (caption or "No caption is saved.")
        if not can_edit:
            text += "\n\nOnly the configured caption admin can edit or delete it."
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

    def _prompt_edit(self, chat_id):
        prompt = self.bot.send_message(
            chat_id,
            "Send the caption to append to group admin media. HTML formatting is supported.\n\n"
            "Send /cancel to leave the current caption unchanged.",
        )
        self.bot.register_next_step_handler(prompt, self._save_caption_step)

    def _save_caption_step(self, message):
        if not self._is_configured_admin_message(message) or message.chat.type != "private":
            return
        caption = (message.text or "").strip()
        if caption.lower() in ("/cancel", "cancel"):
            self.bot.send_message(message.chat.id, "Caption edit canceled.")
            return
        if not caption:
            prompt = self.bot.send_message(message.chat.id, "Caption cannot be empty. Send it again, or /cancel.")
            self.bot.register_next_step_handler(prompt, self._save_caption_step)
            return
        if len(self._visible_text(caption)) > 1024:
            prompt = self.bot.send_message(message.chat.id, "Caption must be 1024 visible characters or fewer. Send it again, or /cancel.")
            self.bot.register_next_step_handler(prompt, self._save_caption_step)
            return
        self.settings.update_one(
            {"_id": self.setting_id}, {"$set": {"caption": caption}}, upsert=True,
        )
        self.bot.send_message(message.chat.id, "✅ Saved media caption updated.")
        self.show_settings(message.chat.id, admin_user_id=message.from_user.id)

    def set_command(self, message):
        if message.chat.type != "private":
            self.bot.reply_to(message, "Please set the saved media caption in a private chat with the bot.")
            return
        self._prompt_edit(message.chat.id)

    def edit_callback(self, call):
        if not self._can_edit(call.from_user.id):
            self.bot.answer_callback_query(call.id, "Only the configured caption admin can edit this setting.", show_alert=True)
            return
        self.bot.answer_callback_query(call.id)
        self._prompt_edit(call.message.chat.id)

    def delete_callback(self, call):
        if not self._can_edit(call.from_user.id):
            self.bot.answer_callback_query(call.id, "Only the configured caption admin can delete this setting.", show_alert=True)
            return
        self.settings.delete_one({"_id": self.setting_id})
        self.bot.answer_callback_query(call.id, "Saved caption deleted.")
        self.show_settings(call.message.chat.id, call.message.message_id, call.from_user.id)

    def register_handlers(self):
        self.bot.message_handler(
            commands=["setmediacaption"], func=self._is_configured_admin_message
        )(self.set_command)
        self.bot.callback_query_handler(func=lambda call: call.data == "caption_edit")(self.edit_callback)
        self.bot.callback_query_handler(func=lambda call: call.data == "caption_delete")(self.delete_callback)
