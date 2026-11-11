import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


class XingtuIdentityStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temporary) / 'identity.db')))
        db.init_db()

    def rows(self, sql='SELECT * FROM creators ORDER BY id', parameters=()):
        connection = db.get_connection()
        try:
            with connection:
                return [dict(row) for row in connection.execute(sql, parameters)]
        finally:
            connection.close()

    def test_migration_preserves_legacy_records_and_defaults_source(self):
        self.rows('DROP TABLE creators')
        self.rows('''CREATE TABLE creators (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sec_uid TEXT NOT NULL UNIQUE,
            unique_id TEXT, uid TEXT, nickname TEXT NOT NULL, avatar_url TEXT,
            followers INTEGER, total_likes INTEGER, aweme_count INTEGER,
            signature TEXT, star_atlas INTEGER, city TEXT, profile_url TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, star_id TEXT)''')
        for values in ((7, 'real-sec', 'core-7', 'star-7'),
                       (8, 'manual_old', None, None)):
            self.rows('''INSERT INTO creators
                (id,sec_uid,uid,star_id,nickname,followers,created_at,updated_at)
                VALUES (?,?,?,?, 'Legacy',42,'2020-01-01','2020-01-02')''', values)
        before = self.rows()
        db.init_db()
        db.init_db()
        after = self.rows()
        for old, migrated in zip(before, after):
            self.assertEqual({key: migrated[key] for key in old}, old)
            self.assertEqual(migrated['source_platform'], 'douyin')
        self.assertEqual(len(after), 2)
        self.assertEqual(db.upsert_xingtu_creator({'star_id': 'star-7', 'uid': 'core-7'}), 7)
        self.assertEqual(self.rows()[0]['sec_uid'], 'real-sec')

    def test_fresh_star_uses_internal_namespace_without_fabricated_evidence(self):
        creator_id = db.upsert_xingtu_creator({'star_id': 'star-1', 'nickname': 'Creator'})
        row = self.rows()[0]
        self.assertEqual(row['id'], creator_id)
        self.assertEqual(row['sec_uid'], 'xingtu:star-1')
        self.assertEqual(row['source_platform'], 'xingtu')
        self.assertEqual(row['star_id'], 'star-1')
        for field in ('uid', 'unique_id', 'profile_url', 'followers', 'total_likes',
                      'aweme_count', 'star_atlas', 'price_1_20', 'median_play_30d'):
            self.assertIsNone(row[field], field)
        for table in ('snapshots', 'sample_posts', 'candidate_assessments', 'recommendations'):
            self.assertEqual(self.rows(f'SELECT * FROM {table}'), [])

    def test_repeated_star_updates_supplied_fields_preserving_uid_and_observation_age(self):
        creator_id = db.upsert_xingtu_creator({
            'star_id': 'star-1', 'uid': 'core-1', 'nickname': 'Before',
            'followers': 123, 'price_1_20': 0, 'category_tag': 'Food',
            'profile_url': 'https://www.xingtu.cn/ad/creator/author-homepage/star-1',
        })
        self.rows("UPDATE creators SET updated_at='2020-01-01'")
        self.assertEqual(db.upsert_xingtu_creator({'star_id': 'star-1', 'nickname': 'After'}), creator_id)
        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row['nickname'], 'After')
        self.assertEqual(row['uid'], 'core-1')
        self.assertEqual(row['followers'], 123)
        self.assertEqual(row['price_1_20'], 0)
        self.assertEqual(row['category_tag'], 'Food')
        self.assertEqual(row['updated_at'], '2020-01-01')
        self.assertEqual(row['profile_url'], 'https://www.xingtu.cn/ad/creator/author-homepage/star-1')
        db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': None, 'followers': 0})
        self.assertEqual(self.rows()[0]['uid'], 'core-1')
        self.assertEqual(self.rows()[0]['followers'], 0)

    def test_actual_uid_merges_compatible_creator_preserving_real_sec_uid(self):
        for prior_star in (None, '', 'star-1'):
            with self.subTest(prior_star=prior_star):
                self.rows('DELETE FROM creators')
                creator_id = db.upsert_creator({
                    'sec_uid': 'real-sec', 'uid': 'core-1', 'nickname': 'Old',
                    'star_id': prior_star, 'followers': 456,
                    'profile_url': 'https://www.douyin.com/user/real-sec',
                })
                self.assertEqual(db.upsert_xingtu_creator({
                    'star_id': 'star-1', 'uid': 'core-1', 'nickname': 'New',
                }), creator_id)
                row = self.rows()[0]
                self.assertEqual(row['sec_uid'], 'real-sec')
                self.assertEqual(row['uid'], 'core-1')
                self.assertEqual(row['star_id'], 'star-1')
                self.assertEqual(row['source_platform'], 'xingtu')
                self.assertEqual(row['followers'], 456)
                self.assertEqual(row['profile_url'], 'https://www.douyin.com/user/real-sec')
                self.assertEqual(len(self.rows()), 1)

    def test_nickname_and_other_id_namespaces_never_merge(self):
        db.upsert_creator({'sec_uid': 'star-1', 'uid': 'different', 'nickname': 'Same'})
        db.upsert_creator({'sec_uid': 'core-1', 'unique_id': 'star-1', 'nickname': 'Same'})
        db.upsert_creator({'sec_uid': 'manual_star-1', 'nickname': 'Same'})
        creator_id = db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1', 'nickname': 'Same'})
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.rows()[-1]['id'], creator_id)
        self.assertEqual(self.rows()[-1]['sec_uid'], 'xingtu:star-1')

    def test_missing_uid_never_matches_empty_legacy_uids(self):
        db.upsert_creator({'sec_uid': 'one', 'uid': '', 'nickname': 'Same'})
        db.upsert_creator({'sec_uid': 'two', 'uid': None, 'nickname': 'Same'})
        db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': '', 'nickname': 'Same'})
        self.assertEqual(len(self.rows()), 3)
        self.assertIsNone(self.rows()[-1]['uid'])

    def test_exact_star_is_found_before_nickname_or_sec_uid(self):
        existing_id = db.upsert_creator({'sec_uid': 'real-sec', 'star_id': 'star-1', 'nickname': 'Old'})
        db.upsert_creator({'sec_uid': 'star-1', 'nickname': 'Same'})
        self.assertEqual(db.upsert_xingtu_creator({'star_id': 'star-1', 'nickname': 'Same'}), existing_id)
        self.assertEqual(self.rows()[0]['sec_uid'], 'real-sec')
        self.assertEqual(len(self.rows()), 2)

    def test_conflicting_uid_on_exact_star_is_rejected_without_changes(self):
        db.upsert_creator({'sec_uid': 'real-sec', 'star_id': 'star-1', 'uid': 'old-core'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'new-core', 'nickname': 'Changed'})
        self.assertEqual(self.rows(), before)

    def test_uid_bound_to_different_star_is_rejected_without_changes(self):
        db.upsert_creator({'sec_uid': 'real-sec', 'star_id': 'other-star', 'uid': 'core-1'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1'})
        self.assertEqual(self.rows(), before)

    def test_ambiguous_uid_is_rejected_even_if_only_one_star_is_compatible(self):
        db.upsert_creator({'sec_uid': 'one', 'uid': 'core-1'})
        db.upsert_creator({'sec_uid': 'two', 'uid': 'core-1', 'star_id': 'other'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1'})
        self.assertEqual(self.rows(), before)

    def test_exact_star_and_uid_pointing_to_different_rows_are_rejected(self):
        db.upsert_creator({'sec_uid': 'one', 'star_id': 'star-1'})
        db.upsert_creator({'sec_uid': 'two', 'uid': 'core-1'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1'})
        self.assertEqual(self.rows(), before)

    def test_duplicate_legacy_star_is_rejected_by_lookup_and_upsert(self):
        db.upsert_creator({'sec_uid': 'one', 'star_id': 'star-1'})
        db.upsert_creator({'sec_uid': 'two', 'star_id': 'star-1'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.get_creator_by_star_id('star-1')
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1'})
        self.assertEqual(self.rows(), before)

    def test_internal_key_collision_is_not_treated_as_stable_id_match(self):
        db.upsert_creator({'sec_uid': 'xingtu:star-1', 'star_id': 'other-star'})
        before = self.rows()
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({'star_id': 'star-1'})
        self.assertEqual(self.rows(), before)

    def test_invalid_star_ids_rejected_without_writes(self):
        for star_id in (None, '', '   ', 123, False):
            with self.subTest(star_id=star_id), self.assertRaises(ValueError):
                db.upsert_xingtu_creator({'star_id': star_id})
        with self.assertRaises(ValueError):
            db.upsert_xingtu_creator({})
        self.assertEqual(self.rows(), [])

    def test_supplied_sec_uid_cannot_override_internal_identity(self):
        db.upsert_xingtu_creator({'star_id': 'star-1', 'sec_uid': 'invented-real-sec'})
        self.assertEqual(self.rows()[0]['sec_uid'], 'xingtu:star-1')

    def test_lookup_returns_full_identity_or_none(self):
        self.assertIsNone(db.get_creator_by_star_id('missing'))
        creator_id = db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1'})
        row = db.get_creator_by_star_id('star-1')
        self.assertEqual(row['id'], creator_id)
        self.assertEqual(row['uid'], 'core-1')
        self.assertEqual(row['source_platform'], 'xingtu')
        self.assertEqual(row['sec_uid'], 'xingtu:star-1')

    def test_legacy_upsert_accepts_source_without_default_erasing_origin(self):
        creator_id = db.upsert_creator({'sec_uid': 'real-sec', 'source_platform': 'xingtu'})
        self.assertEqual(self.rows()[0]['source_platform'], 'xingtu')
        db.upsert_creator({'sec_uid': 'real-sec', 'nickname': 'Changed'})
        self.assertEqual(self.rows()[0]['source_platform'], 'xingtu')
        self.assertEqual(self.rows()[0]['id'], creator_id)
        db.upsert_creator({'sec_uid': 'real-sec', 'source_platform': 'douyin'})
        self.assertEqual(self.rows()[0]['source_platform'], 'douyin')
        db.upsert_creator({'sec_uid': 'default-sec'})
        self.assertEqual(self.rows()[1]['source_platform'], 'douyin')

    def test_task_results_and_knowledge_base_expose_disjoint_identity(self):
        task_id = db.save_task('Task', 'Food')
        creator_id = db.upsert_xingtu_creator({'star_id': 'star-1', 'uid': 'core-1'})
        db.save_recommendation(creator_id, task_id, {'hard_pass': None})
        for row in (db.get_task_results(task_id)[0], db.get_knowledge_base()[0]):
            self.assertEqual(row['uid'], 'core-1')
            self.assertEqual(row['source_platform'], 'xingtu')
            self.assertEqual(row['star_id'], 'star-1')
            self.assertEqual(row['sec_uid'], 'xingtu:star-1')
            self.assertIsNone(row['profile_url'])
        result = db.get_task_results(task_id)[0]
        self.assertIsNone(result['hard_pass'])
        self.assertIsNone(result['observation_id'])
        self.assertEqual(result['sample_posts'], [])


if __name__ == '__main__':
    unittest.main()
