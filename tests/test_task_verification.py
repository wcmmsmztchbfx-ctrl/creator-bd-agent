import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from app import db, main, platform_sessions as sessions


def saved_state(value='original-secret'):
    return {'cookies': [{'name': 'sessionid', 'value': value, 'domain': '.douyin.com',
                         'path': '/', 'expires': -1}], 'origins': []}


class TaskVerificationAPITests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'db')))
        self.manager = sessions.PlatformSessions(state_paths={p: Path(temp) / (p + '.json')
                                                              for p in ('douyin', 'xingtu')})
        self.manager.paths['douyin'].write_text(json.dumps(saved_state()))
        self.browser = SimpleNamespace(verify=AsyncMock(return_value={
            'status': 'authenticated', 'state': saved_state('new-secret')}),
            bind_saved_state=Mock(), close=AsyncMock())
        self.enterContext(patch.dict(sys.modules, {
            'app.douyin_browser': SimpleNamespace(browser=self.browser)}))
        self.enterContext(patch.object(main, 'platform_session_manager', self.manager))
        main.current_running_task.clear()
        main.current_running_task.update(task_id=None, is_running=False)
        self.client = self.enterContext(TestClient(main.app, base_url='http://127.0.0.1:8088'))
        self.tid = db.save_task('blocked', '食品', status='blocked', requirements={
            'keyword': '食品', 'search_queries': ['食品测评'], 'discovery_mode': 'video'})
        self.url = f'/api/tasks/{self.tid}/verify'

    def test_accepted_shape_uses_saved_search_not_login_and_never_restarts_task(self):
        db.update_task_status(self.tid, 'blocked', 'verification', {
            'pause_reason': 'platform_verification', 'query': '实际 & 查询/词', 'search_type': 'user', 'offset': 40})
        before = db.get_task(self.tid)
        with patch.object(self.manager, '_verify_search', new=AsyncMock(side_effect=RuntimeError('private')) ) as verify:
            response = self.client.post(self.url, json={})
            self.client.get('/api/platforms/status')
        self.assertEqual(response.status_code, 202)
        view = response.json()['platforms'][0]
        self.assertEqual(view['platform'], 'douyin')
        self.assertTrue(view['busy'])
        self.assertEqual(view['login']['task_id'], self.tid)
        verify.assert_awaited_once_with({'task_id': self.tid, 'query': '实际 & 查询/词', 'search_type': 'user', 'offset': 40})
        self.assertEqual(db.get_task(self.tid), before)
        self.assertNotIn('original-secret', response.text)

    def test_old_task_falls_back_to_requirements(self):
        with patch.object(self.manager, '_verify_search', new=AsyncMock(side_effect=RuntimeError())) as verify:
            response = self.client.post(self.url, json={})
            self.client.get('/api/platforms/status')
        self.assertEqual(response.status_code, 202)
        verify.assert_awaited_once_with({'task_id': self.tid, 'query': '食品测评', 'search_type': 'video', 'offset': 0})

    def test_shared_browser_success_saves_state_but_does_not_resume_collection(self):
        before = db.get_task(self.tid)
        history = db.get_chat_history()
        response = self.client.post(self.url, json={})
        self.assertEqual(response.status_code, 202)
        for _ in range(10):
            view = self.client.get('/api/platforms/status').json()['platforms']['douyin']
            if not view['busy']:
                break
        self.assertEqual(view['login']['status'], 'succeeded')
        self.assertEqual(view['status'], 'authenticated')
        self.browser.verify.assert_awaited_once_with({
            'task_id': self.tid, 'query': '食品测评', 'search_type': 'video', 'offset': 0})
        self.browser.bind_saved_state.assert_called_once_with()
        self.browser.close.assert_not_awaited()
        self.assertEqual(json.loads(self.manager.paths['douyin'].read_text()), saved_state('new-secret'))
        self.assertEqual(db.get_task(self.tid), before)
        self.assertEqual(db.get_chat_history(), history)
        self.assertFalse(main.current_running_task['is_running'])
        self.assertNotIn('new-secret', json.dumps(view))

    def test_untrusted_requests_unknown_tasks_and_arbitrary_fields_are_rejected(self):
        for headers in ({'Origin': 'https://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'evil.invalid:8088', 'Origin': 'http://evil.invalid:8088'}):
            self.assertEqual(self.client.post(self.url, json={}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/tasks/999999/verify', json={}).status_code, 404)
        self.assertEqual(self.client.post(self.url, json={'url': 'https://evil.invalid'}).status_code, 422)
        db.update_task_status(self.tid, 'completed')
        self.assertEqual(self.client.post(self.url, json={}).status_code, 409)

    def test_global_task_and_auth_jobs_block_verification(self):
        other = db.save_task('active', 'x', status='enriching')
        main.current_running_task.update(task_id=other, is_running=True)
        self.assertEqual(self.client.post(self.url, json={}).status_code, 409)
        db.update_task_status(other, 'completed')
        with patch.object(self.manager, 'busy', return_value=True):
            self.assertEqual(self.client.post(self.url, json={}).status_code, 409)

    def test_invalid_saved_search_or_missing_session_never_opens_browser(self):
        db.update_task_status(self.tid, 'blocked', 'verification', {
            'query': 'food', 'search_type': 'https://evil.invalid', 'offset': 0})
        self.assertEqual(self.client.post(self.url, json={}).status_code, 400)
        self.manager.paths['douyin'].unlink()
        db.update_task_status(self.tid, 'blocked', 'verification', {})
        self.assertEqual(self.client.post(self.url, json={}).status_code, 400)
        self.assertFalse(self.manager.busy())

    def test_stale_cancel_api_preserves_other_task_and_same_task_new_verifier(self):
        other = db.save_task('other blocked', '食品', status='blocked', requirements={
            'search_queries': ['食品测评'], 'discovery_mode': 'user'})

        async def waiting(*args):
            await asyncio.Event().wait()

        with patch.object(self.manager, '_verify_search', side_effect=waiting):
            for next_tid in (other, self.tid):
                with self.subTest(next_tid=next_tid):
                    first = self.client.post(self.url, json={})
                    self.assertEqual(first.status_code, 202)
                    old_id = first.json()['platforms'][0]['login'].get('job_id')
                    self.assertIsInstance(old_id, str)
                    cancelled = self.client.post('/api/platforms/douyin/cancel', json={'expected_job_id': old_id})
                    self.assertEqual(cancelled.status_code, 200)
                    self.assertEqual(cancelled.json()['platform']['login']['job_id'], old_id)
                    second = self.client.post(f'/api/tasks/{next_tid}/verify', json={})
                    self.assertEqual(second.status_code, 202)
                    new_id = second.json()['platforms'][0]['login']['job_id']
                    self.assertNotEqual(new_id, old_id)
                    stale = self.client.post('/api/platforms/douyin/cancel', json={'expected_job_id': old_id})
                    self.assertEqual(stale.status_code, 409)
                    self.assertIn('error', stale.json())
                    current = self.client.get('/api/platforms/status').json()['platforms']['douyin']
                    self.assertTrue(current['busy'])
                    self.assertEqual(current['login']['task_id'], next_tid)
                    self.assertEqual(current['login']['job_id'], new_id)
                    self.assertEqual(self.client.post('/api/platforms/douyin/cancel',
                        json={'expected_job_id': new_id}).status_code, 200)

    def test_legacy_discovery_block_survives_enrichment_progress_replacement(self):
        for stage in (None, 'discovery'):
            with self.subTest(stage=stage):
                db.clear_chat_history()
                meta = {'task_finished': True, 'task_id': self.tid, 'status': 'blocked'}
                if stage:
                    meta['stage'] = stage
                db.save_chat_message('assistant', 'original blocked discovery', meta)
                db.update_task_status(self.tid, 'partial', 'enrichment finished', {'enrichment_processed': 1})
                db.save_chat_message('assistant', 'enrichment finished', {
                    'task_finished': True, 'task_id': self.tid, 'status': 'partial'})
                for _ in range(40):
                    db.save_chat_message('user', 'unrelated later history')
                before = db.get_task(self.tid)
                history = db.get_chat_history(limit=100)
                with patch.object(self.manager, '_verify_search', new=AsyncMock(side_effect=RuntimeError())) as verify:
                    response = self.client.post(self.url, json={})
                    self.client.get('/api/platforms/status')
                self.assertEqual(response.status_code, 202)
                verify.assert_awaited_once_with({'task_id': self.tid, 'query': '食品测评',
                                                'search_type': 'video', 'offset': 0})
                self.assertEqual(db.get_task(self.tid), before)
                self.assertEqual(db.get_chat_history(limit=100), history)

    def test_explicit_resolved_discovery_notice_clears_older_legacy_block(self):
        db.update_task_status(self.tid, 'partial', 'enrichment', {'enrichment_processed': 1})
        db.save_chat_message('assistant', 'blocked discovery', {
            'task_finished': True, 'task_id': self.tid, 'status': 'blocked'})
        db.save_chat_message('assistant', 'explicit completed discovery', {
            'task_finished': True, 'task_id': self.tid, 'status': 'completed', 'stage': 'discovery'})
        with patch.object(self.manager, '_verify_search', new=AsyncMock()) as verify:
            response = self.client.post(self.url, json={})
        self.assertEqual(response.status_code, 409)
        verify.assert_not_awaited()

    def test_enrichment_preserves_search_context_without_relying_on_chat_history(self):
        context = {'pause_reason': 'platform_verification', 'query': '原查询', 'search_type': 'user', 'offset': 60}
        db.update_task_status(self.tid, 'blocked', 'blocked discovery', dict(context))
        cid = db.upsert_creator({'sec_uid': 'synthetic-pending', 'nickname': 'pending'})
        db.save_recommendation(cid, self.tid, {'level': '待核实', 'hard_pass': None})
        with patch.object(main, 'dynamic_metrics_stale', return_value=False):
            asyncio.run(main.background_enrich_worker(self.tid, [cid]))
        task = db.get_task(self.tid)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['progress'].get('search_verification'), context)
        self.assertNotIn('pause_reason', task['progress'])
        db.clear_chat_history()
        with patch.object(self.manager, '_verify_search', new=AsyncMock(side_effect=RuntimeError())) as verify:
            response = self.client.post(self.url, json={})
            self.client.get('/api/platforms/status')
        self.assertEqual(response.status_code, 202)
        verify.assert_awaited_once_with({'task_id': self.tid, 'query': '原查询', 'search_type': 'user', 'offset': 60})

    def test_unrelated_partial_tasks_cannot_use_legacy_recovery(self):
        db.update_task_status(self.tid, 'partial', 'ordinary partial', {'enrichment_processed': 1})
        for role, meta in (
                ('assistant', None),
                ('user', {'task_finished': True, 'task_id': self.tid, 'status': 'blocked'}),
                ('assistant', {'task_finished': True, 'task_id': self.tid, 'status': 'blocked', 'stage': 'enrichment'}),
                ('assistant', {'task_finished': True, 'task_id': self.tid + 1, 'status': 'blocked'}),
                ('assistant', {'task_id': self.tid, 'status': 'blocked'}),
                ('assistant', {'task_finished': True, 'task_id': self.tid, 'status': 'completed', 'stage': 'discovery'})):
            with self.subTest(role=role, meta=meta):
                db.clear_chat_history()
                if meta:
                    db.save_chat_message(role, 'not evidence of a discovery block', meta)
                with patch.object(self.manager, '_verify_search', new=AsyncMock()) as verify:
                    response = self.client.post(self.url, json={})
                self.assertEqual(response.status_code, 409)
                verify.assert_not_awaited()


