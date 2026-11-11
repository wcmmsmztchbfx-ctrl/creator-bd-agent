"""Synthetic fixtures only: never launch a browser or read real login state."""

import asyncio
import copy
import importlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    xingtu = importlib.import_module("app.xingtu")
except ModuleNotFoundError as exc:
    if exc.name != "app.xingtu":
        raise
    xingtu = None


CREATOR = {"unique_id": "synthetic-douyin", "sec_uid": "synthetic-sec", "nickname": "Same name"}
SEARCH_URL = "https://www.xingtu.cn/gw/api/gsearch/search_for_author_square"


def scratch_author(**overrides):
    # Layout observed in scratch/search_author_xingtu.py; not a live capture.
    attrs = {"nick_name": "Same name", "short_id": "synthetic-douyin",
             "author_id": "synthetic-star", "follower": "12345",
             "item_vv_median_30d": "12000", "cpm_video": "80"}
    attrs.update(overrides)
    return {"attribute_datas": attrs}


class Response:
    def __init__(self, payload, status=200, url=SEARCH_URL, keyword="synthetic-douyin"):
        self.status, self.url, self.payload = status, url, payload
        self.request = SimpleNamespace(method="POST", post_data_json={
            "search_param": {"keyword": keyword}, "page_param": {"page": "1"}})

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)


class PendingResponse:
    def __init__(self, page, predicate):
        self.page, self.predicate = page, predicate
        self.value = asyncio.get_running_loop().create_future()

    async def __aenter__(self):
        self.page.pending = self
        return self

    async def __aexit__(self, *args):
        if args[0] is None:
            await asyncio.wait_for(asyncio.shield(self.value), 0.03)
        if not self.value.done():
            self.value.cancel()


