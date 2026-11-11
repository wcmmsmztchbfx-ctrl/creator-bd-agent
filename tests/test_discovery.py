import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import crawler, db


def page(*uids, more=False, cursor=0, followers=1000):
    return {'user_list': [{'user_info': {'sec_uid': uid, 'nickname': uid,
                                        'follower_count': followers}} for uid in uids],
            'has_more': int(more), 'cursor': cursor}


def video_page(*uids, more=False, cursor=0, aweme_id='video-1'):
    return {'data': [{'aweme_info': {'aweme_id': aweme_id,
                                     'author': {'sec_uid': uid, 'nickname': uid,
                                                'follower_count': 1000}}} for uid in uids],
            'has_more': int(more), 'cursor': cursor}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(self.temp.name) / 'db')))
        db.init_db()
        self.enterContext(patch.object(crawler, 'fetch_profile', new=AsyncMock(
            return_value={'nickname': '合成账号', 'follower_count': 1000})))
        now = int(datetime.datetime.now().timestamp())
        self.enterContext(patch.object(crawler, 'fetch_user_posts', new=AsyncMock(return_value=[
            {'aweme_id': 'p', 'create_time': now, 'statistics': {'digg_count': 500}}
        ])))

    async def test_final_target_does_not_cap_candidates_and_pages(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(side_effect=[
            page('a', 'b', more=True, cursor=2), page('b', 'c'), page(), page()
        ])) as search:
            tid = await crawler.execute_bd_search_task('食品', 1)
        self.assertEqual(len(db.get_task_results(tid)), 3)
        self.assertEqual([(call.args[0], call.args[1]) for call in search.await_args_list],
                         [('食品测评', 0), ('食品测评', 2), ('美食分享', 0), ('美食制作', 0)])
        self.assertEqual(db.get_task(tid)['progress']['requested'], 1)

    async def test_stop_keeps_all_discovered_identities(self):
        calls = []
        def stopping(tid):
            calls.append(tid)
            return len(calls) >= 3
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a','b','c',more=True,cursor=3))), patch.object(
            crawler, 'stop_requested', side_effect=stopping):
            tid = await crawler.execute_bd_search_task('食品', 1)
        self.assertEqual(db.get_task(tid)['status'], 'stopped')
        self.assertEqual(len(db.get_task_results(tid)), 3)

    async def test_stop_from_progress_callback_prevents_next_search_request(self):
        def stop_before_search(message, progress):
            with db.get_connection() as conn:
                conn.execute('UPDATE tasks SET stop_requested=1')
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a'))) as search:
            tid = await crawler.execute_bd_search_task('food', 1, progress_callback=stop_before_search)
        search.assert_not_awaited()
        self.assertEqual(db.get_task(tid)['status'], 'stopped')

    async def test_failed_profile_still_in_knowledge_base(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a','b'))), patch.object(
            crawler, 'fetch_profile', new=AsyncMock(side_effect=[{'nickname':'成功'}, RuntimeError('失败')])):
            tid = await crawler.execute_bd_search_task('食品',1)
        self.assertEqual(len(db.get_knowledge_base()),2)
        rows = db.get_task_results(tid)
        self.assertEqual(len(rows),2)
        self.assertIsNone(next(r for r in rows if r['sec_uid']=='b')['hard_pass'])

    async def test_repeated_cursor_does_not_loop_forever(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a',more=True,cursor=1))) as search:
            tid = await crawler.execute_bd_search_task('食品',1)
        for query in ('食品测评', '美食分享', '美食制作'):
            calls = [call for call in search.await_args_list if call.args[0] == query]
            self.assertEqual([call.args[1] for call in calls], [0, 1])
        self.assertEqual(db.get_task(tid)['status'],'partial')

    async def test_missing_cursor_never_guesses_next_offset(self):
        first = page('a', more=True)
        first.pop('cursor')
        with patch.object(crawler, 'search_page', new=AsyncMock(
                side_effect=[first, page('b')])) as search:
            tid = await crawler.execute_bd_search_task('food', 1)
        self.assertEqual(search.await_count, 1)
        self.assertEqual(db.get_task(tid)['status'], 'partial')
        self.assertEqual([row['sec_uid'] for row in db.get_task_results(tid)], ['a'])

    async def test_duplicate_only_pages_trigger_guard_even_with_advancing_cursor(self):
        pages = [page('a', more=True, cursor=index) for index in range(1, 5)]
        with patch.object(crawler, 'search_page', new=AsyncMock(
                side_effect=[*pages, page('b')])) as search:
            tid = await crawler.execute_bd_search_task('food', 1)
        self.assertEqual(search.await_count, 4)
        self.assertEqual(db.get_task(tid)['status'], 'partial')
        self.assertEqual(len(db.get_task_results(tid)), 1)

    async def test_rediscovery_does_not_refresh_old_metrics_timestamp(self):
        cid=db.upsert_creator({'sec_uid':'a','nickname':'旧资料','followers':123})
        with db.get_connection() as conn:
            conn.execute("UPDATE creators SET updated_at='2020-01-01 00:00:00' WHERE id=?",(cid,))
        with patch.object(crawler,'search_page',new=AsyncMock(return_value=page('a','b'))), patch.object(
            crawler,'fetch_profile',new=AsyncMock(side_effect=[RuntimeError('失败'),{'nickname':'成功'}])):
            await crawler.execute_bd_search_task('食品',1)
        row=next(r for r in db.get_knowledge_base() if r['creator_id']==cid)
        self.assertEqual(row['updated_at'],'2020-01-01 00:00:00')
        self.assertEqual(row['followers'],123)

    async def test_refresh_uses_current_task_numeric_conditions(self):
        tid=db.save_task('测试','食品',requirements={'sample_count':2,'min_avg_likes':9999},status='completed')
        cid=db.upsert_creator({'sec_uid':'a','nickname':'旧资料'})
        db.save_recommendation(cid,tid,{'hard_pass':True})
        await crawler.refresh_candidate(tid,cid)
        self.assertFalse(db.get_task_results(tid)[0]['hard_pass'])

    async def test_duplicate_only_advancing_page_does_not_hide_later_creator(self):
        with patch.object(crawler,'search_page',new=AsyncMock(side_effect=[
            page('a',more=True,cursor=1),page('a',more=True,cursor=2),page('b'),page(),page()])) as search:
            tid=await crawler.execute_bd_search_task('食品',1)
        self.assertEqual([(call.args[0], call.args[1]) for call in search.await_args_list],
                         [('食品测评', 0), ('食品测评', 1), ('食品测评', 2),
                          ('美食分享', 0), ('美食制作', 0)])
        self.assertEqual(len(db.get_task_results(tid)),2)

    async def test_sparse_profile_refresh_retains_omitted_identity_and_signature(self):
        tid=db.save_task('测试','食品',requirements={'min_avg_likes':0})
        cid=db.upsert_creator({'sec_uid':'a','nickname':'旧','uid':'id','unique_id':'stable', 'signature':'旧简介'})
        db.save_recommendation(cid,tid,{'hard_pass':None})
        await crawler.refresh_candidate(tid,cid)
        row=db.get_task_results(tid)[0]
        self.assertEqual(row['unique_id'],'stable')
        self.assertEqual(row['signature'],'旧简介')

    async def test_stop_after_profile_does_not_start_posts_request(self):
        async def profile(uid):
            with db.get_connection() as conn:
                conn.execute('UPDATE tasks SET stop_requested=1')
            return {'nickname':'已读取','follower_count':123}
        with patch.object(crawler,'search_page',new=AsyncMock(return_value=page('a'))), patch.object(
            crawler,'fetch_profile',side_effect=profile), patch.object(crawler,'fetch_user_posts',new=AsyncMock()) as posts:
            tid=await crawler.execute_bd_search_task('食品',1)
        posts.assert_not_awaited()
        self.assertEqual(db.get_task(tid)['status'],'stopped')
        self.assertEqual(db.get_knowledge_base()[0]['followers'],123)


class NumericConditionsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(db, "get_setting", side_effect=lambda key, default: default))

    def evaluate(self, **kw):
        args = dict(keyword='食品',nickname='合成',followers=1000,avg_likes=300,
                    max_likes=500,days_since_latest=1,sample_posts=[{}],contacts=[],star_atlas=0)
        args.update(kw)
        return crawler.evaluate_recommendation(**args)

    def test_followers_and_peak_are_hard_conditions(self):
        self.assertFalse(self.evaluate(min_followers=2000)['hard_pass'])
        self.assertFalse(self.evaluate(max_followers=500)['hard_pass'])
        self.assertFalse(self.evaluate(min_max_likes=1000)['hard_pass'])

    def test_unknown_followers_never_pass_required_gate(self):
        self.assertIsNone(self.evaluate(followers=None,min_followers=500)['hard_pass'])


