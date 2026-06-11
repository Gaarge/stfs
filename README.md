# openAI services

Top-level services:

- `users-from-coments` - collect `user_id`, `access_hash`, `username` from comments under posts in `@portnyaginlive`.
- `users-from-chat` - collect unique `user_id`, `access_hash`, `username` from people who wrote in a Telegram chat.
- `users-from-rusprofile` - fill phone/site/email from Rusprofile Excel links and find Telegram users by phone.
- `opros` - generate OpenRouter messages and send them to queued Telegram users.

Shared files:

- `.env` - one environment file for all services.
- `requirements.txt` - all Python dependencies.
- `venv/` - one shared virtualenv.
- `sessions/` - centralized Telegram sessions with clear names.
- `start_clicker.sh` - helper used by `opros` before Telegram retry.

## Setup

Install dependencies into the shared virtualenv:

```bash
venv/bin/python -m pip install -r requirements.txt
```

## Telegram Accounts

Every Telegram service accepts `--account`.

Use these common accounts:

```text
sender    sends outreach messages, currently @dev_all_sites / +79266391488
chat      reads @freelead chat, can point to the same session as sender
search    searches Telegram users by phone
comments  reads comments under channel posts
main      legacy/default account
```

The `.env` pattern is:

```text
TG_<ACCOUNT>_API_ID=...
TG_<ACCOUNT>_API_HASH=...
TG_<ACCOUNT>_PHONE=...
TG_<ACCOUNT>_SESSION=/home/garg/openAI/sessions/<clear-name>.session
```

Examples:

```text
TG_SENDER_SESSION=/home/garg/openAI/sessions/sender_dev_all_sites.session
TG_SEARCH_SESSION=/home/garg/openAI/sessions/search_79802168820.session
TG_COMMENTS_SESSION=/home/garg/openAI/sessions/comments_79802168820.session
TG_CHAT_SESSION=/home/garg/openAI/sessions/sender_dev_all_sites.session
```

If a session is already authorized, the scripts will not ask for a phone number.

## Commands

Send generated outreach messages:

```bash
venv/bin/python opros/send_queue.py --account sender --yes --delay 120
```

Safe send test:

```bash
venv/bin/python opros/send_queue.py --account sender --dry-run --max-per-run 1 --yes
```

Collect first 20 unique users from `@freelead`:

```bash
venv/bin/python users-from-chat/collect_chat_users.py --account chat --limit 20
```

Collect users from comments:

```bash
venv/bin/python users-from-coments/collect_comment_users.py --account comments
```

Fill Rusprofile phone/site data:

```bash
venv/bin/python users-from-rusprofile/fill_contacts_from_rusprofile.py
```

Convert saved Rusprofile search HTML to Excel:

```bash
venv/bin/python users-from-rusprofile/rusprofile_search_txt_to_excel.py
```

Fill Rusprofile email queue from `/id/...` links:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py
```

Find Telegram users by phone:

```bash
venv/bin/python users-from-rusprofile/phone_to_telegram_users.py --account search
```

If Telegram login code does not arrive, choose QR login by typing:

```text
qr
```