class BrowserStub:
    """Stub only the external Playwright boundary, including request correlation."""
    def __init__(self, response, *, login=False, blocked=False, no_input=False,
                 hang=False, error_at=None, extra_responses=()):
        self.response = response
        self.login, self.blocked, self.no_input = login, blocked, no_input
        self.hang, self.error_at = hang, error_at
        self.extra_responses = extra_responses
        self.url = "https://www.xingtu.cn/ad/creator/market"
        self.closed, self.actions, self.timeouts = [], [], []
        self.chromium = self
        self.first = self

    async def start(self):
        return self

    async def stop(self):
        self.closed.append("playwright")

    async def launch(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        if self.error_at == "launch":
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /synthetic/private/path")
        return self

    async def new_context(self, **kwargs):
        self.state_path = kwargs["storage_state"]
        if self.error_at == "context":
            raise RuntimeError("synthetic-secret exception")
        return SimpleNamespace(new_page=self.new_page, close=self.close_context)

    async def close_context(self):
        self.closed.append("context")
        if self.error_at == "close":
            raise RuntimeError("synthetic-secret cleanup")

    async def close(self):
        self.closed.append("browser")

    async def new_page(self):
        return self

    def set_default_timeout(self, timeout):
        self.timeouts.append(timeout)

    async def goto(self, url, **kwargs):
        self.actions.append(("goto", url))
        self.timeouts.append(kwargs.get("timeout"))
        if self.hang:
            await asyncio.Future()
        if self.login:
            self.url = "https://www.xingtu.cn/login?token=synthetic-secret"
        return SimpleNamespace(status=200)

    def locator(self, selector):
        self.selector = selector
        return self

    async def count(self):
        return 0 if self.no_input else 1

    async def is_visible(self):
        return self.blocked

    async def fill(self, value):
        self.actions.append(("fill", value))

    async def wait_for(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        if self.no_input:
            raise TimeoutError("synthetic missing input")

    async def press(self, key):
        self.actions.append(("press", key))
        for response in (*self.extra_responses, self.response):
            if self.pending.predicate(response) and not self.pending.value.done():
                self.pending.value.set_result(response)

    def expect_response(self, predicate, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return PendingResponse(self, predicate)


class XingtuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(xingtu, "app.xingtu adapter is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "storage_state.json"
        self.state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(xingtu, "STORAGE_STATE", self.state).start()

    async def enrich(self, payload=None, creator=None, **options):
        response = options.pop("response", None) or Response(payload)
        stub = BrowserStub(response, **options)
        with patch.object(xingtu, "async_playwright", return_value=stub):
            result = await xingtu.enrich_creator(copy.deepcopy(CREATOR if creator is None else creator))
        self.assertEqual(stub.closed, ["context", "browser", "playwright"])
        self.assertTrue(all(isinstance(t, (int, float)) and 0 < t <= 15000 for t in stub.timeouts))
        self.assertEqual(result["source_url"], "https://www.xingtu.cn/ad/creator/market")
        self.assertIsNotNone(datetime.fromisoformat(result["captured_at"]).tzinfo)
        return result, stub

    async def test_missing_state_is_unavailable_without_starting_browser(self):
        self.state.unlink()
        with patch.object(xingtu, "async_playwright", side_effect=AssertionError("must not launch")):
            result = await xingtu.enrich_creator(CREATOR)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["identity_verified"])
        self.assertIsNone(result["median_play_30d"])
        self.assertEqual(result["audience"], {"gender": None, "age": None, "region": None})
        self.assertIn("identity", result["missing_fields"])

    async def test_scratch_layout_matches_short_id_but_missing_audience_prevents_completion(self):
        result, stub = await self.enrich({"authors": [scratch_author()]})
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["star_id"], "synthetic-star")
        self.assertEqual(result["median_play_30d"], 12000)
        self.assertEqual(result["missing_fields"], ["audience.gender", "audience.age", "audience.region"])
        self.assertEqual(result["field_sources"]["median_play_30d"]["field"],
                         "authors[].attribute_datas.item_vv_median_30d")
        self.assertEqual(stub.state_path, str(self.state))
        self.assertEqual(stub.actions[1:], [("fill", "synthetic-douyin"), ("press", "Enter")])

    async def test_documented_legacy_layout_median_and_prices_keep_field_provenance(self):
        author = {"author_id": "synthetic-star", "attribute_datas": {"short_id": "synthetic-douyin"},
                  "base_info": {"nick_name": "Same name"},
                  "hot_item_info": {"median_play_over_30d": "32100", "expected_play_num": 999999},
                  "price_info": [{"video_type": 1, "price": "0"}, {"video_type": 2, "price": "1800"}],
                  "item_list": [{"vv": 999999}]}
        result, _ = await self.enrich({"authors": [author]})
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["median_play_30d"], 32100)
        self.assertEqual((result["price_1_20"], result["price_21_60"]), (0, 1800))
        self.assertEqual(result["field_sources"]["price_21_60"]["permission"], "session_visible")
        self.assertNotEqual(result["status"], "completed")

    async def test_nickname_or_star_id_alone_never_verifies_douyin_identity(self):
        for attrs in ({"short_id": "someone-else"}, {"short_id": None},
                      {"short_id": "synthetic-star"}):
            with self.subTest(attrs=attrs):
                result, _ = await self.enrich({"authors": [scratch_author(**attrs)]})
                self.assertEqual(result["status"], "identity_unconfirmed")
                self.assertFalse(result["identity_verified"])
                self.assertIsNone(result["median_play_30d"])

    async def test_sec_uid_can_verify_but_conflicting_identity_evidence_cannot(self):
        for short_id, expected in ((None, True), ("someone-else", False)):
            result, _ = await self.enrich({"authors": [scratch_author(short_id=short_id, sec_uid="synthetic-sec")]})
            self.assertEqual(result["identity_verified"], expected)
        result, _ = await self.enrich({"authors": [scratch_author(short_id=None, sec_uid="synthetic-sec")]},
                                      creator={"sec_uid": "synthetic-sec"},
                                      response=Response({"authors": [scratch_author(short_id=None, sec_uid="synthetic-sec")]},
                                                        keyword="synthetic-sec"))
        self.assertTrue(result["identity_verified"])

    async def test_ambiguous_matches_and_conflicting_star_ids_are_not_accepted(self):
        for authors in ([scratch_author(), scratch_author(author_id="other-star")],
                        [{**scratch_author(), "author_id": "conflicting-star"}],
                        [scratch_author(author_id=None)]):
            result, _ = await self.enrich({"authors": authors})
            self.assertEqual(result["status"], "identity_unconfirmed")
            self.assertFalse(result["identity_verified"])

    async def test_zero_is_real_but_unknown_metrics_never_become_zero_or_expected_plays(self):
        for raw, expected in ((0, 0), ("0", 0), ("3.5", 3.5), (None, None), ("", None),
                              (True, None), (-1, None), ("NaN", None), ("Infinity", None),
                              ("1.2\u4e07", None), ({"value": 500}, None)):
            with self.subTest(raw=raw):
                author = scratch_author(item_vv_median_30d=raw)
                author["hot_item_info"] = {"expected_play_num": 999999}
                result, _ = await self.enrich({"authors": [author]})
                self.assertEqual(result["median_play_30d"], expected)
                self.assertEqual("median_play_30d" in result["missing_fields"], expected is None)
                self.assertNotEqual(result["status"], "completed")

    async def test_fallback_median_key_is_recognized(self):
        result, _ = await self.enrich({"authors": [scratch_author(item_vv_median_30d=None, vv_median_30d="450")]})
        self.assertEqual(result["median_play_30d"], 450)

    async def test_unknown_audience_and_contacts_are_not_promoted_from_raw_payload(self):
        author = scratch_author()
        author.update(audience={"gender": {"female": 1}, "age": [1], "region": [1]},
                      contact={"phone": "synthetic-contact"})
        result, _ = await self.enrich({"authors": [author]})
        self.assertEqual(result["audience"], {"gender": None, "age": None, "region": None})
        self.assertEqual(result["contact"]["status"], "\u672a\u83b7\u53d6")
        self.assertIsNone(result["contact"]["value"])
        self.assertNotIn("synthetic-contact", json.dumps(result))

    async def test_empty_authors_is_not_found_but_unknown_or_malformed_schema_is_failed(self):
        for payload, expected in (({"authors": []}, "not_found"), ({}, "failed"),
                                  ({"authors": None}, "failed"), ({"authors": {}}, "failed"),
                                  ({"authors": [None]}, "failed"), ([], "failed")):
            result, _ = await self.enrich(payload)
            self.assertEqual(result["status"], expected)

    async def test_permission_and_login_failures_override_even_populated_authors(self):
        for response in (Response({"authors": [scratch_author()]}, status=401),
                         Response({"authors": []}, status=403), Response({}, status=429),
                         Response({"base_resp": {"status_code": 403, "status_message": "permission denied"},
                                   "authors": [scratch_author()]}),
                         Response({"code": 1001, "message": "\u8bf7\u767b\u5f55"})):
            result, _ = await self.enrich(response=response)
            self.assertEqual(result["status"], "permission_denied")
            self.assertFalse(result["identity_verified"])

    async def test_unknown_api_errors_http_errors_and_invalid_json_are_failed(self):
        for response in (Response({}, status=500), Response(ValueError("synthetic-secret")),
                         Response({"code": 99, "message": "synthetic-secret", "authors": []})):
            result, _ = await self.enrich(response=response)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("synthetic-secret", json.dumps(result))

    async def test_login_redirect_and_visible_challenge_stop_before_search(self):
        for options in ({"login": True}, {"blocked": True}):
            result, stub = await self.enrich(**options)
            self.assertEqual(result["status"], "permission_denied")
            self.assertEqual(len(stub.actions), 1)
            self.assertNotIn("synthetic-secret", json.dumps(result))

    async def test_missing_search_input_reports_unavailable_not_no_results(self):
        result, _ = await self.enrich(no_input=True)
        self.assertEqual(result["status"], "unavailable")

    async def test_unrelated_initial_search_and_lookalike_hosts_are_ignored(self):
        wrongs = (Response({"authors": []}, keyword="other-query"),
                  Response({"authors": []}, url=SEARCH_URL.replace("www.xingtu.cn", "www.xingtu.cn.evil.invalid")))
        result, _ = await self.enrich({"authors": [scratch_author()]}, extra_responses=wrongs)
        self.assertTrue(result["identity_verified"])
        result, _ = await self.enrich(response=wrongs[0])
        self.assertEqual(result["status"], "unavailable")

    async def test_bounded_navigation_timeout_cleans_up_owned_resources(self):
        with patch.object(xingtu, "LOOKUP_TIMEOUT_SECONDS", 0.02):
            result, _ = await asyncio.wait_for(self.enrich(hang=True), 0.5)
        self.assertEqual(result["status"], "unavailable")

    async def test_partial_startup_and_cleanup_exceptions_do_not_leak_or_mask_status(self):
        stub = BrowserStub(Response({}), error_at="context")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(xingtu, "async_playwright", return_value=stub):
            result = await xingtu.enrich_creator(CREATOR)
        self.assertEqual(stub.closed, ["browser", "playwright"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        result, _ = await self.enrich({"authors": []}, error_at="close")
        self.assertEqual(result["status"], "not_found")

    async def test_missing_browser_executable_is_unavailable_and_driver_is_stopped(self):
        stub = BrowserStub(Response({}), error_at="launch")
        with patch.object(xingtu, "async_playwright", return_value=stub):
            result = await xingtu.enrich_creator(CREATOR)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(stub.closed, ["playwright"])
        self.assertNotIn("/synthetic/private/path", json.dumps(result))

    async def test_cleanup_hang_is_bounded_and_other_resources_still_close(self):
        stub = BrowserStub(Response({"authors": []}))
        async def hang_close():
            stub.closed.append("context")
            await asyncio.Future()
        stub.close_context = hang_close
        with patch.object(xingtu, "async_playwright", return_value=stub), patch.object(
            xingtu, "CLEANUP_TIMEOUT_SECONDS", 0.01
        ):
            result = await asyncio.wait_for(xingtu.enrich_creator(CREATOR), 0.5)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(stub.closed, ["context", "browser", "playwright"])

    async def test_cancellation_propagates_after_cleanup(self):
        stub = BrowserStub(Response({}), hang=True)
        with patch.object(xingtu, "async_playwright", return_value=stub):
            task = asyncio.create_task(xingtu.enrich_creator(CREATOR))
            for _ in range(20):
                await asyncio.sleep(0)
                if stub.actions:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stub.closed, ["context", "browser", "playwright"])

    async def test_nickname_only_input_does_not_trigger_lookup(self):
        with patch.object(xingtu, "async_playwright", side_effect=AssertionError("must not launch")):
            result = await xingtu.enrich_creator({"nickname": "Same name", "star_id": "synthetic-star"})
        self.assertEqual(result["status"], "identity_unconfirmed")
        self.assertFalse(result["identity_verified"])


class OfflineBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_with_temp_html_and_all_network_requests_stubbed(self):
        self.assertIsNotNone(xingtu, "app.xingtu adapter is not implemented")
        if xingtu.async_playwright is None:
            self.skipTest("Playwright is not installed")
        real_factory = xingtu.async_playwright
        requests = []
        html = """<!doctype html><html><head><meta charset="utf-8"></head><body>
        <script>
        setTimeout(() => {
          const input = document.createElement('input');
          input.placeholder = '\u8bf7\u8f93\u5165\u8fbe\u4eba';
          document.body.append(input);
        }, 80);
        document.addEventListener('keydown', async event => {
          if (event.key !== 'Enter') return;
          await fetch('/gw/api/gsearch/search_for_author_square', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({search_param: {keyword: event.target.value}, page_param: {page: '1'}})
          });
        });
        </script></body></html>"""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "storage_state.json"
            state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            fixture = Path(tmp) / "market.html"
            fixture.write_text(html, encoding="utf-8")

            async def route(request_route):
                url = request_route.request.url
                requests.append(url)
                if url == "https://www.xingtu.cn/ad/creator/market":
                    await request_route.fulfill(path=str(fixture), content_type="text/html")
                elif url == SEARCH_URL:
                    await request_route.fulfill(json={"authors": [scratch_author()]})
                else:
                    await request_route.abort()

            class LocalBrowser:
                chromium = None

                async def start(self):
                    self.runtime = await real_factory().start()
                    self.chromium = self
                    return self

                async def launch(self, **kwargs):
                    self.browser = await self.runtime.chromium.launch(**kwargs)
                    return self

                async def new_context(self, **kwargs):
                    context = await self.browser.new_context(**kwargs)
                    await context.route("**/*", route)
                    return context

                async def close(self):
                    await self.browser.close()

                async def stop(self):
                    await self.runtime.stop()

            with patch.object(xingtu, "STORAGE_STATE", state), patch.object(
                xingtu, "async_playwright", return_value=LocalBrowser()
            ):
                result = await xingtu.enrich_creator(CREATOR)
        self.assertTrue(result["identity_verified"], result["reason"])
        self.assertEqual(result["median_play_30d"], 12000)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(requests, ["https://www.xingtu.cn/ad/creator/market", SEARCH_URL])


if __name__ == "__main__":
    unittest.main()
