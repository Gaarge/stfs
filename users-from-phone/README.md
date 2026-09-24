# users-from-phone

Resolves Telegram delivery identifiers for phone numbers you are authorized to use. Telegram only returns accounts discoverable through its normal contact-import flow; a number may therefore produce no result. A Telegram username is optional, so `user_id` and `access_hash` are also saved for accounts without one.

The tool imports contacts temporarily. By default it deletes only the contacts that this run newly imported after saving the results. Use `--keep-contacts` to leave them in the account. Do not submit numbers without the person's authorization.

## Setup

Create `.env` in this directory or the repository root:

```text
TG_PHONE_LOOKUP_API_ID=your_api_id
TG_PHONE_LOOKUP_API_HASH=your_api_hash
TG_PHONE_LOOKUP_PHONE=+79991234567
```

`API_ID` and `API_HASH` come from [my.telegram.org](https://my.telegram.org). The phone variable is only for logging in to **your** account; it is not an input contact.

Prepare an input CSV, with numbers in international format:

```csv
phone,first_name,last_name
+79991234567,Anna,Ivanova
+447700900123,Sam,Smith
```

## Run

```bash
venv/bin/python users-from-phone/resolve_phone_contacts.py contacts.csv --i-have-consent
```

To authorize and save a session first, without reading a CSV or importing a
contact, run:

```bash
venv/bin/python users-from-phone/resolve_phone_contacts.py --login-only
```

Append `--qr` to print a QR code immediately.

The output defaults to `users-from-phone/telegram_phone_contacts.csv` and contains `phone`, `found`, `user_id`, `access_hash`, `username`, name fields, and `is_bot`. `user_id` plus `access_hash` lets the existing sender address a found person even when they do not have a public username.

For a different configured account, pass `--account sender1`; its variables are `TG_SENDER1_API_ID`, `TG_SENDER1_API_HASH`, `TG_SENDER1_PHONE`, and optionally `TG_SENDER1_SESSION`.

Telegram may rate-limit bulk contact imports. Keep batches small and delays conservative; the default is 25 numbers with 3 seconds between batches. If Telegram returns a wait time, the script stops without retrying automatically.
