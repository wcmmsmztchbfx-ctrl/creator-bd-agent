import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, MagicMock, patch

from app import platform_sessions as sessions


def state(domain='.douyin.com', expires=-1, value='synthetic-secret'):
    return {'cookies': [{'name': 'sessionid', 'value': value, 'domain': domain,
                         'path': '/', 'expires': expires}], 'origins': []}


class PlatformSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = {p: Path(self.temp) / f'{p}.json' for p in ('douyin', 'xingtu')}
        self.manager = sessions.PlatformSessions(state_paths=self.paths, login_timeout=0.1)
        self.addAsyncCleanup(self.manager.close)

    def save(self, platform='douyin', **kwargs):
        self.paths[platform].write_text(json.dumps(state(**kwargs)), encoding='utf-8')

    def test_saved_credentials_are_not_falsely_authenticated_or_disconnected(self):
        self.save()
        view = self.manager.view('douyin')
        self.assertTrue(view['connected'])
        self.assertTrue(view['has_saved_session'])
        self.assertEqual(view['status'], 'saved')
        self.assertNotIn('synthetic-secret', json.dumps(view))
        self.assertNotIn(str(self.paths['douyin']), json.dumps(view))
        self.assertIn('待验证', view['label'])

    def test_missing_expired_and_invalid_credentials_are_distinct(self):
        self.assertEqual(self.manager.view('douyin')['status'], 'missing')
        self.save(expires=time.time()-1)
        self.assertEqual(self.manager.view('douyin')['status'], 'expired')
        self.paths['douyin'].write_text('{broken', encoding='utf-8')
        self.assertEqual(self.manager.view('douyin')['status'], 'error')

    def test_unrelated_cookie_cannot_prove_platform_login(self):
        self.save(domain='.example.invalid')
        self.assertFalse(self.manager.view('douyin')['has_saved_session'])

    def test_xingtu_uses_its_own_session(self):
        self.save()
        self.assertEqual(self.manager.view('xingtu')['status'], 'missing')
        self.save('xingtu', domain='.xingtu.cn')
        self.assertEqual(self.manager.view('xingtu')['status'], 'saved')

    async def test_check_uses_live_evidence_and_exposes_no_private_errors(self):
        self.save()
        with patch.object(self.manager, '_check', new=AsyncMock(return_value='authenticated')):
            self.manager.start_check('douyin')
            await self.manager.wait('douyin')
        self.assertEqual(self.manager.view('douyin')['status'], 'authenticated')
        with patch.object(self.manager, '_check', new=AsyncMock(side_effect=RuntimeError('synthetic-secret'))):
            self.manager.start_check('douyin')
            await self.manager.wait('douyin')
        view = self.manager.view('douyin')
        self.assertEqual(view['status'], 'error')
        self.assertNotIn('synthetic-secret', json.dumps(view))

    async def test_cookie_change_invalidates_cached_authentication(self):
        self.save()
        with patch.object(self.manager, '_check', new=AsyncMock(return_value='authenticated')):
            self.manager.start_check('douyin')
            await self.manager.wait('douyin')
        self.save(value='different-and-longer')
        self.assertEqual(self.manager.view('douyin')['status'], 'saved')

    async def test_cookie_expiry_invalidates_cached_authentication_without_file_change(self):
        deadline = time.time() + 10
        self.save(expires=deadline)
        with patch.object(self.manager, '_check', new=AsyncMock(return_value='authenticated')):
            self.manager.start_check('douyin')
            await self.manager.wait('douyin')
        with patch.object(sessions.time, 'time', return_value=deadline + 1):
            view = self.manager.view('douyin')
        self.assertEqual(view['status'], 'expired')
        self.assertFalse(view['has_saved_session'])

    async def test_cancelled_switch_retains_previous_credentials(self):
        self.save()
        original = self.paths['douyin'].read_bytes()
        entered = asyncio.Event()
        async def login(platform, replace):
            self.assertTrue(replace)
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.manager, '_login', side_effect=login):
            self.manager.start_login('douyin', replace=True)
            await entered.wait()
            with self.assertRaises(sessions.SessionBusy):
                self.manager.start_login('xingtu', replace=True)
            await self.manager.cancel('douyin')
        self.assertEqual(self.paths['douyin'].read_bytes(), original)
        self.assertFalse(self.manager.view('douyin')['busy'])
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'cancelled')

    async def test_cancel_before_worker_starts_finishes_cleanly_without_opening_browser(self):
        with patch.object(self.manager, '_login', new=AsyncMock()) as login:
            self.manager.start_login('douyin', replace=True)
            await self.manager.cancel('douyin')
        login.assert_not_awaited()
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'cancelled')
        self.assertFalse(self.manager.busy())

    async def test_login_timeout_retains_previous_credentials(self):
        self.save()
        original = self.paths['douyin'].read_bytes()
        async def login(*args):
            await asyncio.Event().wait()
        with patch.object(self.manager, '_login', side_effect=login):
            self.manager.start_login('douyin', replace=True)
            await self.manager.wait('douyin')
        self.assertEqual(self.paths['douyin'].read_bytes(), original)
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'timeout')

    async def test_successful_login_commits_only_verified_state(self):
        self.save()
        replacement = {'status': 'authenticated', 'state': state(value='verified-new-session')}
        with patch.object(self.manager, '_login', new=AsyncMock(return_value=replacement)):
            self.manager.start_login('douyin', replace=True)
            await self.manager.wait('douyin')
        self.assertEqual(json.loads(self.paths['douyin'].read_text())['cookies'][0]['value'], 'verified-new-session')
        self.assertEqual(self.manager.view('douyin')['status'], 'authenticated')

    async def test_bare_success_or_unverified_state_never_replaces_credentials(self):
        self.save()
        old = self.paths['douyin'].read_bytes()
        for result in ('authenticated', {'status': 'unknown', 'state': state(value='unverified')}):
            with patch.object(self.manager, '_login', new=AsyncMock(return_value=result)):
                self.manager.start_login('douyin', replace=True)
                await self.manager.wait('douyin')
            self.assertEqual(self.paths['douyin'].read_bytes(), old)
            self.assertEqual(self.manager.view('douyin')['login']['status'], 'failed')

    async def test_login_failure_exposes_safe_diagnostic_code_not_private_exception(self):
        self.save()
        with patch.object(self.manager, '_login', new=AsyncMock(side_effect=RuntimeError(
                'Execution context was destroyed, most likely because of a navigation; synthetic-secret'))):
            self.manager.start_login('douyin', replace=True)
            await self.manager.wait('douyin')
        view = self.manager.view('douyin')
        self.assertEqual(view['login']['error_code'], 'page_transition')
        self.assertNotIn('synthetic-secret', json.dumps(view))

    async def test_session_file_changed_during_check_is_not_certified_by_old_result(self):
        self.save()
        async def changed(platform):
            self.save(value='external-new-session')
            return 'authenticated'
        with patch.object(self.manager, '_check', side_effect=changed):
            self.manager.start_check('douyin')
            await self.manager.wait('douyin')
        self.assertEqual(self.manager.view('douyin')['status'], 'saved')

    def test_atomic_save_restricts_permissions_and_keeps_original_if_validation_fails(self):
        self.save()
        original = self.paths['douyin'].read_bytes()
        with self.assertRaises(ValueError):
            self.manager.save_state('douyin', {'cookies': [], 'origins': []})
        self.assertEqual(self.paths['douyin'].read_bytes(), original)
        self.manager.save_state('douyin', state(value='new-login-secret'))
        self.assertEqual(os.stat(self.paths['douyin']).st_mode & 0o777, 0o600)


