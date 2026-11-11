# Telegram Subscription Bot

## Project layout

- `bot.py` is the polling and web-health entry point.
- `main/config.py` reads environment settings and admin IDs.
- `main/database.py` creates the MongoDB client and collections.
- `main/admin_caption.py` manages the saved media caption card, commands, and callbacks.
- `main/group_media_caption.py` detects group admin media, preserves album/order handling, and reposts it with the saved caption.
- `requirements.txt` lists the Python dependencies.

Keep the `main` folder beside the entry point and include it in every deployment.

## Configure

Copy `.env.example` to `.env` for local development and fill in the values. Never commit a real `.env` file or paste a bot token into source code.

- `BOT_TOKEN`: token from BotFather.
- `MONGO_URI`: MongoDB connection string. Restrict Atlas network access to the deployment host where practical.
- `ADMIN_ID`: primary admin Telegram user ID. Keep this set to the existing primary admin when adding others.
- `ADMIN_IDS`: optional additional admin Telegram user IDs separated by commas, such as `111111,222222`. Every ID listed here has full bot-admin access, including managing channels, payments, subscribers, and settings. Only add trusted users.
- `UPI_ID` and `CONTACT_USERNAME`: optional payment and contact settings.

On Render, add these values under the service's Environment settings. Render supplies `PORT` for the health endpoint.

## Run

Install dependencies with `pip install -r requirements.txt`, then set the Render start command to `python bot.py`.

The service exposes `/` as a health endpoint and runs one Telegram polling process. Keep a single running instance for the bot token so multiple pollers do not compete for updates.

## Admin caption

The admin panel has a **Saved Caption** card. Configured bot admins can edit or delete the saved caption there. `/setmediacaption` prompts for a caption, and `/setcap <caption>` opens the same HTML preview; changes are saved only after pressing **Save**. In groups and channels, a chat admin can reply to bot-sent media with `/cc` to change its custom caption in place, or `/cc reset` to restore the saved global caption (or clear the caption if none is set). The bot must be able to delete messages in the chat to remove the `/cc` workflow messages; in channels it also needs permission to post the prompt.
