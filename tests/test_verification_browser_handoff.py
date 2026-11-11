import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import platform_sessions as sessions


def saved_state(value='original-secret', **cookie_fields):
    cookie = {'name': 'sessionid', 'value': value, 'domain': '.douyin.com',
              'path': '/', 'expires': -1}
    cookie.update(cookie_fields)
    return {'cookies': [cookie], 'origins': []}


class VerificationBrowserHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.manager = sessions.PlatformSessions(state_paths={
            key: Path(temp) / (key + '.json') for key in ('douyin', 'xingtu')}, login_timeout=1)
        self.path = self.manager.paths['douyin']
        self.path.write_text(json.dumps(saved_state()))
        self.original = self.path.read_bytes()
        self.context = object()
        self.candidate = saved_state('verified-new-secret')
        self.browser = SimpleNamespace(
            context=self.context,
            verify=AsyncMock(return_value={'status': 'authenticated', 'state': self.candidate}),
            bind_saved_state=Mock(), close=AsyncMock())
        self.enterContext(patch.dict(sys.modules, {
            'app.douyin_browser': SimpleNamespace(browser=self.browser)}))
        self.enterContext(patch('playwright.async_api.async_playwright',
                                side_effect=AssertionError('Manager must use the shared browser')))
        self.addAsyncCleanup(self.manager.close)
        self.target = {'task_id': 12, 'query': 'food & review', 'search_type': 'user', 'offset': 20}

    async def complete(self):
        view = self.manager.start_verification(self.target)
        await self.manager.wait('douyin')
        terminal = self.manager.view('douyin')
        self.assertEqual(terminal['login']['job_id'], view['login']['job_id'])
        self.assertEqual(terminal['login']['task_id'], 12)
        self.assertFalse(terminal['busy'])
        return terminal

    async def start_waiting(self):
        entered = asyncio.Event()

        async def waiting(target):
            self.assertEqual(target, self.target)
            self.assertEqual(self.path.read_bytes(), self.original)
            entered.set()
            await asyncio.Event().wait()

        self.browser.verify.side_effect = waiting
        view = self.manager.start_verification(self.target)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'waiting')
        return view

    async def test_verified_result_commits_then_binds_without_closing_shared_context(self):
        committed = []

        def bind():
            committed.append(json.loads(self.path.read_text()))
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
            self.assertIs(self.browser.context, self.context)
            self.browser.close.assert_not_awaited()

        self.browser.bind_saved_state.side_effect = bind
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'succeeded')
        self.assertEqual(terminal['status'], 'authenticated')
        self.assertEqual(json.loads(self.path.read_text()), self.candidate)
        self.assertEqual(committed, [self.candidate])
        self.browser.verify.assert_awaited_once_with(self.target)
        self.browser.bind_saved_state.assert_called_once_with()
        self.browser.close.assert_not_awaited()
        self.assertIs(self.browser.context, self.context)

    async def test_video_target_is_forwarded_unchanged(self):
        self.target.update(search_type='video', offset=60)
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'succeeded')
        self.browser.verify.assert_awaited_once_with(self.target)

    async def test_unverified_results_never_save_or_bind(self):
        for result in (None, 'authenticated', {},
                       {'status': 'waiting', 'state': self.candidate},
                       {'status': 'failed', 'state': self.candidate}):
            with self.subTest(result=result):
                self.browser.verify.return_value = result
                terminal = await self.complete()
                self.assertEqual(terminal['login']['status'], 'failed')
                self.assertEqual(self.path.read_bytes(), self.original)
                self.browser.bind_saved_state.assert_not_called()

    async def test_authenticated_result_still_requires_valid_platform_cookies(self):
        for state in (None, {}, {'cookies': [], 'origins': []},
                      saved_state(domain='.douyin.com.evil.invalid'),
                      saved_state(expires=time.time() - 10),
                      saved_state(expires=True), saved_state(value='')):
            with self.subTest(state=state):
                self.browser.verify.return_value = {'status': 'authenticated', 'state': state}
                terminal = await self.complete()
                self.assertEqual(terminal['login']['status'], 'failed')
                self.assertEqual(self.path.read_bytes(), self.original)
                self.browser.bind_saved_state.assert_not_called()

    async def test_external_file_change_is_not_overwritten_or_bound(self):
        changed = json.dumps(saved_state('external-longer-secret'))

        async def verify(target):
            self.path.write_text(changed)
            return {'status': 'authenticated', 'state': self.candidate}

        self.browser.verify.side_effect = verify
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'failed')
        self.assertEqual(terminal['status'], 'saved')
        self.assertEqual(self.path.read_text(), changed)
        self.browser.bind_saved_state.assert_not_called()
        self.browser.close.assert_awaited_once_with()

    async def test_same_length_replacement_with_restored_mtime_cannot_commit_verification(self):
        original_stat = self.path.stat()
        changed = json.dumps(saved_state('external-secret')).encode()
        self.assertEqual(len(changed), len(self.original))

        async def verify(target):
            self.path.write_bytes(changed)
            os.utime(self.path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            self.assertEqual(self.path.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(self.path.stat().st_size, original_stat.st_size)
            return {'status': 'authenticated', 'state': self.candidate}

        self.browser.verify.side_effect = verify
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'failed')
        self.assertEqual(terminal['status'], 'saved')
        self.assertEqual(self.path.read_bytes(), changed)
        self.browser.bind_saved_state.assert_not_called()
        self.browser.close.assert_awaited_once_with()

    def test_same_length_replacement_with_restored_mtime_invalidates_cached_authentication(self):
        self.manager._record('douyin', 'authenticated')
        self.assertEqual(self.manager.view('douyin')['status'], 'authenticated')
        original_stat = self.path.stat()
        changed = json.dumps(saved_state('external-secret')).encode()
        self.assertEqual(len(changed), len(self.original))
        self.path.write_bytes(changed)
        os.utime(self.path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(self.path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(self.path.stat().st_size, original_stat.st_size)
        view = self.manager.view('douyin')
        self.assertEqual(view['status'], 'saved')
        self.assertIsNone(view['checked_at'])
        self.assertTrue(view['has_saved_session'])
        self.assertNotIn('external-secret', json.dumps(view))

    def test_oversized_state_has_an_error_fingerprint_without_hashing_content(self):
        self.path.write_bytes(b'x' * 5_000_001)
        with patch('hashlib.sha256', side_effect=AssertionError('Oversized state must not be hashed')):
            fingerprint = self.manager._fingerprint('douyin')
        self.assertEqual(fingerprint, (self.path.stat().st_mtime_ns, 5_000_001, 'oversized'))
        self.assertEqual(self.manager.view('douyin')['status'], 'error')

    async def test_browser_exception_preserves_credentials_without_binding_or_private_error(self):
        self.browser.verify.side_effect = RuntimeError('private-secret')
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'failed')
        self.assertNotIn('private-secret', json.dumps(terminal))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.bind_saved_state.assert_not_called()
        self.browser.close.assert_awaited_once_with()

    async def test_rejected_browser_result_closes_shared_context(self):
        self.browser.verify.return_value = {'status': 'failed', 'state': self.candidate}
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.bind_saved_state.assert_not_called()
        self.browser.close.assert_awaited_once_with()

    async def test_failed_atomic_replace_does_not_bind_or_change_original(self):
        with patch.object(sessions.os, 'replace', side_effect=OSError('private-secret')):
            terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob('.pending-login-*')), [])
        self.browser.bind_saved_state.assert_not_called()

    async def test_cancel_closes_shared_browser_and_preserves_credentials(self):
        started = await self.start_waiting()
        with self.assertRaises(sessions.SessionBusy):
            self.manager.start_login('xingtu')
        terminal = await self.manager.cancel('douyin', expected_job_id=started['login']['job_id'])
        self.assertEqual(terminal['login']['status'], 'cancelled')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.close.assert_awaited_once_with()
        self.browser.bind_saved_state.assert_not_called()

    async def test_direct_verifier_cancellation_propagates_after_browser_close(self):
        entered = asyncio.Event()

        async def waiting(target):
            entered.set()
            await asyncio.Event().wait()

        self.browser.verify.side_effect = waiting
        job = asyncio.create_task(self.manager._verify_search(self.target))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            self.browser.close.assert_awaited_once_with()
            self.assertEqual(self.path.read_bytes(), self.original)
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_timeout_closes_shared_browser_without_saving_or_binding(self):
        self.manager.login_timeout = 0.03
        await self.start_waiting()
        await self.manager.wait('douyin')
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'timeout')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.close.assert_awaited_once_with()
        self.browser.bind_saved_state.assert_not_called()

    async def test_cancel_before_start_preserves_identity_without_opening_browser(self):
        started = self.manager.start_verification(self.target)
        terminal = await self.manager.cancel('douyin')
        self.assertEqual(terminal['login']['status'], 'cancelled')
        self.assertEqual(terminal['login']['job_id'], started['login']['job_id'])
        self.assertEqual(terminal['login']['task_id'], 12)
        self.assertEqual(terminal['login']['kind'], 'search_verification')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.verify.assert_not_awaited()
        self.browser.bind_saved_state.assert_not_called()

    async def test_shutdown_closes_retained_browser_once_after_success(self):
        terminal = await self.complete()
        self.assertEqual(terminal['login']['status'], 'succeeded')
        self.browser.close.assert_not_awaited()
        await self.manager.close()
        self.browser.close.assert_awaited_once_with()

    async def test_shutdown_cancels_pending_verification_and_closes_browser_once(self):
        await self.start_waiting()
        await self.manager.close()
        self.assertFalse(self.manager.busy())
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'cancelled')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.browser.bind_saved_state.assert_not_called()
        self.browser.close.assert_awaited_once_with()

    async def test_shutdown_without_jobs_closes_shared_browser_once(self):
        await self.manager.close()
        self.browser.close.assert_awaited_once_with()

    async def test_successful_login_does_not_bind_new_state_to_old_shared_context(self):
        with patch.object(self.manager, '_login', new=AsyncMock(return_value={
                'status': 'authenticated', 'state': self.candidate})):
            self.manager.start_login('douyin', replace=True)
            await self.manager.wait('douyin')
        self.assertEqual(self.manager.view('douyin')['login']['status'], 'succeeded')
        self.assertEqual(json.loads(self.path.read_text()), self.candidate)
        self.browser.bind_saved_state.assert_not_called()
