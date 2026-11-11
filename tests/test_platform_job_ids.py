import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

from app import platform_sessions as sessions


class PlatformJobIDTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = {p: Path(temp) / (p + '.json') for p in ('douyin', 'xingtu')}
        self.state = {'cookies': [{'name': 'sessionid', 'value': 'synthetic-session',
                                  'domain': '.douyin.com', 'path': '/', 'expires': -1}], 'origins': []}
        self.paths['douyin'].write_text(json.dumps(self.state))
        self.manager = sessions.PlatformSessions(state_paths=self.paths, login_timeout=0.03)
        self.addAsyncCleanup(self.manager.close)
        self.enterContext(patch.object(self.manager, '_browse', new=AsyncMock(
            side_effect=AssertionError('Browser access is forbidden in job ID tests'))))
        self.enterContext(patch.object(self.manager, '_verify_search', new=AsyncMock(
            side_effect=AssertionError('Browser access is forbidden in job ID tests'))))
        self.target = {'task_id': 7, 'query': 'food', 'search_type': 'user', 'offset': 0}

    async def test_stale_verifier_id_cannot_cancel_other_task_or_same_task_new_generation(self):
        for next_task_id in (9, 7):
            with self.subTest(next_task_id=next_task_id):
                with patch.object(self.manager, '_verify_search', new=AsyncMock(return_value=None)):
                    first = self.manager.start_verification(self.target)
                    old_id = first['login'].get('job_id')
                    self.assertIsInstance(old_id, str)
                    await self.manager.wait('douyin')
                self.assertEqual(self.manager.view('douyin')['login']['job_id'], old_id)
                entered = asyncio.Event()

                async def waiting(*args):
                    entered.set()
                    await asyncio.Event().wait()

                with patch.object(self.manager, '_verify_search', side_effect=waiting):
                    second = self.manager.start_verification(dict(self.target, task_id=next_task_id))
                    new_id = second['login']['job_id']
                    self.assertNotEqual(old_id, new_id)
                    await entered.wait()
                    active = self.manager.jobs['douyin']
                    with self.assertRaises(sessions.SessionBusy):
                        await self.manager.cancel('douyin', expected_job_id=old_id)
                    self.assertIs(self.manager.jobs['douyin'], active)
                    self.assertFalse(active.done())
                    self.assertFalse(active.cancelling())
                    self.assertEqual(self.manager.view('douyin')['login']['job_id'], new_id)
                    await self.manager.cancel('douyin', expected_job_id=new_id)
                terminal = self.manager.view('douyin')['login']
                self.assertEqual(terminal['status'], 'cancelled')
                self.assertEqual(terminal['job_id'], new_id)

    async def test_all_start_types_issue_distinct_uuid4_ids_and_keep_terminal_identity(self):
        seen = set()
        for operation, method in (('check', '_check'), ('login', '_login'),
                                  ('verification', '_verify_search')):
            for outcome in ('success', 'failure', 'timeout', 'cancel_before_start'):
                with self.subTest(operation=operation, outcome=outcome):
                    async def worker(*args):
                        if outcome == 'failure':
                            raise RuntimeError('synthetic failure')
                        if outcome == 'timeout':
                            raise asyncio.TimeoutError()
                        return ('authenticated' if operation == 'check' else
                                {'status': 'authenticated', 'state': self.state})

                    with patch.object(self.manager, method, side_effect=worker):
                        if operation == 'verification':
                            view = self.manager.start_verification(self.target)
                        else:
                            view = getattr(self.manager, 'start_' + operation)('douyin')
                        job_id = view['login'].get('job_id')
                        self.assertIsInstance(job_id, str)
                        self.assertRegex(job_id, r'^[0-9a-f]{32}$')
                        self.assertEqual(UUID(hex=job_id).version, 4)
                        self.assertNotIn(job_id, seen)
                        seen.add(job_id)
                        if outcome == 'cancel_before_start':
                            await self.manager.cancel('douyin', expected_job_id=job_id)
                        else:
                            await self.manager.wait('douyin')
                    self.assertFalse(self.manager.busy())
                    self.assertEqual(self.manager.view('douyin')['login']['job_id'], job_id)

    async def test_mismatched_id_does_not_cancel_login_or_check(self):
        for operation, method in (('login', '_login'), ('check', '_check')):
            entered = asyncio.Event()

            async def waiting(*args):
                entered.set()
                await asyncio.Event().wait()

            with self.subTest(operation=operation), patch.object(self.manager, method, side_effect=waiting):
                view = getattr(self.manager, 'start_' + operation)('douyin')
                job_id = view['login'].get('job_id')
                self.assertIsInstance(job_id, str)
                await entered.wait()
                with self.assertRaises(sessions.SessionBusy):
                    await self.manager.cancel('douyin', expected_job_id='stale')
                self.assertTrue(self.manager.busy())
                await self.manager.cancel('douyin', expected_job_id=job_id)
                self.assertFalse(self.manager.busy())

    async def test_check_without_saved_session_still_gets_fresh_identity(self):
        first = self.manager.start_check('xingtu')['login'].get('job_id')
        second = self.manager.start_check('xingtu')['login'].get('job_id')
        self.assertIsInstance(first, str)
        self.assertIsInstance(second, str)
        self.assertNotEqual(first, second)
        self.assertEqual(self.manager.view('xingtu')['login']['job_id'], second)

    async def test_legacy_cancel_still_works_without_an_expected_id(self):
        with patch.object(self.manager, '_login', new=AsyncMock()):
            started = self.manager.start_login('douyin')
            view = await self.manager.cancel('douyin')
        self.assertFalse(view['busy'])
        self.assertEqual(view['login']['status'], 'cancelled')
        self.assertEqual(view['login'].get('job_id'), started['login'].get('job_id'))

    async def test_cancel_does_not_rewrite_a_replacement_started_while_it_was_awaiting(self):
        async def waiting(*args):
            await asyncio.Event().wait()

        with patch.object(self.manager, '_login', side_effect=waiting):
            first = self.manager.start_login('douyin')
            old_id = first['login']['job_id']
            original = self.manager.jobs['douyin']
            replacement = {}
            original.add_done_callback(lambda task: replacement.update(self.manager.start_login('douyin')))
            view = await self.manager.cancel('douyin', expected_job_id=old_id)
            new_id = replacement['login']['job_id']
            self.assertNotEqual(new_id, old_id)
            self.assertTrue(view['busy'])
            self.assertEqual(view['login']['job_id'], new_id)
            self.assertNotEqual(view['login']['status'], 'cancelled')
            self.assertFalse(self.manager.jobs['douyin'].cancelling())
            await self.manager.cancel('douyin', expected_job_id=new_id)
