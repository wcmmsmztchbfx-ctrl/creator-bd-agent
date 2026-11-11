import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    module = importlib.import_module('app.douyin_browser')
except ModuleNotFoundError:
    module = None


class BrowserEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'Shared browser adapter is missing')

    def test_real_user_endpoint_does_not_require_cli_channel_name(self):
        self.assertTrue(module.response_matches(
            'https://www.douyin.com/aweme/v1/web/discover/search/?keyword=food&offset=0&search_channel=aweme_user_web',
            'food', 'user', 0))

    def test_response_is_bound_to_host_keyword_type_and_cursor(self):
        good = 'https://www.douyin.com/aweme/v1/web/discover/search/?keyword=food&offset=0'
        for url, kind, cursor in ((good.replace('www.douyin.com', 'www.douyin.com.evil.invalid'), 'user', 0),
                                  (good.replace('food', 'other'), 'user', 0),
                                  (good, 'video', 0), (good, 'user', 20)):
            self.assertFalse(module.response_matches(url, 'food', kind, cursor))

    def test_nested_challenge_is_not_a_real_empty_result(self):
        payload = {'status_code': 0, 'user_list': [], 'data': [{'search_nil_info': {'search_nil_type': 'verify_check'}}]}
        with self.assertRaises(module.BrowserSearchBlocked):
            module.validate_payload(payload, 'user')

    def test_shell_or_unknown_schema_never_passes(self):
        for payload in ({}, {'status_code': 0}, {'status_code': True, 'user_list': []},
                        {'status_code': 0, 'user_list': 'invalid'},
                        {'status_code': 0, 'user_list': [{'sec_uid': 123}]}):
            with self.assertRaises(RuntimeError):
                module.validate_payload(payload, 'user')

    def test_direct_user_layout_normalizes_stable_ids(self):
        from app.crawler import normalize_search_authors
        users = normalize_search_authors({'data': [{'sec_uid': 'stable-user', 'nickname': 'Account'}]}, 'user')
        self.assertEqual([u['sec_uid'] for u in users], ['stable-user'])

    def test_malformed_account_identifiers_are_not_normalized(self):
        from app.crawler import normalize_search_authors
        for value in (123, True, {'value': 'id'}, ['id'], '  '):
            with self.subTest(value=value):
                self.assertEqual(normalize_search_authors({'user_list': [{'sec_uid': value}]}, 'user'), [])


class SharedContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'Shared browser adapter is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = module.DouyinBrowser(state_path=Path(self.tmp.name) / 'state.json')
        self.adapter.state_path.write_text('{}')
        self.adapter._fingerprint = self.adapter._state_fingerprint()
        self.context = SimpleNamespace(storage_state=AsyncMock(return_value={
            'cookies': [{'name': 'sessionid', 'value': 'synthetic-session', 'domain': '.douyin.com', 'path': '/', 'expires': -1}],
            'origins': []}))
        self.adapter.context = self.context
        self.adapter.page = SimpleNamespace(is_closed=lambda: False, url='https://www.douyin.com/search/food?type=user')
        self.adapter._key = ('food', 'user')

    async def test_verification_retains_context_and_next_search_uses_same_result(self):
        payload = {'status_code': 0, 'user_list': [{'user_info': {'sec_uid': 'stable'}}], 'has_more': 0}
        with patch.object(self.adapter, '_ensure', new=AsyncMock()) as ensure, patch.object(
            self.adapter, '_collect', new=AsyncMock(return_value=payload)) as collect, patch.object(
            self.adapter, '_challenge_visible', new=AsyncMock(return_value=False)):
            result = await self.adapter.verify({'query': 'food', 'search_type': 'user', 'offset': 0})
            self.assertEqual(result['status'], 'authenticated')
            self.adapter.state_path.write_text('{"saved":true}')
            self.adapter.bind_saved_state()
            returned = await self.adapter.search('food', 0, 20, search_type='user')
        self.assertEqual(returned, payload)
        self.assertEqual(collect.await_count, 1, 'Search must consume verified page, not a new CLI/browser')
        self.assertIs(self.adapter.context, self.context)
        self.assertEqual(ensure.await_count, 2)

    async def test_new_challenge_invalidates_cached_success(self):
        self.adapter._cache = {'key': ('food', 'user'), 'offset': 0, 'time': module.time.monotonic(),
                               'payload': {'status_code': 0, 'user_list': [{'sec_uid': 'stable'}]}}
        with patch.object(self.adapter, '_ensure', new=AsyncMock()), patch.object(
            self.adapter, '_challenge_visible', new=AsyncMock(return_value=True)):
            with self.assertRaises(module.BrowserSearchBlocked):
                await self.adapter.search('food', 0, 20, search_type='user')

    async def test_changed_credentials_do_not_reuse_old_browser(self):
        self.adapter.state_path.write_text('{"changed":true}')
        with patch.object(self.adapter, '_close_resources', new=AsyncMock()) as close, patch.object(
            module, 'async_playwright', side_effect=RuntimeError('test-new-context')):
            with self.assertRaises(RuntimeError):
                await self.adapter._ensure()
        self.assertGreaterEqual(close.await_count, 1)

    async def test_equal_metadata_does_not_hide_credential_replacement(self):
        import os
        before = self.adapter._state_fingerprint()
        stat = self.adapter.state_path.stat()
        self.adapter.state_path.write_text('[]')
        os.utime(self.adapter.state_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(self.adapter._state_fingerprint(), before)

    async def test_repeated_cancel_does_not_cancel_resource_cleanup(self):
        import asyncio
        started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def close_browser():
            started.set()
            await release.wait()
        async def stop_runtime():
            stopped.set()
        self.adapter.browser = SimpleNamespace(close=close_browser)
        self.adapter.runtime = SimpleNamespace(stop=stop_runtime)
        job = asyncio.create_task(self.adapter.close())
        await started.wait()
        job.cancel()
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        release.set()
        await asyncio.wait_for(stopped.wait(), 1)
        await self.adapter.close()
        self.assertIsNone(self.adapter.browser)

    async def test_cancelled_verification_does_not_synthesize_success(self):
        import asyncio
        with patch.object(self.adapter, '_ensure', new=AsyncMock()), patch.object(
            self.adapter, '_collect', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.adapter.verify({'query': 'food', 'search_type': 'user'})
        self.assertIsNone(self.adapter._cache)

    async def test_navigation_during_response_decode_invalidates_evidence(self):
        async def decode():
            self.adapter.page.url = 'https://www.douyin.com/search/other?type=user'
            return {'status_code': 0, 'user_list': [{'sec_uid': 'stable'}]}
        self.adapter.timeout = .01
        self.adapter.poll_interval = .001
        self.adapter._responses.put_nowait((self.adapter._generation, SimpleNamespace(
            url='https://www.douyin.com/aweme/v1/web/discover/search/?keyword=food&offset=0',
            status=200, json=decode)))
        with patch.object(self.adapter, '_challenge_visible', new=AsyncMock(return_value=False)):
            with self.assertRaises(RuntimeError):
                await self.adapter._collect('food', 'user', 0)


class VisibleResultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'state.json'
        path.write_text(json.dumps({'cookies': [{'name': 'sessionid', 'value': 'synthetic-session',
            'domain': '.douyin.com', 'path': '/', 'expires': -1}], 'origins': []}))
        self.adapter = module.DouyinBrowser(state_path=path, headless=True, timeout=.1)
        self.adapter.poll_interval = .01
        await self.adapter._ensure()
        self.addAsyncCleanup(self.adapter.close)
        self.requests = []
        self.html = '''<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容" value="food">
          <nav><a href="/user/self">My account</a></nav>
          <main><ul><li><a href="/user/MS4wLjABsynthetic1">First user</a></li>
          <li><a href="/user/MS4wLjABsynthetic2">Second user</a></li></ul></main>'''

        async def route(request):
            self.requests.append(request.request.url)
            if request.request.url.startswith('https://www.douyin.com/search/'):
                await request.fulfill(content_type='text/html', body=self.html)
            else:
                await request.abort()
        await self.adapter.context.route('**/*', route)

    async def test_visible_results_complete_verification_and_are_reused_without_new_window(self):
        state = await self.adapter.verify({'query': 'food', 'search_type': 'user'})
        self.assertEqual(state['status'], 'authenticated')
        page, context = self.adapter.page, self.adapter.context
        result = await self.adapter.search('food', search_type='user')
        self.assertEqual(result['source'], 'browser_visible_user_results')
        self.assertNotIn('has_more', result)
        self.assertEqual(result['pagination_status'], 'unknown')
        self.assertEqual(len(result['user_list']), 2)
        self.assertTrue(all('follower_count' not in row['user_info'] for row in result['user_list']))
        self.assertIs(page, self.adapter.page)
        self.assertIs(context, self.adapter.context)
        self.assertEqual(len(self.requests), 1)

    async def test_overlay_blocks_visible_links_until_human_clears_it(self):
        import asyncio
        self.html += '<div id="captcha">请完成下列验证后继续</div>'
        job = asyncio.create_task(self.adapter.verify({'query': 'food', 'search_type': 'user'}))
        await self.adapter.page.wait_for_url('**/search/**')
        await asyncio.sleep(.05)
        self.assertFalse(job.done())
        # A test fixture simulates the human completion; production never does.
        await self.adapter.page.locator('#captcha').evaluate('e => e.remove()')
        self.assertEqual((await asyncio.wait_for(job, 1))['status'], 'authenticated')

    async def test_query_mismatch_and_navigation_links_do_not_verify(self):
        self.html = '<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容" value="other"><a href="/user/self">Self</a>'
        with self.assertRaises(RuntimeError):
            await self.adapter.search('food', search_type='user')

    async def test_no_captured_next_page_cannot_replay_first_page(self):
        await self.adapter.verify({'query': 'food', 'search_type': 'user'})
        with self.assertRaises(RuntimeError):
            await self.adapter.search('food', offset=20, search_type='user')

    async def test_hidden_or_obscured_links_cannot_verify(self):
        for style in ('visibility:hidden', 'opacity:0'):
            self.html = self.html.replace('<main>', '<main style="' + style + '">')
            with self.subTest(style=style), self.assertRaises(RuntimeError):
                await self.adapter.search('food', search_type='user')
            self.adapter._key = None
        self.html = '''<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容" value="food">
          <ul><li><a href="/user/MS4wLjABone">One</a></li><li><a href="/user/MS4wLjABtwo">Two</a></li></ul>
          <div style="position:fixed;inset:0;background:white">Unrecognized verification overlay</div>'''
        with self.assertRaises(RuntimeError):
            await self.adapter.search('food', search_type='user')

    async def test_manual_navigation_invalidates_verified_cache(self):
        await self.adapter.verify({'query': 'food', 'search_type': 'user'})
        await self.adapter.page.goto('https://www.douyin.com/search/other?type=user')
        self.html = '<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容" value="food">'
        with self.assertRaises(RuntimeError):
            await self.adapter.search('food', search_type='user')

    async def test_same_query_retry_navigates_after_transient_failure(self):
        original = self.html
        self.html = '<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容" value="food">'
        with self.assertRaises(RuntimeError):
            await self.adapter.search('food', search_type='user')
        self.html = original
        result = await self.adapter.search('food', search_type='user')
        self.assertEqual(len(result['user_list']), 2)


if __name__ == '__main__':
    unittest.main()
