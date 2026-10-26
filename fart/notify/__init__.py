"""Telegram notifications to the owner chat (milestone 2): a durable outbox in SQLite, one sender
(`fart-notify`, 20 s loop), Dutch plain-language messages that end with the fixed footer.

Enabled only when TELEGRAM_NOTIFY_ENABLED=1 and both TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID
exist. Messages go to the owner chat only. The token is used in the request URL and is never logged.
"""
