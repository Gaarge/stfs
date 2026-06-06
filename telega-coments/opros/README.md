# opros

This folder generates a personal OpenRouter message for each lead and sends it in Telegram.

Files:

- `leads_queue.txt` - queue file.
- `leads_queue_processed.txt` - successfully sent leads.
- `message.txt` - optional static message text for `--use-static-message`.
- `send_errors.jsonl` - created automatically for failed sends.
- `sessions/sender.session` - copied sender Telegram session.

## Queue Formats

The queue may contain JSONL:

```json
{"user_id": 123, "access_hash": 456, "username": "username", "phone": "+79999999999", "site": "https://example.com"}
```

Or simple CSV-like lines:

```text
"123","456","username"
"123","456","username","+79999999999"
```

If a queue row has a phone, duplicate checks use the phone only.
If it has no phone, duplicate checks use `user_id`, `access_hash`, or `username`.

## Run

Add `OPENROUTER_API_KEY` and Telegram account variables to `.env`.
The script reads `.env` from these locations:

```text
opros/.env
telega-coments/.env
../.env
./.env
```

When running from `/home/garg/openAI`, `../.env` is `/home/garg/openAI/.env`.

Then run:

```bash
users-from-coments/venv/bin/python opros/send_queue.py --account sender --yes
```

Safe test:

```bash
users-from-coments/venv/bin/python opros/send_queue.py --account sender --dry-run --max-per-run 3
```

Set delay:

```bash
users-from-coments/venv/bin/python opros/send_queue.py --account sender --yes --delay 120
```

Telegram send errors:

If Telegram returns an error while sending a message, the script runs `start_clicker.sh`, waits 4 minutes, and retries the same message to the same lead once. If the retry fails too, the script stops. If the retry succeeds, the queue continues.

The retry delay and clicker path can be changed:

```bash
users-from-coments/venv/bin/python opros/send_queue.py --account sender --yes --telegram-retry-sleep 240 --clicker-script ../pipline/start_clicker.sh
```

Static-message fallback:

```bash
users-from-coments/venv/bin/python opros/send_queue.py --account sender --use-static-message --yes
```

## Account Selection

For `--account sender`, variables are read in this order:

```text
TG_SENDER_API_ID
TG_SENDER_API_HASH
TG_SENDER_PHONE
TG_SENDER_SESSION
```

For another account, for example `--account main`, use:

```text
TG_MAIN_API_ID
TG_MAIN_API_HASH
TG_MAIN_PHONE
TG_MAIN_SESSION
```

If `TG_<ACCOUNT>_SESSION` is not set, the session file is:

```text
opros/sessions/<account>.session
```
