# users-from-chat

Collect unique Telegram users who wrote messages in a chat.

Default chat:

```text
https://t.me/freelead
```

If a private chat has no public link, pass its exact visible title with `--chat`, for example `--chat 'Заявочки 💅🏼'`. The authorized account must already have that chat in its dialog list; the script refuses to choose when several dialogs have the same title.

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

## Save directly to the username registry

The collector can also import the collected people directly into the protected registry at `https://lbam.tech/username-registry`. Every imported row is saved with `username`, `user_id`, `access_hash` and the chat label you provide. New rows start with `used = no`.

Set the admin key only in the current terminal, then run the collector:

```bash
export REGISTRY_ADMIN_KEY='admin-key-from-the-server'
venv/bin/python users-from-chat/collect_chat_users.py \
  --account chat \
  --chat 'https://t.me/teachersrooms' \
  --months 6 \
  --save-to-registry \
  --registry-chat 'Учительской'
```

The CSV is still written locally as a backup. The collector imports only new records: people already saved from another chat stay unchanged and are reported as skipped. Rows without a public `username` are imported by their `user_id + access_hash`; those values are enough to write to them later. Only rows without `user_id` or `access_hash` are skipped.

Do not share `REGISTRY_ADMIN_KEY`; regular users only need the separate client key.

## Collect from a list of chats

`collect_chat_list.py` reads all `https://t.me/...` links from a Markdown/text file, scans each available chat for recent authors, and imports only new Telegram accounts into the registry. The `chat` field receives the real Telegram title of each chat.

The script never joins chats. Public chats with readable history can work without membership; private or unavailable chats are recorded as skipped in the summary and do not stop the remaining batch.

```bash
export REGISTRY_ADMIN_KEY='admin-key-from-the-server'
venv/bin/python users-from-chat/collect_chat_list.py \
  --account chat \
  --list '/absolute/path/to/chats.md' \
  --months 6
```

The summary contains counts and error reasons, not collected accounts. By default it is saved as `users-from-chat/telegram_chat_batch_summary.json`.


## Telegram Limits And Access

The script prints clear stop reasons for common Telegram restrictions:

- `FloodWaitError` - Telegram asks this account to wait before reading more.
- `ChannelPrivateError` / `UserNotParticipantError` - the account cannot access the chat or has not joined it.
- `ChatAdminRequiredError` - Telegram requires stronger permissions for this action.
- `UserBannedInChannelError` - the account is banned/restricted in the chat.
- invalid or missing chat username/link.

By default the script stops on `FloodWaitError` and prints how many seconds Telegram asked to wait.

To let it wait automatically only for small limits, pass seconds:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account chat --limit 20 --max-flood-wait 300
```

This means: if Telegram asks to wait 300 seconds or less, the script waits and continues; if Telegram asks for more, it stops.

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
