import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = str(Path(temporary_directory.name) / "test.sqlite3")
        path_patch = patch.object(db, "DB_PATH", self.database_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        self.prepare_database()
        db.init_db()

    def prepare_database(self):
        pass

    def query(self, sql, parameters=()):
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]


class TaskTests(DatabaseTestCase):
    def test_two_argument_save_task_defaults_to_queued(self):
        task_id = db.save_task("first task", "keyword")
        self.assertIsInstance(task_id, int)
        task = db.get_all_tasks()[0]
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["requirements"], {})
        self.assertEqual(task["message"], "")
        self.assertEqual(task["progress"], {})
        self.assertEqual(task["creator_count"], 0)
        self.assertEqual(task["updated_at"], task["created_at"])

    def test_requirements_round_trip_and_explicit_status(self):
        requirements = {
            "keyword": "creator's topic",
            "sample_count": 3,
            "min_avg_likes": 1200,
            "active_days": None,
            "content_preferences": ["reviews", "tutorials"],
            "exclusions": [],
        }
        task_id = db.save_task("custom task", "creator's topic", requirements, "running")
        task = db.get_task(task_id)
        self.assertEqual(task["name"], "custom task")
        self.assertEqual(task["keyword"], "creator's topic")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["requirements"], requirements)
        self.assertEqual(task["message"], "")
        self.assertEqual(task["progress"], {})
        self.assertEqual(task, db.get_all_tasks()[0])
        stored = self.query("SELECT requirements_json FROM tasks WHERE id = ?", (task_id,))[0]
        self.assertEqual(json.loads(stored["requirements_json"]), requirements)

    def test_get_task_returns_none_when_missing(self):
        self.assertIsNone(db.get_task(999))

    def test_update_status_message_progress_and_timestamp(self):
        task_id = db.save_task("task", "keyword")
        self.query("UPDATE tasks SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (task_id,))
        db.update_task_status(task_id, "running", "captured first creator", {"processed": 1, "total": 3})
        task = db.get_task(task_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["message"], "captured first creator")
        self.assertEqual(task["progress"], {"processed": 1, "total": 3})
        self.assertNotEqual(task["updated_at"], "2000-01-01 00:00:00")
        db.update_task_status(task_id, "partial", "one candidate failed")
        self.assertEqual(db.get_task(task_id)["progress"], {"processed": 1, "total": 3})
        db.update_task_status(task_id, "failed", progress={})
        task = db.get_task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["message"], "")
        self.assertEqual(task["progress"], {})

    def test_recovery_interrupts_only_queued_and_running_tasks(self):
        active_ids = [db.save_task(status, "keyword", status=status) for status in ("queued", "running")]
        terminal_ids = [
            db.save_task(status, "keyword", status=status)
            for status in ("completed", "complete", "partial", "failed", "interrupted")
        ]
        db.update_task_status(active_ids[1], "running", "in progress", {"processed": 2})
        before = {task_id: db.get_task(task_id) for task_id in terminal_ids}
        db.recover_interrupted_tasks()
        for task_id in active_ids:
            task = db.get_task(task_id)
            self.assertEqual(task["status"], "interrupted")
            self.assertTrue(task["message"])
        self.assertEqual(db.get_task(active_ids[1])["progress"], {"processed": 2})
        self.assertEqual(before, {task_id: db.get_task(task_id) for task_id in terminal_ids})
        recovered = db.get_all_tasks()
        db.recover_interrupted_tasks()
        self.assertEqual(db.get_all_tasks(), recovered)

    def test_task_list_preserves_order_and_creator_count(self):
        first = db.save_task("first", "one")
        second = db.save_task("second", "two")
        db.add_empty_creator_row(first)
        db.add_empty_creator_row(first)
        tasks = db.get_all_tasks()
        self.assertEqual([task["id"] for task in tasks], [second, first])
        self.assertEqual([task["creator_count"] for task in tasks], [0, 2])
        self.assertEqual(db.get_task(first), tasks[1])


