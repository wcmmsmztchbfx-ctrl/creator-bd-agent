"""Real Chromium regressions with fully routed traffic and empty login state."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from app import xingtu, xingtu_details as details
from tests.test_xingtu_discovery_browser import RoutedRuntime


MARKET_HTML = '''<meta charset="utf-8">
<button>\u6635\u79f0\u627e\u4eba</button>
<input placeholder="\u8f93\u5165\u8fbe\u4eba\u6635\u79f0\u3001\u6296\u97f3\u53f7\u6216\u661f\u56feID">
<script>
document.querySelector('input').onkeydown = event => {
  if (event.key === 'Enter') {
    fetch('/gw/api/gsearch/search_for_author_square', {
      method: 'POST', body: JSON.stringify({
        search_param: {keyword: event.target.value}, page_param: {page: '1'}
      })
    });
  }
};
</script>'''

DETAIL_HTML = '''<meta charset="utf-8">
<button id="connect">\u8fde\u63a5\u7528\u6237</button>
<button id="fans">\u7c89\u4e1d\u753b\u50cf</button>
<button id="contact">\u8054\u7cfb</button>
<button id="wechat">\u67e5\u770b\u5fae\u4fe1</button>
<script>
const query = '?o_author_id=111&platform_source=1&platform_channel=1';
fetch('/gw/api/author/get_author_base_info' + query);
fetch('/gw/api/data_sp/get_author_spread_info' + query
      + '&type=1&flow_type=0&only_assign=false&range=2');
fetch('/gw/api/author/get_author_marketing_info' + query);
document.querySelector('#fans').onclick = () =>
  fetch('/gw/api/data_sp/get_author_fans_distribution'
        + '?o_author_id=111&platform_source=1&author_type=1');
document.querySelector('#wechat').onclick = () =>
  fetch('/gw/api/ggeneric/apply_contact_info?dest_id=111&contact_type=1');
</script>'''


class XingtuDetailRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delay_price = False
        self.hang_contact = False
        self.spread_median = '350000'
        self.price_release = asyncio.Event()
        self.price_seen = asyncio.Event()
        self.contact_started = asyncio.Event()
        self.unexpected_requests = []
        self.page = None

    def observe_response(self, response):
        if urlsplit(response.url).path == details.PRICE_PATH:
            self.price_seen.set()

    async def route(self, route):
        request = route.request
        if self.page is None:
            self.page = request.frame.page
            self.page.on('response', self.observe_response)
        url = urlsplit(request.url)
        if url.scheme != 'https' or url.netloc != 'www.xingtu.cn':
            self.unexpected_requests.append(request.url)
            await route.abort()
            return
        path = url.path
        if path == '/ad/creator/market':
            await route.fulfill(content_type='text/html', body=MARKET_HTML)
            return
        if path == '/ad/creator/author-homepage/douyin-video/111':
            await route.fulfill(content_type='text/html', body=DETAIL_HTML)
            return
        responses = {
            xingtu.SEARCH_PATH: {'authors': [{
                'star_id': '111',
                'attribute_datas': {'id': '111', 'core_user_id': '222',
                                    'vv_median_30d': '11111'},
            }]},
            details.BASE_PATH: {'id': '111', 'core_user_id': '222',
                                'unique_id': 'fixture-handle', 'sec_uid': 'MS4w-fixture'},
            details.SPREAD_PATH: {'play_mid': self.spread_median,
                                  'expect_cpm': {'cpm_21_60': '1234'}},
            details.PRICE_PATH: {'price_info': [
                {'author_id': '111', 'task_category': 1, 'video_type': kind,
                 'enable': True, 'is_open': True, 'price': price}
                for kind, price in ((1, '1200'), (2, '2400'))
            ]},
            details.FANS_PATH: {'distributions': [
                {'type': kind, 'distribution_list': [
                    {'distribution_key': label, 'distribution_value': '100'}
                ]}
                for kind, label in ((1, 'female'), (2, '24-30'), (4, 'Zhejiang'))
            ]},
            details.CONTACT_PATH: {'apply_result': True, 'wechat': 'fixture_business'},
        }
        if path not in responses:
            self.unexpected_requests.append(request.url)
            await route.abort()
            return
        if path == details.PRICE_PATH and self.delay_price:
            # The old implementation reaches contact before this response.
            # A bounded fallback also permits a fix that awaits prices first.
            try:
                await asyncio.wait_for(self.price_release.wait(), 1)
            except TimeoutError:
                pass
        if path == details.FANS_PATH and not self.delay_price:
            await asyncio.wait_for(self.price_seen.wait(), 5)
        if path == details.CONTACT_PATH:
            self.contact_started.set()
            self.price_release.set()
            if self.hang_contact:
                return
            # Ensure the browser has received the price before contact finishes.
            await asyncio.wait_for(self.price_seen.wait(), 5)
        await route.fulfill(json={'base_resp': {'status_code': 0}, **responses[path]})

    async def lookup(self, timeout=10):
        runtime = RoutedRuntime(self.route)
        try:
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state.json'
                state.write_text('{"cookies": [], "origins": []}', encoding='utf-8')
                with patch.object(xingtu, 'STORAGE_STATE', state), patch.object(
                        xingtu, 'async_playwright', return_value=runtime), patch.object(
                            xingtu, 'LOOKUP_TIMEOUT_SECONDS', timeout):
                    result = await xingtu.enrich_creator({
                        'source_platform': 'xingtu', 'star_id': '111', 'uid': '222',
                        'sec_uid': 'xingtu:111',
                    })
            self.assertEqual(self.unexpected_requests, [])
            return result
        finally:
            self.price_release.set()
            await runtime.cleanup()

    def assert_core_fields(self, result, median=350000):
        self.assertTrue(result['identity_verified'], result)
        self.assertEqual(result['star_id'], '111')
        self.assertEqual(result['median_play_30d'], median)
        self.assertEqual(result['audience'], {
            'gender': {'female': 1.0}, 'age': {'24-30': 1.0},
            'region': {'Zhejiang': 1.0},
        })
        for field in ('identity', 'median_play_30d', 'audience.gender',
                      'audience.age', 'audience.region'):
            self.assertNotIn(field, result['missing_fields'])
        self.assertEqual(result['source_url'],
                         'https://www.xingtu.cn/ad/creator/author-homepage/douyin-video/111')

    async def test_optional_contact_exceeding_global_timeout_preserves_core_success(self):
        self.hang_contact = True
        # Scale the 50s outer budget below the 5s contact wait, not its handler.
        result = await self.lookup(timeout=3)
        self.assertTrue(self.contact_started.is_set(), result)
        self.assert_core_fields(result)
        self.assertIsNone(result['contact'].get('value'))
        self.assertEqual(result['status'], 'completed', result)

    async def test_price_response_arriving_during_contact_is_not_lost(self):
        self.delay_price = True
        result = await self.lookup()
        self.assert_core_fields(result)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['contact'].get('value'), 'fixture_business')
        self.assertTrue(self.price_seen.is_set())
        self.assertEqual(result.get('price_1_20'), 1200, result)
        self.assertEqual(result.get('price_21_60'), 2400, result)
        for field in ('price_1_20', 'price_21_60'):
            self.assertEqual(result['field_sources'][field]['source_url'], result['source_url'])

    async def test_cpm_without_detail_median_preserves_search_median(self):
        self.spread_median = None
        result = await self.lookup()
        self.assert_core_fields(result, median=11111)
        self.assertEqual(result['status'], 'completed', result)
        self.assertAlmostEqual(result.get('expected_cpm', -1), 12.34)


if __name__ == '__main__':
    unittest.main()