class PageEvidenceTests(unittest.TestCase):
    def test_url_and_cookies_alone_cannot_prove_login(self):
        self.assertEqual(sessions.classify_page('douyin', 'https://creator.douyin.com/creator-micro/content/upload', ''), 'unknown')
        self.assertEqual(sessions.classify_page('xingtu', 'https://www.xingtu.cn/ad/creator/market', '达人广场'), 'unknown')

    def test_login_challenge_and_positive_ui_evidence_are_distinct(self):
        self.assertEqual(sessions.classify_page('douyin', 'https://creator.douyin.com/login', '扫码登录'), 'expired')
        self.assertEqual(sessions.classify_page('douyin', 'https://www.douyin.com/verify', '安全验证'), 'verification_required')
        self.assertEqual(sessions.classify_page('douyin', 'https://creator.douyin.com/creator-micro/home', '发布作品 作品管理 数据中心'), 'authenticated')
        self.assertEqual(sessions.classify_page('xingtu', 'https://www.xingtu.cn/ad/creator/market', '达人广场', search_visible=True), 'authenticated')
        self.assertEqual(sessions.classify_page('xingtu', 'https://www.xingtu.cn.evil.invalid/ad/creator/market', '达人广场', search_visible=True), 'unknown')


class LoginWindowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.manager = sessions.PlatformSessions(state_paths={
            key: Path(temp) / (key + '.json') for key in ('douyin', 'xingtu')})
        self.manager.poll_interval = 0.001
        self.page = SimpleNamespace(goto=AsyncMock(), is_closed=Mock(return_value=False))
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page),
                                       storage_state=AsyncMock(return_value=state()))
        self.browser = SimpleNamespace(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=self.browser)))
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=runtime)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        self.enterContext(patch('playwright.async_api.async_playwright', factory))

    async def test_unauthenticated_app_shell_does_not_close_login_window(self):
        observed = asyncio.Event()
        async def empty_state():
            observed.set()
            return {'cookies': [], 'origins': []}
        self.context.storage_state.side_effect = empty_state
        with patch.object(self.manager, '_observe', new=AsyncMock(return_value='authenticated')):
            task = asyncio.create_task(self.manager._browse('douyin', login=True, replace=True))
            try:
                await observed.wait()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'A public page shell must not terminate a fresh login')
                self.browser.close.assert_not_awaited()
                self.assertEqual(self.manager.login_states['douyin']['status'], 'waiting')
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_verified_login_requires_stable_observations_and_valid_session(self):
        with patch.object(self.manager, '_observe', new=AsyncMock(return_value='authenticated')) as observe:
            result = await asyncio.wait_for(self.manager._browse('douyin', login=True, replace=True), 0.1)
        self.assertEqual(result['status'], 'authenticated')
        self.assertGreaterEqual(observe.await_count, 2)
        self.assertEqual(self.page.goto.await_args.kwargs['wait_until'], 'commit')
        self.assertNotIn('storage_state', self.browser.new_context.await_args.kwargs)
        self.browser.close.assert_awaited_once()

    async def test_login_job_id_survives_waiting_window_and_success(self):
        self.addAsyncCleanup(self.manager.close)
        with patch.object(self.manager, '_observe', new=AsyncMock(return_value='authenticated')):
            view = self.manager.start_login('douyin', replace=True)
            job_id = view['login'].get('job_id')
            self.assertIsInstance(job_id, str)

            async def candidate_state():
                self.assertEqual(self.manager.view('douyin')['login']['job_id'], job_id)
                self.assertEqual(self.manager.view('douyin')['login']['status'], 'waiting')
                return state()

            self.context.storage_state.side_effect = candidate_state
            await self.manager.wait('douyin')
        terminal = self.manager.view('douyin')['login']
        self.assertEqual(terminal['status'], 'succeeded')
        self.assertEqual(terminal['job_id'], job_id)

    async def test_navigation_during_dom_observation_does_not_close_window(self):
        from playwright.async_api import Error
        with patch.object(self.manager, '_observe', new=AsyncMock(side_effect=[
                Error('Execution context was destroyed, most likely because of a navigation'),
                'authenticated', 'authenticated'])):
            result = await asyncio.wait_for(self.manager._browse('douyin', login=True, replace=True), 0.1)
        self.assertEqual(result['status'], 'authenticated')

    async def test_aborted_initial_redirect_keeps_login_open(self):
        from playwright.async_api import Error
        self.page.goto.side_effect = Error('net::ERR_ABORTED during redirect')
        with patch.object(self.manager, '_observe', new=AsyncMock(return_value='authenticated')):
            result = await asyncio.wait_for(self.manager._browse('douyin', login=True, replace=True), 0.1)
        self.assertEqual(result['status'], 'authenticated')
