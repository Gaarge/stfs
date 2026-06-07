# users-from-rusprofile

This folder prepares leads from a Rusprofile Excel file.

Expected Excel columns:

- `Порядковый номер` or `№` (optional)
- `ФИО`
- `Ссылка`
- `Телефон`
- `Сайт`

The scripts can add missing columns:

- `Телефон`
- `Сайт`
- `Telegram user_id`
- `Telegram access_hash`
- `Telegram username`

## 1. Fill Phone And Site From Rusprofile

```bash
venv/bin/python users-from-rusprofile/fill_contacts_from_rusprofile.py
```

Quick test:

```bash
venv/bin/python users-from-rusprofile/fill_contacts_from_rusprofile.py --limit 5 --dry-run
```

Default Excel:

```text
users-from-rusprofile/rusprofile_users.xlsx
```

## 2. Find Telegram Users By Phone

```bash
venv/bin/python users-from-rusprofile/phone_to_telegram_users.py --account search
```

Quick test:

```bash
venv/bin/python users-from-rusprofile/phone_to_telegram_users.py --account search --limit 5
```

Output CSV:

```text
users-from-rusprofile/telegram_users_from_phones.csv
```

## Account Selection

For `--account search`, variables are read in this order:

```text
TG_SEARCH_API_ID
TG_SEARCH_API_HASH
TG_SEARCH_PHONE
TG_SEARCH_SESSION
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
users-from-rusprofile/sessions/<account>.session
```

There is already a copied search session:

```text
/home/garg/openAI/sessions/search_79802168820.session
```
