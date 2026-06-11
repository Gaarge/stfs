# opros-email

This folder generates a personal OpenRouter message for each email from a Rusprofile Excel file or text queue and sends it through Gmail SMTP.

Default input:

```text
/home/garg/openAI/users-from-rusprofile/rusprofile_users_from_test.xlsx
```

The Excel file must contain an `Email` column. Optional columns such as `ФИО`, `Сайт`, and `Ссылка` are read if present.

Current text queue:

```text
/home/garg/openAI/users-from-rusprofile/rusprofile_email_queue.txt
```

The text queue may contain one email per line. The sender reads a snapshot of the file when it starts, so another script may keep appending new emails while this one is sending.

## Setup

Add the OpenRouter key and Gmail app password to `/home/garg/openAI/.env`:

```text
OPENROUTER_API_KEY=...
GMAIL_APP_PASSWORD=...
```

The Gmail sender is set to:

```text
vladram3707@gmail.com
```

Gmail usually requires an app password for SMTP login, not the regular account password.

If SMTP ports `465` and `587` do not work through Tailscale exit node, use Gmail API over HTTPS instead of SMTP:

```bash
venv/bin/python -m pip install -r requirements.txt
```

Then create an OAuth Desktop client in Google Cloud Console, download its JSON, and save it as:

```text
opros-email/gmail_credentials.json
```

The first Gmail API run opens a Google login page and saves the token to:

```text
opros-email/gmail_token.json
```

Do not commit `gmail_credentials.json` or `gmail_token.json`.

## Safe Test

Print the first 3 emails without connecting to Gmail:

```bash
venv/bin/python opros-email/send_email_queue.py --use-static-message --message "Тестовое сообщение" --dry-run --max-per-run 3 --yes
```

Generate real OpenRouter text for 1 email, but do not send:

```bash
venv/bin/python opros-email/send_email_queue.py --dry-run --max-per-run 1 --yes
```

Generate real OpenRouter text from the text queue, but do not send:

```bash
venv/bin/python opros-email/send_email_queue.py --queue users-from-rusprofile/rusprofile_email_queue.txt --dry-run --max-per-run 1 --yes
```

## Send

Send emails one by one:

```bash
venv/bin/python opros-email/send_email_queue.py --yes
```

Send from the text queue:

```bash
venv/bin/python opros-email/send_email_queue.py --queue users-from-rusprofile/rusprofile_email_queue.txt --yes
```

Recommended batched run from the text queue:

```bash
venv/bin/python opros-email/send_email_queue.py \
  --queue users-from-rusprofile/rusprofile_email_queue.txt \
  --yes \
  --max-total 450 \
  --batch-size 30 \
  --delay 120 \
  --batch-pause 600
```

This sends up to 450 emails per run, 30 emails per batch, waits 120 seconds between emails, and waits 10 minutes after each full batch.

Recommended batched run through Gmail API instead of SMTP:

```bash
venv/bin/python opros-email/send_email_queue.py \
  --send-method gmail-api \
  --queue users-from-rusprofile/rusprofile_email_queue.txt \
  --yes \
  --max-total 450 \
  --batch-size 30 \
  --delay 120 \
  --batch-pause 600
```

Small real test:

```bash
venv/bin/python opros-email/send_email_queue.py \
  --queue users-from-rusprofile/rusprofile_email_queue.txt \
  --yes \
  --max-total 3 \
  --batch-size 3 \
  --delay 120 \
  --batch-pause 600
```

Useful options:

- `--max-total 450` - max emails in this run. The script hard-caps this at 450.
- `--batch-size 30` - emails before a long pause.
- `--delay 120` - seconds between successful emails inside a batch.
- `--batch-pause 600` - seconds to pause after every full batch.
- `--max-per-run N` - old alias limit; the effective limit is `min(--max-total, --max-per-run, 450)`.

SMTP network options:

- `--smtp-ipv4` is enabled by default, so SMTP connections use IPv4 first.
- `--smtp-fallback` is enabled by default, so if `465/SSL` fails the script also tries `587/STARTTLS`.
- `--smtp-timeout 30` controls the SMTP connect timeout.

If you see `Network is unreachable`, ping can still work because ping uses ICMP while Gmail SMTP uses TCP ports `465` or `587`. The script now logs each SMTP connection attempt separately.

## State Files

- `email_users_processed.jsonl` - successfully sent emails, skipped on future runs.
- `send_errors.jsonl` - failed sends and generation errors.
- `message.txt` - optional static text for `--use-static-message`.

Every real successful send is appended to `email_users_processed.jsonl`, so the next run skips that email. The final summary is printed even when the run stops because of an error or `Ctrl+C`.

The final contact block is appended by the script to every message:

```text
Если для Вас это актуально, то свяжитесь со мной: tg - @dev_all_sites или позвоните по телефону - 89162040241
```
