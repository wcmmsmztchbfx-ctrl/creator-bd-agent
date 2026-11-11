import datetime
import importlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, qualification


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(tmp) / 'store.db')))
        db.init_db()
        self.assertIsNotNone(importlib.util.find_spec('app.workflow'), 'workflow storage is missing')
        self.store = importlib.import_module('app.workflow')
        self.store.init_workflow_db()
        self.task = db.save_task('brief', 'topic', {'sample_count': 5, 'min_avg_likes': 100})

    def sql(self, statement, args=()):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute(statement, args).fetchall()

    def candidate(self, task=None, uid='one', hard_pass=True, **metrics):
        task = self.task if task is None else task
        cid = db.upsert_creator({'sec_uid': uid, 'nickname': uid, 'followers': 1000})
        db.save_snapshot(cid, task, dict(sample_count=5, avg_likes=200, max_likes=500,
                                       days_since_latest=1, **metrics))
        db.save_recommendation(cid, task, {'level': 'numeric-pass', 'hard_pass': hard_pass,
                                         'reason': 'original numeric rule'})
        # New candidates need an explicit structured assessment; a legacy
        # hard_pass flag alone is not an admission ticket.
        if hard_pass is True:
            self.qualify(cid, task)
        elif hard_pass is None:
            self.qualify(cid, task, topic_status='unknown', error_code='not_reviewed',
                         sample_count=0, matched_count=0)
        return cid

    def qualify(self, cid, task=None, *, topic_status='matched',
                pattern='consistent_creator', sample_count=6, matched_count=6,
                error_code=None, now=None):
        task = self.task if task is None else task
        requirements = db.get_task(task)['requirements']
        policy = qualification.policy_from_requirements(requirements)
        row = next(row for row in db.get_task_results(task) if row['creator_id'] == cid)
        numeric = qualification.evaluate_numeric_conditions(row, requirements, now=now)
        topic = {
            'topic_status': topic_status, 'method': 'text_llm',
            'posts': [{'aweme_id': f'p{i}', 'decision': 'yes', 'evidence_id': f'p{i}'}
                      for i in range(6)],
            'account': {'account_type': 'independent_creator',
                        'evidence_ids': ['p0', 'p1'], 'reason': 'Synthetic personal creator across posts'},
            'creator_pattern': pattern, 'sample_count': sample_count,
            'matched_count': matched_count,
            'unknown_count': max(0, sample_count - matched_count),
            'evidence_ids': [], 'reason': 'synthetic review',
            'prompt_version': 'synthetic', 'model': 'synthetic', 'error_code': error_code,
            'truncated_ids': [], 'ratio': (matched_count / sample_count) if sample_count else None,
            'min_samples': policy['min_samples'], 'min_ratio': policy['min_ratio'],
        }
        assessment = qualification.build_assessment(
            row, row.get('observation_id'), [], requirements, numeric, topic, policy, [])
        assessment['topic_reviewed'] = topic_status != 'unknown' and error_code not in ('not_reviewed',)
        db.save_candidate_assessment(task, cid, row.get('observation_id'), assessment)
        return assessment

    def enrichment(self, cid, task=None, **changes):
        data = dict(status='completed', identity_verified=True, median_play_30d=1234,
                    audience={'gender': {'female': 60}, 'age': {'18-24': 50}, 'region': {'Shanghai': 20}},
                    missing_fields=[], source_url='https://example.invalid/profile',
                    captured_at=datetime.datetime.now().isoformat())
        data.update(changes)
        self.store.save_enrichment(self.task if task is None else task, cid, data)
        return data

    def test_identity_bound_profile_and_business_contact_reach_kb_and_frozen_report(self):
        cid = self.candidate(uid='xingtu:111')
        self.sql("UPDATE creators SET source_platform='xingtu',star_id='111',uid='222' WHERE id=?", (cid,))
        source = 'https://www.xingtu.cn/ad/creator/author-homepage/douyin-video/111'
        db.save_contacts(cid, [{'type':'微信','value':'old_public_contact','source':'主页简介'}])
        self.enrichment(cid, star_id='111', source_url=source,
            profile={'star_id':'111','uid':'222','unique_id':'verified-handle','sec_uid':'MS4w-verified'},
            contact={'status':'已获取','type':'微信','value':'demo_business',
                     'source':'星图后台','source_url':source,'permission':'session_visible'})
        row = next(c for c in self.store.get_candidates(self.task) if c['creator_id'] == cid)
        self.assertEqual(row['unique_id'], 'verified-handle')
        self.assertEqual(row['profile_url'], 'https://www.douyin.com/user/MS4w-verified')
        self.assertEqual(row['wechat'], 'demo_business')
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])
        report = self.store.get_report(self.task)[0]
        self.assertEqual(report['wechat'], 'demo_business')
        self.assertEqual(report['unique_id'], 'verified-handle')

    def test_unverified_or_conflicting_profile_never_updates_canonical_record(self):
        for verified, star, uid in ((False,'111','222'), (True,'999','222'), (True,'111','333')):
            with self.subTest(verified=verified, star=star, uid=uid):
                cid = self.candidate(uid=f'test-{verified}-{star}-{uid}')
                self.sql("UPDATE creators SET source_platform='xingtu',star_id='111',uid='222' WHERE id=?", (cid,))
                self.enrichment(cid, identity_verified=verified, star_id=star,
                    profile={'star_id':star,'uid':uid,'unique_id':'must-not-copy'})
                row = next(c for c in self.store.get_candidates(self.task) if c['creator_id'] == cid)
                self.assertNotEqual(row['unique_id'], 'must-not-copy')

    def test_report_freezes_nested_assessment_time_and_provenance(self):
        cid = self.candidate()
        assessed = self.qualify(cid)
        self.enrichment(cid)
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])
        frozen = self.store.get_report(self.task)[0]
        self.assertEqual(frozen['assessment_at'], assessed['assessed_at'])
        self.assertEqual(frozen['assessment_prompt_version'], assessed['prompt_version'])
        self.assertEqual(frozen['assessment_method'], 'text_llm')
        self.assertEqual(frozen['assessment_model'], 'synthetic')
        self.sql('UPDATE creators SET signature=? WHERE id=?', ('changed bio', cid))
        self.assertEqual(self.store.get_report(self.task)[0], frozen)

    def test_stop_and_timing_survive_reinitialization_and_finish_is_fixed(self):
        db.update_task_status(self.task, 'running')
        self.sql("UPDATE tasks SET started_at='2026-01-01 00:00:00' WHERE id=?", (self.task,))
        self.store.request_stop(self.task)
        self.store.init_workflow_db()
        state = self.store.task_state(self.task)
        self.assertEqual(state['status'], 'stopping')
        self.assertTrue(state['stop_requested'])
        self.assertTrue(self.store.should_stop(self.task))
        db.update_task_status(self.task, 'stopped')
        before = self.store.task_state(self.task)
        self.assertTrue(before['finished_at'])
        self.assertEqual(self.store.task_state(self.task)['elapsed_seconds'], before['elapsed_seconds'])
        self.assertEqual(before['started_at'], '2026-01-01 00:00:00')

    def test_restart_interrupts_enrichment_and_stopping_not_waiting_selection(self):
        ids = [db.save_task(s, 'topic', status=s) for s in ('enriching', 'stopping', 'awaiting_selection')]
        db.recover_interrupted_tasks()
        self.assertEqual([db.get_task(i)['status'] for i in ids], ['interrupted', 'interrupted', 'awaiting_selection'])
        self.assertTrue(db.get_task(ids[0])['finished_at'])

    def test_dynamic_counts_include_failed_candidates_and_selection_flags(self):
        good = self.candidate()
        bad = self.candidate(uid='unconfirmed', hard_pass=None)
        self.assertEqual(self.store.get_report(self.task), [])
        self.store.set_candidate_selection(self.task, [good], [bad])
        rows = {r['creator_id']: r for r in self.store.get_candidates(self.task)}
        self.assertEqual(set(rows), {good, bad})
        self.assertTrue(rows[good]['selected'])
        self.assertTrue(rows[bad]['excluded'])
        self.assertIsNone(rows[bad]['enrichment'])
        self.assertEqual(rows[bad]['tags'], [])
        self.enrichment(good)
        self.store.admit_report(self.task, [good])
        self.assertEqual(self.store.task_state(self.task)['counts'],
                         dict(candidates=2, discovered=2, eligible=1, pending=1, rejected=0,
                              prequalified=2, topic_reviewed=1, selected=1, enriched=1,
                              report=1, qualified=1, target=5))
        self.store.remove_report_entry(self.task, good)
        self.assertEqual(self.store.task_state(self.task)['counts']['report'], 0)
        self.assertEqual(len(self.store.get_candidates(self.task)), 2)
        self.assertEqual(len(db.get_knowledge_base()), 2)

    def test_core_gate_rejects_unverified_nonnumeric_and_empty_audiences(self):
        cid = self.candidate()
        cases = [dict(identity_verified=False), dict(identity_verified='false'),
                 dict(median_play_30d=None), dict(median_play_30d='1234'),
                 dict(median_play_30d=True), dict(median_play_30d=float('nan')),
                 dict(audience={'gender': {}, 'age': [], 'region': '  '}),
                 dict(status='permission_denied'), dict(captured_at='invalid')]
        for changes in cases:
            with self.subTest(changes=changes):
                self.enrichment(cid, **changes)
                outcome = self.store.admit_report(self.task, [cid])
                self.assertEqual(outcome['admitted'], [])
                self.assertEqual(outcome['pending'], [cid])
                self.assertEqual(self.store.get_report(self.task), [])

    def test_missing_exception_is_explicit_never_qualified(self):
        cid = self.candidate(hard_pass=None)
        self.enrichment(cid, identity_verified=False, median_play_30d=None)
        self.store.admit_report(self.task, [cid], allow_missing=True)
        row = self.store.get_report(self.task)[0]
        self.assertTrue(row['manual_include_missing'])
        self.assertFalse(row['qualified'])
        self.assertTrue(row['missing_fields'])
        self.assertTrue(row['risk'])
        self.assertEqual(self.store.task_state(self.task)['counts']['qualified'], 0)

    def test_report_is_deduped_frozen_and_manual_fields_do_not_overwrite_rules(self):
        cid = self.candidate()
        self.enrichment(cid)
        self.store.admit_report(self.task, [cid, cid])
        frozen = self.store.get_report(self.task)[0]
        db.upsert_creator({'sec_uid': 'one', 'nickname': 'changed', 'followers': 9999})
        self.enrichment(cid, median_play_30d=9999)
        self.store.admit_report(self.task, [cid])
        self.assertEqual(self.store.get_report(self.task), [frozen])
        self.store.update_report(self.task, [dict(creator_id=cid, manual_level='manual', manual_reason='human',
                                                recommend_level='forged', reason='forged', qualified=False)])
        saved = self.store.get_report(self.task)[0]
        self.assertEqual(saved['recommend_level'], 'numeric-pass')
        self.assertEqual(saved['reason'], 'original numeric rule')
        self.assertEqual(saved['manual_level'], 'manual')
        self.assertEqual(saved['manual_reason'], 'human')
        self.assertTrue(saved['qualified'])
        self.assertEqual(saved['requirements'], {'sample_count': 5, 'min_avg_likes': 100})
        self.assertEqual(db.get_task_results(self.task)[0]['reason'], 'original numeric rule')

    def test_ttl_defaults_to_24h_is_configurable_and_is_not_reset_on_save(self):
        cid = self.candidate()
        old = (datetime.datetime.now() - datetime.timedelta(hours=25)).isoformat()
        self.enrichment(cid, captured_at=old)
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [])
        self.assertEqual(self.store.get_enrichment(self.task, cid)['captured_at'], old)
        self.assertEqual(db.get_all_settings()['dynamic_ttl_hours'], '24')
        db.save_setting('dynamic_ttl_hours', '48')
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])

    def test_tags_preserve_source_and_kb_exposes_enrichment_and_task_history(self):
        cid = self.candidate()
        self.store.set_candidate_selection(self.task, [], [cid])
        tags = [{'name': 'review', 'source': 'manual'}, {'name': 'numeric', 'source': 'auto', 'basis': 'followers'}]
        self.store.update_tags(cid, tags)
        self.enrichment(cid, status='not_found', identity_verified=False)
        rows = self.store.get_kb(search='one', tag='review', enrichment_status='not_found')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tags'], tags)
        self.assertEqual(rows[0]['enrichment_status'], 'not_found')
        self.assertTrue(rows[0]['tasks'][0]['excluded'])
        self.assertEqual(self.store.get_kb(tag='absent'), [])

    def test_kb_attachment_reevaluates_every_current_threshold_not_old_pass(self):
        cid = self.candidate()
        self.enrichment(cid)
        for requirements in ({'min_followers': 1001}, {'max_followers': 999}, {'min_avg_likes': 201},
                             {'min_max_likes': 501}, {'active_days': 0}):
            with self.subTest(requirements=requirements):
                task = db.save_task('stricter', 'topic', requirements)
                self.store.attach_kb(task, [cid, cid])
                rows = self.store.get_candidates(task)
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]['hard_pass'])
                self.assertEqual(self.store.admit_report(task, [cid])['admitted'], [])
                exception = self.store.admit_report(task, [cid], allow_missing=True)
                if requirements == {'active_days': 0}:
                    # No exact publish timestamp is stored here: unknown may
                    # be explicitly admitted, but it is never qualified.
                    self.assertEqual(exception['admitted'], [cid])
                    self.assertFalse(self.store.get_report(task)[0]['qualified'])
                else:
                    # Known numeric failures cannot be admitted even as a
                    # "missing data" exception.
                    self.assertEqual(exception['admitted'], [])
                    self.assertEqual(self.store.get_report(task), [])

    def test_kb_does_not_inherit_old_failure_and_unknown_metrics_stay_pending(self):
        cid = self.candidate(hard_pass=False)
        self.enrichment(cid)
        task = db.save_task('new brief', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(task, [cid])
        self.assertIsNone(self.store.get_candidates(task)[0]['hard_pass'])
        self.qualify(cid, task)
        self.assertEqual(self.store.admit_report(task, [cid])['admitted'], [cid])
        self.sql('UPDATE snapshots SET avg_likes=NULL WHERE creator_id=?', (cid,))
        task2 = db.save_task('unknown', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(task2, [cid])
        self.assertIsNone(self.store.get_candidates(task2)[0]['hard_pass'])
        self.assertEqual(self.store.admit_report(task2, [cid])['pending'], [cid])

    def test_expired_numeric_snapshot_cannot_pass_kb_attachment(self):
        cid = self.candidate()
        self.enrichment(cid)
        self.sql("UPDATE snapshots SET captured_at='2020-01-01 00:00:00' WHERE creator_id=?", (cid,))
        task = db.save_task('fresh brief', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(task, [cid])
        self.assertEqual(self.store.admit_report(task, [cid])['pending'], [cid])

    def test_unscoped_kb_export_is_data_list_not_qualified_recommendation(self):
        cid = self.candidate()
        self.enrichment(cid)
        task = db.save_task('data list', '')
        self.store.attach_kb(task, [cid])
        self.store.admit_report(task, [cid])
        row = self.store.get_report(task)[0]
        self.assertTrue(row['data_list'])
        self.assertFalse(row['qualified'])

    def test_migration_preserves_legacy_rows_and_does_not_promote_to_report(self):
        cid = self.candidate()
        before = self.sql('SELECT * FROM recommendations')
        self.store.init_workflow_db()
        self.store.init_workflow_db()
        self.assertEqual(self.sql('SELECT * FROM recommendations'), before)
        self.assertEqual(self.store.get_report(self.task), [])
        self.assertEqual(self.store.get_candidates(self.task)[0]['creator_id'], cid)

    def test_discovery_placeholder_is_updated_without_duplicate_candidates(self):
        cid = self.candidate(hard_pass=None)
        db.save_recommendation(cid, self.task, {'level': 'detail', 'hard_pass': True})
        self.assertEqual(len(self.sql('SELECT * FROM recommendations')), 1)
        self.assertEqual(db.get_task_results(self.task)[0]['recommend_level'], 'detail')

    def test_legacy_duplicates_use_latest_recommendation_and_snapshot_without_deletion(self):
        cid = self.candidate()
        self.sql("INSERT INTO recommendations (task_id,creator_id,recommend_level,hard_pass,created_at) VALUES (?,?, 'latest',0,'2026-01-01')", (self.task, cid))
        db.save_snapshot(cid, self.task, {'avg_likes': 900})
        rows = db.get_task_results(self.task)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['recommend_level'], 'latest')
        self.assertEqual(rows[0]['avg_likes'], 900)
        self.assertEqual(len(self.sql('SELECT * FROM recommendations')), 2)

    def test_running_progress_update_cannot_clear_pending_stop(self):
        db.update_task_status(self.task, 'running')
        self.store.request_stop(self.task)
        db.update_task_status(self.task, 'running', 'safe step finished', {'processed': 1})
        state = db.get_task(self.task)
        self.assertTrue(state['stop_requested'])
        self.assertEqual(state['status'], 'stopping')
        self.assertEqual(state['progress'], {'processed': 1})

    def test_partial_creator_upsert_preserves_omitted_identity_and_metrics(self):
        cid = db.upsert_creator(dict(sec_uid='full', nickname='Full', unique_id='public', uid='uid',
                                     followers=123, total_likes=456, aweme_count=7, signature='bio',
                                     star_atlas=1, city='Shanghai', star_id='star', median_play_30d=900))
        db.upsert_creator(dict(sec_uid='full', nickname='Discovery', profile_url='https://example.invalid'))
        row = self.sql('SELECT unique_id,uid,followers,total_likes,aweme_count,signature,star_atlas,city,star_id,median_play_30d FROM creators WHERE id=?', (cid,))[0]
        self.assertEqual(row, ('public', 'uid', 123, 456, 7, 'bio', 1, 'Shanghai', 'star', 900))
        db.upsert_creator(dict(sec_uid='full', followers=None))
        self.assertIsNone(self.sql('SELECT followers FROM creators WHERE id=?', (cid,))[0][0])
        self.assertEqual(self.sql('SELECT nickname FROM creators WHERE id=?', (cid,))[0][0], 'Discovery')

    def test_information_kind_can_admit_missing_without_fake_rule_rating(self):
        cid = self.candidate()
        task = db.save_task('information', '', {'report_kind': 'information'})
        self.store.attach_kb(task, [cid])
        self.assertEqual(self.store.admit_report(task, [cid])['admitted'], [cid])
        row = self.store.get_report(task)[0]
        self.assertEqual(row['recommend_level'], '\u8d44\u6599\u6e05\u5355')
        self.assertEqual(row['reason'], '\u65e0\u9700\u6c42\uff0c\u4e0d\u63d0\u4f9b\u5339\u914d\u8bc4\u7ea7')
        self.assertFalse(row['qualified'])
        self.assertTrue(row['missing_fields'])

    def test_enrichment_reuse_requires_fresh_verified_data(self):
        cid = self.candidate()
        self.enrichment(cid)
        other = db.save_task('other', 'topic', {'min_avg_likes': 100})
        self.assertEqual(self.store.get_enrichment(other, cid)['median_play_30d'], 1234)
        old = (datetime.datetime.now() - datetime.timedelta(hours=25)).isoformat()
        self.enrichment(cid, captured_at=old)
        self.assertIsNone(self.store.get_enrichment(other, cid))
        self.enrichment(cid, identity_verified=False)
        self.assertIsNone(self.store.get_enrichment(other, cid))

    def test_partial_discovery_does_not_make_expired_followers_fresh(self):
        cid = self.candidate()
        self.enrichment(cid)
        self.sql("UPDATE creators SET updated_at='2020-01-01 00:00:00' WHERE id=?", (cid,))
        db.upsert_creator(dict(sec_uid='one', nickname='new discovery'))
        task = db.save_task('follower threshold', 'topic', {'min_followers': 10})
        self.store.attach_kb(task, [cid])
        self.assertEqual(self.store.admit_report(task, [cid])['pending'], [cid])

    def test_pre_timing_schema_migration_preserves_original_tasks_and_data(self):
        path = str(Path(db.DB_PATH).with_name('legacy.db'))
        with patch.object(db, 'DB_PATH', path):
            with sqlite3.connect(path) as conn:
                conn.executescript('''CREATE TABLE tasks (id INTEGER PRIMARY KEY,name TEXT NOT NULL,
                    keyword TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
                    INSERT INTO tasks VALUES (42,'historic','old','completed','2020-01-01 01:00:00');
                    CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    INSERT INTO settings VALUES ('custom','preserved');''')
            self.store.init_workflow_db()
            before = db.get_task(42)
            self.store.init_workflow_db()
            self.assertEqual(db.get_task(42), before)
            self.assertEqual(before['created_at'], '2020-01-01 01:00:00')
            self.assertEqual(before['finished_at'], '2020-01-01 01:00:00')
            self.assertEqual(db.get_setting('custom'), 'preserved')
            self.assertEqual(self.store.get_report(42), [])

    def test_unstructured_brief_requires_explicit_exception_and_never_qualifies(self):
        cid = self.candidate()
        self.enrichment(cid)
        task = db.save_task('brief only', 'topic', dict(report_kind='recommendation', brief='find creators',
                                                       unstructured_brief=True))
        self.store.attach_kb(task, [cid])
        self.assertEqual(self.store.admit_report(task, [cid])['pending'], [cid])
        self.store.admit_report(task, [cid], allow_missing=True)
        row = self.store.get_report(task)[0]
        self.assertFalse(row['qualified'])
        self.assertTrue(row['allow_missing'])
        self.assertIn('\u9700\u6c42\u5c1a\u672a\u7ed3\u6784\u5316\u786e\u8ba4', row['missing_fields'])

    def test_report_freezes_actual_enrichment_commercial_fields_not_legacy_values(self):
        cid = self.candidate()
        db.upsert_creator(dict(sec_uid='one', price_1_20=99999, price_21_60=88888, median_play_30d=77777))
        self.enrichment(cid, star_id='verified-star', price_1_20=321, price_21_60=None)
        self.store.admit_report(self.task, [cid])
        row = self.store.get_report(self.task)[0]
        self.assertEqual(row['star_id'], 'verified-star')
        self.assertEqual(row['price_1_20'], 321)
        self.assertIsNone(row['price_21_60'])
        self.assertEqual(row['median_play_30d'], 1234)

    def test_missing_enrichment_does_not_promote_legacy_commercial_zeroes(self):
        cid = self.candidate()
        db.upsert_creator(dict(sec_uid='one', price_1_20=0, median_play_30d=0))
        self.store.admit_report(self.task, [cid], allow_missing=True)
        row = self.store.get_report(self.task)[0]
        self.assertIsNone(row['median_play_30d'])
        self.assertIsNone(row['price_1_20'])

    def test_latest_identity_failure_blocks_reuse_of_older_success(self):
        cid = self.candidate()
        self.enrichment(cid)
        second = db.save_task('second', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(second, [cid])
        self.enrichment(cid, task=second, status='identity_unconfirmed', identity_verified=False)
        third = db.save_task('third', 'topic', {'min_avg_likes': 100})
        self.assertIsNone(self.store.get_enrichment(third, cid))

    def test_missing_snapshot_metrics_do_not_become_zero_and_pass_zero_threshold(self):
        task = db.save_task('zero threshold', 'topic', {'min_avg_likes': 0, 'min_max_likes': 0})
        cid = db.upsert_creator(dict(sec_uid='unknown', nickname='Unknown'))
        db.save_snapshot(cid, task, {'sample_count': 1})
        db.save_recommendation(cid, task, {'hard_pass': True})
        self.enrichment(cid, task=task)
        self.assertEqual(self.store.admit_report(task, [cid])['pending'], [cid])
        self.assertIsNone(db.get_task_results(task)[0]['avg_likes'])

    def test_legacy_duplicates_do_not_inflate_task_creator_count(self):
        cid = self.candidate()
        self.sql("INSERT INTO recommendations (task_id,creator_id,recommend_level,created_at) VALUES (?,?,'old','2020-01-01')", (self.task, cid))
        self.assertEqual(db.get_task(self.task)['creator_count'], 1)
        self.assertEqual(db.get_all_tasks()[0]['creator_count'], 1)

    def test_nonfinite_playback_exception_is_unknown_and_json_serializable(self):
        cid = self.candidate()
        self.enrichment(cid, median_play_30d=float('nan'))
        self.store.admit_report(self.task, [cid], allow_missing=True)
        row = self.store.get_report(self.task)[0]
        self.assertIsNone(row['enrichment']['median_play_30d'])
        self.assertFalse(row['qualified'])
        json.dumps(row, allow_nan=False)

    def test_activity_rechecks_exact_post_age_not_rounded_snapshot_days(self):
        now = datetime.datetime.fromisoformat('2026-11-05T18:00:00+08:00')
        cid = self.candidate(latest_post_date='2026-10-28 18:00:00')
        self.sql('UPDATE tasks SET requirements_json=? WHERE id=?',
                 (json.dumps({'active_days': 7}), self.task))
        self.sql('UPDATE snapshots SET days_since_latest=7,captured_at=?',
                 ((now - datetime.timedelta(hours=20)).isoformat(),))
        self.enrichment(cid, captured_at=now.isoformat())
        self.qualify(cid, self.task, now=now)
        with patch.object(self.store, '_now', return_value=now):
            outcome = self.store.admit_report(self.task, [cid])
            self.assertEqual(outcome['pending'], [cid])
            self.assertIn('days_since_latest', outcome['details'][cid])
            self.assertEqual(self.store.admit_report(self.task, [cid], allow_missing=True)['admitted'], [])
            self.assertEqual(self.store.get_report(self.task), [])

    def test_activity_requires_exact_timestamp_and_interprets_naive_posts_as_shanghai(self):
        now = datetime.datetime.fromisoformat('2026-11-05T10:00:00+00:00')
        class FrozenDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.astimezone().replace(tzinfo=None)
        cid = self.candidate()
        self.sql('UPDATE tasks SET requirements_json=? WHERE id=?',
                 (json.dumps({'active_days': 7}), self.task))
        self.sql('UPDATE snapshots SET captured_at=?', (now.isoformat(),))
        self.enrichment(cid, captured_at=now.isoformat())
        cases = [(None, False), ('2026-10-29', False), ('invalid', False),
                 ('2026-10-29 18:00:00', True), ('2026-10-29T10:00:00Z', True),
                 ('2026-10-29 17:59:59', False), ('2026-11-06 18:00:00', False)]
        with patch.object(self.store, '_now', return_value=now), patch.object(db.datetime, 'datetime', FrozenDateTime):
            for latest, admitted in cases:
                with self.subTest(latest=latest):
                    self.store.remove_report_entry(self.task, cid)
                    self.sql('UPDATE snapshots SET latest_post_date=?', (latest,))
                    self.qualify(cid, self.task, now=now)
                    outcome = self.store.admit_report(self.task, [cid])
                    self.assertEqual(outcome['admitted'], [cid] if admitted else [])
                    self.store.remove_report_entry(self.task, cid)

    def test_newer_identity_failure_invalidates_task_local_reuse_but_not_frozen_report(self):
        cid = self.candidate()
        self.enrichment(cid)
        self.store.admit_report(self.task, [cid])
        frozen = self.store.get_report(self.task)
        second = db.save_task('second', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(second, [cid])
        self.enrichment(cid, task=second)
        third = db.save_task('identity review', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(third, [cid])
        for status in ('identity_unconfirmed', 'failed', 'unavailable'):
            with self.subTest(status=status):
                self.enrichment(cid, task=third, status=status, identity_verified=False)
                self.assertIsNone(self.store.get_enrichment(second, cid))
                self.assertEqual(self.store.admit_report(second, [cid])['pending'], [cid])
                self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])
                self.assertEqual(self.store.get_report(self.task), frozen)

    def test_newer_declared_missing_identity_invalidates_local_even_with_verified_flag(self):
        cid = self.candidate()
        self.enrichment(cid)
        second = db.save_task('second', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(second, [cid])
        self.enrichment(cid, task=second, missing_fields=['identity'])
        self.assertIsNone(self.store.get_enrichment(self.task, cid))
        self.assertEqual(self.store.admit_report(self.task, [cid])['pending'], [cid])

    def test_contradictory_identity_evidence_does_not_promote_commercial_fields(self):
        cid = self.candidate()
        for changes in (dict(status='identity_unconfirmed'), dict(missing_fields=['identity']),
                        dict(missing_fields=['identity_verified'])):
            with self.subTest(changes=changes):
                self.store.remove_report_entry(self.task, cid)
                self.enrichment(cid, star_id='not-verified', price_1_20=123, price_21_60=456,
                                identity_verified=True, **changes)
                self.store.admit_report(self.task, [cid], allow_missing=True)
                row = self.store.get_report(self.task)[0]
                for field in ('star_id', 'price_1_20', 'price_21_60'):
                    self.assertIsNone(row[field])
                self.assertEqual(row['enrichment']['star_id'], 'not-verified')

    def test_audience_requires_nonempty_numeric_distributions(self):
        cid = self.candidate()
        for value in ('未公开', {'female': '未公开'}, {'female': True},
                      {'female': -1}, {'female': float('inf')}, {'': 60},
                      {'female': {'value': 60}}, [60], {'female': 60, 'male': None}):
            with self.subTest(value=value):
                self.store.remove_report_entry(self.task, cid)
                self.enrichment(cid, audience=dict(gender=value, age={'18-24': 50}, region={'Shanghai': 20}))
                self.assertEqual(self.store.admit_report(self.task, [cid])['pending'], [cid])
        self.store.remove_report_entry(self.task, cid)
        self.enrichment(cid, audience=dict(gender={'female': 0}, age={'18-24': 0}, region={'Shanghai': 0}))
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])

    def test_declared_missing_core_fields_block_even_when_values_present(self):
        cid = self.candidate()
        for field in ('identity', 'identity_verified', 'median_play_30d', 'audience',
                      'audience.gender', 'audience.age', 'audience.region'):
            for missing in ([field], field):
                with self.subTest(missing=missing):
                    self.store.remove_report_entry(self.task, cid)
                    self.enrichment(cid, missing_fields=missing)
                    self.assertEqual(self.store.admit_report(self.task, [cid])['pending'], [cid])
        self.store.remove_report_entry(self.task, cid)
        self.enrichment(cid, missing_fields=['price_1_20', 'contact'])
        self.assertEqual(self.store.admit_report(self.task, [cid])['admitted'], [cid])
        self.assertTrue(self.store.get_report(self.task)[0]['qualified'])

    def test_enrichment_requires_explicit_valid_source_url_and_capture_timestamp(self):
        cid = self.candidate()
        for changes in (dict(source_url=''), dict(source_url='not a URL'), dict(source_url=None),
                        dict(source_url='file:///tmp/profile'), dict(source_url='https:///profile'),
                        dict(source_url='https://example.invalid/bad path'), dict(captured_at=None),
                        dict(captured_at='2026-11-05')):
            with self.subTest(changes=changes):
                self.store.remove_report_entry(self.task, cid)
                self.enrichment(cid, **changes)
                self.assertEqual(self.store.admit_report(self.task, [cid])['pending'], [cid])
        self.store.remove_report_entry(self.task, cid)
        payload = self.enrichment(cid)
        payload.pop('captured_at')
        self.store.save_enrichment(self.task, cid, payload)
        self.assertEqual(self.store.admit_report(self.task, [cid])['pending'], [cid])

    def test_formal_report_removes_only_resolved_generic_star_missing_and_preserves_original(self):
        cid = self.candidate()
        original = '星图指标与粉丝画像、公开商务联系渠道、近期作品数据'
        self.sql('UPDATE recommendations SET missing_fields=?', (original,))
        self.enrichment(cid)
        self.store.admit_report(self.task, [cid])
        row = self.store.get_report(self.task)[0]
        self.assertEqual(row['missing_fields'], ['公开商务联系渠道', '近期作品数据'])
        self.assertEqual(db.get_task_results(self.task)[0]['missing_fields'], original)
        self.assertEqual(row['enrichment_source_url'], 'https://example.invalid/profile')
        self.assertEqual(row['enrichment']['source_url'], 'https://example.invalid/profile')

    def test_unverified_identity_does_not_copy_commercial_fields_into_exception_report(self):
        cid = self.candidate()
        source = self.enrichment(cid, status='identity_unconfirmed', identity_verified=False,
                                 star_id='wrong-person', price_1_20=123, price_21_60=456, expected_cpm=7)
        self.store.admit_report(self.task, [cid], allow_missing=True)
        row = self.store.get_report(self.task)[0]
        for field in ('star_id', 'price_1_20', 'price_21_60', 'expected_cpm'):
            self.assertIsNone(row[field])
        self.assertEqual(row['enrichment']['star_id'], 'wrong-person')
        self.assertEqual(row['enrichment_source_url'], source['source_url'])
        self.assertFalse(row['qualified'])

    def test_refreshed_snapshot_uses_only_its_own_sample_posts_in_report_and_kb_copy(self):
        cid = self.candidate()
        old_posts = [dict(aweme_id='old-' + str(n), likes=20) for n in range(5)]
        new_posts = [dict(aweme_id='new-' + str(n), likes=500) for n in range(5)]
        db.save_sample_posts(cid, self.task, old_posts)
        db.save_snapshot(cid, self.task, {'avg_likes': 500, 'sample_count': 5})
        db.save_sample_posts(cid, self.task, new_posts)
        self.enrichment(cid)
        row = db.get_task_results(self.task)[0]
        self.assertEqual(row['avg_likes'], 500)
        self.assertEqual([p['aweme_id'] for p in row['sample_posts']], ['new-' + str(n) for n in range(5)])
        self.qualify(cid, self.task)
        self.store.admit_report(self.task, [cid])
        frozen = self.store.get_report(self.task)[0]
        self.assertEqual(frozen['sample_posts'], row['sample_posts'])
        second = db.save_task('reuse', 'topic', {'min_avg_likes': 100})
        self.store.attach_kb(second, [cid])
        copied = db.get_task_results(second)[0]
        self.assertEqual([{k: v for k, v in p.items() if k != 'observation_id'} for p in copied['sample_posts']],
                         [{k: v for k, v in p.items() if k != 'observation_id'} for p in row['sample_posts']])
        self.assertTrue(all(p['observation_id'] == copied['observation_id'] for p in copied['sample_posts']))
        self.assertEqual(self.sql('SELECT COUNT(*) FROM sample_posts WHERE task_id=?', (self.task,)), [(10,)])
        db.save_snapshot(cid, self.task, {'avg_likes': 900})
        self.assertEqual(db.get_task_results(self.task)[0]['sample_posts'], [])
        self.assertEqual(self.store.get_report(self.task)[0], frozen)

    def test_sample_association_migration_preserves_legacy_rows_with_explicit_fallback(self):
        cid = self.candidate()
        self.sql('DROP TABLE sample_posts')
        self.sql('''CREATE TABLE sample_posts (id INTEGER PRIMARY KEY,creator_id INTEGER,task_id INTEGER,
                    aweme_id TEXT,desc TEXT,likes INTEGER,comments INTEGER,post_date TEXT)''')
        self.sql("INSERT INTO sample_posts VALUES (17,?,?, 'legacy','old evidence',20,1,'2026-01-01')",
                 (cid, self.task))
        before = self.sql('SELECT * FROM sample_posts')
        self.store.init_workflow_db()
        self.store.init_workflow_db()
        self.assertIn('observation_id', [row[1] for row in self.sql('PRAGMA table_info(sample_posts)')])
        self.assertEqual(self.sql('SELECT id,creator_id,task_id,aweme_id,desc,likes,comments,post_date FROM sample_posts'), before)
        self.assertEqual(self.sql('SELECT observation_id FROM sample_posts'), [(None,)])
        row = db.get_task_results(self.task)[0]
        self.assertEqual(row['sample_posts'][0]['aweme_id'], 'legacy')
        self.assertTrue(row['sample_posts_legacy_fallback'])
        db.save_snapshot(cid, self.task, {'avg_likes': 500})
        db.save_sample_posts(cid, self.task, [dict(aweme_id='associated', likes=500)])
        row = db.get_task_results(self.task)[0]
        self.assertEqual([p['aweme_id'] for p in row['sample_posts']], ['associated'])
        self.assertFalse(row['sample_posts_legacy_fallback'])
        association = self.sql('SELECT observation_id FROM sample_posts WHERE aweme_id=?', ('associated',))[0][0]
        self.assertEqual(association, self.sql('SELECT MAX(id) FROM snapshots WHERE creator_id=? AND task_id=?', (cid, self.task))[0][0])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM sample_posts'), [(2,)])


if __name__ == '__main__':
    unittest.main()
