"""Real Chromium against local fixtures; never uses the saved account."""

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import xingtu
from tests.test_xingtu_discovery_browser import RoutedRuntime


class XingtuEnrichmentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_nickname_labels_do_not_abort_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{"cookies": [], "origins": []}')
            html = '''<meta charset="utf-8"><button>昵称找人</button>
                <div hidden>昵称找人</div>
                <input placeholder="输入达人昵称、抖音号或星图ID">
                <script>document.querySelector('input').onkeydown = e => {
                if(e.key === 'Enter') fetch('/gw/api/gsearch/search_for_author_square', {
                method:'POST', body:JSON.stringify({search_param:{keyword:e.target.value},
                page_param:{page:'1'}})});};</script>'''
            async def route(r):
                if r.request.url == xingtu.MARKET_URL:
                    await r.fulfill(body=html, content_type='text/html')
                elif r.request.url.endswith(xingtu.SEARCH_PATH):
                    await r.fulfill(json={'authors': [{'star_id': 'test-star',
                        'attribute_datas': {'id': 'test-star', 'core_user_id': '123',
                                            'vv_median_30d': '350000'}}]})
                else:
                    await r.abort()
            runtime = RoutedRuntime(route)
            async def retain_search_result(page, creator, result):
                return result
            try:
                with patch.object(xingtu, 'STORAGE_STATE', state), patch.object(
                        xingtu, 'async_playwright', return_value=runtime), patch(
                            'app.xingtu_details.enrich_details', side_effect=retain_search_result):
                    # This regression isolates the market selector; detail reads
                    # have a separate full-browser fixture below.
                    result = await xingtu.enrich_creator({'source_platform': 'xingtu',
                        'star_id': 'test-star', 'uid': '123'})
                self.assertTrue(result['identity_verified'], result.get('reason'))
                self.assertEqual(result['median_play_30d'], 350000)
            finally:
                await runtime.cleanup()


class XingtuDetailBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_creator_reads_verified_fans_and_contact_into_complete_result(self):
        detail = __import__('app.xingtu_details', fromlist=['enrich_details']) if importlib.util.find_spec('app.xingtu_details') else None
        self.assertIsNotNone(detail, 'The real detail adapter is missing')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text('{"cookies": [], "origins": []}')
            async def route(r):
                from urllib.parse import urlsplit
                path = urlsplit(r.request.url).path
                responses = {
                    '/gw/api/author/get_author_base_info': {'id':'111', 'core_user_id':'222',
                        'unique_id':'demo-handle','sec_uid':'MS4w-test-verified'},
                    '/gw/api/data_sp/get_author_spread_info': {'play_mid':'350000',
                        'expect_cpm':{'cpm_21_60':'1234'}},
                    '/gw/api/author/get_author_marketing_info': {'price_info':[]},
                    '/gw/api/data_sp/get_author_fans_distribution': {'distributions':[
                        {'type':1,'distribution_list':[{'distribution_key':'female','distribution_value':'60'},
                            {'distribution_key':'male','distribution_value':'40'}]},
                        {'type':2,'distribution_list':[{'distribution_key':'24-30','distribution_value':'100'}]},
                        {'type':4,'distribution_list':[{'distribution_key':'浙江','distribution_value':'100'}]}]},
                    '/gw/api/ggeneric/apply_contact_info': {'apply_result':True,'wechat':'demo_business'},
                }
                if '/author-homepage/' in path:
                    await r.fulfill(content_type='text/html', body='''<meta charset="utf-8">
                        <button id="connect">连接用户</button><button id="fans">粉丝画像</button>
                        <button id="contact">联系</button><button id="wechat">查看微信</button>
                        <script>
                        const q='?o_author_id=111&platform_source=1&platform_channel=1';
                        fetch('/gw/api/author/get_author_base_info'+q);
                        fetch('/gw/api/data_sp/get_author_spread_info'+q+'&type=1&flow_type=0&only_assign=false&range=2');
                        fetch('/gw/api/author/get_author_marketing_info'+q);
                        document.querySelector('#fans').onclick=()=>fetch('/gw/api/data_sp/get_author_fans_distribution?o_author_id=111&platform_source=1&author_type=1');
                        document.querySelector('#wechat').onclick=()=>fetch('/gw/api/ggeneric/apply_contact_info?dest_id=111&contact_type=1');
                        </script>''')
                elif path in responses:
                    await r.fulfill(json={'base_resp':{'status_code':0}, **responses[path]})
                else:
                    await r.abort()
            runtime = RoutedRuntime(route)
            try:
                await runtime.start()
                await runtime.launch(headless=True)
                context = await runtime.new_context(storage_state=str(state))
                page = await context.new_page()
                result = await detail.enrich_details(page,
                    {'source_platform':'xingtu','star_id':'111','uid':'222'}, xingtu._empty_result())
                self.assertEqual(result['status'], 'completed', result)
                self.assertTrue(result['identity_verified'])
                self.assertEqual(result['median_play_30d'], 350000)
                self.assertEqual(result['audience']['gender']['female'], .6)
                self.assertEqual(result['contact']['value'], 'demo_business')
                self.assertNotIn('?', result['source_url'])
                self.assertEqual(result['profile']['unique_id'], 'demo-handle')
            finally:
                await runtime.cleanup()
