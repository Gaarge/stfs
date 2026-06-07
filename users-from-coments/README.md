# users-from-coments

Collect unique Telegram users from comments under posts in the public channel `@portnyaginlive`.

The script uses your Telegram user account through Telethon, not a bot. It saves:

- `user_id`
- `access_hash`
- `username`

Output file:

```text
telegram_comment_users.csv
```

## Run

From the project root:

```bash
venv/bin/python users-from-coments/collect_comment_users.py --account comments
```

Quick test on the latest 20 channel posts:

```bash
venv/bin/python users-from-coments/collect_comment_users.py --account comments --post-limit 20
```

Limit comments per post:

```bash
venv/bin/python users-from-coments/collect_comment_users.py --account comments --comment-limit-per-post 100
```

If Telegram login code does not arrive, choose QR login by typing:

```text
qr
```

## Files

- `collect_comment_users.py` - main script.
- `/home/garg/openAI/sessions/comments_79802168820.session` - saved Telegram login session.
- `telegram_comment_users.csv` - latest collected result.
- `/home/garg/openAI/venv/` - shared Python environment.
- `/home/garg/openAI/requirements.txt` - shared dependencies.

## Account Selection

For `--account comments`, variables are read in this order:

```text
TG_COMMENTS_API_ID
TG_COMMENTS_API_HASH
TG_COMMENTS_PHONE
TG_COMMENTS_SESSION
```

For another account, for example `--account main`, use:

```text
TG_MAIN_API_ID
TG_MAIN_API_HASH
TG_MAIN_PHONE
TG_MAIN_SESSION
```
