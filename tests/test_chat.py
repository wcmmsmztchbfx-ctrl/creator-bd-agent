import asyncio
import importlib.util
import json
import unittest
from unittest.mock import patch

import httpx

if importlib.util.find_spec('app.chat'):
    from app import chat as service
else:
    from app import crawler as service


SETTINGS = {
    'llm_enabled': 'true',
    'llm_api_key': 'test-secret-not-a-real-key',
    'llm_base_url': 'https://model.example/v1',
    'llm_model': 'test-model',
}


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_revision_prompt_keeps_plan_separate_from_execution(self):
        prompt = service.get_system_prompt()['content']
        self.assertIn('修改已有需求', prompt)
        self.assertIn('不等于取消', prompt)

    async def test_disabled_model_does_not_fake_search(self):
        with patch.object(service, 'get_all_settings', return_value={**SETTINGS, 'llm_enabled': 'false'}):
            result = await service.parse_and_respond_chat('帮我找食品达人')
        self.assertIsNone(result.get('task_card'))
        self.assertEqual(result.get('engine'), 'unavailable')
        self.assertIn('启用', result['reply'])

    async def request(self, answer, message='找3位食品达人', history=None, status=200):
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            body = {'choices': [{'message': {'content': json.dumps(answer, ensure_ascii=False)}}]}
            return httpx.Response(status, json=body)
        original = httpx.AsyncClient
        def client(*args, **kwargs):
            return original(*args, **{**kwargs, 'transport': httpx.MockTransport(handler)})
        with patch.object(service, 'get_all_settings', return_value=SETTINGS), patch.object(service.httpx, 'AsyncClient', side_effect=client):
            result = await service.parse_and_respond_chat(message, history=history or [])
        return result, captured

    async def test_system_prompt_and_multiturn_history_are_sent(self):
        result, captured = await self.request(
            {'intent': 'chat', 'reply': '可以，我们先聊商品。', 'task_card': None},
            history=[{'role': 'user', 'content': '我做零食'}, {'role': 'assistant', 'content': '希望多少人？'}],
        )
        messages = captured[0]['messages']
        self.assertEqual(messages[0]['role'], 'system')
        self.assertIn('不得', messages[0]['content'])
        self.assertEqual(messages[1]['content'], '我做零食')
        self.assertEqual(messages[-1]['content'], '找3位食品达人')
        self.assertEqual(result['engine'], 'llm')
        self.assertEqual(result['reply'], '可以，我们先聊商品。')
        self.assertIsNone(result['task_card'])

    async def test_task_card_preserves_constraints(self):
        card = {'category': '食品', 'keyword': '零食测评', 'sample_count': 3, 'min_avg_likes': 10000,
                'active_days': 7, 'content_preferences': ['实测'], 'exclusions': ['纯搬运']}
        result, _ = await self.request({'intent': 'search', 'reply': '请确认条件', 'task_card': card})
        self.assertEqual(result['task_card'], {**card, 'min_followers': 0, 'max_followers': None,
                                                'min_max_likes': 0, 'discovery_mode': 'xingtu',
                                                'search_queries': ['零食测评'],
                                                'topic_min_samples': 5, 'topic_min_ratio': 0.6})

    async def test_prompt_example_is_an_executable_xingtu_plan(self):
        from app.contracts import TaskCard

        prompt = service.get_system_prompt()['content']
        start = prompt.index('{\n  "category"')
        example, _ = json.JSONDecoder().raw_decode(prompt[start:])
        card = TaskCard.model_validate(example)
        self.assertEqual(card.discovery_mode, 'xingtu')
        self.assertLessEqual(len(card.search_queries), 3)

    async def test_legacy_model_cards_remain_readable_without_losing_constraints(self):
        for mode in ('user', 'video', 'hybrid', 'xingtu'):
            with self.subTest(mode=mode):
                result, _ = await self.request({'intent': 'search', 'reply': '请确认', 'task_card': {
                    'category': '食品', 'keyword': '控糖零食', 'discovery_mode': mode,
                    'search_queries': ['控糖零食'], 'min_followers': 1000, 'max_followers': 5000}})
                self.assertIsNotNone(result['task_card'])
                self.assertEqual(result['task_card']['discovery_mode'], mode)
                self.assertEqual(result['task_card']['search_queries'], ['控糖零食'])
                self.assertEqual(result['task_card']['max_followers'], 5000)

    async def test_no_task_card_for_clarification(self):
        result, _ = await self.request({'intent': 'clarify', 'reply': '你想找哪类食品达人？', 'task_card': None})
        self.assertIsNone(result['task_card'])
        self.assertEqual(result['intent'], 'clarify')

    async def test_invalid_model_card_is_not_executable(self):
        result, _ = await self.request({'intent': 'search', 'reply': '开始', 'task_card': {
            'category': '食品', 'keyword': '食品', 'sample_count': 5000}})
        self.assertIsNone(result['task_card'])
        self.assertEqual(result['error']['code'], 'invalid_response')

    async def test_provider_error_is_visible_and_not_fallback(self):
        result, _ = await self.request({}, status=401)
        self.assertIsNone(result['task_card'])
        self.assertEqual(result['error']['code'], 'authentication_failed')
        self.assertNotIn(SETTINGS['llm_api_key'], json.dumps(result))

    async def test_timeout_has_no_task_card(self):
        def handler(request):
            raise httpx.ReadTimeout('provider timeout', request=request)
        original = httpx.AsyncClient
        with patch.object(service, 'get_all_settings', return_value=SETTINGS), patch.object(
            service.httpx, 'AsyncClient', side_effect=lambda **kw: original(**{**kw, 'transport': httpx.MockTransport(handler)})
        ):
            result = await service.parse_and_respond_chat('你好', history=[])
        self.assertEqual(result['error']['code'], 'timeout')
        self.assertIsNone(result['task_card'])

    async def test_connection_test_uses_synthetic_message_only(self):
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
        original = httpx.AsyncClient
        with patch.object(service, 'get_all_settings', return_value={**SETTINGS, 'llm_enabled': 'false'}), patch.object(
            service.httpx, 'AsyncClient', side_effect=lambda **kw: original(**{**kw, 'transport': httpx.MockTransport(handler)})
        ):
            result = await service.test_llm_connection()
        self.assertTrue(result['ok'])
        self.assertEqual(len(captured[0]['messages']), 2)
        self.assertNotIn('食品', json.dumps(captured[0], ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
