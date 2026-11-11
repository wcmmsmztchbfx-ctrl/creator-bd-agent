import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import crawler, db


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(db, "get_setting", side_effect=lambda key, default: default))

    def evaluate(self, **kwargs):
        params = dict(keyword='食品', nickname='合成测试账号', followers=10000,
                      avg_likes=9000, max_likes=20000, days_since_latest=2,
                      sample_posts=[{'aweme_id': '1'}], contacts=[], star_atlas=0)
        params.update(kwargs)
        return crawler.evaluate_recommendation(**params)

    def test_missing_posts_are_unknown_not_dormant(self):
        result = self.evaluate(days_since_latest=None, sample_posts=[])
        self.assertEqual(result['level'], '待核实')
        self.assertIsNone(result['hard_pass'])

    def test_rules_do_not_claim_content_match_or_ai(self):
        result = self.evaluate()
        self.assertNotIn('高度吻合', result['reason'])
        self.assertIn('规则', result['reason'])

    def test_user_minimum_is_enforced(self):
        result = self.evaluate(min_avg_likes=10000)
        self.assertFalse(result['hard_pass'])

    def test_user_active_window_is_enforced(self):
        result = self.evaluate(days_since_latest=8, active_days=7)
        self.assertFalse(result['hard_pass'])

    def test_no_user_active_limit_does_not_invent_hard_gate(self):
        result = self.evaluate(days_since_latest=90, active_days=None)
        self.assertTrue(result['hard_pass'])


class CrawlerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, 'DB_PATH', str(Path(self.temp.name) / 'test.db'))
        self.path_patch.start()
        db.init_db()

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    async def test_search_failure_marks_task_failed(self):
        failed = subprocess.CompletedProcess([], 1, json.dumps({'ok': False, 'error': {'code': 'not_authenticated'}}), '')
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(side_effect=RuntimeError('搜索会话不可用'))):
            with self.assertRaises(RuntimeError):
                await crawler.execute_bd_search_task('食品', 2)
        task = db.get_all_tasks()[0]
        self.assertEqual(task['status'], 'failed')
        self.assertNotIn('test-secret', task.get('message', ''))

    async def test_empty_search_is_not_success(self):
        empty = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'data': {'data': []}}), '')
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value={'status_code': 0, 'data': [], 'has_more': 0})):
            with self.assertRaises(RuntimeError):
                await crawler.execute_bd_search_task('食品', 2)
        self.assertEqual(db.get_all_tasks()[0]['status'], 'failed')

    async def test_constraints_and_partial_state_are_saved(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'data': {
            'user_list': [{'user_info': {'sec_uid': 'a', 'nickname': 'a', 'follower_count': 1000}},
                          {'user_info': {'sec_uid': 'b', 'nickname': 'b', 'follower_count': 1000}}],
            'has_more': 0}}), '')
        profile = {'nickname': '合成测试账号', 'unique_id': 'test-account', 'follower_count': 1000}
        now = int(datetime.datetime.now().timestamp())
        posts = [{'aweme_id': 'old-pinned', 'create_time': now - 86400 * 100, 'statistics': {'digg_count': 200}},
                 {'aweme_id': 'latest', 'create_time': now - 86400 * 2, 'statistics': {'digg_count': 400}}]
        calls = []
        async def fake_profile(uid):
            calls.append(uid)
            if uid == 'b': raise RuntimeError('synthetic profile error')
            return profile
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=json.loads(result.stdout)['data'])), patch.object(
            crawler, 'fetch_profile', side_effect=fake_profile
        ), patch.object(crawler, 'fetch_user_posts', new=AsyncMock(return_value=posts)):
            task_id = await crawler.execute_bd_search_task('食品', 2, min_avg_likes=500, active_days=7)
        task = db.get_task(task_id)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['requirements']['min_avg_likes'], 500)
        rows = db.get_task_results(task_id)
        self.assertEqual(len(rows), 2)  # Failed profiles remain as discovered candidates.
        successful = next(row for row in rows if row['sec_uid'] == 'a')
        self.assertEqual(successful['days_since_latest'], 2)
        self.assertFalse(successful['hard_pass'])

    async def test_missing_likes_cannot_shrink_denominator_to_pass(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'data': {
            'user_list': [{'user_info': {'sec_uid': 'a', 'nickname': 'a', 'follower_count': 1000}}],
            'has_more': 0}}), '')
        now = int(datetime.datetime.now().timestamp())
        posts = [{'aweme_id': 'known', 'create_time': now, 'statistics': {'digg_count': 10000}},
                 {'aweme_id': 'missing', 'create_time': now - 1000, 'statistics': {}}]
        with patch.object(crawler.douyin_browser, 'search', new=AsyncMock(return_value=json.loads(result.stdout)['data'])), patch.object(
            crawler, 'fetch_profile', new=AsyncMock(return_value={'nickname': '合成测试'})
        ), patch.object(crawler, 'fetch_user_posts', new=AsyncMock(return_value=posts)):
            task_id = await crawler.execute_bd_search_task('食品', 1, min_avg_likes=9000)
        self.assertEqual(db.get_task(task_id)['status'], 'partial')
        row = db.get_task_results(task_id)[0]
        self.assertEqual(row['recommend_level'], '待核实')
        self.assertIsNone(row['hard_pass'])
        self.assertIsNone(row['avg_likes'])


if __name__ == '__main__':
    unittest.main()
