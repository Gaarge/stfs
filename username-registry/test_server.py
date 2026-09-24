import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

import server


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.registry = server.Registry(os.path.join(self.directory.name, "test.sqlite3"))
        self.registry.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def test_claim_can_happen_only_once(self):
        self.registry.add("@Example", "10001", "-12345", "Чат")
        self.assertEqual(
            self.registry.claim("example"),
            {"available": True, "username": "example", "user_id": "10001", "access_hash": "-12345", "chat": "Чат"},
        )
        self.assertEqual(self.registry.claim("example"), {"available": False, "reason": "already_used"})

    def test_duplicate_username_is_rejected_case_insensitively(self):
        self.registry.add("Example", "10001", "12345", "Чат")
        with self.assertRaises(server.RegistryError) as context:
            self.registry.add("@example", "10002", "12346", "Другой чат")
        self.assertEqual(context.exception.status, 409)

    def test_duplicate_telegram_user_is_rejected(self):
        self.registry.add("first", "10001", "12345", "Чат")
        with self.assertRaises(server.RegistryError) as context:
            self.registry.add("second", "10001", "12345", "Другой чат")
        self.assertEqual(context.exception.error, "duplicate_user_id")

    def test_user_without_username_is_saved_by_telegram_id(self):
        self.registry.add(None, "10001", "12345", "Чат")
        with self.registry.connect() as connection:
            row = connection.execute("SELECT username, user_id, access_hash, used FROM usernames").fetchone()
        self.assertIsNone(row["username"])
        self.assertEqual(row["user_id"], "10001")
        self.assertEqual(row["access_hash"], "12345")
        self.assertEqual(row["used"], 0)

    def test_import_is_not_partially_saved_when_a_duplicate_exists(self):
        self.registry.add("existing", "10001", "12345", "Чат")
        with self.assertRaises(server.RegistryError):
            self.registry.import_records([
                {"username": "new", "user_id": "10002", "access_hash": "12346", "chat": "Новый чат"},
                {"username": "existing", "user_id": "10003", "access_hash": "12347", "chat": "Старый чат"},
            ])
        self.assertEqual(self.registry.claim("new"), {"available": False, "reason": "not_found"})

    def test_import_can_skip_existing_records_when_requested(self):
        self.registry.add("existing", "10001", "12345", "Первый чат")
        imported, skipped = self.registry.import_records([
            {"username": "existing", "user_id": "10001", "access_hash": "12345", "chat": "Второй чат"},
            {"username": "new", "user_id": "10002", "access_hash": "12346", "chat": "Второй чат"},
        ], ignore_existing=True)
        self.assertEqual((imported, skipped), (1, 1))
        with self.registry.connect() as connection:
            existing_chat = connection.execute("SELECT chat FROM usernames WHERE user_id = '10001'").fetchone()["chat"]
        self.assertEqual(existing_chat, "Первый чат")

    def test_empty_legacy_database_is_migrated(self):
        legacy_path = os.path.join(self.directory.name, "legacy.sqlite3")
        connection = server.sqlite3.connect(legacy_path)
        connection.execute("CREATE TABLE usernames (username TEXT PRIMARY KEY, chat TEXT, used INTEGER, used_at TEXT)")
        connection.close()

        migrated = server.Registry(legacy_path)
        migrated.initialize()
        with migrated.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(usernames)")}
        self.assertTrue({"username", "user_id", "access_hash", "chat", "used", "used_at"}.issubset(columns))

    def test_current_database_is_migrated_without_losing_records(self):
        legacy_path = os.path.join(self.directory.name, "current.sqlite3")
        connection = server.sqlite3.connect(legacy_path)
        connection.execute("""
            CREATE TABLE usernames (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                user_id TEXT NOT NULL UNIQUE,
                access_hash TEXT NOT NULL,
                chat TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                used_at TEXT
            )
        """)
        connection.execute(
            "INSERT INTO usernames VALUES (?, ?, ?, ?, ?, ?)",
            ("existing", "10001", "12345", "Чат", 0, None),
        )
        connection.commit()
        connection.close()

        migrated = server.Registry(legacy_path)
        migrated.initialize()
        with migrated.connect() as connection:
            row = connection.execute("SELECT username, user_id, access_hash, chat FROM usernames").fetchone()
            info = list(connection.execute("PRAGMA table_info(usernames)"))
        self.assertEqual(dict(row), {"username": "existing", "user_id": "10001", "access_hash": "12345", "chat": "Чат"})
        self.assertEqual(next(column["name"] for column in info if column["pk"]), "user_id")

    def test_claim_next_uses_import_and_add_order(self):
        self.registry.import_records([
            {"username": "first", "user_id": "10001", "access_hash": "12345", "chat": "Первый"},
            {"username": "second", "user_id": "10002", "access_hash": "12346", "chat": "Второй"},
        ])
        self.registry.add("third", "10003", "12347", "Третий")
        result = self.registry.claim_next(2)
        self.assertTrue(result["available"])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["claimed"], 2)
        self.assertFalse(result["exhausted"])
        self.assertEqual([row["username"] for row in result["items"]], ["first", "second"])
        final = self.registry.claim_next()
        self.assertEqual(final["item"]["username"], "third")
        self.assertFalse(final["exhausted"])
        self.assertEqual(self.registry.claim_next(5), {"available": False, "requested": 5, "claimed": 0, "items": [], "exhausted": True})

    def test_simultaneous_claim_next_calls_do_not_overlap(self):
        for number in range(1, 7):
            self.registry.add(f"person{number}", str(10000 + number), str(20000 + number), "Чат")
        with ThreadPoolExecutor(max_workers=2) as executor:
            left, right = list(executor.map(lambda _: self.registry.claim_next(3), range(2)))
        issued = [row["user_id"] for result in (left, right) for row in result["items"]]
        self.assertEqual(len(issued), 6)
        self.assertEqual(len(set(issued)), 6)
        self.assertEqual(set(issued), {str(10000 + number) for number in range(1, 7)})

    def test_claim_next_size_is_validated(self):
        self.assertEqual(server.normalize_claim_next_size(None), 1)
        self.assertEqual(server.normalize_claim_next_size("5"), 5)
        for value in (0, -1, True, "not-a-number", 10001):
            with self.assertRaises(server.RegistryError):
                server.normalize_claim_next_size(value)


if __name__ == "__main__":
    unittest.main()