class SearchNormalizationTests(unittest.TestCase):
    def test_user_search_reads_only_stable_user_identity_and_ignores_video_authors(self):
        payload = {
            'user_list': [
                {'user_info': {'sec_uid': 'u1', 'nickname': '同名', 'follower_count': 1000}},
                {'user_info': {'sec_uid': 'u1', 'nickname': '重复', 'follower_count': 1000}},
            ],
            'data': [
                {'user_list': [{'user_info': {'sec_uid': 'u2', 'nickname': '嵌套'}}]},
                {'user_info': {'sec_uid': 'u3', 'nickname': '直接'}},
                {'aweme_info': {'author': {'sec_uid': 'video-only', 'nickname': '视频作者'}}},
            ],
        }
        authors = crawler.normalize_search_authors(payload, 'user')
        self.assertEqual([item['sec_uid'] for item in authors], ['u1', 'u2', 'u3'])
        self.assertTrue(all(item['nickname'] != '视频作者' for item in authors))

    def test_video_search_keeps_source_post_id_for_audit(self):
        payload = {'data': [
            {'aweme_info': {'aweme_id': '7123456789012345678',
                            'author': {'sec_uid': 'v1', 'nickname': '视频作者',
                                       'follower_count': 1000}}},
            {'aweme_info': {'aweme_id': '7123456789012345678',
                            'author': {'sec_uid': 'v1', 'nickname': '重复'}}},
        ]}
        authors = crawler.normalize_search_authors(payload, 'video')
        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0]['source_post_id'], '7123456789012345678')
        self.assertEqual(authors[0]['search_type'], 'video')


class DiscoveryAssessmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(self.temp.name) / 'db')))
        db.init_db()
        self.now = int(datetime.datetime.now().timestamp())
        self.enterContext(patch.object(crawler, 'fetch_profile', new=AsyncMock(
            return_value={'nickname': '合成账号', 'follower_count': 1000, 'signature': '公开简介'})))
        self.posts = [{'aweme_id': f'p{index}', 'desc': '食品实测，配料表解读',
                       'create_time': self.now - 86400 * (index + 1),
                       'statistics': {'digg_count': 500}} for index in range(6)]
        self.enterContext(patch.object(crawler, 'fetch_user_posts', new=AsyncMock(return_value=self.posts)))

    def topic(self, status='matched'):
        return {'topic_status': status, 'method': 'text_llm',
                'posts': [{'aweme_id': post['aweme_id'], 'decision': 'yes',
                           'evidence_id': post['aweme_id']} for post in self.posts],
                'account': {'account_type': 'independent_creator',
                            'evidence_ids': [post['aweme_id'] for post in self.posts[:2]],
                            'reason': 'Synthetic personal creator across posts'},
                'creator_pattern': 'consistent_creator' if status == 'matched' else 'uncertain',
                'sample_count': 6, 'matched_count': 6 if status == 'matched' else 0,
                'unknown_count': 0, 'evidence_ids': [], 'reason': '合成方向',
                'prompt_version': 'synthetic', 'model': 'synthetic', 'error_code': None,
                'truncated_ids': [], 'ratio': 1.0 if status == 'matched' else None,
                'min_samples': 5, 'min_ratio': 0.6}

    async def test_sources_are_saved_at_discovery_time_with_query_and_type(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a'))):
            tid = await crawler.execute_bd_search_task('食品', 1, search_queries=['食品测评'],
                                                       discovery_mode='user')
        cid = db.get_task_results(tid)[0]['creator_id']
        sources = db.get_discovery_sources(tid, cid)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['search_type'], 'user')
        self.assertEqual(sources[0]['query'], '食品测评')
        self.assertEqual(sources[0]['source_post_id'], '')

    async def test_hybrid_search_dedupes_by_sec_uid_and_keeps_both_sources(self):
        async def search(keyword, offset=0, count=20, search_type='user'):
            return page('a') if search_type == 'user' else video_page('a', 'b')
        with patch.object(crawler, 'search_page', new=AsyncMock(side_effect=search)):
            tid = await crawler.execute_bd_search_task('食品', 1, discovery_mode='hybrid',
                                                       search_queries=['食品'])
        rows = db.get_task_results(tid)
        self.assertEqual(len(rows), 2)
        first = next(row for row in rows if row['sec_uid'] == 'a')
        sources = db.get_discovery_sources(tid, first['creator_id'])
        self.assertEqual({(item['search_type'], item['source_post_id']) for item in sources},
                         {('user', ''), ('video', 'video-1')})

    async def test_pagination_keeps_the_first_page_search_type(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(side_effect=[
                page('a', more=True, cursor=2), page('b'), page(), page()])) as search:
            tid = await crawler.execute_bd_search_task('食品', 1, discovery_mode='user')
        self.assertEqual([(call.args[0], call.args[1]) for call in search.await_args_list],
                         [('食品测评', 0), ('食品测评', 2), ('美食分享', 0), ('美食制作', 0)])
        self.assertTrue(all(call.kwargs['search_type'] == 'user' for call in search.await_args_list))
        self.assertEqual(len(db.get_task_results(tid)), 2)

    async def test_user_search_failure_is_not_silently_replaced_by_video(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(
                side_effect=RuntimeError('user search unavailable'))) as search:
            with self.assertRaises(RuntimeError):
                await crawler.execute_bd_search_task('食品', 1)
        self.assertEqual(search.await_count, 1)
        self.assertEqual(search.await_args.kwargs['search_type'], 'user')
        self.assertEqual(db.get_all_tasks()[0]['status'], 'failed')

    async def test_known_follower_failure_skips_posts_and_model_review(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a', followers=100))), patch.object(
                crawler, 'fetch_user_posts', new=AsyncMock()) as posts, patch.object(
                crawler.qualification, 'review_creator_topic', new=AsyncMock()) as review:
            tid = await crawler.execute_bd_search_task('食品', 1, min_followers=10000)
        posts.assert_not_awaited()
        review.assert_not_awaited()
        row = db.get_task_results(tid)[0]
        self.assertEqual(row['recommend_level'], '淘汰')
        assessment = db.get_current_candidate_assessment(tid, row['creator_id'])
        self.assertEqual(assessment['numeric_status'], 'failed')
        self.assertEqual(assessment['overall_status'], 'rejected')
        self.assertEqual(assessment['topic']['error_code'], 'not_reviewed')
        self.assertEqual(len(db.get_knowledge_base()), 1)

    async def test_model_failure_keeps_candidate_pending_and_does_not_become_pass(self):
        with patch.object(crawler, 'search_page', new=AsyncMock(return_value=page('a'))), patch.object(
                crawler.qualification, 'review_creator_topic',
                new=AsyncMock(return_value={**self.topic('unknown'), 'error_code': 'timeout'})):
            tid = await crawler.execute_bd_search_task('食品', 1, min_avg_likes=100)
        row = db.get_task_results(tid)[0]
        self.assertEqual(row['recommend_level'], '待核实')
        assessment = db.get_current_candidate_assessment(tid, row['creator_id'])
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertEqual(assessment['topic_status'], 'unknown')

    async def test_budget_pause_keeps_every_discovery_and_stops_new_model_calls(self):
        db.save_setting('max_topic_reviews_per_run', '1')
        with patch.object(crawler, 'search_page', new=AsyncMock(
                return_value=page('a', 'b', 'c'))), patch.object(
                crawler.qualification, 'review_creator_topic',
                new=AsyncMock(side_effect=lambda *args: self.topic('matched'))) as review:
            tid = await crawler.execute_bd_search_task('食品', 1, min_avg_likes=100)
        task = db.get_task(tid)
        self.assertEqual(task['status'], 'partial')
        self.assertEqual(task['progress']['pause_reason'], 'topic_review_budget')
        self.assertEqual(review.await_count, 1)
        self.assertEqual(len(db.get_task_results(tid)), 3)
        statuses = [row['recommend_level'] for row in db.get_task_results(tid)]
        self.assertIn('可选', statuses)
        self.assertIn('待核实', statuses)
