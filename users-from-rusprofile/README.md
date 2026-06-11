# users-from-rusprofile

This folder prepares leads from a Rusprofile Excel file.

Expected Excel columns:

- `Порядковый номер` or `№` (optional)
- `ФИО`
- `Ссылка`
- `Телефон`
- `Сайт`
- `Email`

The scripts can add missing columns:

- `Телефон`
- `Сайт`
- `Email`
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

## 3. Fill Email Queue From Rusprofile ID Links

For Excel files with rows like:

```text
№ | ФИО | Ссылка
1 | Иванов Иван Иванович | /id/318774600076574
```

Run:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py
```

Quick test:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py --limit 5 --dry-run
```

The script opens each `https://www.rusprofile.ru/id/...` page through `curl`, picks a random User-Agent from 12 variants, fills the `Email` column, and appends unique emails to:

```text
users-from-rusprofile/rusprofile_email_queue.txt
```

If emails are visible only inside your paid Rusprofile account, pass the logged-in browser cookies to curl:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py \
  --xlsx users-from-rusprofile/rusprofile_users_from_test.xlsx \
  --cookie 'name1=value1; name2=value2'
```

Or put that same raw Cookie value into `cookies.txt` and pass it as a file:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py \
  --xlsx users-from-rusprofile/rusprofile_users_from_test.xlsx \
  --cookie-file cookies.txt
```

If Rusprofile still returns the page as if you are not logged in, pass the same User-Agent that was shown in DevTools for the browser request:

```bash
venv/bin/python users-from-rusprofile/fill_emails_from_rusprofile_ids.py \
  --xlsx users-from-rusprofile/rusprofile_users_from_test.xlsx \
  --cookie-file cookies.txt \
  --user-agent 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 YaBrowser/25.2.0.0 Safari/537.36'
```

Do not commit cookies. A live cookie can give access to your Rusprofile account until the session expires.

## 4. Convert Rusprofile HTML TXT To Excel

If `test.txt` contains Rusprofile search HTML with links like `/id/318774600076574`, convert it to Excel:

```bash
venv/bin/python users-from-rusprofile/rusprofile_search_txt_to_excel.py
```

Default input:

```text
test.txt
```

Default output:

```text
users-from-rusprofile/rusprofile_users_from_test.xlsx
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
