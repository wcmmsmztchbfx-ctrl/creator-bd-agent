import json
import unittest
from unittest.mock import patch
import httpx
from app import chat

SETTINGS = {'llm_enabled':'true','llm_model':'synthetic-model','llm_base_url':'https://model.invalid/v1','llm_api_key':'synthetic-secret'}


class ChatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, answer, message='你好'):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':answer}}]})
        original=httpx.AsyncClient
        with patch.object(chat,'get_all_settings',return_value=SETTINGS),patch.object(
            chat.httpx,'AsyncClient',side_effect=lambda **kw:original(**{**kw,'transport':httpx.MockTransport(handler)})):
            result=await chat.parse_and_respond_chat(message,history=[])
        return result,requests

    async def test_plain_model_reply_is_real_chat_not_a_protocol_error(self):
        r,requests=await self.ask('你好，我可以帮你整理达人需求。')
        self.assertEqual(r['engine'],'llm')
        self.assertIsNone(r['error'])
        self.assertEqual(r['reply'],'你好，我可以帮你整理达人需求。')
        self.assertIsNone(r['task_card'])
        self.assertEqual(requests[0]['response_format'],{'type':'json_object'})

    async def test_plain_reply_never_becomes_an_executable_task(self):
        r,_=await self.ask('已经帮你找到了5位达人。','找5位食品达人')
        self.assertEqual(r['intent'],'chat')
        self.assertIsNone(r['task_card'])

    async def test_chat_json_can_omit_unused_task_card(self):
        r,_=await self.ask(json.dumps({'intent':'chat','reply':'你好'}))
        self.assertEqual(r['engine'],'llm')
        self.assertEqual(r['reply'],'你好')
        self.assertIsNone(r['task_card'])

    async def test_bad_search_object_remains_unexecutable(self):
        r,_=await self.ask(json.dumps({'intent':'search','reply':'开始','task_card':{'keyword':'食品','sample_count':9999}}))
        self.assertIsNone(r['task_card'])
        self.assertEqual(r['error']['code'],'invalid_response')

    async def test_direct_valid_task_card_becomes_pending_card_not_execution(self):
        card={'category':'美妆','keyword':'美妆测评','sample_count':5,'min_followers':10000}
        r,_=await self.ask(json.dumps(card),'找5位美妆测评达人，先确认参数')
        self.assertIsNone(r['error'])
        self.assertEqual(r['intent'],'search')
        self.assertEqual(r['task_card']['min_followers'],10000)
        self.assertIn('确认',r['reply'])

    async def test_direct_invalid_task_card_is_still_rejected(self):
        r,_=await self.ask(json.dumps({'category':'美妆','keyword':'美妆','sample_count':9999}))
        self.assertIsNone(r['task_card'])
        self.assertEqual(r['error']['code'],'invalid_response')

    async def test_model_identifier_is_supplied_without_credentials(self):
        _,requests=await self.ask(json.dumps({'intent':'chat','reply':'模型标识为 synthetic-model','task_card':None}),'你是什么模型')
        self.assertIn('synthetic-model',requests[0]['messages'][0]['content'])
        self.assertNotIn('synthetic-secret',requests[0]['messages'][0]['content'])

    async def test_empty_json_mode_reply_retries_once_as_plain_chat(self):
        captured=[]
        def handler(request):
            data=json.loads(request.content);captured.append(data)
            text='' if 'response_format' in data else '当前接口配置的模型是 synthetic-model。'
            return httpx.Response(200,json={'choices':[{'message':{'content':text},'finish_reason':'stop'}]})
        original=httpx.AsyncClient
        with patch.object(chat,'get_all_settings',return_value=SETTINGS),patch.object(
            chat.httpx,'AsyncClient',side_effect=lambda **kw:original(**{**kw,'transport':httpx.MockTransport(handler)})):
            r=await chat.parse_and_respond_chat('你是什么模型',history=[])
        self.assertEqual(r['engine'],'llm')
        self.assertIn('synthetic-model',r['reply'])
        self.assertEqual(len(captured),2)
        self.assertNotIn('response_format',captured[1])

    async def test_qualification_never_retries_outside_its_budget(self):
        captured=[]
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':''},'finish_reason':'stop'}]})
        original=httpx.AsyncClient
        with patch.object(chat.httpx,'AsyncClient',side_effect=lambda **kw:original(**{**kw,'transport':httpx.MockTransport(handler)})):
            with self.assertRaises(chat.ModelFailure):
                await chat.request_model_json(SETTINGS,[{'role':'system','content':'Return JSON'}])
        self.assertEqual(len(captured),1)
