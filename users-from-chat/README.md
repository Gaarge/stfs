# users-from-chat

Collect unique Telegram users who wrote messages in a chat.

Default chat:

```text
https://t.me/freelead
```

The script logs in through a normal Telegram user account with Telethon, not a bot. It saves:

- `user_id`
- `access_hash`
- `username`
- plus message date/name helper columns

`user_id` is unique and stable for Telegram users, so it is the main dedupe key. `username` is also checked as a fallback, because it is optional and can change. `access_hash` is not used for uniqueness.

Output file:

```text
telegram_chat_users.csv
```

## Run

From `/home/garg/openAI`:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account chat
```

Quick test, collect only the first 20 unique accounts found in recent messages:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account chat --limit 20
```

Use an existing sender session/account:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account sender --limit 20
```

Change the time window:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account chat --months 3
```

## Account Selection

For `--account chat`, variables are read in this order:

```text
TG_CHAT_API_ID
TG_CHAT_API_HASH
TG_CHAT_PHONE
TG_CHAT_SESSION
```

For another account, for example `--account main`, use:

```text
TG_MAIN_API_ID
TG_MAIN_API_HASH
TG_MAIN_PHONE
TG_MAIN_SESSION
```

The script also reads `.env` from:

```text
users-from-chat/.env
../.env
./.env
```

If the session is already authorized, the script will not ask for a phone number.
