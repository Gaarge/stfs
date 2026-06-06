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
users-from-coments/venv/bin/python users-from-coments/collect_comment_users.py --account comments
```

Quick test on the latest 20 channel posts:

```bash
users-from-coments/venv/bin/python users-from-coments/collect_comment_users.py --account comments --post-limit 20
```

Limit comments per post:

```bash
users-from-coments/venv/bin/python users-from-coments/collect_comment_users.py --account comments --comment-limit-per-post 100
```

If Telegram login code does not arrive, choose QR login by typing:

```text
qr
```

## Files

- `collect_comment_users.py` - main script.
- `sessions/comments.session` - saved Telegram login session.
- `telegram_comment_users.csv` - latest collected result.
- `venv/` - local Python environment with Telethon installed.
- `requirements.txt` - dependencies if the environment needs to be recreated.

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