class MigrationTests(DatabaseTestCase):
    def prepare_database(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript("""
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );
                INSERT INTO tasks (id, name, keyword, status, created_at)
                VALUES (42, 'historical task', 'old keyword', 'completed', '2020-01-02 03:04:05');
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta_json TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO chat_messages (role, content, meta_json, created_at)
                VALUES ('assistant', 'historical reply', '{"old":true}', '2020-01-02 03:04:05');
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO settings VALUES ('llm_api_key', 'synthetic-test-key');
            """)

    def test_migration_is_idempotent_and_preserves_legacy_records(self):
        db.init_db()
        columns = {column["name"] for column in self.query("PRAGMA table_info(tasks)")}
        self.assertTrue({"requirements_json", "message", "progress_json", "updated_at"} <= columns)
        db.recover_interrupted_tasks()
        task = db.get_task(42)
        self.assertEqual(task["name"], "historical task")
        self.assertEqual(task["keyword"], "old keyword")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["created_at"], "2020-01-02 03:04:05")
        self.assertEqual(task["requirements"], {})
        self.assertEqual(task["progress"], {})
        self.assertEqual(task["message"], "")
        self.assertEqual(task["updated_at"], task["created_at"])
        self.assertEqual(db.get_chat_history()[0]["content"], "historical reply")
        self.assertEqual(db.get_chat_history()[0]["meta"], {"old": True})
        self.assertEqual(db.get_all_settings()["llm_api_key"], "synthetic-test-key")
        new_id = db.save_task("new task", "new keyword")
        self.assertGreater(new_id, 42)
        self.assertEqual(db.get_task(new_id)["status"], "queued")

    def test_repeated_migration_does_not_reset_new_task_fields(self):
        task_id = db.save_task("task", "keyword", {"active_days": None}, "running")
        db.update_task_status(task_id, "partial", "timeout", {"processed": 1})
        before = db.get_task(task_id)
        db.init_db()
        self.assertEqual(db.get_task(task_id), before)


class ChatTests(DatabaseTestCase):
    def test_save_chat_message_returns_inserted_id_and_metadata(self):
        message_id = db.save_chat_message("user", "hello", {"task_id": 7})
        self.assertIsInstance(message_id, int)
        message = db.get_chat_history()[0]
        self.assertEqual(message["id"], message_id)
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["content"], "hello")
        self.assertEqual(message["meta"], {"task_id": 7})
        self.assertGreater(db.save_chat_message("assistant", "reply"), message_id)
        self.assertIsNone(db.get_chat_history()[-1]["meta"])

    def test_history_returns_latest_limit_in_ascending_order(self):
        for index in range(35):
            db.save_chat_message("user", str(index))
        self.assertEqual([message["content"] for message in db.get_chat_history(3)], ["32", "33", "34"])
        self.assertEqual([message["content"] for message in db.get_chat_history()], [str(index) for index in range(5, 35)])
        self.assertEqual(len(db.get_chat_history(100)), 35)
        self.assertEqual(db.get_chat_history(0), [])

    def test_invalid_legacy_metadata_does_not_break_history(self):
        db.save_chat_message("assistant", "old message")
        self.query("UPDATE chat_messages SET meta_json = 'not json'")
        self.assertIsNone(db.get_chat_history()[0]["meta"])

    def test_clear_history_deletes_chat_only(self):
        task_id = db.save_task("task", "keyword")
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator", "followers": 123})
        db.save_snapshot(creator_id, task_id, {"sample_count": 1, "avg_likes": 12})
        db.save_sample_posts(creator_id, task_id, [{"aweme_id": "post", "likes": 12}])
        db.save_contacts(creator_id, [{"type": "email", "value": "test@example.invalid"}])
        db.save_recommendation(creator_id, task_id, {"level": "review", "hard_pass": False})
        db.save_setting("llm_api_key", "synthetic-test-key")
        tables = ("tasks", "creators", "snapshots", "sample_posts", "contacts", "recommendations", "settings")
        before = {table: self.query(f"SELECT * FROM {table}") for table in tables}
        db.save_chat_message("user", "clear me")
        db.save_chat_message("assistant", "clear me too")
        db.clear_chat_history()
        db.clear_chat_history()
        self.assertEqual(db.get_chat_history(), [])
        self.assertEqual(before, {table: self.query(f"SELECT * FROM {table}") for table in tables})


