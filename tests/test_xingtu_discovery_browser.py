"""Real headless Chromium; synthetic pages, routed traffic, and empty temp state only."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import xingtu_discovery as discovery


MARKET_URL = "https://www.xingtu.cn/ad/creator/market"
SEARCH_URL = "https://www.xingtu.cn/gw/api/gsearch/search_for_author_square"
KEYWORD = "\u7f8e\u98df"
REAL_PLAYWRIGHT = discovery.async_playwright


def search_payload(page=1, *, star_id="star-first", has_more=False):
    return {
        "base_resp": {"status_code": 0},
        "authors": [{"star_id": star_id, "attribute_datas": {
            "id": star_id, "core_user_id": "123456", "nick_name": "Synthetic creator",
            "follower": "25000", "vv_median_30d": "345678",
            "last_10_items": [{"item_id": "post-1", "item_title": "Synthetic food review",
                               "like_cnt": "1200", "comment_cnt": "14"}],
        }}],
        "pagination": {"page": page, "has_more": has_more, "total_count": 2},
    }


HTML = r"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<button id="content-mode" type="button"></button>
<input id="nickname" placeholder="Nickname">
<input id="query" hidden>
<div class="el-pagination"><button class="btn-next" type="button" disabled>Next</button></div>
<script>
const config = __CONFIG__;
const mode = document.querySelector('#content-mode');
const input = document.querySelector('#query');
const nickname = document.querySelector('#nickname');
const next = document.querySelector('.el-pagination .btn-next');
const pagination = document.querySelector('.el-pagination');
pagination.hidden = config.paginationInitiallyHidden;
mode.textContent = '\u5185\u5bb9\u627e\u4eba';
input.placeholder = '\u6309\u5185\u5bb9\u5173\u952e\u8bcd\u627e\u8fbe\u4eba';
let page = 0;
let keyword = '';
mode.addEventListener('click', () => {
  input.hidden = false;
  nickname.hidden = true;
});
async function send(url, body, method = 'POST') {
  // text/plain keeps synthetic cross-origin decoys free of CORS preflights.
  return fetch(url, {method, headers: {'Content-Type': 'text/plain'},
                     ...(method === 'POST' ? {body: JSON.stringify(body)} : {})});
}
async function query(requestedPage) {
  page = requestedPage;
  const body = {search_param: {keyword}, page_param: {page: String(page)}};
  for (const decoy of config.decoys) {
    await send(decoy.url, decoy.body || body, decoy.method || 'POST');
  }
  const response = await send('/gw/api/gsearch/search_for_author_square', body);
  const data = await response.json();
  const renderPagination = () => {
    pagination.hidden = false;
    next.disabled = !data.pagination?.has_more;
  };
  if (config.paginationDelayMs) {
    setTimeout(renderPagination, config.paginationDelayMs);
  } else {
    renderPagination();
  }
}
input.addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    keyword = input.value;
    query(1).catch(() => {});
  }
});
next.addEventListener('click', () => query(page + 1).catch(() => {}));
</script></body></html>"""


class RoutedRuntime:
    """Install network fixtures before the adapter's first real navigation."""

    def __init__(self, handler):
        self.handler = handler
        self.chromium = self
        self.runtime = self.browser = self.context = None
        self.stopped = False

    async def start(self):
        self.runtime = await REAL_PLAYWRIGHT().start()
        return self

    async def launch(self, **kwargs):
        self.browser = await self.runtime.chromium.launch(**kwargs)
        return self

    async def new_context(self, **kwargs):
        self.context = await self.browser.new_context(**kwargs)
        await self.context.route("**/*", self.handler)
        return self.context

    async def close(self):
        await self.browser.close()

    async def stop(self):
        await self.runtime.stop()
        self.stopped = True

    async def cleanup(self):
        if self.browser is not None and self.browser.is_connected():
            await self.browser.close()
        if self.runtime is not None and not self.stopped:
            await self.stop()


class XingtuDiscoveryBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "storage_state.json"
        self.state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        self.fixture = Path(temp.name) / "market.html"
        self.decoys = []
        self.pagination_delay_ms = 0
        self.pagination_initially_hidden = False
        self.responses = {1: (200, search_payload())}
        self.requests = []
        self.unexpected_requests = []
        self.navigation_behavior = "fulfill"
        self.search_behavior = "fulfill"
        self.navigation_started = asyncio.Event()
        self.search_started = asyncio.Event()
        self.runtime = RoutedRuntime(self.route)
        self.addAsyncCleanup(self.runtime.cleanup)
        self.enterContext(patch.object(discovery, "async_playwright", return_value=self.runtime))
        self.adapter = discovery.XingtuDiscovery(state_path=self.state, headless=True)

    async def route(self, route):
        request = route.request
        self.requests.append((request.url, request.method,
                              request.post_data_json if request.post_data else None))
        if request.url == MARKET_URL:
            self.navigation_started.set()
            if self.navigation_behavior == "hold":
                return  # The intercepted navigation stays pending until cancellation.
            if self.navigation_behavior == "abort":
                await route.abort("failed")
                return
            config = {"decoys": self.decoys, "paginationDelayMs": self.pagination_delay_ms,
                      "paginationInitiallyHidden": self.pagination_initially_hidden}
            self.fixture.write_text(HTML.replace("__CONFIG__", json.dumps(config)),
                                    encoding="utf-8")
            await route.fulfill(path=str(self.fixture), content_type="text/html")
            return
        body = request.post_data_json if request.post_data else None
        if request.url == SEARCH_URL and request.method == "POST" and body and (
                body.get("search_param", {}).get("keyword") == KEYWORD
                and body.get("page_param", {}).get("page") in ("1", "2")):
            self.search_started.set()
            if self.search_behavior == "abort":
                await route.abort("failed")
                return
            status, payload = self.responses[int(body["page_param"]["page"])]
            await route.fulfill(status=status, json=payload)
            return
        if any(request.url == decoy["url"] for decoy in self.decoys):
            await route.fulfill(json=search_payload(star_id="unrelated-author"),
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        self.unexpected_requests.append(request.url)
        await route.abort()

    def assert_closed(self):
        self.assertIsNotNone(self.runtime.context)
        self.assertTrue(self.adapter.page.is_closed())
        self.assertEqual(self.runtime.context.pages, [])
        self.assertFalse(self.runtime.browser.is_connected())
        self.assertTrue(self.runtime.stopped, "adapter must stop its Playwright driver")
        self.assertEqual(self.unexpected_requests, [])
        self.assertEqual(self.state.read_text(encoding="utf-8"), '{"cookies": [], "origins": []}')

    async def test_content_mode_and_next_button_drive_two_correlated_pages(self):
        self.responses = {
            1: (200, search_payload(1, has_more=True)),
            2: (200, search_payload(2, star_id="star-second")),
        }
        async with self.adapter as adapter:
            self.assertTrue(await adapter.page.locator("#query").is_hidden())
            first = await adapter.search_page(KEYWORD)
            self.assertTrue(await adapter.page.locator("#nickname").is_hidden())
            self.assertEqual(await adapter.page.locator("#query").input_value(), KEYWORD)
            await adapter.page.wait_for_function(
                "!document.querySelector('.el-pagination .btn-next').disabled")
            second = await adapter.search_page(KEYWORD, 2)
            self.assertEqual((first["page"], second["page"]), (1, 2))
            self.assertEqual([row["star_id"] for row in first["authors"]], ["star-first"])
            self.assertEqual([row["star_id"] for row in second["authors"]], ["star-second"])
            self.assertTrue(first["has_more"])
            self.assertFalse(second["has_more"])
            self.assertEqual(first["authors"][0]["median_play_30d"], 345678)
            self.assertEqual(first["authors"][0]["posts"][0]["statistics"]["digg_count"], 1200)
            self.assertEqual((adapter.keyword, adapter.current_page), (KEYWORD, 2))
        self.assertEqual(self.requests, [
            (MARKET_URL, "GET", None),
            (SEARCH_URL, "POST", {"search_param": {"keyword": KEYWORD}, "page_param": {"page": "1"}}),
            (SEARCH_URL, "POST", {"search_param": {"keyword": KEYWORD}, "page_param": {"page": "2"}}),
        ])
        self.assert_closed()

    async def immediate_second_page_with_delayed_pagination(self):
        self.pagination_delay_ms = 100
        self.responses = {
            1: (200, search_payload(1, has_more=True)),
            2: (200, search_payload(2, star_id="star-second")),
        }
        try:
            async with self.adapter as adapter:
                first = await adapter.search_page(KEYWORD)
                self.assertTrue(first["has_more"])
                self.assertEqual([row["star_id"] for row in first["authors"]], ["star-first"])
                # No readiness wait: production must tolerate the response-to-render gap.
                try:
                    second = await adapter.search_page(KEYWORD, 2)
                except discovery.XingtuDiscoveryError as error:
                    await adapter.page.wait_for_function("""() => {
                        const next = document.querySelector('.el-pagination .btn-next');
                        return next.getClientRects().length > 0 && !next.disabled;
                    }""", timeout=2000)
                    self.fail(f"page 1 has_more=True, but immediate page 2 raised {error.code}; "
                              "the next button became visible/enabled after the 100 ms render delay")
                self.assertEqual(second["page"], 2)
                self.assertEqual([row["star_id"] for row in second["authors"]], ["star-second"])
                self.assertFalse(second["has_more"])
                self.assertEqual((adapter.keyword, adapter.current_page), (KEYWORD, 2))
            self.assertEqual(self.requests, [
                (MARKET_URL, "GET", None),
                (SEARCH_URL, "POST", {"search_param": {"keyword": KEYWORD}, "page_param": {"page": "1"}}),
                (SEARCH_URL, "POST", {"search_param": {"keyword": KEYWORD}, "page_param": {"page": "2"}}),
            ])
        finally:
            self.assert_closed()

    async def test_immediate_page_two_waits_for_pagination_visible_and_enabled_after_100ms(self):
        self.pagination_initially_hidden = True
        await self.immediate_second_page_with_delayed_pagination()

    async def test_immediate_page_two_waits_for_visible_next_button_enabled_after_100ms(self):
        await self.immediate_second_page_with_delayed_pagination()

    async def test_only_matching_keyword_page_post_path_and_official_host_are_accepted(self):
        self.decoys = [
            {"url": SEARCH_URL, "body": {"search_param": {"keyword": "other-query"},
                                         "page_param": {"page": "1"}}},
            {"url": SEARCH_URL, "body": {"search_param": {"keyword": KEYWORD},
                                         "page_param": {"page": "5"}}},
            {"url": SEARCH_URL.replace("www.xingtu.cn", "www.xingtu.cn.evil.invalid")},
            {"url": SEARCH_URL + "/unrelated"},
            {"url": SEARCH_URL, "method": "GET"},
        ]
        async with self.adapter as adapter:
            seen = []
            adapter.page.on("response", lambda response: seen.append(response.url))
            result = await adapter.search_page(KEYWORD)
            self.assertEqual([row["star_id"] for row in result["authors"]], ["star-first"])
            self.assertEqual(seen, [decoy["url"] for decoy in self.decoys] + [SEARCH_URL])
        self.assertEqual([url for url, _, _ in self.requests],
                         [MARKET_URL] + [decoy["url"] for decoy in self.decoys] + [SEARCH_URL])
        self.assert_closed()

    async def test_customer_rights_error_is_preserved_instead_of_empty_or_generic_failure(self):
        self.responses[1] = (200, {
            "base_resp": {"status_code": 10003,
                          "status_message": "\u60a8\u7684\u5ba2\u6237\u6743\u76ca\u7b49\u7ea7\u8fc7\u4f4e\uff0c\u6682\u65f6\u65e0\u6cd5\u8bbf\u95ee\u66f4\u591a\u6570\u636e"},
            "authors": search_payload()["authors"],
        })
        with self.assertRaises(discovery.XingtuDiscoveryError) as error:
            async with self.adapter as adapter:
                await adapter.search_page(KEYWORD)
        self.assertEqual(error.exception.code, "permission_denied")
        self.assertIn("\u6743\u76ca", str(error.exception))
        self.assertEqual((self.adapter.keyword, self.adapter.current_page), (None, 0))
        self.assert_closed()

    async def test_http_forbidden_is_preserved_as_permission_denied(self):
        self.responses[1] = (403, search_payload())
        with self.assertRaises(discovery.XingtuDiscoveryError) as error:
            async with self.adapter as adapter:
                await adapter.search_page(KEYWORD)
        self.assertEqual(error.exception.code, "permission_denied")
        self.assertEqual(self.adapter.current_page, 0)
        self.assert_closed()

    async def test_aborted_navigation_closes_partial_startup(self):
        self.navigation_behavior = "abort"
        with self.assertRaises(discovery.XingtuDiscoveryError) as error:
            async with self.adapter:
                self.fail("aborted navigation must not enter the discovery context")
        self.assertEqual(error.exception.code, "browser_unavailable")
        self.assertEqual(self.requests, [(MARKET_URL, "GET", None)])
        self.assert_closed()

    async def cancel_lookup(self, started):
        async def lookup():
            async with self.adapter as adapter:
                return await adapter.search_page(KEYWORD)

        task = asyncio.create_task(lookup())
        try:
            await asyncio.wait_for(started.wait(), 10)
            self.assertFalse(task.done(), "cancel an in-flight browser operation")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.adapter.current_page, 0)
        self.assert_closed()

    async def test_cancelled_navigation_closes_context_browser_and_driver(self):
        self.navigation_behavior = "hold"
        await self.cancel_lookup(self.navigation_started)

    async def test_cancelled_search_closes_context_browser_and_driver(self):
        self.search_behavior = "abort"
        await self.cancel_lookup(self.search_started)

    async def test_real_response_timeout_closes_context_browser_and_driver(self):
        self.search_behavior = "abort"
        with self.assertRaises(discovery.XingtuDiscoveryError) as error:
            async with self.adapter as adapter:
                await adapter.search_page(KEYWORD)
        self.assertTrue(self.search_started.is_set())
        self.assertEqual(error.exception.code, "search_unavailable")
        self.assertEqual((self.adapter.keyword, self.adapter.current_page), (None, 0))
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
