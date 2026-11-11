import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import db, main, platform_sessions


class PlatformAPITests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        self.manager = platform_sessions.PlatformSessions(state_paths={
            key: Path(temp) / (key + '.json') for key in ('douyin', 'xingtu')})
        self.enterContext(patch.object(main, 'platform_session_manager', self.manager, create=True))
        main.current_running_task.clear()
        main.current_running_task.update(is_running=False, task_id=None)
        self.client = self.enterContext(TestClient(main.app, base_url='http://127.0.0.1:8088'))

    def test_read_status_is_local_and_never_returns_session_contents(self):
        self.manager.paths['douyin'].write_text(json.dumps({'cookies': [
            {'name': 'sessionid', 'value': 'synthetic-private-cookie', 'domain': '.douyin.com', 'expires': -1}]}))
        with patch.object(self.manager, '_check', new=AsyncMock()) as check:
            response = self.client.get('/api/platforms/status')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['cache-control'])
        data = response.json()['platforms']
        self.assertEqual(data['douyin']['status'], 'saved')
        self.assertTrue(data['xingtu']['connected'])
        self.assertNotIn('synthetic-private-cookie', response.text)
        check.assert_not_awaited()

    def test_unknown_platform_is_rejected(self):
        self.assertEqual(self.client.post('/api/platforms/unknown/login', json={'replace': True}).status_code, 404)

    def test_untrusted_origin_cannot_open_login_window(self):
        with patch.object(self.manager, 'start_login') as login:
            response = self.client.post('/api/platforms/douyin/login', json={'replace': True},
                                        headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status_code, 403)
        login.assert_not_called()

    def test_matching_untrusted_host_and_origin_cannot_open_login_window(self):
        with patch.object(self.manager, 'start_login') as login:
            response = self.client.post('/api/platforms/douyin/login', json={'replace': True}, headers={
                'Host': 'rebound.invalid:8088', 'Origin': 'http://rebound.invalid:8088',
                'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(response.status_code, 403)
        login.assert_not_called()

    def test_running_collection_blocks_session_replacement(self):
        tid = db.save_task('active', 'food', status='running')
        main.current_running_task.update(task_id=tid, is_running=True)
        with patch.object(self.manager, 'start_login') as login:
            response = self.client.post('/api/platforms/douyin/login', json={'replace': True})
        self.assertEqual(response.status_code, 409)
        login.assert_not_called()

    def test_start_login_opens_job_and_cancel_uses_same_platform(self):
        expected = self.manager.view('xingtu')
        with patch.object(self.manager, 'start_login', return_value=expected) as start:
            response = self.client.post('/api/platforms/xingtu/login', json={'replace': True})
        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with('xingtu', replace=True)
        with patch.object(self.manager, 'cancel', new=AsyncMock(return_value=expected)) as cancel:
            response = self.client.post('/api/platforms/xingtu/cancel', json={})
        self.assertEqual(response.status_code, 200)
        cancel.assert_awaited_once_with('xingtu')

    def test_existing_auth_job_prevents_starting_a_collection(self):
        with patch.object(self.manager, 'busy', return_value=True), patch.object(main, 'execute_bd_search_task', new=AsyncMock()):
            response = self.client.post('/api/execute', json={'category': '食品', 'keyword': '食品', 'sample_count': 5})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.get_all_tasks(), [])

    def test_auth_job_prevents_staring_xingtu_reads(self):
        tid = db.save_task('completed', 'food', status='completed')
        with patch.object(self.manager, 'busy', return_value=True), patch.object(main, 'selection_error', return_value=None):
            response = self.client.post(f'/api/tasks/{tid}/enrich', json={'creator_ids': [1]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.get_task(tid)['status'], 'completed')
