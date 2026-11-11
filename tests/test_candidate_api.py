import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import db, main, qualification


class CandidateAPITests(unittest.TestCase):
    def test_blocked_search_is_not_reported_as_partial_completion(self):
        from app.xingtu_discovery import XingtuDiscoveryError
        with patch('app.xingtu_pipeline.XingtuDiscovery.__aenter__',
                   new=AsyncMock(side_effect=XingtuDiscoveryError('verification_required', '星图需要人工验证'))):
            response = self.client.post('/api/execute', json={'category': '女装', 'keyword': '女装穿搭', 'sample_count': 5})
        self.assertEqual(response.status_code, 200)
        tid = response.json()['task_id']
        state = self.client.get('/api/status', params={'task_id': tid}).json()
        self.assertEqual(state['status'], 'blocked')
        self.assertFalse(state['is_running'])
        self.assertEqual(state['counts']['discovered'], 0)
        message = next(item for item in db.get_chat_history() if item.get('meta', {}).get('task_id') == tid)
        self.assertEqual(message['meta']['status'], 'blocked')
        self.assertEqual(message['meta']['counts']['discovered'], 0)
        self.assertNotIn('部分完成', message['content'])

    def test_old_chat_completion_card_uses_current_task_state_without_rewriting_history(self):
        tid = db.save_task('女装', '女装穿搭', status='partial')
        db.update_task_status(tid, 'partial', '需要人工验证', {'pause_reason': 'platform_verification'})
        db.save_chat_message('assistant', '旧的部分完成提示', {'task_finished': True, 'task_id': tid, 'status': 'partial'})
        db.init_db()
        messages = self.client.get('/api/chat/history').json()['messages']
        shown = next(item for item in messages if item.get('meta', {}).get('task_id') == tid)
        self.assertEqual(shown['meta']['status'], 'blocked')
        self.assertEqual(shown['meta']['counts']['discovered'], 0)
        self.assertNotIn('部分完成', shown['content'])
        self.assertIn('验证', shown['content'])
        original = next(item for item in db.get_chat_history() if item.get('meta', {}).get('task_id') == tid)
        self.assertEqual(original['content'], '旧的部分完成提示')

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db,'DB_PATH',str(Path(tmp)/'db')))
        main.current_running_task.clear()
        main.current_running_task.update(is_running=False,task_id=None)
        self.client = self.enterContext(TestClient(main.app))
        self.task = db.save_task('食品','食品',requirements={'sample_count':5,'min_avg_likes':100},status='completed')
        self.cid = db.upsert_creator({'sec_uid':'synthetic','nickname':'合成账号','followers':1000})
        db.save_snapshot(self.cid,self.task,{'avg_likes':300,'max_likes':500,
                                             'days_since_latest':1,'latest_post_date':'2026-11-04 12:00:00'})
        db.save_recommendation(self.cid,self.task,{'level':'可选','hard_pass':True,'reason':'合成规则'})
        self.assess()

    def assess(self, creator_id=None, numeric_status='passed', topic_status='matched', error_code=None):
        requirements = db.get_task(self.task)['requirements']
        policy = qualification.policy_from_requirements(requirements)
        creator_id = self.cid if creator_id is None else creator_id
        row = next(row for row in db.get_task_results(self.task) if row['creator_id'] == creator_id)
        numeric = qualification.evaluate_numeric_conditions(row, requirements)
        if numeric_status != numeric['status']:
            numeric = {'status': numeric_status, 'failed_fields': ['followers'] if numeric_status == 'failed' else [],
                       'missing_fields': [] if numeric_status != 'unknown' else ['followers'],
                       'reason_codes': [], 'details': ['synthetic']}
        topic = {'topic_status': topic_status, 'method': 'text_llm',
                 'posts': [{'aweme_id': f'p{i}', 'decision': 'yes', 'evidence_id': f'p{i}'}
                           for i in range(6)],
                 'account': {'account_type': 'independent_creator',
                             'evidence_ids': ['p0', 'p1'], 'reason': 'Synthetic personal creator across posts'},
                 'creator_pattern': 'consistent_creator', 'sample_count': 6,
                 'matched_count': 6 if topic_status == 'matched' else 0,
                 'unknown_count': 0, 'evidence_ids': [], 'reason': 'synthetic review',
                 'prompt_version': 'synthetic', 'model': 'synthetic', 'error_code': error_code,
                 'truncated_ids': [], 'ratio': 1.0 if topic_status == 'matched' else 0.0,
                 'min_samples': policy['min_samples'], 'min_ratio': policy['min_ratio']}
        assessment = qualification.build_assessment(
            row, row.get('observation_id'), [], requirements, numeric, topic, policy, [])
        assessment['topic_reviewed'] = True
        db.save_candidate_assessment(self.task, creator_id, row.get('observation_id'), assessment)

    def test_candidates_do_not_automatically_become_report(self):
        response=self.client.get(f'/api/tasks/{self.task}/candidates')
        self.assertEqual(response.status_code,200)
        all_rows=self.client.get(f'/api/tasks/{self.task}/candidates',params={'view':'all'}).json()
        self.assertEqual(len(all_rows['creators']),1)
        self.assertEqual(all_rows['creators'][0]['candidate_status'],'eligible')
        self.assertEqual(self.client.get('/api/results',params={'task_id':self.task}).json()['creators'],[])

    def test_stop_is_persisted_and_unknown_task_rejected(self):
        db.update_task_status(self.task,'running','运行')
        self.assertEqual(self.client.post(f'/api/tasks/{self.task}/stop').status_code,200)
        status=self.client.get('/api/status',params={'task_id':self.task}).json()
        self.assertTrue(status['stop_requested'])
        self.assertEqual(self.client.post('/api/tasks/99999/stop').status_code,404)

    def test_explicit_missing_admission_is_not_qualified(self):
        url=f'/api/tasks/{self.task}/report'
        response=self.client.post(url,json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['admitted'])
        response=self.client.post(url,json={'creator_ids':[self.cid],'allow_missing':True})
        self.assertEqual(len(response.json()['admitted']),1)
        rows=self.client.get('/api/results',params={'task_id':self.task}).json()['creators']
        self.assertTrue(rows[0]['allow_missing'])
        self.assertFalse(rows[0]['qualified'])

    def test_enrichment_requires_finished_search_and_valid_membership(self):
        db.update_task_status(self.task,'running','搜索')
        response=self.client.post(f'/api/tasks/{self.task}/enrich',json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,409)
        db.update_task_status(self.task,'completed','结束')
        response=self.client.post(f'/api/tasks/{self.task}/enrich',json={'creator_ids':[99999]})
        self.assertEqual(response.status_code,400)

    def test_manual_rating_does_not_overwrite_original_rule(self):
        self.client.post(f'/api/tasks/{self.task}/report',json={'creator_ids':[self.cid],'allow_missing':True})
        response=self.client.post('/api/results/batch_save',json={'task_id':self.task,'creators':[
            {'creator_id':self.cid,'manual_level':'中推荐','manual_reason':'人工理由','reason':'不能覆盖'}]})
        self.assertEqual(response.status_code,200)
        row=self.client.get('/api/results',params={'task_id':self.task}).json()['creators'][0]
        self.assertEqual(row['reason'],'合成规则')
        self.assertEqual(row['manual_level'],'中推荐')
        self.assertEqual(row['manual_reason'],'人工理由')

    def test_removing_report_entry_keeps_candidate_and_kb(self):
        self.client.post(f'/api/tasks/{self.task}/report',json={'creator_ids':[self.cid],'allow_missing':True})
        self.client.post('/api/results/delete_row',json={'task_id':self.task,'creator_id':self.cid})
        self.assertEqual(len(self.client.get('/api/kb').json()['creators']),1)
        self.assertEqual(len(self.client.get(f'/api/tasks/{self.task}/candidates').json()['creators']),1)

    def test_kb_no_brief_creates_information_list(self):
        response=self.client.post('/api/kb/import',json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,200)
        tid=response.json()['task_id']
        self.assertEqual(db.get_task(tid)['requirements'].get('report_kind'),'information')

    def test_no_enrichment_before_explicit_confirmation(self):
        with patch('app.main.enrich_creator',new=AsyncMock(return_value={'status':'unavailable'})) as adapter:
            self.client.get(f'/api/tasks/{self.task}/candidates')
            self.client.get('/api/kb')
            adapter.assert_not_awaited()

    def test_invalid_conditions_and_ttl_rejected(self):
        self.assertEqual(self.client.post('/api/execute',json={'keyword':'食品','min_followers':100,'max_followers':10}).status_code,422)
        self.assertEqual(self.client.post('/api/settings',json={'settings':{'dynamic_ttl_hours':'-2'}}).status_code,422)

    def test_expired_evidence_is_rejected_before_enrichment_is_queued(self):
        with db.get_connection() as conn:
            conn.execute("UPDATE snapshots SET captured_at='2020-01-01 00:00:00'")
        with patch.object(main,'refresh_candidate',new=AsyncMock()) as refresh, patch.object(
            main,'enrich_creator',new=AsyncMock(return_value={'status':'unavailable'})) as enrich:
            response=self.client.post(f'/api/tasks/{self.task}/enrich',json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,409)
        refresh.assert_not_awaited()
        enrich.assert_not_awaited()

    def test_automatic_tags_have_provenance_and_manual_changes_persist(self):
        rows=self.client.get('/api/kb').json()['creators']
        self.assertTrue(any(t['source']=='automatic' for t in rows[0]['tags']))
        self.client.patch(f'/api/kb/{self.cid}/tags',json={'tags':['复核名单']})
        rows=self.client.get('/api/kb',params={'tag':'复核名单'}).json()['creators']
        self.assertEqual(rows[0]['tags'][0]['source'],'manual')

    def test_kb_numeric_filters_match_actual_numbers_not_unknown(self):
        self.assertEqual(self.client.get('/api/kb',params={'min_followers':2000}).json()['creators'],[])
        self.assertEqual(len(self.client.get('/api/kb',params={'min_avg_likes':200}).json()['creators']),1)

    def test_selection_and_exclusion_can_be_saved_without_starting_enrichment(self):
        response=self.client.patch(f'/api/tasks/{self.task}/selection',json={'creator_ids':[],'excluded_ids':[self.cid]})
        self.assertEqual(response.status_code,200)
        row=self.client.get(f'/api/tasks/{self.task}/candidates',params={'view':'all'}).json()['creators'][0]
        self.assertTrue(row['excluded'])
        self.assertEqual(self.client.get('/api/results',params={'task_id':self.task}).json()['creators'],[])

    def test_task_report_and_export_redact_configured_secret_without_changing_storage(self):
        import json
        import zipfile
        from io import BytesIO
        from app import exporter
        key='synthetic-private-api-key'
        db.save_setting('llm_api_key',key)
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET name=? WHERE id=?',(key,self.task))
        db.save_recommendation(self.cid,self.task,{'hard_pass':True,'reason':key,'level':'高推荐'})
        self.client.post(f'/api/tasks/{self.task}/report',json={'creator_ids':[self.cid],'allow_missing':True})
        self.assertNotIn(key,self.client.get('/api/tasks').text)
        self.assertNotIn(key,self.client.get('/api/results',params={'task_id':self.task}).text)
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter,'EXPORTS_DIR',tmp):
            response=self.client.get('/api/export',params={'task_id':self.task})
            with zipfile.ZipFile(BytesIO(response.content)) as workbook:
                self.assertNotIn(key,workbook.read('xl/sharedStrings.xml').decode())
        self.assertEqual(db.get_task(self.task)['name'],key)

    def test_completed_adapter_cannot_bypass_a_failed_numeric_gate(self):
        from datetime import datetime
        db.save_recommendation(self.cid,self.task,{'hard_pass':False,'level':'淘汰'})
        self.assess(numeric_status='failed')
        data={'status':'completed','identity_verified':True,'median_play_30d':100,
              'audience':{'gender':{'female':60},'age':{'18-24':20},'region':{'浙江':10}},
              'source_url':'https://example.invalid/','captured_at':datetime.now().astimezone().isoformat()}
        with patch.object(main,'enrich_creator',new=AsyncMock(return_value=data)) as adapter:
            response=self.client.post(f'/api/tasks/{self.task}/enrich',json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['code'],'ineligible_selection')
        adapter.assert_not_awaited()
        self.assertEqual(db.get_task(self.task)['status'],'completed')
        self.assertEqual(self.client.get('/api/results',params={'task_id':self.task}).json()['creators'],[])

    def test_selection_validation_and_write_share_worker_reservation_lock(self):
        def checked(*args,**kw):
            self.assertTrue(main.task_lock._is_owned())
        with patch.object(main.workflow,'set_candidate_selection',side_effect=checked) as save:
            response=self.client.patch(f'/api/tasks/{self.task}/selection',json={'creator_ids':[self.cid]})
        self.assertEqual(response.status_code,200)
        save.assert_called_once()

    def test_export_requires_an_explicit_existing_task(self):
        self.assertEqual(self.client.get('/api/export').status_code,400)
        self.assertEqual(self.client.get('/api/export',params={'task_id':999999}).status_code,404)

    def add_pending(self, uid='pending', avg_likes=300):
        cid = db.upsert_creator({'sec_uid': uid, 'nickname': '待审' + uid, 'followers': 1000})
        db.save_snapshot(cid, self.task, {'avg_likes': avg_likes, 'max_likes': 500,
                                          'days_since_latest': 1,
                                          'latest_post_date': '2026-11-04 12:00:00'})
        db.save_recommendation(cid, self.task, {'level': '待核实', 'hard_pass': None,
                                                 'reason': '未审核'})
        return cid

    def test_candidate_views_filter_but_summary_stays_full(self):
        pending = self.add_pending()
        default = self.client.get(f'/api/tasks/{self.task}/candidates').json()
        self.assertEqual([row['creator_id'] for row in default['creators']], [self.cid])
        self.assertEqual(default['summary']['discovered'], 2)
        self.assertEqual(default['summary']['eligible'], 1)
        self.assertEqual(default['summary']['pending'], 1)
        pending_rows = self.client.get(f'/api/tasks/{self.task}/candidates',
                                       params={'view': 'pending'}).json()
        self.assertEqual([row['creator_id'] for row in pending_rows['creators']], [pending])
        rejected = self.client.get(f'/api/tasks/{self.task}/candidates',
                                   params={'view': 'rejected'}).json()
        self.assertEqual(rejected['creators'], [])
        self.assertEqual(rejected['summary']['eligible'], 1)
        self.assertEqual(self.client.get(f'/api/tasks/{self.task}/candidates',
                                         params={'view': 'nonsense'}).status_code, 422)

    def test_selection_rejects_unreviewed_and_keeps_existing_selection(self):
        pending = self.add_pending()
        self.client.patch(f'/api/tasks/{self.task}/selection',
                          json={'creator_ids': [self.cid], 'excluded_ids': []})
        response = self.client.patch(f'/api/tasks/{self.task}/selection',
                                     json={'creator_ids': [pending], 'excluded_ids': []})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ineligible_selection')
        self.assertEqual(response.json()['creator_ids'], [pending])
        rows = {row['creator_id']: row for row in self.client.get(
            f'/api/tasks/{self.task}/candidates', params={'view': 'all'}).json()['creators']}
        self.assertTrue(rows[self.cid]['selected'])
        self.assertFalse(rows[pending]['selected'])

    def test_review_api_is_explicit_and_can_pause_on_budget(self):
        one = self.add_pending('one')
        two = self.add_pending('two')
        db.save_setting('max_topic_reviews_per_run', '1')
        self.assess(self.cid)
        async def reserve_review(*args, budget):
            budget['used'] += 1
            return {'overall_status': 'pending', 'topic_reviewed': True}
        review = AsyncMock(side_effect=reserve_review)
        with patch.object(main, 'review_existing_candidate', new=review):
            response = self.client.post(f'/api/tasks/{self.task}/review', json={'creator_ids': None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()['creator_ids']), {one, two})
        task = db.get_task(self.task)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['progress']['pause_reason'], 'topic_review_budget')
        self.assertEqual(review.await_count, 1)
        self.assertEqual(self.client.post(f'/api/tasks/{self.task}/review',
                                          json={'creator_ids': [99999]}).status_code, 400)

    def test_review_api_rejects_when_another_task_is_running(self):
        self.add_pending('one')
        other = db.save_task('other', 'other', status='running')
        main.current_running_task.update(is_running=True, task_id=other)
        response = self.client.post(f'/api/tasks/{self.task}/review', json={'creator_ids': None})
        self.assertEqual(response.status_code, 409)
        db.update_task_status(other, 'completed')
        main.current_running_task.update(is_running=False, task_id=None)

    def test_selected_and_pending_excluded_can_be_saved_or_enriched(self):
        pending = self.add_pending()
        selection = {'creator_ids': [self.cid], 'excluded_ids': [pending]}
        self.assertEqual(self.client.patch(f'/api/tasks/{self.task}/selection', json=selection).status_code, 200)
        with patch.object(main, 'enrich_creator', new=AsyncMock(return_value={'status': 'unavailable'})):
            self.assertEqual(self.client.post(f'/api/tasks/{self.task}/enrich', json=selection).status_code, 200)
        rows = {r['creator_id']: r for r in main.workflow.get_candidates(self.task)}
        self.assertTrue(rows[self.cid]['selected'])
        self.assertTrue(rows[pending]['excluded'])
        self.assertEqual(self.client.patch(f'/api/tasks/{self.task}/selection',
                                          json={'creator_ids': [self.cid], 'excluded_ids': [99999]}).status_code, 400)

    def test_settings_reject_sample_limit_below_confirmed_minimum_atomically(self):
        response = self.client.post('/api/settings', json={'settings': {
            'single_post_sample': '1', 'llm_api_key': 'must-not-be-saved'}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(db.get_all_settings()['single_post_sample'], '10')
        self.assertNotEqual(db.get_all_settings().get('llm_api_key'), 'must-not-be-saved')

    def test_worker_rechecks_eligibility_after_refresh(self):
        import asyncio
        async def refresh(*args):
            with db.get_connection() as conn:
                conn.execute('UPDATE snapshots SET avg_likes=1')
            self.assess(numeric_status='failed')
        with patch.object(main, 'dynamic_metrics_stale', return_value=True), patch.object(
                main, 'refresh_candidate', side_effect=refresh), patch.object(
                main, 'enrich_creator', new=AsyncMock()) as enrich:
            asyncio.run(main.background_enrich_worker(self.task, [self.cid]))
        enrich.assert_not_awaited()
        self.assertEqual(db.get_task(self.task)['status'], 'partial')
        progress = db.get_task(self.task)['progress']
        self.assertIn('enrichment_succeeded', progress)
        self.assertEqual(progress['enrichment_succeeded'], 0)
        self.assertEqual(progress['enrichment_failed'], 0)
        self.assertEqual(progress['enrichment_skipped'], 1)
        self.assertEqual(progress['enrichment_processed'], 0)
        self.assertEqual(progress['enrichment_unprocessed'], 0)
        self.assertEqual(main.workflow.get_report(self.task), [])

    def test_worker_does_not_enrich_after_failed_refresh(self):
        import asyncio
        with patch.object(main, 'dynamic_metrics_stale', return_value=True), patch.object(
                main, 'refresh_candidate', new=AsyncMock(side_effect=RuntimeError('fixture failure'))), patch.object(
                main, 'enrich_creator', new=AsyncMock()) as enrich:
            asyncio.run(main.background_enrich_worker(self.task, [self.cid]))
        enrich.assert_not_awaited()
        self.assertEqual(db.get_task(self.task)['status'], 'failed')
        progress = db.get_task(self.task)['progress']
        self.assertEqual(progress['enrichment_succeeded'], 0)
        self.assertEqual(progress['enrichment_failed'], 1)
        self.assertEqual(progress['enrichment_processed'], 0)
        self.assertEqual(progress['enrichment_unprocessed'], 0)

    def test_enrichment_finish_has_stage_actual_report_count_and_readable_reason(self):
        import asyncio
        data = {'status': 'unavailable', 'identity_verified': True,
                'reason': 'audience_schema_not_verified', 'missing_fields': ['audience']}
        with patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                main, 'enrich_creator', new=AsyncMock(return_value=data)):
            asyncio.run(main.background_enrich_worker(self.task, [self.cid]))
        message = db.get_chat_history()[-1]
        self.assertEqual(message['meta']['stage'], 'enrichment')
        self.assertTrue(message['meta']['task_finished'])
        self.assertEqual(message['meta']['task_id'], self.task)
        self.assertEqual(message['meta']['status'], 'failed')
        self.assertEqual(message['meta']['counts']['report'], 0)
        self.assertIn('粉丝画像尚未获取/接口未核实', message['content'])
        self.assertIn('0', message['content'])
        self.assertNotIn('已进入正式报告', message['content'])

    def test_enrichment_permission_and_identity_reasons_are_not_fake_completion(self):
        import asyncio
        for reason, label in (
                ('customer_entitlement_required', '星图客户权益等级不足，无法读取更多数据'),
                ('audience_permission_denied', '星图客户权益不足'),
                ('no_unique_douyin_identity_mapping', '星图账号身份未能唯一匹配')):
            with self.subTest(reason=reason), patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                    main, 'enrich_creator', new=AsyncMock(return_value={
                        'status': 'unavailable', 'reason': reason, 'identity_verified': False})):
                asyncio.run(main.background_enrich_worker(self.task, [self.cid]))
            message = db.get_chat_history()[-1]
            self.assertIn(label, message['content'])
            self.assertEqual(message['meta']['counts']['report'], 0)
            self.assertEqual(message['meta']['status'], 'failed')
        self.assertEqual(main.workflow.get_report(self.task), [])

    def test_enrichment_summary_groups_multiple_reasons_without_private_errors(self):
        import asyncio
        other = self.add_pending('second')
        self.assess(other)
        with patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                main, 'enrich_creator', new=AsyncMock(side_effect=[
                    {'status': 'permission_denied', 'reason': 'login_or_permission_challenge'},
                    RuntimeError('private-cookie-secret')])):
            asyncio.run(main.background_enrich_worker(self.task, [self.cid, other]))
        message = db.get_chat_history()[-1]
        self.assertIn('平台要求登录或人工验证', message['content'])
        self.assertIn('平台读取异常', message['content'])
        self.assertNotIn('private-cookie-secret', str(message))
        self.assertEqual(message['meta']['counts']['report'], 0)

    def test_completed_enrichment_still_uses_admission_gate_and_existing_report_count(self):
        import asyncio
        main.workflow.admit_report(self.task, [self.cid], allow_missing=True)
        other = self.add_pending('second')
        self.assess(other)
        with patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                main, 'enrich_creator', new=AsyncMock(return_value={'status': 'completed', 'identity_verified': False})):
            asyncio.run(main.background_enrich_worker(self.task, [other]))
        message = db.get_chat_history()[-1]
        self.assertEqual(message['meta']['counts']['report'], 1)
        self.assertEqual(len(main.workflow.get_report(self.task)), 1)
        self.assertIn('身份尚未核验', message['content'])
        self.assertNotIn('已进入正式报告', message['content'])

    def test_verified_enrichment_reports_actual_admission(self):
        import asyncio
        from datetime import datetime
        data = {'status': 'completed', 'identity_verified': True, 'median_play_30d': 100,
                'audience': {'gender': {'female': 60}, 'age': {'18-24': 20}, 'region': {'浙江': 10}},
                'source_url': 'https://www.xingtu.cn/ad/creator/market',
                'captured_at': datetime.now().astimezone().isoformat(), 'missing_fields': []}
        with patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                main, 'enrich_creator', new=AsyncMock(return_value=data)):
            asyncio.run(main.background_enrich_worker(self.task, [self.cid]))
        message = db.get_chat_history()[-1]
        self.assertEqual(message['meta']['status'], 'completed')
        self.assertEqual(message['meta']['counts']['report'], 1)
        self.assertEqual(message['meta']['stage'], 'enrichment')
        self.assertEqual(len(main.workflow.get_report(self.task)), 1)

    def completed_enrichment(self):
        from datetime import datetime
        return {'status': 'completed', 'identity_verified': True, 'median_play_30d': 100,
                'audience': {'gender': {'female': 60}, 'age': {'18-24': 20}, 'region': {'浙江': 10}},
                'source_url': 'https://www.xingtu.cn/ad/creator/market',
                'captured_at': datetime.now().astimezone().isoformat(), 'missing_fields': []}

    def run_enrichment(self, ids, **adapter_kwargs):
        import asyncio
        with patch.object(main, 'dynamic_metrics_stale', return_value=False), patch.object(
                main, 'enrich_creator', new=AsyncMock(**adapter_kwargs)):
            asyncio.run(main.background_enrich_worker(self.task, ids))
        message = db.get_chat_history()[-1]
        self.assertIn('progress', message['meta'])
        return message

    def test_five_failed_enrichments_have_zero_success_and_per_creator_reasons(self):
        ids = [self.cid] + [self.add_pending(f'failed-{i}') for i in range(4)]
        for cid in ids[1:]:
            self.assess(cid)
        data = {'status': 'failed', 'reason': 'browser_or_response_failure', 'identity_verified': False}
        message = self.run_enrichment(ids, return_value=data)
        self.assertEqual(message['meta']['status'], 'failed')
        self.assertEqual(message['meta']['counts']['report'], 0)
        progress = message['meta']['progress']
        for field, expected in {'enrichment_requested': 5, 'enrichment_processed': 5,
                                'enrichment_succeeded': 0, 'enrichment_failed': 5,
                                'enrichment_admitted': 0, 'admission_pending': 5,
                                'enrichment_unprocessed': 0}.items():
            self.assertEqual(progress[field], expected, field)
        self.assertEqual(progress, db.get_task(self.task)['progress'])
        self.assertEqual({item['creator_id'] for item in progress['enrichment_failure_details']}, set(ids))
        self.assertEqual(progress['enrichment_failure_reasons']['平台读取异常'], 5)
        for cid in ids:
            self.assertEqual(main.workflow.get_enrichment(self.task, cid)['reason'], 'browser_or_response_failure')
        self.assertIn('补全成功 0/5', message['content'])
        self.assertIn('本轮新增正式报告 0', message['content'])
        self.assertNotIn('星图本轮处理', message['content'])
        self.assertEqual(self.client.get('/api/export', params={'task_id': self.task}).status_code, 404)

    def test_mixed_enrichment_counts_success_separately_from_processed_and_admitted(self):
        other = self.add_pending('mixed')
        self.assess(other)
        message = self.run_enrichment([self.cid, other], side_effect=[self.completed_enrichment(),
            {'status': 'failed', 'reason': 'no_unique_douyin_identity_mapping'}])
        self.assertEqual(message['meta']['status'], 'partial')
        progress = message['meta']['progress']
        self.assertEqual(progress['enrichment_processed'], 2)
        self.assertEqual(progress['enrichment_succeeded'], 1)
        self.assertEqual(progress['enrichment_failed'], 1)
        self.assertEqual(progress['enrichment_admitted'], 1)
        self.assertEqual(progress['admission_pending'], 1)
        self.assertEqual(message['meta']['counts']['report'], 1)

    def test_failed_retry_does_not_count_historical_report_as_new_success(self):
        main.workflow.admit_report(self.task, [self.cid], allow_missing=True)
        before = main.workflow.get_report(self.task)
        message = self.run_enrichment([self.cid], return_value={
            'status': 'failed', 'reason': 'browser_or_response_failure'})
        self.assertEqual(message['meta']['status'], 'failed')
        self.assertEqual(message['meta']['counts']['report'], 1)
        self.assertEqual(message['meta']['progress']['enrichment_admitted'], 0)
        self.assertEqual(message['meta']['progress']['enrichment_succeeded'], 0)
        self.assertEqual(main.workflow.get_report(self.task), before)
        self.assertIn('本轮新增正式报告 0', message['content'])
        self.assertIn('正式报告当前 1', message['content'])

    def test_successful_duplicate_selection_does_not_readmit_historical_report(self):
        main.workflow.admit_report(self.task, [self.cid], allow_missing=True)
        message = self.run_enrichment([self.cid, self.cid], return_value=self.completed_enrichment())
        progress = message['meta']['progress']
        self.assertEqual(message['meta']['status'], 'completed')
        self.assertEqual(progress['enrichment_requested'], 1)
        self.assertEqual(progress['enrichment_processed'], 1)
        self.assertEqual(progress['enrichment_succeeded'], 1)
        self.assertEqual(progress['enrichment_admitted'], 0)

    def test_completed_but_unverified_data_is_not_report_admission(self):
        message = self.run_enrichment([self.cid], return_value={
            **self.completed_enrichment(), 'identity_verified': False})
        self.assertEqual(message['meta']['status'], 'partial')
        self.assertEqual(message['meta']['progress']['enrichment_succeeded'], 1)
        self.assertEqual(message['meta']['progress']['enrichment_admitted'], 0)
        self.assertEqual(message['meta']['progress']['admission_pending'], 1)
        self.assertEqual(main.workflow.get_report(self.task), [])

    def test_stop_before_enrichment_has_no_fake_failure_or_success(self):
        db.update_task_status(self.task, 'enriching', 'fixture')
        main.workflow.request_stop(self.task)
        message = self.run_enrichment([self.cid], side_effect=AssertionError('No platform after stop'))
        self.assertEqual(message['meta']['status'], 'stopped')
        progress = message['meta']['progress']
        for field in ('enrichment_processed', 'enrichment_succeeded', 'enrichment_failed', 'enrichment_admitted'):
            self.assertEqual(progress[field], 0, field)
        self.assertEqual(progress['enrichment_unprocessed'], 1)
        self.assertTrue(db.get_task(self.task)['stop_requested'])

    def test_stop_after_success_preserves_saved_report_and_remaining_count(self):
        other = self.add_pending('stopped')
        self.assess(other)
        db.update_task_status(self.task, 'enriching', 'fixture')
        async def enrich(candidate):
            main.workflow.request_stop(self.task)
            return self.completed_enrichment()
        message = self.run_enrichment([self.cid, other], side_effect=enrich)
        self.assertEqual(message['meta']['status'], 'stopped')
        progress = message['meta']['progress']
        self.assertEqual(progress['enrichment_succeeded'], 1)
        self.assertEqual(progress['enrichment_admitted'], 1)
        self.assertEqual(progress['enrichment_unprocessed'], 1)
        self.assertEqual(progress['enrichment_failed'], 0)
        self.assertEqual(message['meta']['counts']['report'], 1)

    def test_unexpected_admission_error_preserves_completed_work_in_receipt(self):
        with patch.object(main.workflow, 'admit_report', side_effect=RuntimeError('private-secret')):
            message = self.run_enrichment([self.cid], return_value=self.completed_enrichment())
        self.assertEqual(message['meta']['status'], 'failed')
        self.assertEqual(message['meta']['progress']['enrichment_processed'], 1)
        self.assertEqual(message['meta']['progress']['enrichment_succeeded'], 1)
        self.assertEqual(message['meta']['progress']['enrichment_admitted'], 0)
        self.assertNotIn('private-secret', str(message))

    def test_identity_and_audience_query_failures_have_safe_distinct_labels(self):
        for reason, label in [('identity_lookup_failed', '星图账号身份匹配失败'),
                              ('audience_detail_query_failed', '星图粉丝详情查询失败')]:
            with self.subTest(reason=reason):
                message = self.run_enrichment([self.cid], return_value={'status': 'failed', 'reason': reason})
                self.assertEqual(message['meta']['status'], 'failed')
                self.assertIn(label, message['content'])
                self.assertEqual(message['meta']['progress']['enrichment_failure_reasons'][label], 1)

    def test_verified_report_can_export_frozen_enrichment_to_excel(self):
        import zipfile
        from io import BytesIO
        from app import exporter
        data = {**self.completed_enrichment(), 'star_id': 'synthetic-star',
                'price_1_20': 1200, 'price_21_60': 2400, 'expected_cpm': 35.5}
        self.run_enrichment([self.cid], return_value=data)
        # Export must read the admitted snapshot, not the latest failed retry.
        self.run_enrichment([self.cid], return_value={'status': 'failed', 'reason': 'browser_or_response_failure'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            response = self.client.get('/api/export', params={'task_id': self.task})
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(BytesIO(response.content)) as workbook:
                text = workbook.read('xl/sharedStrings.xml').decode()
                sheet = workbook.read('xl/worksheets/sheet1.xml').decode()
        for value in ('synthetic-star', data['source_url'], data['captured_at'], 'completed'):
            self.assertIn(value, text)
        for value in ('1200', '2400', '35.5'):
            self.assertIn(f'<v>{value}</v>', sheet)

    def test_review_retries_placeholder_and_retains_budget_after_save_failure(self):
        from app import crawler
        db.save_setting('max_topic_reviews_per_run', '1')
        candidates = [self.add_pending(str(index)) for index in range(3)]
        for cid in candidates:
            db.save_sample_posts(cid, self.task, [
                {'aweme_id': f'{cid}-{index}', 'desc': 'food recipe', 'likes': 300}
                for index in range(6)])
            self.assess(cid, topic_status='unknown', error_code='budget_exhausted')
        policy = qualification.policy_from_requirements(db.get_task(self.task)['requirements'])
        topic = qualification.unknown_topic_assessment(policy, 'model_error', 'temporary fixture error')
        with patch.object(qualification, 'review_creator_topic', new=AsyncMock(return_value=topic)) as review, patch.object(
                crawler, 'save_candidate_assessment', side_effect=RuntimeError('fixture save failure')):
            response = self.client.post(f'/api/tasks/{self.task}/review', json={'creator_ids': candidates})
        self.assertEqual(response.status_code, 200)
        review.assert_awaited_once()
        state = db.get_task(self.task)
        self.assertEqual(state['progress']['topic_reviews'], 1)
        self.assertEqual(state['progress']['review_failed'], 1)
        self.assertEqual(state['progress']['pause_reason'], 'topic_review_budget')

    def test_explicit_review_retries_unknown_results_then_reuses_completed_review(self):
        completed = db.get_current_candidate_assessment(self.task, self.cid)['topic']
        for code in ('not_reviewed', 'budget_exhausted', 'model_error'):
            with self.subTest(code=code):
                cid = self.add_pending(code)
                db.save_sample_posts(cid, self.task, [
                    {'aweme_id': f'{code}-{index}', 'desc': 'food recipe', 'likes': 300}
                    for index in range(6)])
                self.assess(cid, topic_status='unknown', error_code=code)
                with patch.object(qualification, 'review_creator_topic', new=AsyncMock(return_value=completed)) as review:
                    for _ in range(2):
                        response = self.client.post(f'/api/tasks/{self.task}/review', json={'creator_ids': [cid]})
                        self.assertEqual(response.status_code, 200)
                        current = db.get_current_candidate_assessment(self.task, cid)
                        self.assertEqual(current['overall_status'], 'eligible')
                    review.assert_awaited_once()
                self.assertEqual(db.get_task(self.task)['progress']['topic_reviews'], 0)

    def test_kb_placeholder_is_current_and_explicit_review_completes_it(self):
        db.save_sample_posts(self.cid, self.task, [
            {'aweme_id': str(index), 'desc': 'food recipe', 'likes': 300} for index in range(6)])
        target = db.save_task('kb review', 'food', {'min_avg_likes': 100}, status='completed')
        response = self.client.post('/api/kb/import', json={'creator_ids': [self.cid], 'task_id': target})
        self.assertEqual(response.status_code, 200)
        placeholder = db.get_current_candidate_assessment(target, self.cid)
        self.assertIsNotNone(placeholder)
        self.assertEqual(placeholder['topic']['error_code'], 'not_reviewed')
        self.assess()
        topic = db.get_current_candidate_assessment(self.task, self.cid)['topic']
        with patch.object(qualification, 'review_creator_topic', new=AsyncMock(return_value=topic)) as review:
            response = self.client.post(f'/api/tasks/{target}/review', json={'creator_ids': [self.cid]})
        self.assertEqual(response.status_code, 200)
        review.assert_awaited_once()
        self.assertEqual(db.get_current_candidate_assessment(target, self.cid)['overall_status'], 'eligible')

    def test_settings_validate_merged_policy_against_higher_confirmed_minimum(self):
        db.save_task('larger sample', 'food', {'topic_min_samples': 8}, status='completed')
        response = self.client.post('/api/settings', json={'settings': {'single_post_sample': '7'}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(db.get_all_settings()['single_post_sample'], '10')
        db.save_setting('single_post_sample', '1')
        response = self.client.post('/api/settings', json={'settings': {'dynamic_ttl_hours': '48'}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(db.get_all_settings()['dynamic_ttl_hours'], '24')
