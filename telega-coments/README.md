# telega-coments

Project folders:

- `users-from-coments` - collect `user_id`, `access_hash`, `username` from comments under posts in `@portnyaginlive`.
- `users-from-rusprofile` - fill phone/site from Rusprofile Excel links and find Telegram users by phone.
- `opros` - send Telegram messages to queued users and mark successful sends in `leads_queue_processed.txt`.

## Telegram Accounts

Telegram scripts support account selection with `--account`.

For example:

```bash
users-from-coments/venv/bin/python users-from-rusprofile/phone_to_telegram_users.py --account search
users-from-coments/venv/bin/python opros/send_queue.py --account sender
users-from-coments/venv/bin/python users-from-coments/collect_comment_users.py --account comments
```

Environment variables use this pattern:

```text
TG_<ACCOUNT>_API_ID
TG_<ACCOUNT>_API_HASH
TG_<ACCOUNT>_PHONE
TG_<ACCOUNT>_SESSION
```

For `--account main`:

```text
TG_MAIN_API_ID=...
TG_MAIN_API_HASH=...
TG_MAIN_PHONE=...
TG_MAIN_SESSION=sessions/main.session
```

If session is already authorized, the scripts will not ask for a phone number.

## Dependencies

Each folder has its own `requirements.txt`. The existing local virtualenv is:

```text
users-from-coments/venv
```

Install all dependencies into it:

```bash
users-from-coments/venv/bin/pip install -r users-from-rusprofile/requirements.txt -r opros/requirements.txt
```

If Telegram login code does not arrive, choose QR login by typing:

```text
qr
```