class CreatorRegressionTests(DatabaseTestCase):
    def test_notes_edit_preserves_unknown_snapshot_statistics(self):
        task_id = db.save_task("task", "keyword")
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        db.save_snapshot(creator_id, task_id, {"avg_likes": None, "max_likes": None})
        db.save_recommendation(creator_id, task_id, {})
        row = db.get_task_results(task_id)[0]
        row["bd_notes"] = "only notes changed"
        db.batch_save_task_results(task_id, [row])
        saved = db.get_task_results(task_id)[0]
        self.assertEqual(saved["bd_notes"], "only notes changed")
        self.assertIsNone(saved["avg_likes"])
        self.assertIsNone(saved["max_likes"])
        snapshot = self.query("SELECT avg_likes, max_likes FROM snapshots")[0]
        self.assertEqual(snapshot, {"avg_likes": None, "max_likes": None})

    def test_notes_edit_preserves_known_zero_snapshot_statistics(self):
        task_id = db.save_task("task", "keyword")
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        db.save_snapshot(creator_id, task_id, {"avg_likes": 0, "max_likes": 0})
        db.save_recommendation(creator_id, task_id, {})
        row = db.get_task_results(task_id)[0]
        row["bd_notes"] = "only notes changed"
        db.save_task_result_item(task_id, row)
        saved = db.get_task_results(task_id)[0]
        self.assertEqual(saved["avg_likes"], 0)
        self.assertEqual(saved["max_likes"], 0)
        snapshot = self.query("SELECT avg_likes, max_likes FROM snapshots")[0]
        self.assertEqual(snapshot, {"avg_likes": 0, "max_likes": 0})

    def test_recommendation_preserves_unknown_and_explicit_hard_pass(self):
        task_id = db.save_task("task", "keyword")
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        values=[]
        for recommendation in ({"hard_pass": None}, {"hard_pass": False}, {"hard_pass": True}, {}):
            db.save_recommendation(creator_id, task_id, recommendation)
            values.append(self.query("SELECT hard_pass FROM recommendations ORDER BY id DESC LIMIT 1")[0]['hard_pass'])
        rows = self.query("SELECT hard_pass FROM recommendations ORDER BY id")
        self.assertEqual(values, [None, 0, 1, None])
        self.assertEqual(len(rows),1)

    def test_extracted_contacts_are_unverified_unless_explicitly_verified(self):
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        db.save_contacts(creator_id, [
            {"type": "email", "value": "extracted@example.invalid"},
            {"type": "email", "value": "unverified@example.invalid", "verified": False},
            {"type": "email", "value": "verified@example.invalid", "verified": True},
        ])
        contacts = self.query("SELECT contact_value, verified FROM contacts ORDER BY id")
        self.assertEqual(contacts, [
            {"contact_value": "extracted@example.invalid", "verified": 0},
            {"contact_value": "unverified@example.invalid", "verified": 0},
            {"contact_value": "verified@example.invalid", "verified": 1},
        ])

    def test_existing_contact_verification_is_not_rewritten(self):
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        self.query("""
            INSERT INTO contacts (creator_id, contact_type, contact_value, source, verified, created_at)
            VALUES (?, 'email', 'legacy@example.invalid', 'profile', 1, '2020-01-01')
        """, (creator_id,))
        before = self.query("SELECT * FROM contacts")
        db.init_db()
        db.save_contacts(creator_id, [{"type": "email", "value": "legacy@example.invalid"}])
        self.assertEqual(self.query("SELECT * FROM contacts"), before)

    def test_new_creator_missing_commercial_values_are_unknown(self):
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        db.upsert_creator({"sec_uid": "creator", "nickname": "Updated"})
        row = self.query("SELECT * FROM creators WHERE id = ?", (creator_id,))[0]
        for field in ("star_atlas", "star_id", "price_1_20", "price_21_60", "median_play_30d", "expected_cpm"):
            with self.subTest(field=field):
                self.assertIsNone(row[field])

    def test_new_creator_explicit_commercial_values_are_preserved(self):
        data = {
            "sec_uid": "creator", "nickname": "Creator", "star_atlas": 0, "star_id": "known-star",
            "price_1_20": 0, "price_21_60": 1200, "median_play_30d": 30000, "expected_cpm": 40.0,
        }
        creator_id = db.upsert_creator(data)
        db.upsert_creator({"sec_uid": "creator", "nickname": "Updated"})
        row = self.query("SELECT * FROM creators WHERE id = ?", (creator_id,))[0]
        for field in ("star_atlas", "star_id", "price_1_20", "price_21_60", "median_play_30d", "expected_cpm"):
            with self.subTest(field=field):
                self.assertEqual(row[field], data[field])

    def test_manual_row_commercial_values_remain_unknown_after_saving(self):
        task_id = db.save_task("task", "keyword")
        db.add_empty_creator_row(task_id)
        row = db.get_task_results(task_id)[0]
        for field in ("star_atlas", "star_id", "price_1_20", "price_21_60", "median_play_30d", "expected_cpm"):
            with self.subTest(stage="created", field=field):
                self.assertIsNone(row[field])
        row.update(star_id="", price_1_20=None, price_21_60="", median_play_30d=None, expected_cpm="")
        db.save_task_result_item(task_id, row)
        saved = db.get_task_results(task_id)[0]
        for field in ("star_atlas", "star_id", "price_1_20", "price_21_60", "median_play_30d", "expected_cpm"):
            with self.subTest(stage="saved", field=field):
                self.assertIsNone(saved[field])

    def test_saving_known_commercial_values_keeps_zero_and_numeric_inputs(self):
        task_id = db.save_task("task", "keyword")
        db.add_empty_creator_row(task_id)
        row = db.get_task_results(task_id)[0]
        row.update(star_id="known-star", price_1_20=0, price_21_60="1500", median_play_30d="30000", expected_cpm="50.0")
        db.save_task_result_item(task_id, row)
        saved = db.get_task_results(task_id)[0]
        self.assertEqual(saved["star_id"], "known-star")
        self.assertEqual(saved["price_1_20"], 0)
        self.assertEqual(saved["price_21_60"], 1500)
        self.assertEqual(saved["median_play_30d"], 30000)
        self.assertEqual(saved["expected_cpm"], 50.0)

    def test_legacy_zero_commercial_values_are_not_migrated(self):
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "Creator"})
        self.query("""
            UPDATE creators SET star_atlas = 0, star_id = '', price_1_20 = 0,
                price_21_60 = 0, median_play_30d = 0, expected_cpm = 0
            WHERE id = ?
        """, (creator_id,))
        before = self.query("SELECT * FROM creators")
        db.init_db()
        self.assertEqual(self.query("SELECT * FROM creators"), before)

    def test_manual_row_has_unknown_followers_and_hard_pass(self):
        task_id = db.save_task("task", "keyword")
        created = db.add_empty_creator_row(task_id)
        row = db.get_task_results(task_id)[0]
        self.assertEqual(row["creator_id"], created["creator_id"])
        self.assertIsNone(row["followers"])
        self.assertIsNone(row["hard_pass"])
        self.assertTrue(row["sec_uid"].startswith("manual_"))

    def test_saving_manual_row_does_not_turn_unknown_followers_into_zero(self):
        task_id = db.save_task("task", "keyword")
        db.add_empty_creator_row(task_id)
        row = db.get_task_results(task_id)[0]
        row.update(followers=None, nickname="Edited creator", bd_notes="follow up")
        db.batch_save_task_results(task_id, [row])
        saved = db.get_task_results(task_id)[0]
        self.assertIsNone(saved["followers"])
        self.assertEqual(saved["nickname"], "Edited creator")
        self.assertEqual(saved["bd_notes"], "follow up")

    def test_existing_creator_result_and_contact_functions_are_preserved(self):
        task_id = db.save_task("task", "keyword")
        creator_id = db.upsert_creator({"sec_uid": "creator", "nickname": "First", "followers": 100})
        self.assertEqual(db.upsert_creator({"sec_uid": "creator", "nickname": "Updated", "followers": 200}), creator_id)
        contacts = [{"type": "email", "value": "test@example.invalid", "source": "profile"}]
        db.save_contacts(creator_id, contacts)
        db.save_contacts(creator_id, contacts)
        db.save_snapshot(creator_id, task_id, {"sample_count": 1, "avg_likes": 50, "latest_post_date": "2026-11-01"})
        db.save_sample_posts(creator_id, task_id, [{"aweme_id": "post", "desc": "Example", "likes": 50, "date": "2026-11-01"}])
        db.save_recommendation(creator_id, task_id, {"level": "review", "hard_pass": False})
        result = db.get_task_results()[0]
        self.assertEqual(result["nickname"], "Updated")
        self.assertEqual(result["followers"], 200)
        self.assertEqual(result["hard_pass"], 0)
        self.assertEqual(result["avg_likes"], 50)
        self.assertEqual(len(result["contacts"]), 1)
        self.assertEqual(result["sample_posts"][0]["aweme_id"], "post")
        self.assertEqual(len(db.get_knowledge_base("Updated")), 1)
        db.delete_creator_from_task(task_id, creator_id)
        self.assertEqual(db.get_task_results(task_id), [])
        self.assertEqual(len(db.get_knowledge_base()), 1)
        self.assertEqual(len(db.get_all_tasks()), 1)

    def test_settings_key_behavior_is_unchanged(self):
        db.save_setting("llm_api_key", "synthetic-test-key")
        self.assertEqual(db.get_setting("llm_api_key"), "synthetic-test-key")
        self.assertEqual(db.get_all_settings()["llm_api_key"], "synthetic-test-key")
        db.save_setting("llm_api_key", "")
        self.assertEqual(db.get_setting("llm_api_key"), "")
        self.assertEqual(db.get_setting("missing", "fallback"), "fallback")


class ContactEditingTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.task_id = db.save_task("task", "keyword")
        self.creator_id = db.add_empty_creator_row(self.task_id)["creator_id"]
        self.other_creator_id = db.add_empty_creator_row(self.task_id)["creator_id"]
        contacts = [
            {"type": "\u5fae\u4fe1", "value": "wechat-one"},
            {"type": "\u5fae\u4fe1", "value": "wechat-two"},
            {"type": "\u7535\u8bdd", "value": "telephone-one"},
            {"type": "\u7535\u8bdd", "value": "telephone-two"},
            {"type": "\u624b\u673a", "value": "mobile-one"},
            {"type": "\u624b\u673a", "value": "mobile-two"},
            {"type": "email", "value": "test@example.invalid"},
            {"type": "other", "value": "unrelated-contact"},
        ]
        db.save_contacts(self.creator_id, contacts)
        db.save_contacts(self.other_creator_id, contacts)
        self.before = self.query("SELECT * FROM contacts ORDER BY id")
        self.item = next(row for row in db.get_task_results(self.task_id) if row["creator_id"] == self.creator_id)

    def test_blank_wechat_deletes_only_this_creators_wechat_contacts(self):
        self.item.pop("phone")
        self.item["wechat"] = "  "
        db.save_task_result_item(self.task_id, self.item)
        expected = [row for row in self.before if not (
            row["creator_id"] == self.creator_id and row["contact_type"] == "\u5fae\u4fe1"
        )]
        self.assertEqual(self.query("SELECT * FROM contacts ORDER BY id"), expected)
        saved = next(row for row in db.get_task_results(self.task_id) if row["creator_id"] == self.creator_id)
        self.assertEqual(saved["wechat"], "")
        self.assertEqual(saved["phone"], "telephone-one")

    def test_blank_phone_deletes_telephone_and_mobile_but_not_other_contacts(self):
        self.item.pop("wechat")
        self.item["phone"] = ""
        db.save_task_result_item(self.task_id, self.item)
        expected = [row for row in self.before if not (
            row["creator_id"] == self.creator_id and row["contact_type"] in ("\u7535\u8bdd", "\u624b\u673a")
        )]
        self.assertEqual(self.query("SELECT * FROM contacts ORDER BY id"), expected)
        saved = next(row for row in db.get_task_results(self.task_id) if row["creator_id"] == self.creator_id)
        self.assertEqual(saved["phone"], "")
        self.assertEqual(saved["wechat"], "wechat-one")

    def test_explicit_null_contacts_clear_both_fields(self):
        self.item.update(wechat=None, phone=None)
        db.batch_save_task_results(self.task_id, [self.item])
        expected = [row for row in self.before if not (
            row["creator_id"] == self.creator_id and row["contact_type"] in ("\u5fae\u4fe1", "\u7535\u8bdd", "\u624b\u673a")
        )]
        self.assertEqual(self.query("SELECT * FROM contacts ORDER BY id"), expected)

    def test_omitted_contact_fields_preserve_all_contacts(self):
        self.item.pop("wechat")
        self.item.pop("phone")
        self.item["bd_notes"] = "notes only"
        db.save_task_result_item(self.task_id, self.item)
        self.assertEqual(self.query("SELECT * FROM contacts ORDER BY id"), self.before)

    def test_nonblank_contact_edits_still_persist(self):
        self.item.update(wechat="replacement-wechat", phone="replacement-phone")
        db.save_task_result_item(self.task_id, self.item)
        saved = next(row for row in db.get_task_results(self.task_id) if row["creator_id"] == self.creator_id)
        self.assertEqual(saved["wechat"], "replacement-wechat")
        self.assertEqual(saved["phone"], "replacement-phone")
        self.assertEqual(
            self.query("SELECT * FROM contacts WHERE creator_id = ? ORDER BY id", (self.other_creator_id,)),
            [row for row in self.before if row["creator_id"] == self.other_creator_id],
        )


