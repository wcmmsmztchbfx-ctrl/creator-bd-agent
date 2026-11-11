import importlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import crawler, db, workflow

try:
    pipeline = importlib.import_module('app.xingtu_pipeline')
except ModuleNotFoundError:
    pipeline = None


class FakeDiscovery:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def search_page(self, keyword, page=1):
        self.calls.append((keyword, page))
        return {'authors': self.rows, 'has_more': False, 'skipped_count': 0}


class XingtuPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(pipeline, 'Xingtu primary pipeline is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(self.tmp.name) / 'kb.db')))
        workflow.init_workflow_db()
        self.row = {'star_id': 'synthetic-star', 'uid': '123456', 'source_platform': 'xingtu',
            'nickname': 'Synthetic creator', 'followers': 25000, 'median_play_30d': 999999,
            'posts': [{'aweme_id': str(i), 'desc': 'Synthetic food review',
                'create_time': int(time.time()) - 86400 * (i+1),
                'statistics': {'digg_count': 1000 + i, 'comment_count': 5}} for i in range(6)]}

    async def test_xingtu_search_uses_actual_recent_likes_without_douyin_calls(self):
        source = FakeDiscovery([self.row])
        with patch.object(pipeline, 'XingtuDiscovery', return_value=source), patch.object(
            crawler, 'fetch_profile', new=AsyncMock(side_effect=AssertionError('No Douyin profile'))), patch.object(
            crawler, 'fetch_user_posts', new=AsyncMock(side_effect=AssertionError('No Douyin posts'))), patch.object(
            crawler, 'search_page', new=AsyncMock(side_effect=AssertionError('No Douyin search'))):
            task_id = await crawler.execute_bd_search_task('食品', 5, discovery_mode='xingtu',
                search_queries=['食品测评'], min_followers=10000)
        task = db.get_task(task_id)
        self.assertEqual(task['requirements']['discovery_mode'], 'xingtu')
        self.assertEqual(task['status'], 'completed')
        row = db.get_task_results(task_id)[0]
        self.assertEqual(row['source_platform'], 'xingtu')
        self.assertEqual(row['avg_likes'], 1002.5)
        self.assertEqual(row['max_likes'], 1005)
        self.assertNotIn('douyin.com/user/xingtu', row.get('profile_url') or '')
        self.assertEqual(db.get_discovery_sources(task_id, row['creator_id'])[0]['search_type'], 'xingtu')
        self.assertEqual(workflow.get_report(task_id), [])

    async def test_permission_error_is_platform_specific_and_does_not_fallback(self):
        from app.xingtu_discovery import XingtuDiscoveryError
        source = FakeDiscovery([])
        source.search_page = AsyncMock(side_effect=XingtuDiscoveryError('permission_denied', '星图客户权益不足'))
        with patch.object(pipeline, 'XingtuDiscovery', return_value=source), patch.object(
            crawler, 'search_page', new=AsyncMock(side_effect=AssertionError('No Douyin fallback'))):
            task_id = await crawler.execute_bd_search_task('食品', discovery_mode='xingtu')
        task = db.get_task(task_id)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['progress']['platform'], 'xingtu')
        self.assertIn('星图', task['message'])
        self.assertNotIn('抖音搜索', task['message'])

    async def test_xingtu_refresh_keeps_native_identity_and_never_calls_douyin(self):
        source = FakeDiscovery([self.row])
        source.lookup_creator = AsyncMock(return_value=self.row)
        with patch.object(pipeline, 'XingtuDiscovery', return_value=source):
            tid = await crawler.execute_bd_search_task('食品', discovery_mode='xingtu', search_queries=['食品测评'])
        cid = db.get_task_results(tid)[0]['creator_id']
        with patch('app.xingtu_discovery.XingtuDiscovery', return_value=source), patch.object(
            crawler, 'fetch_profile', new=AsyncMock(side_effect=AssertionError('No Douyin profile'))), patch.object(
            crawler, 'fetch_user_posts', new=AsyncMock(side_effect=AssertionError('No Douyin posts'))):
            await crawler.refresh_candidate(tid, cid)
        result = db.get_task_results(tid)[0]
        self.assertEqual(result['creator_id'], cid)
        self.assertEqual(result['star_id'], self.row['star_id'])
        self.assertEqual(result['source_platform'], 'xingtu')
        self.assertEqual(result['avg_likes'], 1002.5)

    async def test_native_star_identity_does_not_compare_internal_key_to_douyin_ids(self):
        from app.xingtu import _identity
        author = {'star_id': 'synthetic-star', 'attribute_datas': {'id': 'synthetic-star', 'core_user_id': '123456'}}
        creator = {'source_platform': 'xingtu', 'star_id': 'synthetic-star', 'sec_uid': 'xingtu:synthetic-star', 'uid': '123456'}
        self.assertEqual(_identity(author, creator)[0], 'synthetic-star')
        self.assertIsNone(_identity(author, {**creator, 'uid': 'another-user'}))
        self.assertIsNone(_identity(author, {**creator, 'star_id': 'another-star'}))

    async def test_retained_brand_biography_still_rejects_merged_xingtu_account(self):
        old = db.upsert_creator({'sec_uid': 'original-douyin-id', 'uid': '123456',
            'nickname': '某品牌', 'signature': '本账号为食品品牌官方账号', 'followers': 25000})
        with patch.object(pipeline, 'XingtuDiscovery', return_value=FakeDiscovery([self.row])):
            tid = await crawler.execute_bd_search_task('食品', discovery_mode='xingtu', search_queries=['食品测评'])
        row = db.get_task_results(tid)[0]
        self.assertEqual(row['creator_id'], old)
        self.assertEqual(db.get_current_candidate_assessment(tid, old)['assessment']['overall_status'], 'rejected')

    async def test_stopped_refresh_never_opens_platform(self):
        source = FakeDiscovery([self.row])
        with patch.object(pipeline, 'XingtuDiscovery', return_value=source):
            tid = await crawler.execute_bd_search_task('食品', discovery_mode='xingtu', search_queries=['食品测评'])
        cid = db.get_task_results(tid)[0]['creator_id']
        workflow.request_stop(tid)
        # request_stop only mutates active tasks; model a persisted manual stop.
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET stop_requested=1 WHERE id=?', (tid,))
        with patch('app.xingtu_discovery.XingtuDiscovery', side_effect=AssertionError('No network after stop')):
            await crawler.refresh_candidate(tid, cid)


if __name__ == '__main__':
    unittest.main()
