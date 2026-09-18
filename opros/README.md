# opros

`send_queue.py` sends the prepared promotional MP4 with its text as one Telegram message. Recipients come only from the username-registry service: each request atomically reserves a previously unused record, so parallel clients do not receive the same person.

The sender supports any number of Telegram accounts named `senderN`, where `N` is a positive integer: `sender1`, `sender2`, `sender3` and so on. Each account has its own Telegram session, Saved Messages video template, worker and error-recovery state.

Files:

- `message.txt` — text used as the video caption.
- `/home/garg/Загрузки/промо_итог.mp4` — default video.
- `/home/garg/opros/prev.jpg` — permanent video cover.
- `registry_claims.jsonl` — every record returned by the registry.
- `registry_sent.jsonl` — successful sends.
- `registry_send_errors.jsonl` — final failed sends.
- `telegram_recovery.jsonl` — detailed error-recovery events: error, `/start` to `@SpamBot`, wait, retry and account disablement.
- `media-cache/promo-templates/senderN.json` — local reference to the promo message kept in that sender's Saved Messages. It contains no video copy and no Telegram credentials.
- `sessions/<account>.session` — local Telegram sessions by default.

## Configuration

Put the regular registry client key into `opros/.env`, `../.env`, or the current directory's `.env`:

```text
REGISTRY_API_KEY=...
```

For separate Telegram sessions, the usual variables are:

```text
TG_SENDER1_SESSION=/path/to/sender1.session
TG_SENDER2_SESSION=/path/to/sender2.session
TG_SENDER3_SESSION=/path/to/sender3.session
```

`TG_SENDER1_API_ID`, `TG_SENDER1_API_HASH`, `TG_SENDER1_PHONE` (and the corresponding variables for every other sender) can also be set if necessary. Shared `TG_API_ID` and `TG_API_HASH` are accepted as a fallback.

## Commands

From `/home/garg/opros` first validate the text, video and cover without connecting to Telegram or the registry:

```bash
.venv/bin/python opros/send_queue.py --accounts sender1,sender2 --dry-run
```

Test exactly one person from a selected account. This never calls the VPS and does not reserve a recipient:

```bash
.venv/bin/python opros/send_queue.py --account sender1 --test-username @some_username
```

Run any selected accounts continuously. Each account claims one new recipient at a time; they work concurrently:

```bash
.venv/bin/python opros/send_queue.py --accounts sender1,sender2 --yes --delay 60
```

For example, add a third account simply by naming it `sender3`:

```bash
.venv/bin/python opros/send_queue.py --accounts sender1,sender2,sender3 --yes --delay 60
```

On the first real run, each selected sender uploads the 46 MB MP4 once into its own Saved Messages. The script stores that message's ID locally and every later recipient receives a new message made from the existing Telegram media, with the prepared text as its caption. This is not a visible forward and avoids uploading the MP4 from this computer for every recipient. If the MP4 or `prev.jpg` changes, that sender automatically uploads a new template once.

For a small real batch, apply a cap per account:

```bash
.venv/bin/python opros/send_queue.py --accounts sender1,sender2 --max-per-run 5 --yes --delay 60
```

Without `--max-per-run`, the process continues until the registry has no unused records or all selected accounts have been disabled after recovery failures. Before a normal run without `--yes`, it asks for one confirmation before the first registry request.

## Cumulative sender statistics

To see how many unique Telegram accounts each sender has successfully written to, run:

```bash
.venv/bin/python opros/send_stats.py
```

It reads `registry_sent.jsonl`, deduplicates recipients separately for each sender, and also shows raw successful-send events. Test messages are shown separately, so repeated tests of the same person do not inflate the unique-recipient number. Use `--json` when the numbers need to be consumed by another script.

## Telegram error recovery

All workers are independent. A Telegram error from one sender does not pause the others.

- For either sender, the script records the failed recipient and, from that same Telegram account, sends `/start` to `@SpamBot`.
- Once Telegram confirms that `/start` was sent to `@SpamBot`, it waits 2 seconds and retries the original message to the same recipient once.
- If the retry works, that error cycle ends and sending continues.
- If that two-second retry fails, the recipient and Telegram error are written to `registry_send_errors.jsonl`. Only then does that sender wait 120 seconds and claim the next record; the normal `--delay` is not added a second time.
- Two unrecovered error cycles disable only that account. The other accounts keep working. If all selected accounts are disabled, the process stops.

The two-minute cooldowns and Telegram sending stay asynchronous: a waiting or disabled account does not stop the other workers. If `/start` itself cannot be sent to `@SpamBot`, that is logged; the sender waits 120 seconds before moving to the next recipient rather than attempting the two-second retry.
