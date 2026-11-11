import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from app import crawler, db


VERIFY = {'status_code': 0, 'user_list': [], 'has_more': 0,
          'search_nil_info': {'search_nil_type': 'verify_check', 'search_nil_item': 'verify_check'}}


class SearchAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_zero_verification_is_not_empty_success(self):
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=VERIFY)) as search:
            with self.assertRaisesRegex(RuntimeError, '验证'):
                await crawler.search_page('食品测评')
        search.assert_awaited_once_with('食品测评', 0, 20, search_type='user')

    async def test_next_page_also_detects_verification(self):
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=VERIFY)) as search:
            with self.assertRaisesRegex(RuntimeError, '验证'):
                await crawler.search_page('食品测评', offset=10)
        search.assert_awaited_once_with('食品测评', 10, 20, search_type='user')

    async def test_genuine_empty_search_still_returns_empty(self):
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value={
                'status_code': 0, 'user_list': [], 'has_more': 0})):
            result = await crawler.search_page('食品测评')
        self.assertEqual(result['user_list'], [])

    async def test_visible_results_with_unknown_pagination_are_partial_not_exhaustive(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        db.init_db()
        payload = {'status_code': 0, 'user_list': [{'user_info': {'sec_uid': 'synthetic'}}],
                   'pagination_status': 'unknown'}

        async def collect(uid, creator_id, task_id, settings, keyword, progress,
                          requirements, policy, budget):
            progress['saved'] += 1
            return {'overall_status': 'pending'}

        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=payload)) as search, patch.object(
                crawler, '_collect_candidate', side_effect=collect):
            tid = await crawler.execute_bd_search_task('食品', 5, search_queries=['食品测评'])
        task = db.get_task(tid)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['progress']['pause_reason'], 'browser_pagination_unknown')
        self.assertEqual(task['progress']['saved'], 1)
        self.assertEqual(task['progress']['pending'], 1)
        self.assertEqual(task['progress']['failed'], 0)
        self.assertEqual(task['progress']['missing_posts'], 0)
        self.assertIn('不能判定搜索已穷尽', task['message'])
        search.assert_awaited_once_with('食品测评', 0, 20, search_type='user')

    async def test_user_and_video_search_use_shared_browser_not_cli_or_subprocess(self):
        for search_type in ('user', 'video'):
            with self.subTest(search_type=search_type), patch.object(
                    crawler.douyin_browser, 'search', new=AsyncMock(return_value={
                        'status_code': 0, 'user_list': [], 'data': [], 'has_more': 0})) as search, patch.object(
                    crawler, '_cli', new=AsyncMock(side_effect=AssertionError('Search must not use CLI'))), patch.object(
                    crawler.subprocess, 'run', side_effect=AssertionError('Search must not spawn subprocess')):
                result = await crawler.search_page('食品测评', offset=40, count=12, search_type=search_type)
                self.assertEqual(result['status_code'], 0)
                search.assert_awaited_once_with('食品测评', 40, 12, search_type=search_type)

    async def test_task_pauses_on_verification_without_trying_more_queries(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        db.init_db()
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=VERIFY)) as search:
            task_id = await crawler.execute_bd_search_task('食品', 5, search_queries=['食品测评'])
        task = db.get_task(task_id)
        self.assertEqual(task['status'], 'blocked')
        self.assertIsNotNone(task['finished_at'])
        self.assertEqual(task['progress']['pause_reason'], 'platform_verification')
        self.assertEqual(task['progress']['query'], '食品测评')
        self.assertEqual(task['progress']['search_type'], 'user')
        self.assertEqual(task['progress']['offset'], 0)
        self.assertEqual(task['progress'].get('search_verification'), {
            'pause_reason': 'platform_verification', 'query': '食品测评',
            'search_type': 'user', 'offset': 0})
        self.assertIn('验证', task['message'])
        search.assert_awaited_once_with('食品测评', 0, 20, search_type='user')

    async def test_blocked_later_video_page_keeps_exact_search_context(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        db.init_db()
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(side_effect=[
                {'status_code': 0, 'data': [], 'has_more': 1, 'cursor': 30},
                VERIFY])) as search:
            tid = await crawler.execute_bd_search_task('食品', search_queries=['食品测评'], discovery_mode='video')
        self.assertEqual(db.get_task(tid)['progress']['query'], '食品测评')
        self.assertEqual(db.get_task(tid)['progress']['search_type'], 'video')
        self.assertEqual(db.get_task(tid)['progress']['offset'], 30)
        self.assertEqual(search.await_args_list, [
            call('食品测评', 0, 20, search_type='video'),
            call('食品测评', 30, 20, search_type='video')])

    async def test_genuine_empty_first_query_does_not_prevent_second_query(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        db.init_db()
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value={
                'status_code': 0, 'user_list': [], 'has_more': 0})) as search:
            with self.assertRaises(RuntimeError):
                await crawler.execute_bd_search_task('食品', 5)
        self.assertEqual(search.await_count, 3)

    async def test_legacy_verification_partial_status_is_migrated_without_data_loss(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(temp) / 'kb.db')))
        db.init_db()
        tid = db.save_task('legacy', '食品', status='partial')
        db.update_task_status(tid, 'partial', '平台验证', {'pause_reason': 'platform_verification', 'candidates': 0})
        finished = db.get_task(tid)['finished_at']
        other = db.save_task('normal partial', '食品', status='partial')
        db.init_db()
        self.assertEqual(db.get_task(tid)['status'], 'blocked')
        self.assertEqual(db.get_task(tid)['finished_at'], finished)
        self.assertEqual(db.get_task(other)['status'], 'partial')