if __name__ == "__main__":
    unittest.main()


class QualificationStorageTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        from app import qualification
        self.qualification = qualification
        self.task_id = db.save_task('任务', '食品', {'category': '食品', 'keyword': '食品',
                                                'min_avg_likes': 100, 'topic_min_samples': 5,
                                                'topic_min_ratio': 0.6})
        self.creator_id = db.upsert_creator({'sec_uid': 'u1', 'nickname': '合成', 'followers': 1000})
        self.observation = db.save_snapshot(self.creator_id, self.task_id, {
            'avg_likes': 300, 'max_likes': 500, 'days_since_latest': 1,
            'latest_post_date': '2026-11-04 12:00:00'})
        db.save_sample_posts(self.creator_id, self.task_id,
                             [{'aweme_id': f'p{i}', 'desc': '食品', 'likes': 300} for i in range(10)])
        db.save_recommendation(self.creator_id, self.task_id,
                               {'level': '待核实', 'hard_pass': None, 'reason': '合成'})

    def assessment(self):
        requirements = db.get_task(self.task_id)['requirements']
        policy = self.qualification.policy_from_requirements(requirements)
        row = db.get_task_results(self.task_id)[0]
        numeric = self.qualification.evaluate_numeric_conditions(row, requirements)
        topic = {'topic_status': 'matched', 'method': 'text_llm',
                 'posts': [{'aweme_id': f'p{i}', 'decision': 'yes', 'evidence_id': f'p{i}'}
                           for i in range(6)],
                 'account': {'account_type': 'independent_creator',
                             'evidence_ids': ['p0', 'p1'], 'reason': 'Synthetic personal creator across posts'},
                 'creator_pattern': 'consistent_creator', 'sample_count': 6, 'matched_count': 6,
                 'unknown_count': 0, 'evidence_ids': [], 'reason': '合成',
                 'prompt_version': 'v', 'model': 'm', 'error_code': None, 'truncated_ids': [],
                 'ratio': 1.0, 'min_samples': 5, 'min_ratio': 0.6}
        payload = self.qualification.build_assessment(
            row, self.observation, [], requirements, numeric, topic, policy, [])
        payload['topic_reviewed'] = True
        return payload

    def test_assessment_tables_are_created_idempotently_and_old_rows_are_preserved(self):
        db.init_db()
        db.init_db()
        self.assertIn('candidate_assessments', [row['name'] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")])
        self.assertIn('creator_discovery_sources', [row['name'] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")])
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM candidate_assessments'), [{'n': 0}])

    def test_discovery_source_dedupes_with_empty_post_id_not_null(self):
        db.save_discovery_source(self.task_id, self.creator_id, 'user', '食品', '')
        db.save_discovery_source(self.task_id, self.creator_id, 'user', '食品', '')
        db.save_discovery_source(self.task_id, self.creator_id, 'video', '食品', 'v1')
        sources = db.get_discovery_sources(self.task_id, self.creator_id)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]['source_post_id'], '')
        self.assertIsNotNone(sources[0]['source_post_id'])

    def test_current_assessment_matches_observation_requirements_and_policy(self):
        first = db.save_candidate_assessment(self.task_id, self.creator_id,
                                             self.observation, self.assessment())
        current = db.get_current_candidate_assessment(self.task_id, self.creator_id)
        self.assertEqual(current['id'], first)
        self.assertEqual(current['overall_status'], 'eligible')
        self.assertTrue(current['topic_reviewed'])
        # A new observation invalidates the cached assessment without deleting it.
        db.save_snapshot(self.creator_id, self.task_id, {'avg_likes': 300, 'max_likes': 500,
                                                        'days_since_latest': 1,
                                                        'latest_post_date': '2026-11-04 12:00:00'})
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM candidate_assessments'), [{'n': 1}])

    def test_changed_requirements_or_ttl_invalidates_without_losing_history(self):
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET requirements_json=? WHERE id=?',
                         (json.dumps({'category': '食品', 'keyword': '食品', 'min_avg_likes': 9999}), self.task_id))
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM candidate_assessments')[0]['n'], 2)
        with db.get_connection() as conn:
            conn.execute('UPDATE candidate_assessments SET created_at=?', ('2020-01-01 00:00:00',))
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET requirements_json=? WHERE id=?',
                         (json.dumps({'category': '食品', 'keyword': '食品', 'min_avg_likes': 100}), self.task_id))
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))

    def test_observation_posts_limit_is_ten_and_legacy_is_explicit(self):
        posts = db.get_observation_posts(self.creator_id, self.task_id, self.observation, limit=10)
        self.assertEqual(len(posts['posts']), 10)
        self.assertFalse(posts['legacy_fallback'])
        limited = db.get_observation_posts(self.creator_id, self.task_id, self.observation, limit=5)
        self.assertEqual(len(limited['posts']), 5)

    def test_changed_bio_or_posts_invalidates_assessment_without_deleting_history(self):
        for table, field, value in (('creators', 'signature', 'changed bio'),
                                    ('sample_posts', 'desc', 'changed post')):
            with self.subTest(field=field):
                db.save_candidate_assessment(self.task_id, self.creator_id,
                                             self.observation, self.assessment())
                self.assertIsNotNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))
                with db.get_connection() as conn:
                    conn.execute(f'UPDATE {table} SET {field}=?', (value,))
                self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM candidate_assessments')[0]['n'], 2)

    def test_review_cannot_extend_underlying_observation_ttl(self):
        import datetime
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        now = datetime.datetime.now().astimezone()
        with db.get_connection() as conn:
            conn.execute('UPDATE snapshots SET captured_at=?', ((now - datetime.timedelta(hours=25)).isoformat(),))
            conn.execute('UPDATE candidate_assessments SET created_at=?',
                         ((now - datetime.timedelta(hours=23)).isoformat(),))
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))

    def test_changed_current_metrics_cannot_keep_old_eligibility(self):
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        with db.get_connection() as conn:
            conn.execute('UPDATE snapshots SET avg_likes=1')
        current = db.get_current_candidate_assessment(self.task_id, self.creator_id)
        self.assertNotEqual((current or {}).get('overall_status'), 'eligible')

    def test_expired_profile_numeric_evidence_invalidates_recent_review(self):
        requirements = db.get_task(self.task_id)['requirements']
        requirements['min_followers'] = 100
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET requirements_json=?', (json.dumps(requirements),))
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        with db.get_connection() as conn:
            conn.execute("UPDATE creators SET updated_at='2020-01-01 00:00:00'")
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))

    def test_tampered_stored_hash_is_not_accepted(self):
        db.save_candidate_assessment(self.task_id, self.creator_id, self.observation, self.assessment())
        with db.get_connection() as conn:
            conn.execute("UPDATE candidate_assessments SET input_hash='not-the-current-input'")
        self.assertIsNone(db.get_current_candidate_assessment(self.task_id, self.creator_id))
