import datetime
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import chat, crawler, db, exporter, main


class FakeXingtuDiscovery:
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


class FullWorkflowTests(unittest.TestCase):
    def test_conversation_confirmation_rules_storage_and_filtered_excel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', str(Path(tmp) / 'test.db')), patch.object(
            exporter, 'EXPORTS_DIR', tmp
        ):
            main.current_running_task.clear()
            main.current_running_task.update(is_running=False, task_id=None, keyword='', step='', message='')
            with TestClient(main.app) as client:
                db.save_setting('llm_enabled', 'true')
                db.save_setting('llm_api_key', 'synthetic-only')
                card = dict(category='食品', keyword='食品测评', sample_count=2, min_avg_likes=10000,
                            active_days=7, content_preferences=[], exclusions=[],
                            discovery_mode='user', search_queries=['食品测评'],
                            topic_min_samples=5, topic_min_ratio=0.6)
                replies = [json.dumps({'intent': 'chat', 'reply': '先了解商品。', 'task_card': None}),
                           json.dumps({'intent': 'search', 'reply': '请确认均赞与活跃条件。', 'task_card': card})]
                with patch.object(chat, '_complete', new=AsyncMock(side_effect=replies)) as model:
                    response = client.post('/api/chat', json={'message': '先聊聊食品需求'})
                    self.assertEqual(response.json()['intent'], 'chat')
                    self.assertEqual(db.get_all_tasks(), [])
                    response = client.post('/api/chat', json={'message': '找2位，均赞1万，7天更新'})
                    self.assertEqual(response.json()['task_card'],
                                     {**card, 'min_followers': 0, 'max_followers': None, 'min_max_likes': 0})
                    messages = model.call_args_list[1].args[1]
                    self.assertEqual(messages[0]['role'], 'system')
                    self.assertEqual(messages[1]['content'], '先聊聊食品需求')
                now = int(datetime.datetime.now().timestamp())
                source = FakeXingtuDiscovery([
                    {'star_id': 'synthetic-star-' + actor, 'uid': uid,
                     'source_platform': 'xingtu', 'nickname': '合成' + actor, 'followers': 1000,
                     'posts': [{'aweme_id': f'{actor}-{index}', 'desc': '合成食品实测样本，仅用于测试',
                                'create_time': now - 86400 * (index + 1),
                                'statistics': {'digg_count': likes, 'comment_count': 5}}
                               for index in range(6)]}
                    for actor, uid, likes in (('a', '10001', 9000), ('b', '10002', 20000))])
                def review(settings, messages, max_tokens=2000):
                    payload = json.loads(messages[-1]['content'].split('\n', 1)[1])
                    return {'posts': [{'aweme_id': post['aweme_id'], 'decision': 'yes',
                                       'evidence_id': post['aweme_id'], 'reason': '合成食品方向'}
                                      for post in payload['posts']],
                            'account': {'account_type': 'independent_creator',
                                        'evidence_ids': [post['aweme_id'] for post in payload['posts'][:2]],
                                        'reason': 'Synthetic personal creator across posts'},
                            'creator_pattern': 'consistent_creator', 'reason': '持续食品实测'}
                with patch('app.xingtu_pipeline.XingtuDiscovery', return_value=source), patch.object(
                    crawler.douyin_browser, 'search', new=AsyncMock(side_effect=AssertionError('No Douyin search'))
                ) as browser_search, patch.object(
                    chat, 'request_model_json', side_effect=review
                ), patch.object(
                    crawler, 'fetch_profile', new=AsyncMock(side_effect=AssertionError('No Douyin profile'))
                ) as profile, patch.object(
                    crawler, 'fetch_user_posts', new=AsyncMock(side_effect=AssertionError('No Douyin posts'))
                ) as posts:
                    response = client.post('/api/execute', json=card)
                browser_search.assert_not_awaited()
                profile.assert_not_awaited()
                posts.assert_not_awaited()
                self.assertEqual(source.calls, [('食品测评', 1)])
                self.assertEqual(response.status_code, 200)
                task_id = response.json()['task_id']
                self.assertEqual(db.get_task(task_id)['status'], 'completed')
                self.assertEqual(db.get_task(task_id)['requirements']['discovery_mode'], 'xingtu')
                self.assertEqual(client.get('/api/results',params={'task_id':task_id}).json()['creators'], [])
                results = client.get(f'/api/tasks/{task_id}/candidates',params={'view':'all'}).json()['creators']
                self.assertEqual({r['nickname']: r['recommend_level'] for r in results}, {'合成a': '淘汰', '合成b': '可选'})
                self.assertEqual({r['nickname']: r['candidate_status'] for r in results},
                                 {'合成a': 'rejected', '合成b': 'eligible'})
                self.assertEqual({r['nickname']: r['avg_likes'] for r in results},
                                 {'合成a': 9000, '合成b': 20000})
                self.assertTrue(all(r['discovery_sources'][0]['search_type'] == 'xingtu' for r in results))
                self.assertTrue(all(r['source_platform'] == 'xingtu' for r in results))
                self.assertTrue(all(not r['unique_id'] for r in results))
                # Formal report admission is a separate explicit action.
                client.post(f'/api/tasks/{task_id}/report',json={
                    'creator_ids':[r['creator_id'] for r in results], 'allow_missing':True})
                admitted = client.get('/api/results', params={'task_id': task_id}).json()['creators']
                self.assertEqual([r['nickname'] for r in admitted], ['合成b'])
                response = client.get('/api/export', params={'task_id': task_id, 'search': '合成b'})
                self.assertEqual(response.status_code, 200)
                from io import BytesIO
                with zipfile.ZipFile(BytesIO(response.content)) as z:
                    strings = z.read('xl/sharedStrings.xml').decode()
                    self.assertIn('合成b', strings)
                    self.assertNotIn('合成a', strings)
                self.assertNotIn('synthetic-only', client.get('/api/settings').text)
                client.delete('/api/chat/history')
                self.assertEqual(db.get_chat_history(), [])
                self.assertEqual(len(db.get_all_tasks()), 1)
            main.current_running_task.clear()
            main.current_running_task.update(is_running=False, task_id=None, keyword='', step='', message='')


if __name__ == '__main__':
    unittest.main()
