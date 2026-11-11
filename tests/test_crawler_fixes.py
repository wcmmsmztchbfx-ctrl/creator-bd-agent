import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import crawler, db, qualification


class CrawlerFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(self.temp.name) / 'test.db')))
        db.init_db()
        self.requirements = {'keyword': 'food', 'min_followers': 10000,
                             'min_avg_likes': 100, 'topic_min_samples': 5}
        self.tid = db.save_task('fixture', 'food', requirements=self.requirements)
        self.cid = db.upsert_creator({'sec_uid': 'fixture', 'nickname': 'Fixture',
                                      'followers': 20000, 'signature': 'Food reviews'})
        db.save_recommendation(self.cid, self.tid, {'hard_pass': None})
        self.now = int(datetime.datetime.now().timestamp())
        self.posts = [{'aweme_id': f'p{index}', 'desc': 'Food review',
                       'create_time': self.now - index * 86400,
                       'statistics': {'digg_count': 500}} for index in range(6)]
        self.enterContext(patch.object(crawler, 'fetch_profile', new=AsyncMock(
            return_value={'nickname': 'Fixture', 'follower_count': 20000,
                          'signature': 'Food reviews'})))
        self.enterContext(patch.object(crawler, 'fetch_user_posts', new=AsyncMock(return_value=self.posts)))
        self.review = self.enterContext(patch.object(qualification, 'review_creator_topic',
                                                     new=AsyncMock(side_effect=lambda *args: self.topic())))

    def topic(self, status='matched'):
        return {'topic_status': status, 'creator_pattern': 'consistent_creator',
                'posts': [{'aweme_id': post['aweme_id'], 'decision': 'yes',
                           'evidence_id': post['aweme_id']} for post in self.posts],
                'account': {'account_type': 'independent_creator',
                            'evidence_ids': [post['aweme_id'] for post in self.posts[:2]],
                            'reason': 'Synthetic personal creator across posts'},
                'sample_count': 6, 'matched_count': 6 if status == 'matched' else 0,
                'unknown_count': 0, 'ratio': 1.0 if status == 'matched' else 0.0,
                'min_samples': 5, 'min_ratio': 0.6, 'error_code': None,
                'method': 'text_llm', 'model': 'fixture', 'prompt_version': 'fixture'}

    def observation(self, legacy=False):
        samples = [{'aweme_id': post['aweme_id'], 'desc': post['desc'], 'likes': 500,
                    'date': datetime.date.today().isoformat()} for post in self.posts]
        if legacy:
            db.save_sample_posts(self.cid, self.tid, samples)
        oid = db.save_snapshot(self.cid, self.tid, {
            'sample_count': 6, 'avg_likes': 500, 'max_likes': 500,
            'latest_post_date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'days_since_latest': 0})
        if not legacy:
            db.save_sample_posts(self.cid, self.tid, samples)
        return oid

    def save_assessment(self, topic):
        candidate = crawler._candidate_creator(self.tid, self.cid)
        oid = candidate['observation_id']
        posts = db.get_observation_posts(self.cid, self.tid, oid)['posts']
        policy = qualification.policy_from_requirements(self.requirements)
        numeric = qualification.evaluate_numeric_conditions(candidate, self.requirements)
        assessment = qualification.build_assessment(candidate, oid, posts, self.requirements,
                                                       numeric, topic, policy)
        assessment['topic_reviewed'] = topic.get('error_code') is None
        db.save_candidate_assessment(self.tid, self.cid, oid, assessment)
        return assessment

    async def test_stop_during_posts_saves_evidence_without_paid_review(self):
        async def posts(*args, **kwargs):
            with db.get_connection() as conn:
                conn.execute('UPDATE tasks SET stop_requested=1 WHERE id=?', (self.tid,))
            return self.posts
        with patch.object(crawler, 'fetch_user_posts', side_effect=posts):
            await crawler.refresh_candidate(self.tid, self.cid)
        self.review.assert_not_awaited()
        assessment = db.get_current_candidate_assessment(self.tid, self.cid)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertEqual(assessment['topic']['error_code'], 'stopped')
        self.assertEqual(assessment['assessment']['post_fetch']['status'], 'ok')
        self.assertEqual(len(db.get_observation_posts(self.cid, self.tid,
                                                     assessment['observation_id'])['posts']), 6)

    async def test_stopped_refresh_never_starts_profile_request(self):
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET stop_requested=1 WHERE id=?', (self.tid,))
        with patch.object(crawler, 'fetch_profile', new=AsyncMock()) as profile:
            await crawler.refresh_candidate(self.tid, self.cid)
        profile.assert_not_awaited()
        self.review.assert_not_awaited()

    async def test_early_numeric_failure_binds_new_null_metric_snapshot(self):
        old_id = self.observation()
        with patch.object(crawler, 'fetch_profile', new=AsyncMock(
                return_value={'nickname': 'Fixture', 'follower_count': 200})), patch.object(
                crawler, 'fetch_user_posts', new=AsyncMock()) as posts:
            await crawler.refresh_candidate(self.tid, self.cid)
        posts.assert_not_awaited()
        self.review.assert_not_awaited()
        candidate = crawler._candidate_creator(self.tid, self.cid)
        self.assertGreater(candidate['observation_id'], old_id)
        self.assertIsNone(candidate['avg_likes'])
        self.assertIsNone(candidate['max_likes'])
        assessment = db.get_current_candidate_assessment(self.tid, self.cid)
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment['observation_id'], candidate['observation_id'])
        self.assertEqual(assessment['overall_status'], 'rejected')

    async def test_retryable_placeholders_are_not_reused(self):
        self.observation()
        policy = qualification.policy_from_requirements(self.requirements)
        for code in ('budget_exhausted', 'model_disabled', 'timeout', 'not_reviewed',
                     'knowledge_base', 'invalid_response'):
            with self.subTest(code=code):
                self.save_assessment(qualification.unknown_topic_assessment(policy, code, 'Retry'))
                self.review.reset_mock()
                assessment = await crawler.review_existing_candidate(self.tid, self.cid)
                self.review.assert_awaited_once()
                self.assertTrue(assessment['topic_reviewed'])
                self.assertEqual(assessment['topic_status'], 'matched')

    async def test_completed_topic_outcomes_are_reused_without_charging(self):
        self.observation()
        for status in ('matched', 'not_matched'):
            with self.subTest(status=status):
                self.save_assessment(self.topic(status))
                budget = {'used': 0, 'limit': 1, 'exhausted': False}
                assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
                self.assertEqual(assessment['topic_status'], status)
                self.assertEqual(budget['used'], 0)
        self.review.assert_not_awaited()

    async def test_legacy_fallback_posts_cannot_prove_current_eligibility(self):
        self.observation(legacy=True)
        assessment = await crawler.review_existing_candidate(self.tid, self.cid)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertTrue(assessment['post_fetch']['legacy_fallback'])
        self.assertFalse(assessment['topic_reviewed'])
        self.review.assert_not_awaited()

    async def test_stopped_stored_review_never_spends_budget(self):
        self.observation()
        with db.get_connection() as conn:
            conn.execute('UPDATE tasks SET stop_requested=1 WHERE id=?', (self.tid,))
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(assessment['topic']['error_code'], 'stopped')
        self.assertEqual(budget['used'], 0)
        self.review.assert_not_awaited()

    async def test_budget_reservation_survives_assessment_save_failure(self):
        self.observation()
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        with patch.object(crawler, 'save_candidate_assessment', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(budget['used'], 1)
        assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(assessment['topic']['error_code'], 'budget_exhausted')
        self.assertTrue(budget['exhausted'])
        self.review.assert_awaited_once()

    async def test_budget_reservation_survives_model_exception(self):
        self.observation()
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        self.review.side_effect = RuntimeError('model failure')
        with self.assertRaises(RuntimeError):
            await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(budget['used'], 1)
        assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(assessment['topic']['error_code'], 'budget_exhausted')
        self.review.assert_awaited_once()

    async def test_failed_recommendation_save_does_not_refund_budget(self):
        self.observation()
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        with patch.object(crawler, 'save_recommendation', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(budget['used'], 1)
        assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(assessment['topic_status'], 'matched')
        self.review.assert_awaited_once()

    async def test_model_error_never_reuses_a_nominal_matched_result(self):
        self.observation()
        self.save_assessment({**self.topic(), 'error_code': 'timeout'})
        assessment = await crawler.review_existing_candidate(self.tid, self.cid)
        self.assertIsNone(assessment['topic']['error_code'])
        self.review.assert_awaited_once()

    async def test_new_observation_with_insufficient_posts_does_not_spend_budget(self):
        self.posts = self.posts[:1]
        self.observation()
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        assessment = await crawler.review_existing_candidate(self.tid, self.cid, budget=budget)
        self.assertEqual(assessment['topic']['error_code'], 'insufficient_samples')
        self.assertEqual(budget['used'], 0)
        self.review.assert_not_awaited()

    async def test_stop_during_normalization_never_starts_review(self):
        normalize = qualification.normalize_review_posts
        def stopping_normalize(*args, **kwargs):
            with db.get_connection() as conn:
                conn.execute('UPDATE tasks SET stop_requested=1 WHERE id=?', (self.tid,))
            return normalize(*args, **kwargs)
        with patch.object(qualification, 'normalize_review_posts', side_effect=stopping_normalize):
            await crawler.refresh_candidate(self.tid, self.cid)
        self.review.assert_not_awaited()


class PostResponseFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_checks_stop_between_post_pages_and_keeps_first_page(self):
        stopped = False
        def response(*args):
            nonlocal stopped
            stopped = True
            return {'pages': [{'status_code': 0, 'aweme_list': [{'aweme_id': 'p1'}],
                               'has_more': 1, 'max_cursor': 100}]}
        with patch.object(crawler, '_run_python', side_effect=response) as run:
            posts = await crawler._fetch_user_posts_api('fixture', stop_check=lambda: stopped)
        self.assertEqual(posts, [{'aweme_id': 'p1'}])
        self.assertEqual(run.call_count, 1)

    async def test_api_uses_only_platform_post_cursors(self):
        with patch.object(crawler, '_run_python', side_effect=[
                {'pages': [{'status_code': 0, 'aweme_list': [{'aweme_id': 'p1'}],
                            'has_more': 1, 'max_cursor': 100}]},
                {'pages': [{'status_code': 0, 'aweme_list': [], 'has_more': 0}]}]) as run:
            posts = await crawler._fetch_user_posts_api('fixture')
        self.assertEqual(posts, [{'aweme_id': 'p1'}])
        self.assertEqual([call.args[1][-1] for call in run.call_args_list], ['0', '100'])

    async def test_stop_after_api_failure_prevents_page_fallback(self):
        stopped = False
        async def api(*args, **kwargs):
            nonlocal stopped
            stopped = True
            raise crawler.PostFetchError('fixture error')
        diagnostics = {'task_id': 99}
        with patch.object(crawler, 'stop_requested', side_effect=lambda tid: stopped), patch.object(
                crawler, '_fetch_user_posts_api', side_effect=api), patch.object(
                crawler, '_fetch_user_posts_playwright', new=AsyncMock()) as page:
            self.assertEqual(await crawler.fetch_user_posts('fixture', diagnostics=diagnostics), [])
        page.assert_not_awaited()
        self.assertEqual(diagnostics['status'], 'stopped')

    async def test_api_error_response_is_not_empty_success(self):
        with patch.object(crawler, '_run_python', return_value={
                'pages': [{'status_code': 500, 'status_msg': 'fixture error', 'aweme_list': []}]}):
            with self.assertRaises(crawler.PostFetchError):
                await crawler._fetch_user_posts_api('fixture')

    async def test_api_missing_post_response_is_unavailable(self):
        with patch.object(crawler, '_run_python', return_value={'pages': [{}]}):
            with self.assertRaises(crawler.PostFetchUnavailable):
                await crawler._fetch_user_posts_api('fixture')

    async def test_api_permission_response_is_unavailable(self):
        with patch.object(crawler, '_run_python', return_value={
                'pages': [{'status_code': 403, 'aweme_list': []}]}):
            with self.assertRaises(crawler.PostFetchUnavailable):
                await crawler._fetch_user_posts_api('fixture')

    async def test_successful_empty_api_response_does_not_trigger_fallback(self):
        diagnostics = {}
        with patch.object(crawler, '_run_python', return_value={
                'pages': [{'status_code': 0, 'aweme_list': []}]}), patch.object(
                crawler, '_fetch_user_posts_playwright', new=AsyncMock()) as page:
            self.assertEqual(await crawler.fetch_user_posts('fixture', diagnostics=diagnostics), [])
        page.assert_not_awaited()
        self.assertEqual(diagnostics['status'], 'empty')

    async def test_page_api_error_is_failed_not_empty(self):
        response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',
                                   status=200, json=AsyncMock(return_value={
                                       'status_code': 500, 'aweme_list': []}))
        diagnostics = {}
        with patch.object(crawler, '_fetch_user_posts_api', new=AsyncMock(
                side_effect=crawler.PostFetchError('fixture API error'))), patch.object(
                crawler.Path, 'is_file', return_value=True), patch.object(
                crawler, 'async_playwright', return_value=self.browser_fixture(response)):
            self.assertEqual(await crawler.fetch_user_posts('fixture', diagnostics=diagnostics), [])
        self.assertEqual(diagnostics['status'], 'failed')

    def browser_fixture(self, response=None):
        page = SimpleNamespace(url='https://www.douyin.com/user/fixture',
                               wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        handlers = {}
        page.on = lambda event, handler: handlers.update({event: handler})
        async def goto(*args, **kwargs):
            if response is not None:
                await handlers['response'](response)
        page.goto = AsyncMock(side_effect=goto)
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))))
        manager.__aexit__ = AsyncMock(return_value=False)
        return manager

    async def test_page_without_captured_response_is_unavailable_not_empty(self):
        with patch.object(crawler.Path, 'is_file', return_value=True), patch.object(
                crawler, 'async_playwright', return_value=self.browser_fixture()):
            with self.assertRaises(crawler.PostFetchUnavailable):
                await crawler._fetch_user_posts_playwright('fixture')

    async def test_page_permission_response_is_unavailable_not_empty(self):
        response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',
                                   status=403, json=AsyncMock(return_value={}))
        with patch.object(crawler.Path, 'is_file', return_value=True), patch.object(
                crawler, 'async_playwright', return_value=self.browser_fixture(response)):
            with self.assertRaises(crawler.PostFetchUnavailable):
                await crawler._fetch_user_posts_playwright('fixture')

    async def test_successful_empty_page_remains_a_real_empty(self):
        response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',
                                   status=200, json=AsyncMock(return_value={
                                       'status_code': 0, 'aweme_list': []}))
        with patch.object(crawler.Path, 'is_file', return_value=True), patch.object(
                crawler, 'async_playwright', return_value=self.browser_fixture(response)):
            self.assertEqual(await crawler._fetch_user_posts_playwright('fixture'), [])

    async def test_api_failure_and_no_page_response_remain_unavailable(self):
        diagnostics = {}
        with patch.object(crawler, '_fetch_user_posts_api', new=AsyncMock(
                side_effect=crawler.PostFetchError('fixture API error'))), patch.object(
                crawler.Path, 'is_file', return_value=True), patch.object(
                crawler, 'async_playwright', return_value=self.browser_fixture()):
            self.assertEqual(await crawler.fetch_user_posts('fixture', diagnostics=diagnostics), [])
        self.assertEqual(diagnostics['status'], 'unavailable')

