"""Offline checks for independent Telegram sender recovery workers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("send_queue_under_test", Path(__file__).with_name("send_queue.py"))
assert SPEC and SPEC.loader
sender = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sender
SPEC.loader.exec_module(sender)


class Args:
    def __init__(self, account: str | None = None, accounts: list[str] | None = None) -> None:
        self.account = account
        self.accounts = accounts or []


class FakeClient:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = iter(outcomes)
        self.bot_messages: list[tuple[str, str]] = []

    async def send_file(self, *args, **kwargs):
        outcome = next(self.outcomes)
        if outcome:
            raise outcome

    async def send_message(self, peer, message):
        self.bot_messages.append((peer, message))


class CachedTemplateClient:
    def __init__(self, media):
        self.media = media
        self.upload_attempted = False

    async def get_messages(self, peer, ids):
        self.request = (peer, ids)
        return SimpleNamespace(id=ids, media=self.media)

    async def send_file(self, *args, **kwargs):
        self.upload_attempted = True
        raise AssertionError("existing template must be reused without an upload")


def record(number: int = 1) -> dict[str, str]:
    return {"username": f"person{number}", "user_id": str(1000 + number), "access_hash": str(2000 + number), "chat": "Chat"}


class SenderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_any_positive_sender_number_is_accepted(self):
        self.assertEqual(
            sender.requested_account_names(Args(accounts=["sender1,sender2,sender3,sender10,sender99"])),
            ["sender1", "sender2", "sender3", "sender10", "sender99"],
        )
        for name in ("sender", "main", "sender0", "sender-1", "sender01", "account3"):
            with self.assertRaises(SystemExit):
                sender.requested_account_names(Args(account=name))

    async def test_sender1_contacts_spam_bot_and_successful_retry_resets_cycle(self):
        config = sender.AccountConfig("sender1", 1, "hash", "", Path("session"))
        lead = sender.Lead("person1", 1001, 2001, "Chat")
        template = sender.PromoTemplate(object(), 1, "fingerprint")
        client = FakeClient([None])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = await sender.recover_after_telegram_error(
                config, client, record(), lead, "caption", template, 0,
                root / "sent.jsonl", root / "errors.jsonl", root / "recovery.jsonl",
                retry_wait_seconds=0, next_recipient_wait_seconds=0,
            )
        self.assertEqual(outcome, (True, False, 0))
        self.assertEqual(client.bot_messages, [("SpamBot", "/start")])

    async def test_sender2_contacts_spam_bot_and_two_failed_cycles_disable_it(self):
        config = sender.AccountConfig("sender2", 1, "hash", "", Path("session"))
        lead = sender.Lead("person1", 1001, 2001, "Chat")
        template = sender.PromoTemplate(object(), 1, "fingerprint")
        first_client = FakeClient([RuntimeError("first retry failed")])
        second_client = FakeClient([RuntimeError("second retry failed")])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = await sender.recover_after_telegram_error(
                config, first_client, record(1), lead, "caption", template, 0,
                root / "sent.jsonl", root / "errors.jsonl", root / "recovery.jsonl",
                retry_wait_seconds=0, next_recipient_wait_seconds=0,
            )
            second = await sender.recover_after_telegram_error(
                config, second_client, record(2), lead, "caption", template, first[2],
                root / "sent.jsonl", root / "errors.jsonl", root / "recovery.jsonl",
                retry_wait_seconds=0, next_recipient_wait_seconds=0,
            )
        self.assertEqual(first, (False, False, 1))
        self.assertEqual(second, (False, True, 2))
        self.assertEqual(first_client.bot_messages, [("SpamBot", "/start")])
        self.assertEqual(second_client.bot_messages, [("SpamBot", "/start")])

    async def test_failed_same_recipient_retry_waits_two_then_120_seconds(self):
        config = sender.AccountConfig("sender3", 1, "hash", "", Path("session"))
        lead = sender.Lead("person1", 1001, 2001, "Chat")
        template = sender.PromoTemplate(object(), 1, "fingerprint")
        client = FakeClient([RuntimeError("retry failed")])
        waits: list[float] = []

        async def fake_sleep(seconds: float):
            waits.append(seconds)

        with tempfile.TemporaryDirectory() as directory, patch.object(sender.asyncio, "sleep", fake_sleep):
            root = Path(directory)
            outcome = await sender.recover_after_telegram_error(
                config, client, record(), lead, "caption", template, 0,
                root / "sent.jsonl", root / "errors.jsonl", root / "recovery.jsonl",
                retry_wait_seconds=2, next_recipient_wait_seconds=120,
            )
        self.assertEqual(outcome, (False, False, 1))
        self.assertEqual(waits, [2, 120])

    async def test_existing_saved_message_is_reused_without_uploading_the_mp4(self):
        config = sender.AccountConfig("sender1", 1, "hash", "", Path("session"))
        media = object()
        client = CachedTemplateClient(media)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "video.mp4"
            thumbnail_path = root / "cover.jpg"
            video_path.write_bytes(b"video")
            thumbnail_path.write_bytes(b"cover")
            video = sender.PromoVideo(video_path, 142, 1920, 1080, thumbnail_path)
            fingerprint = sender.promo_template_fingerprint(video)
            cache_path = root / "sender1.json"
            cache_path.write_text('{"fingerprint": "' + fingerprint + '", "message_id": 42}', encoding="utf-8")
            template = await sender.prepare_promo_template(client, config, video, cache_path)

        self.assertEqual(client.request, ("me", 42))
        self.assertIs(template.media, media)
        self.assertFalse(client.upload_attempted)

    async def test_second_worker_can_finish_while_first_is_waiting_for_recovery(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def slow_sender1():
            waiting.set()
            await release.wait()
            return "sender1 finished"

        async def fast_sender2():
            await waiting.wait()
            return "sender2 finished"

        first = asyncio.create_task(slow_sender1())
        second = asyncio.create_task(fast_sender2())
        self.assertEqual(await second, "sender2 finished")
        self.assertFalse(first.done())
        release.set()
        self.assertEqual(await first, "sender1 finished")


if __name__ == "__main__":
    unittest.main()
