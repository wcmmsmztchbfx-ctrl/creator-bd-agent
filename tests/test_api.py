import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db, main


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", os.path.join(self.temp.name, "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.reset_status()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.reset_status)
        # No test can accidentally fall through to the real crawler or model.
        self.crawler = patch.object(main, "execute_bd_search_task", new_callable=AsyncMock)
        self.crawler_mock = self.crawler.start()
        self.addCleanup(self.crawler.stop)
        self.chat = patch.object(main, "parse_and_respond_chat", new_callable=AsyncMock)
        self.chat_mock = self.chat.start()
        self.addCleanup(self.chat.stop)

    @staticmethod
    def reset_status():
        main.current_running_task.clear()
        main.current_running_task.update(
            is_running=False, task_id=None, keyword="", step="", message=""
        )

    def browser_page(self):
        from playwright.sync_api import sync_playwright

        playwright = self.enterContext(sync_playwright())
        browser = playwright.chromium.launch(headless=True)
        self.addCleanup(browser.close)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.evaluate("""() => {
          window.lucide = {createIcons() {}};
          window.requests = [];
          window.fetch = async (url, options = {}) => {
            requests.push({url, options});
            return {ok: true, json: async () => ({messages: [], status: 'queued', task_id: 1})};
          };
        }""")
        page.set_content(main.HTML_CONTENT)
        return page

    def task_row(self, task_id):
        with db.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def set_task_status(self, task_id, status):
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))

    def test_settings_redact_key_and_report_configuration(self):
        db.save_setting("llm_api_key", "test-secret")
        response = self.client.get("/api/settings")
        self.assertNotIn("llm_api_key", response.json()["settings"])
        self.assertTrue(response.json()["settings"]["llm_api_key_configured"])
        self.assertNotIn("test-secret", response.text)

    def test_blank_key_preserves_saved_key_and_explicit_clear_removes_it(self):
        db.save_setting("llm_api_key", "test-secret")
        self.client.post("/api/settings", json={"settings": {"llm_api_key": "  "}})
        self.assertEqual(db.get_setting("llm_api_key"), "test-secret")
        self.client.post("/api/settings", json={"settings": {}, "clear_llm_api_key": True})
        self.assertEqual(db.get_setting("llm_api_key"), "")

    def test_chat_passes_previous_history_before_saving_current_user(self):
        db.save_chat_message("user", "previous")
        db.save_chat_message("assistant", "which category?")
        self.chat_mock.return_value = dict(
            intent="clarify", reply="How many?", task_card=None,
            engine="llm", error=None, prompt_version="v1"
        )
        response = self.client.post("/api/chat", json={"message": "food"})
        self.assertEqual(response.status_code, 200)
        history = self.chat_mock.call_args.kwargs.get("history")
        self.assertIsNotNone(history)
        self.assertEqual([m["content"] for m in history], ["previous", "which category?"])
        self.assertEqual([m["content"] for m in db.get_chat_history()][-2:], ["food", "How many?"])
        self.crawler_mock.assert_not_called()

    def test_chat_error_never_returns_or_persists_executable_card(self):
        self.chat_mock.return_value = dict(
            intent="search", reply="Model unavailable", task_card={"category": "food", "keyword": "food"},
            engine="unavailable", error={"code": "disabled", "message": "Model disabled"}, prompt_version="v1"
        )
        response = self.client.post("/api/chat", json={"message": "food"})
        self.assertIsNone(response.json()["task_card"])
        self.assertIsNotNone(response.json()["error"])
        self.assertFalse((db.get_chat_history()[-1].get("meta") or {}).get("category"))

    def test_chat_exception_is_visible_without_fake_card(self):
        self.chat_mock.side_effect = RuntimeError("provider failure")
        response = self.client.post("/api/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["error"])
        self.assertIsNone(response.json()["task_card"])
        self.assertTrue(response.json()["reply"])

    def test_clear_history_deletes_stored_messages(self):
        db.save_chat_message("user", "private old message")
        self.assertEqual(self.client.delete("/api/chat/history").status_code, 200)
        self.assertEqual(db.get_chat_history(), [])
        welcome = self.client.get("/api/chat/history").json()["messages"][0]["content"]
        self.assertNotIn("自动调用", welcome)

    def test_existing_chat_history_redacts_key_in_content_and_nested_metadata_without_deletion(self):
        secret = "synthetic-legacy-key"
        db.save_setting("llm_api_key", secret)
        db.save_chat_message("user", "legacy pasted key: " + secret)
        db.save_chat_message("assistant", "old response " + secret, {
            "keyword": secret, "content_preferences": [secret],
            "nested": {"llm_api_key": secret, "detail": "legacy " + secret}
        })
        response = self.client.get("/api/chat/history")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.text)
        self.assertEqual(len(response.json()["messages"]), 2)
        self.assertIn("[redacted]", response.json()["messages"][0]["content"])
        stored = db.get_chat_history()
        self.assertEqual(len(stored), 2)
        self.assertIn(secret, stored[0]["content"])
        self.assertEqual(stored[1]["meta"]["nested"]["llm_api_key"], secret)

    def test_prompt_preview(self):
        with patch.object(main, "get_system_prompt", return_value={"content": "fixed prompt", "version": "v1"}, create=True):
            response = self.client.get("/api/chat/prompt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"content": "fixed prompt", "version": "v1"})

    def test_connection_test_uses_saved_or_override_key_without_persisting_or_exposing(self):
        db.save_setting("llm_api_key", "saved-secret")
        with patch.object(main, "test_llm_connection", new_callable=AsyncMock, create=True) as probe:
            probe.return_value = {"ok": True, "message": "Connected", "model": "mock"}
            response = self.client.post("/api/llm/test", json={})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            probe.assert_awaited_once()
            probe.reset_mock()
            probe.return_value = {"ok": False, "message": "bad override-secret", "model": "mock"}
            response = self.client.post("/api/llm/test", json={"llm_api_key": "override-secret", "llm_model": "mock"})
            self.assertNotIn("override-secret", response.text)
            self.assertEqual(probe.call_args.kwargs["overrides"]["llm_api_key"], "override-secret")
        self.assertEqual(db.get_setting("llm_api_key"), "saved-secret")

    def test_execute_validates_all_hard_criteria(self):
        invalid = [
            {"keyword": " "}, {"count": 0}, {"count": 51}, {"count": 2.5}, {"count": True},
            {"min_avg_likes": -1}, {"min_avg_likes": "NaN"}, {"min_avg_likes": "Infinity"},
            {"active_days": 0}, {"active_days": 3651}, {"active_days": 1.5},
            {"content_preferences": "not-a-list"}, {"exclusions": [1]},
        ]
        with patch.object(main.BackgroundTasks, "add_task"):
            for changes in invalid:
                with self.subTest(changes=changes):
                    self.reset_status()
                    response = self.client.post("/api/execute", json={"keyword": "food", **changes})
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(db.get_all_tasks(), [])

    def test_execute_reserves_busy_and_persists_requirements_before_enqueue(self):
        payload = dict(keyword="food", count=7, task_name="exact", category="snacks", min_avg_likes=12000,
                       active_days=21, content_preferences=["reviews"], exclusions=["ads"])

        def queued(*args, **kwargs):
            self.assertTrue(main.current_running_task["is_running"])
            row = self.task_row(main.current_running_task["task_id"])
            self.assertEqual(row["status"], "queued")
            requirements = json.loads(row["requirements_json"])
            for key in ("keyword", "min_avg_likes", "active_days", "content_preferences", "exclusions"):
                self.assertEqual(requirements[key], payload[key])
            self.assertEqual(requirements["sample_count"], 7)

        with patch.object(main.BackgroundTasks, "add_task", side_effect=queued) as enqueue:
            response = self.client.post("/api/execute", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(response.json().get("task_id"))
            self.assertEqual(self.client.post("/api/execute", json={"keyword": "another"}).status_code, 409)
            self.assertEqual(enqueue.call_count, 1)

    def test_enqueue_failure_marks_task_failed_and_releases_busy(self):
        with patch.object(main.BackgroundTasks, "add_task", side_effect=RuntimeError("queue failed")):
            response = self.client.post("/api/execute", json={"keyword": "food"})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.client.get("/api/status").json()["is_running"])
        self.assertEqual(self.task_row(db.get_all_tasks()[0]["id"])["status"], "failed")

    def test_worker_propagates_criteria_and_preserves_partial_status(self):
        async def partial(keyword, count, custom_task_name=None, **kwargs):
            self.set_task_status(kwargs["task_id"], "partial")
            return kwargs["task_id"]

        self.crawler_mock.side_effect = partial
        response = self.client.post("/api/execute", json={"keyword": "food", "count": 4, "min_avg_likes": 9000, "active_days": 12})
        self.assertEqual(response.status_code, 200)
        args = self.crawler_mock.call_args.kwargs
        self.assertEqual(args["min_avg_likes"], 9000)
        self.assertEqual(args["active_days"], 12)
        self.assertEqual(self.task_row(response.json()["task_id"])["status"], "partial")
        self.assertEqual(self.client.get("/api/status").json()["status"], "partial")
        self.assertNotIn("顺利执行完成", db.get_chat_history()[-1]["content"])

    def test_worker_progress_callback_exposes_actual_crawler_progress(self):
        observed = []

        async def progress(keyword, count, **kwargs):
            kwargs["progress_callback"]("synthetic candidate 1", {"saved": 1, "requested": count})
            observed.append(main.api_get_status())
            self.set_task_status(kwargs["task_id"], "completed")
            return kwargs["task_id"]

        self.crawler_mock.side_effect = progress
        self.client.post("/api/execute", json={"keyword": "food"})
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["message"], "synthetic candidate 1")
        self.assertEqual(observed[0]["progress"]["saved"], 1)
        self.assertTrue(observed[0]["is_running"])

    def test_worker_does_not_claim_success_without_completed_db_status(self):
        self.crawler_mock.return_value = 1
        response = self.client.post("/api/execute", json={"keyword": "food"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.task_row(response.json()["task_id"])["status"], "failed")
        self.assertIsNone(self.crawler_mock.call_args.kwargs["active_days"])
        self.assertFalse(self.client.get("/api/status").json()["is_running"])

    def test_worker_exception_is_failed_in_database(self):
        self.crawler_mock.side_effect = RuntimeError("zero candidates")
        response = self.client.post("/api/execute", json={"keyword": "food"})
        self.assertEqual(self.task_row(response.json()["task_id"])["status"], "failed")
        self.assertEqual(self.client.get("/api/status").json()["status"], "failed")

    def test_worker_completed_requires_completed_database_status(self):
        async def completed(keyword, count, **kwargs):
            self.set_task_status(kwargs["task_id"], "completed")
            return kwargs["task_id"]

        self.crawler_mock.side_effect = completed
        response = self.client.post("/api/execute", json={"keyword": "food"})
        self.assertEqual(self.client.get("/api/status").json()["status"], "completed")
        self.assertEqual(self.task_row(response.json()["task_id"])["status"], "completed")

    def test_old_worker_completion_does_not_read_new_tasks_global_status(self):
        first_id = db.save_task("first", "first")
        second_id = db.save_task("second", "second")
        self.set_task_status(second_id, "queued")
        main.current_running_task.update(task_id=first_id, is_running=True)

        async def finish_then_reserve_another(*args, **kwargs):
            self.set_task_status(first_id, "completed")
            main.current_running_task.update(task_id=second_id, is_running=True, status="queued")
            return first_id

        self.crawler_mock.side_effect = finish_then_reserve_another
        asyncio.run(main.background_search_worker(first_id, main.ExecuteTaskRequest(keyword="first")))
        self.assertEqual(db.get_chat_history()[-1]["meta"]["status"], "completed")
        self.assertEqual(main.current_running_task["task_id"], second_id)
        self.assertTrue(main.current_running_task["is_running"])

    def test_status_without_task_id_clears_stale_busy(self):
        main.current_running_task.update(is_running=True, task_id=None)
        self.assertFalse(self.client.get("/api/status").json()["is_running"])

    def test_status_reconciles_stale_busy_from_database(self):
        task_id = db.save_task("old", "food")
        self.set_task_status(task_id, "failed")
        main.current_running_task.update(is_running=True, task_id=task_id)
        response = self.client.get("/api/status")
        self.assertFalse(response.json()["is_running"])
        self.assertEqual(response.json()["status"], "failed")
        self.assertFalse(self.client.get("/api/results").json()["running_status"]["is_running"])

    def test_startup_recovers_interrupted_tasks(self):
        task_id = db.save_task("old", "food")
        self.set_task_status(task_id, "running")
        main.on_startup()
        self.assertEqual(self.task_row(task_id)["status"], "interrupted")

    def test_export_applies_level_and_each_frontend_search_field(self):
        task_id = db.save_task("export", "food", requirements={'keyword':'food'})
        first = db.add_empty_creator_row(task_id)
        second = db.add_empty_creator_row(task_id)
        creators = db.get_task_results(task_id)
        for item in creators:
            item.update(nickname="Other", unique_id="other", category_tag="other", wechat="other", reason="other",
                        recommend_level="淘汰" if item["creator_id"] == second["creator_id"] else "高推荐")
        target = next(item for item in creators if item["creator_id"] == first["creator_id"])
        target.update(nickname="NickMatch", unique_id="IdMatch", category_tag="TagMatch", wechat="WxMatch", reason="ReasonMatch")
        db.batch_save_task_results(task_id, creators)
        main.workflow.admit_report(task_id, [first['creator_id'], second['creator_id']], allow_missing=True)
        path = Path(self.temp.name) / "export.xlsx"
        path.write_bytes(b"test workbook")
        with patch.object(main, "export_task_to_excel", return_value=str(path)) as exporter:
            for search in ("nickmatch", "IDMATCH", "tagmatch", "wxmatch", "reasonmatch"):
                with self.subTest(search=search):
                    response = self.client.get("/api/export", params={"task_id": task_id, "recommend_level": "高推荐", "search": search})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual([c["creator_id"] for c in exporter.call_args.args[2]], [first["creator_id"]])
            exporter.reset_mock()
            response = self.client.get("/api/export", params={"task_id": task_id, "search": "no matches"})
            self.assertEqual(response.status_code, 404)
            exporter.assert_not_called()

    def test_frontend_chat_escaping_and_controls(self):
        html = self.client.get("/").text
        self.assertIn("escapeHtml(msg.content)", html)
        self.assertIn("escapeHtml(tc.keyword)", html)
        self.assertNotIn("triggerExecuteFromChat('${tc.keyword}'", html)
        self.assertIn("taskCards", html)
        self.assertIn("method: 'DELETE'", html)
        self.assertIn("/api/llm/test", html)
        self.assertIn("/api/chat/prompt", html)
        self.assertNotIn("value = s.llm_api_key", html)
        self.assertNotIn("黄金客户 (已解锁)", html)
        self.assertNotIn("当前抖音 Cookie 状态健康正常", html)
        self.assertEqual(html.count("function showToast("), 1)
        self.assertIn("loadingWrapper.remove()", html)
        self.assertIn("await saveAllReportChanges()", html)
        self.assertIn("params.set('recommend_level'", html)
        self.assertIn("params.set('search'", html)

    def test_mobile_settings_cards_do_not_shrink_over_their_content(self):
        page = self.browser_page()
        page.set_viewport_size({"width": 390, "height": 844})
        page.add_style_tag(content="#viewSETTINGS {gap:24px} #viewSETTINGS > .glass-card {padding:20px} #viewSETTINGS .glass-card > div {margin-bottom:20px}")
        page.evaluate("switchTab('SETTINGS')")
        sizes = page.locator('#viewSETTINGS > .glass-card').evaluate_all("cards => cards.map(c => ({client:c.clientHeight, content:c.scrollHeight}))")
        self.assertTrue(sizes)
        self.assertTrue(all(s['content'] <= s['client'] + 2 for s in sizes), sizes)

    def test_browser_chat_treats_html_as_text_and_executes_exact_card(self):
        page = self.browser_page()
        hostile = "<img src=x onerror='window.injected=true'>\"'"
        card = dict(category=hostile, keyword=hostile, sample_count=7, min_avg_likes=3456,
                    active_days=19, content_preferences=[hostile], exclusions=["ads"])
        page.evaluate("""({hostile, card}) => {
          appendChatMessageDOM({role: 'user', content: hostile});
          appendChatMessageDOM({role: 'assistant', content: hostile, meta: card});
        }""", {"hostile": hostile, "card": card})
        self.assertIsNone(page.evaluate("window.injected"))
        self.assertEqual(page.locator("#chatMessages img").count(), 0)
        self.assertIn(hostile, page.locator("#chatMessages").text_content())
        page.locator("#chatMessages button").last.click()
        request = page.evaluate("requests.find(r => r.url === '/api/execute')")
        self.assertIsNotNone(request)
        payload = json.loads(request["options"]["body"])
        for field in ("keyword", "min_avg_likes", "active_days", "content_preferences", "exclusions"):
            self.assertEqual(payload[field], card[field])
        self.assertEqual(payload["count"], card["sample_count"])

    def test_browser_stored_report_and_kb_strings_are_inert_and_copy_uses_closures(self):
        page = self.browser_page()
        hostile = "\"><img data-injected src=x onerror='window.injected=true'>"
        quoted = "');window.injected=true;//"
        creator = dict(creator_id=11, nickname=hostile, unique_id=hostile, category_tag=hostile,
                       reason=hostile, risk=hostile, star_id=hostile, wechat=quoted, phone=hostile,
                       bd_notes=hostile, latest_post_date=hostile, first_seen=hostile,
                       days_since_latest=hostile, avatar_url=hostile, profile_url=hostile,
                       recommend_level="待核实", contacts=[{"contact_type": hostile, "contact_value": quoted}])
        page.evaluate("""creator => {
          window.copied = [];
          window.copyToClipboard = text => copied.push(text);
          reportCreators = [creator];
          renderReportTable();
          renderKBTable([creator]);
        }""", creator)
        self.assertIsNone(page.evaluate("window.injected"))
        self.assertEqual(page.locator("[data-injected]").count(), 0)
        self.assertEqual(page.locator('[onerror], [onfocus], [onmouseover]').count(), 0)
        self.assertIn(hostile, page.locator("#reportTableBody").text_content())
        self.assertIn(hostile, page.locator("#kbTableBody").text_content())
        self.assertEqual(page.locator('#reportTableBody [title]').first.get_attribute("title"), hostile)
        page.locator('#reportTableBody button[title="复制微信"]').dispatch_event("click")
        page.locator('#kbTableBody button[title="复制"]').dispatch_event("click")
        self.assertEqual(page.evaluate("copied"), [quoted, quoted])
        self.assertIsNone(page.evaluate("window.injected"))

    def test_browser_evidence_escapes_all_post_fields_and_uses_safe_urls(self):
        page = self.browser_page()
        hostile = "<img data-injected src=x onerror='window.injected=true'>"
        page.evaluate("""hostile => {
          reportCreators = [{creator_id: 17, nickname: 'evidence', profile_url: 'javascript:window.injected=true',
            avatar_url: 'data:image/svg+xml,<svg onload="window.injected=true"/>',
            sample_posts: [{desc: hostile, post_date: hostile, likes: hostile, comments: hostile}]}];
          renderReportTable();
          document.querySelector('#reportTableBody button[title="查看规则筛选依据与作品"]').click();
        }""", hostile)
        self.assertIsNone(page.evaluate("window.injected"))
        self.assertEqual(page.locator("#modalPostsContainer img").count(), 0)
        self.assertIn(hostile, page.locator("#modalPostsContainer").text_content())
        self.assertNotIn("javascript:", page.locator("#modalProfileLink").get_attribute("href"))
        self.assertNotIn("data:", page.locator("#modalAvatar").get_attribute("src"))

    def test_browser_url_allowlist_and_numeric_ids_block_attribute_and_inline_js_injection(self):
        page = self.browser_page()
        for url in ("javascript:window.injected=true", "data:text/html,<script>alert(1)</script>",
                    "java\nscript:window.injected=true", "\" onmouseover=window.injected=true x=\""):
            with self.subTest(url=url):
                page.evaluate("""url => {
                  const creator = {creator_id: 3, nickname: 'test', profile_url: url, avatar_url: url};
                  reportCreators = [creator];
                  renderReportTable();
                  renderKBTable([creator]);
                }""", url)
                urls = page.locator('#reportTableBody a, #kbTableBody a').evaluate_all("nodes => nodes.map(n => n.getAttribute('href'))")
                self.assertTrue(all(value == "#" or value.startswith(("http://", "https://")) for value in urls))
                self.assertEqual(page.locator('[onmouseover], [onerror]').count(), 0)
        page.evaluate("""() => {
          reportCreators = [{creator_id: '3);window.injected=true;//', nickname: 'bad id'}];
          renderReportTable();
        }""")
        self.assertNotIn("window.injected", page.locator("#reportTableBody").inner_html())
        self.assertIsNone(page.evaluate("window.injected"))
        page.evaluate("""() => {
          reportCreators = [{creator_id: 4, nickname: 'valid', profile_url: 'https://example.com/?q="quoted"&n=1',
            avatar_url: 'http://example.com/avatar.png'}];
          renderReportTable();
        }""")
        self.assertTrue(page.locator("#reportTableBody a").get_attribute("href").startswith("https://example.com/"))
        self.assertEqual(page.locator("#reportTableBody img").get_attribute("src"), "http://example.com/avatar.png")

    def test_browser_search_filtered_evidence_opens_the_clicked_creator(self):
        page = self.browser_page()
        page.evaluate("""() => {
          reportCreators = [
            {creator_id: 21, nickname: 'first unrelated', reason: 'wrong evidence', recommend_level: '高推荐'},
            {creator_id: 42, nickname: 'matched creator', reason: 'correct evidence', recommend_level: '高推荐'}
          ];
          currentReportFilter = '高推荐';
          reportSearchTerm = 'matched';
          renderReportTable();
          document.querySelector('#reportTableBody button[title="查看规则筛选依据与作品"]').click();
        }""")
        self.assertEqual(page.locator("#modalName").text_content(), "matched creator")
        self.assertEqual(page.locator("#modalReason").text_content(), "correct evidence")

    def test_browser_send_failure_always_clears_spinner_and_reenables_button(self):
        page = self.browser_page()
        page.evaluate("""() => {
          window.fetch = () => new Promise((resolve, reject) => {window.rejectChat = reject;});
          document.getElementById('chatInput').value = 'hello';
          void sendChatMessage();
        }""")
        self.assertTrue(page.locator("#chatSendBtn").is_disabled())
        self.assertEqual(page.locator("#chatLoading").count(), 1)
        page.evaluate("window.rejectChat(new Error('synthetic offline failure'))")
        page.wait_for_function("!document.getElementById('chatSendBtn').disabled")
        self.assertEqual(page.locator("#chatLoading").count(), 0)
        self.assertIn("synthetic offline failure", page.locator("#chatMessages [role=alert]").text_content())

    def test_browser_identical_chat_reply_and_error_render_once_with_alert_role(self):
        page = self.browser_page()
        page.evaluate("""() => appendChatMessageDOM({
          role: 'assistant', content: 'synthetic model unavailable',
          error: {message: 'synthetic model unavailable'}
        })""")
        text = page.locator("#chatMessages").text_content()
        self.assertEqual(text.count("synthetic model unavailable"), 1)
        self.assertEqual(page.locator("#chatMessages [role=alert]").count(), 1)

    def test_browser_mobile_top_navigation_keeps_all_views_reachable_and_main_full_width(self):
        page = self.browser_page()
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).flexDirection"), "column")
        for tab in ("CHAT", "KB", "REPORT", "SETTINGS"):
            with self.subTest(tab=tab):
                button = page.locator("#nav" + tab)
                bounds = button.bounding_box()
                self.assertGreaterEqual(bounds["x"], 0)
                self.assertLessEqual(bounds["x"] + bounds["width"], 390)
                self.assertLess(bounds["y"] + bounds["height"], 200)
                button.click()
                self.assertEqual(page.evaluate("currentTab"), tab)
                self.assertTrue(page.locator("#view" + tab).is_visible())
                for other in ("CHAT", "KB", "REPORT", "SETTINGS"):
                    if other != tab:
                        self.assertFalse(page.locator("#view" + other).is_visible())
                main_box = page.locator("main").bounding_box()
                self.assertGreaterEqual(main_box["width"], 350)
                self.assertLessEqual(main_box["x"] + main_box["width"], 390)
                self.assertEqual(page.locator("main").evaluate("node => getComputedStyle(node).minWidth"), "0px")
                self.assertEqual(page.locator("main").evaluate("node => getComputedStyle(node).minHeight"), "0px")
                self.assertEqual(page.locator("#view" + tab).evaluate("node => getComputedStyle(node).paddingLeft"), "12px")
        page.locator("#navREPORT").click()
        self.assertGreater(page.locator("#excelSheetContainer").evaluate("node => node.scrollWidth"),
                           page.locator("#excelSheetContainer").evaluate("node => node.clientWidth"))
        page.locator("#excelSheetContainer").evaluate("node => node.scrollLeft = 200")
        self.assertGreater(page.locator("#excelSheetContainer").evaluate("node => node.scrollLeft"), 0)

    def test_browser_export_saves_first_and_aborts_when_save_fails(self):
        page = self.browser_page()
        page.evaluate("""async () => {
          currentSelectedTaskId = 7;
          currentReportFilter = '高推荐';
          reportSearchTerm = 'wx match';
          reportCreators = [{creator_id: 1, nickname: 'edited name'}];
          hasUnsavedChanges = true;
          window.renderReportTable = () => {};
          window.requests = [];
          window.downloaded = false;
          URL.createObjectURL = () => 'blob:test';
          URL.revokeObjectURL = () => {};
          HTMLAnchorElement.prototype.click = function() { window.downloaded = true; };
          window.fetch = async (url, options = {}) => {
            requests.push({url, options});
            return {ok: true, json: async () => ({status: 'ok'}), blob: async () => new Blob(['synthetic'])};
          };
          await downloadCurrentReportExcel();
        }""")
        requests = page.evaluate("requests")
        self.assertEqual(requests[0]["url"], "/api/results/batch_save")
        self.assertIn("recommend_level=", requests[1]["url"])
        self.assertIn("search=wx+match", requests[1]["url"])
        self.assertIn("task_id=7", requests[1]["url"])
        self.assertTrue(page.evaluate("downloaded"))
        page.evaluate("""async () => {
          hasUnsavedChanges = true;
          window.requests = [];
          window.downloaded = false;
          window.fetch = async (url, options = {}) => {
            requests.push({url, options});
            return {ok: false, json: async () => ({error: 'save denied'})};
          };
          await downloadCurrentReportExcel();
        }""")
        self.assertEqual(len(page.evaluate("requests")), 1)
        self.assertFalse(page.evaluate("downloaded"))
        self.assertTrue(page.evaluate("hasUnsavedChanges"))

    def test_browser_settings_preview_and_connection_test_never_refill_key(self):
        page = self.browser_page()
        page.evaluate("""async () => {
          window.fetch = async (url, options = {}) => {
            requests.push({url, options});
            if (url === '/api/settings') return {ok: true, json: async () => ({settings: {llm_api_key_configured: true}})};
            if (url === '/api/chat/prompt') return {ok: true, json: async () => ({version: 'v-test', content: '<img onerror=x>'})};
            return {ok: false, json: async () => ({ok: false, message: 'synthetic authentication failure'})};
          };
          await loadSettings();
          await previewSystemPrompt();
          await testLlmConnection();
        }""")
        self.assertEqual(page.locator("#cfgLlmApiKey").input_value(), "")
        self.assertIn("已配置", page.locator("#cfgLlmApiKey").get_attribute("placeholder"))
        self.assertIn("<img onerror=x>", page.locator("#systemPromptPreview").text_content())
        self.assertEqual(page.locator("#systemPromptPreview img").count(), 0)
        self.assertIn("synthetic authentication failure", page.locator("#llmTestStatus").text_content())
        self.assertFalse(page.locator("#btnTestLlm").is_disabled())
        payload = json.loads(page.evaluate("requests.find(r => r.url === '/api/llm/test').options.body"))
        self.assertNotIn("llm_api_key", payload)

    def test_browser_report_missing_metrics_stay_unknown_on_focus_and_blur(self):
        page = self.browser_page()
        page.evaluate("""() => {
          reportCreators = [{creator_id: 1, nickname: 'unknown metrics', avg_likes: null, max_likes: null,
            followers: null, price_1_20: null, price_21_60: null, median_play_30d: null, expected_cpm: null,
            recommend_level: '待核实', content_preferences: [], exclusions: []}];
          renderReportTable();
        }""")
        average = page.locator('[onblur*="\'avg_likes\'"]')
        self.assertEqual(average.text_content(), "待核实")
        average.dispatch_event("blur")
        self.assertIsNone(page.evaluate("reportCreators[0].avg_likes"))
        for field in ("followers", "price_1_20", "price_21_60", "median_play_30d", "max_likes"):
            with self.subTest(field=field):
                cell = page.locator(f'[onblur*="\'{field}\'"]')
                self.assertEqual(cell.text_content(), "待核实")
                cell.dispatch_event("blur")
                self.assertIsNone(page.evaluate(f"reportCreators[0].{field}"))


if __name__ == "__main__":
    unittest.main()
