"""Offline browser contracts for candidate selection, KB reuse and formal reports."""
import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


UI = Path(__file__).resolve().parents[1] / "app" / "ui.html"


class CandidateUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1800)
        self.page.route("**/*", lambda route: route.abort())
        self.page.evaluate("""() => {
          window.lucide = {createIcons() {}};
          window.requests = [];
          window.task = {id: 7, status: 'completed', keyword: '食品', elapsed_seconds: 125,
            started_at: '2026-11-05T01:00:00Z', finished_at: '2026-11-05T01:02:05Z',
            counts: {candidates: 9, discovered: 9, eligible: 2, pending: 1, rejected: 0,
              prequalified: 2, topic_reviewed: 2, selected: 0, enriched: 0,
              report: 0, qualified: 0, target: 5}, progress: {}};
          const assessment = (status, numeric, topic) => ({
            id: 1, overall_status: status, candidate_status: status,
            numeric: {status: numeric, failed_fields: [], missing_fields: [],
              reason_codes: [], details: []},
            topic: {topic_status: topic, method: 'text_llm',
              posts: topic === 'matched' ? Array.from({length: 6}, (_, i) => ({
                aweme_id: 'p' + (i + 1), decision: 'yes', evidence_id: 'p' + (i + 1)})) : [],
              account: {account_type: topic === 'matched' ? 'independent_creator' : 'unknown',
                evidence_ids: topic === 'matched' ? ['p1', 'p2'] : [],
                reason: topic === 'matched' ? 'Synthetic personal creator across posts' : ''},
              creator_pattern: topic === 'matched' ? 'consistent_creator' : 'uncertain',
              sample_count: topic === 'matched' ? 6 : 0,
              matched_count: topic === 'matched' ? 6 : 0, unknown_count: 0,
              evidence_ids: topic === 'matched' ? ['p1', 'p2'] : [], reason: '合成方向审核',
              prompt_version: 'fixture', model: 'fixture-model', error_code: null,
              truncated_ids: [], ratio: topic === 'matched' ? 1 : null,
              min_samples: 5, min_ratio: 0.6},
            assessment: {overall_status: status, reason: '合成审核说明'},
            topic_reviewed: true});
          window.creators = [
            {creator_id: 11, nickname: '候选甲', followers: 20000, avg_likes: 1234,
             max_likes: 5678, reason: '粉丝量达标', updated_at: '2026-11-05',
             latest_post_date: '2026-10-31', captured_at: '2026-11-04',
             profile_url: 'https://example.com/11', tags: ['待核实'],
             candidate_status: 'eligible', assessment: assessment('eligible', 'passed', 'matched'),
             discovery_sources: [{search_type: 'user', query: '食品', discovered_at: '2026-11-05 10:00:00'}],
             sample_posts: [{desc: '作品证据', url: 'https://example.com/video', likes: 3}]},
            {creator_id: 12, nickname: '候选乙', followers: 30000, avg_likes: 4000,
             max_likes: 9000, reason: '数值通过', candidate_status: 'eligible',
             assessment: assessment('eligible', 'passed', 'matched'), tags: [],
             sample_posts: []},
            {creator_id: 13, nickname: '候选丙', followers: null, avg_likes: null,
             max_likes: null, missing_fields: ['followers'], tags: [],
             candidate_status: 'pending',
             assessment: assessment('pending', 'unknown', 'unknown'),
             sample_posts: []}];
          window.history = [];
          window.report = [];
          window.importResult = {task_id: 7, admitted: [11], pending: [12]};
          window.fetch = async (url, options = {}) => {
            requests.push({url: String(url), method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null});
            if (window.offline) throw Error('offline');
            const path = String(url).split('?')[0];
            let data = {};
            if (path === '/api/chat/history') data = {messages: window.history};
            else if (path === '/api/status') data = {task: window.task, is_running: false};
            else if (path.endsWith('/candidates')) data = {task: window.task, creators: window.creators,
              summary: window.task.counts};
            else if (path.endsWith('/selection')) {
              const body = JSON.parse(options.body);
              window.creators.forEach(c => { c.selected = body.creator_ids.includes(c.creator_id);
                c.excluded = body.excluded_ids.includes(c.creator_id); });
              window.task.counts.selected = body.creator_ids.length;
              data = {status: 'saved'};
            }
            else if (path.endsWith('/stop')) {
              window.task = {...window.task, stop_requested: true}; data = {task: window.task};
            } else if (path.endsWith('/enrich')) {
              window.task = {...window.task, status: 'enriching', finished_at: null};
              data = {status: 'queued', task_id: 7};
            } else if (path.endsWith('/report')) data = {admitted: [11], pending: []};
            else if (path === '/api/kb/import') data = window.importResult;
            else if (path === '/api/kb') data = {creators: window.creators};
            else if (path === '/api/tasks') data = {tasks: [window.task]};
            else if (path === '/api/results') data = {creators: window.report,
              tasks: [window.task], selected_task: window.task};
            else if (path === '/api/settings') data = {settings: {dynamic_ttl_hours: '48',
              llm_api_key_configured: true}};
            else if (path === '/api/execute') data = {status: 'queued', task_id: 7};
            else if (path === '/api/results/batch_save') data = {status: 'ok'};
            return {ok: !window.failRequests, json: async () => data};
          };
        }""")
        # No CDN, font or platform request can leave this fixture.
        html = re.sub(r'<script src="[^"]+"></script>', '', UI.read_text(encoding="utf-8"))
        html = re.sub(r"@import url\([^;]+;", '', html)
        self.page.set_content(html)

    def open_drawer(self):
        self.page.evaluate("openCandidateDrawer(7)")

    def requests(self, suffix):
        return self.page.evaluate("s => requests.filter(r => r.url.split('?')[0].endsWith(s))", suffix)

    def open_detail(self, creator_id):
        self.page.locator(f'[data-candidate-detail="{creator_id}"]').first.click()
        self.page.wait_for_selector('#candidateDetailBack')

    def filter_candidates(self, status):
        self.page.locator(f'[data-candidate-filter="{status}"]').click()

    def test_new_and_historical_cards_submit_xingtu_without_source_controls(self):
        for mode, label in [('xingtu', '星图内容检索'), ('user', '用户搜索优先'),
                            ('video', '仅视频搜索'), ('hybrid', '用户搜索 + 视频补充'),
                            (None, '星图内容检索')]:
            with self.subTest(mode=mode):
                self.page.evaluate("""mode => renderChatMessages([{role: 'assistant', content: '请确认',
                  meta: {category: '食品', keyword: '控糖零食', sample_count: 7,
                    discovery_mode: mode, search_queries: ['控糖零食', '零食分享'],
                    min_avg_likes: 100, min_followers: 2000, max_followers: 9000,
                    min_max_likes: 300, active_days: 14, content_preferences: ['实测'],
                    exclusions: ['直播切片']}}])""", mode)
                chat = self.page.locator('#chatMessages')
                self.assertIn(label, chat.inner_text())
                self.assertIn('本次执行使用星图内容检索', chat.inner_text())
                self.assertEqual(chat.locator('select, input[type=radio]').count(), 0)
                chat.locator('[data-execute-card]').click()
                body = self.requests('/api/execute')[-1]['body']
                self.assertEqual(body, {'keyword': '控糖零食', 'count': 7, 'category': '食品',
                    'min_avg_likes': 100, 'active_days': 14, 'min_followers': 2000,
                    'max_followers': 9000, 'min_max_likes': 300, 'content_preferences': ['实测'],
                    'exclusions': ['直播切片'], 'discovery_mode': 'xingtu',
                    'search_queries': ['控糖零食', '零食分享'], 'topic_min_samples': 5,
                    'topic_min_ratio': 0.6})

    def test_xingtu_candidate_source_actual_profile_and_unknown_douyin_id(self):
        self.page.evaluate("""() => {
          Object.assign(creators[0], {sec_uid: 'xingtu:123', unique_id: '', source_platform: 'xingtu',
            profile_url: 'https://www.xingtu.cn/ad/creator/author-homepage/douyin/123',
            discovery_sources: [{search_type: 'xingtu', query: '控糖零食'}]});
        }""")
        self.open_drawer()
        self.assertIn('星图内容检索', self.page.locator('[data-creator-id="11"]').inner_text())
        self.open_detail(11)
        detail = self.page.locator('#candidateDetail')
        self.assertIn('星图内容检索', detail.inner_text())
        self.assertNotIn('xingtu:123', detail.inner_text())
        self.assertEqual(detail.get_by_role('link', name='打开主页').get_attribute('href'),
                         'https://www.xingtu.cn/ad/creator/author-homepage/douyin/123')

    def test_xingtu_detail_missing_profile_and_handle_are_noninteractive_unknowns(self):
        for profile_url in (None, '', ' ', '#', 'javascript:alert(1)'):
            with self.subTest(profile_url=profile_url):
                self.page.evaluate("""url => {
                  closeCandidateDrawer();
                  Object.assign(creators[0], {sec_uid: 'xingtu:123', unique_id: null,
                    source_platform: 'xingtu', profile_url: url});
                }""", profile_url)
                self.open_drawer()
                self.open_detail(11)
                detail = self.page.locator('#candidateDetail')
                profile = detail.locator('dl > div').filter(has=self.page.locator('dt', has_text='主页'))
                handle = detail.locator('dl > div').filter(has=self.page.locator('dt', has_text='抖音号'))
                self.assertEqual(profile.locator('dd').inner_text(), '未获取')
                self.assertEqual(profile.locator('dd > span').inner_text(), '未获取')
                self.assertEqual(profile.locator('a, button, [tabindex]').count(), 0)
                self.assertEqual(detail.get_by_role('link', name='打开主页').count(), 0)
                self.assertEqual(handle.locator('dd').inner_text(), '未获取')
                self.assertNotIn('xingtu:123', detail.inner_text())

    def test_xingtu_source_stays_visible_in_compact_desktop_rows(self):
        self.page.set_viewport_size({'width': 1024, 'height': 900})
        self.page.evaluate("""() => Object.assign(creators[0], {source_platform: 'xingtu',
          followers: 123456789, avg_likes: 1234567, max_likes: 99999999})""")
        self.open_drawer()
        visible = self.page.locator('[data-creator-id="11"] .candidate-metrics').evaluate("""node => {
          const offset = node.textContent.indexOf('星图内容检索');
          if (offset < 0) return false;
          const range = document.createRange();
          range.setStart(node.firstChild, offset);
          range.setEnd(node.firstChild, offset + 6);
          return range.getBoundingClientRect().right <= node.getBoundingClientRect().right;
        }""")
        self.assertTrue(visible, 'Xingtu source must not be hidden by metrics truncation')

    def test_xingtu_report_and_modal_keep_real_douyin_handle_separate(self):
        for handle, expected in [('', ''), ('xingtu:123', ''), ('real_douyin_123', 'real_douyin_123')]:
            with self.subTest(handle=handle):
                self.page.evaluate("""handle => {
                  report = [{...creators[0], sec_uid: 'xingtu:123', unique_id: handle,
                    profile_url: 'https://www.xingtu.cn/ad/creator/author-homepage/douyin/123'}];
                  reportCreators = report; renderReportTable(); openReportModal(11);
                }""", handle)
                cell = self.page.locator('#reportTableBody [onblur*="\'unique_id\'"]')
                self.assertEqual(cell.inner_text(), expected)
                self.assertNotIn('xingtu:123', self.page.locator('#modalId').inner_text())
                if expected:
                    self.assertIn(expected, self.page.locator('#modalId').inner_text())
                self.assertEqual(self.page.locator('#modalProfileLink').get_attribute('href'),
                                 'https://www.xingtu.cn/ad/creator/author-homepage/douyin/123')

    def test_internal_xingtu_keys_never_become_profile_links_or_douyin_handles(self):
        for key in ('xingtu:123', 'xingtu%3A123'):
            with self.subTest(key=key):
                self.page.evaluate("""key => {
                  closeCandidateDrawer();
                  Object.assign(creators[0], {sec_uid: 'xingtu:123', unique_id: 'xingtu:123',
                    profile_url: 'https://www.douyin.com/user/' + key});
                  renderKBTable(creators);
                }""", key)
                self.open_drawer()
                self.open_detail(11)
                self.assertEqual(self.page.locator('#candidateDetail').get_by_role(
                    'link', name='打开主页').count(), 0)
                self.assertNotIn('xingtu:123', self.page.locator('#candidateDetail').inner_text())
                self.assertNotIn('xingtu:123', self.page.locator('#kbTableBody').inner_text())
                self.assertEqual(self.page.locator('#kbTableBody a').first.get_attribute('href'), '#')
                self.page.locator('#candidateDetailBack').click()

    def test_xingtu_blocked_empty_state_does_not_claim_douyin_captcha(self):
        self.page.evaluate("""() => {
          task.status = 'blocked'; task.progress = {platform: 'xingtu', pause_reason: 'permission_denied'};
          task.counts.discovered = 0; creators = []; displayTaskProgress(task);
        }""")
        self.open_drawer()
        text = self.page.locator('#candidateList').inner_text()
        self.assertIn('星图', text)
        self.assertIn('权益', text)
        self.assertIn('尚未取得任何候选', text)
        self.assertNotIn('抖音', text)
        self.assertNotIn('验证码', text)
        self.assertNotIn('人工验证', self.page.locator('#taskProgressText').inner_text())

    def test_completion_opens_candidates_in_chat_not_report(self):
        self.page.evaluate("""() => appendChatMessageDOM({role: 'assistant', content: '搜索结束',
          meta: {task_finished: true, task_id: 7, status: 'completed'}})""")
        button = self.page.locator('#chatMessages').get_by_role("button", name="查看候选达人", exact=True)
        self.assertEqual(button.count(), 1)
        button.click()
        self.assertEqual(self.page.evaluate("currentTab"), "CHAT")
        self.assertTrue(self.page.locator("#candidateDrawer").is_visible())
        self.assertEqual(self.requests('/api/results'), [])
        self.assertIn('候选甲', self.page.locator('#candidateList').text_content())

    def test_verification_with_zero_accounts_never_shows_green_completion(self):
        self.page.evaluate("""() => {
          task.status = 'blocked'; task.progress = {pause_reason: 'platform_verification'};
          for (const key of ['candidates','discovered','eligible','pending','rejected']) task.counts[key] = 0;
          creators = []; displayTaskProgress(task);
          appendChatMessageDOM({role: 'assistant', content: '平台需要验证',
            meta: {task_finished: true, task_id: 7, status: 'blocked', counts: task.counts,
              pause_reason: 'platform_verification'}});
          openCandidateDrawer(7);
        }""")
        self.page.wait_for_function("candidatesLoaded")
        self.assertIn('需要人工验证', self.page.locator('#taskProgressText').inner_text())
        self.assertNotIn('部分完成', self.page.locator('#taskProgressText').inner_text())
        self.assertIn('检索受阻', self.page.locator('#chatMessages').inner_text())
        self.assertEqual(self.page.locator('#chatMessages .bg-emerald-50').count(), 0)
        empty = self.page.locator('#candidateList').inner_text()
        self.assertIn('尚未取得任何候选', empty)
        self.assertNotIn('原始线索仍保留', empty)
        self.assertTrue(self.page.locator('#stopTaskBtn').is_hidden())

    def test_partial_and_failed_result_cards_are_not_success_green(self):
        for state in ('partial', 'failed', 'interrupted'):
            self.page.evaluate("""status => renderChatMessages([{role: 'assistant', content: '状态说明',
              meta: {task_finished: true, task_id: 7, status}}])""", state)
            self.assertEqual(self.page.locator('#chatMessages .bg-emerald-50').count(), 0)

    def test_progress_uses_server_elapsed_counts_and_freezes_when_finished(self):
        self.page.evaluate('checkGlobalStatus()')
        text = self.page.locator('#taskProgress').text_content()
        for value in ('02:05', '发现 9', '可选 2', '待核实 1', '不符合 0', '已选 0', '报告 0', '达标 0', '目标 5'):
            self.assertIn(value, text)
        self.page.wait_for_timeout(1100)
        self.assertIn('02:05', self.page.locator('#taskProgress').text_content())
        self.assertNotIn('%', text)

    def test_stop_waits_for_terminal_state_and_offline_is_visible(self):
        self.page.evaluate("task = {...task, status: 'running', finished_at: null}; checkGlobalStatus()")
        self.page.locator('#stopTaskBtn').click()
        self.assertEqual(len(self.requests('/stop')), 1)
        self.assertIn('正在停止', self.page.locator('#taskProgress').text_content())
        self.assertTrue(self.page.locator('#stopTaskBtn').is_disabled())
        self.page.evaluate('offline = true; checkGlobalStatus()')
        self.assertIn('连接中断', self.page.locator('#taskProgress').text_content())
        self.page.evaluate("offline = false; task.status = 'stopped'; checkGlobalStatus()")
        self.assertIn('已停止', self.page.locator('#taskProgress').text_content())

    def test_enrichment_requires_terminal_search_selection_and_second_confirmation(self):
        self.page.evaluate("task.status = 'running'; task.finished_at = null")
        self.open_drawer()
        self.assertTrue(self.page.locator('#confirmEnrichmentBtn').is_disabled())
        self.page.evaluate("task.status = 'stopped'; loadCandidates(7)")
        self.page.locator('[data-candidate-select="11"]').check()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.page.locator('[data-candidate-exclude="13"]').check()
        self.page.locator('#candidateDetailBack').click()
        self.assertEqual(self.requests('/enrich'), [])
        self.page.locator('#confirmEnrichmentBtn').click()
        self.assertEqual(self.requests('/enrich'), [])
        self.page.locator('#submitEnrichmentBtn').click()
        sent = self.requests('/enrich')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['body'], {'creator_ids': [11], 'excluded_ids': [13]})
        self.assertTrue(self.page.locator('#confirmEnrichmentBtn').is_disabled())
        self.assertGreater(len(self.requests('/candidates')), 1)
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')

    def test_exclusion_and_selection_survive_refresh_without_kb_deletion(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.page.locator('[data-candidate-exclude="13"]').check()
        self.page.evaluate('loadCandidates(7)')
        self.page.locator('#candidateDetailBack').click()
        self.filter_candidates('eligible')
        self.assertTrue(self.page.locator('[data-candidate-select="11"]').is_checked())
        self.filter_candidates('pending')
        self.open_detail(13)
        self.assertTrue(self.page.locator('[data-candidate-exclude="13"]').is_checked())
        self.page.evaluate("switchTab('KB')")
        self.assertIn('候选乙', self.page.locator('#kbTableBody').text_content())
        self.assertEqual(self.page.evaluate("requests.filter(r => r.method === 'DELETE')"), [])

    def test_missing_enrichment_is_pending_and_explicit_exception_is_synchronous(self):
        self.page.evaluate("""creators[0].enrichment = {status: 'permission_denied',
          missing_fields: ['audience'], identity_verified: false}""")
        self.open_drawer()
        self.open_detail(11)
        self.assertIn('无权限', self.page.locator('#candidateDetail').text_content())
        self.assertIn('audience', self.page.locator('#candidateDetail').text_content())
        self.assertEqual(self.requests('/report'), [])
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('[data-admit-missing="11"]').click()
        self.assertEqual(self.requests('/report')[0]['body'], {'creator_ids': [11], 'allow_missing': True})
        self.assertEqual(self.requests('/enrich'), [])
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')

    def test_kb_bulk_import_existing_new_and_information_list(self):
        self.page.evaluate("switchTab('KB')")
        self.page.locator('[data-kb-select="11"]').check()
        self.page.locator('#kbImportMode').select_option('existing')
        self.page.locator('#kbImportTask').select_option('7')
        self.page.locator('#kbImportBtn').click()
        self.assertEqual(self.requests('/api/kb/import')[-1]['body'],
                         {'creator_ids': [11], 'task_id': 7, 'brief': '', 'allow_missing': False})
        self.assertIn('待处理 1', self.page.locator('#kbImportStatus').text_content())
        self.page.locator('#kbImportMode').select_option('new')
        self.page.locator('#kbImportBrief').fill('食品新品投放')
        self.page.locator('#kbImportBtn').click()
        self.assertEqual(self.requests('/api/kb/import')[-1]['body']['brief'], '食品新品投放')
        self.page.locator('#kbImportMode').select_option('list')
        self.page.locator('#kbImportBtn').click()
        payload = self.requests('/api/kb/import')[-1]['body']
        self.assertIsNone(payload['task_id'])
        self.assertEqual(payload['brief'], '')
        self.assertEqual(self.requests('/enrich'), [])
        self.page.locator('#kbPendingBtn').click()
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertTrue(self.page.locator('#candidateDrawer').is_visible())

    def test_kb_tags_filter_and_edit_use_safe_structured_payload(self):
        self.page.evaluate("switchTab('KB')")
        self.page.locator('#kbTagFilter').fill('待核实')
        self.page.locator('#kbEnrichmentFilter').select_option('permission_denied')
        self.page.evaluate('loadKnowledgeBase()')
        url = self.requests('/api/kb')[-1]['url']
        self.assertIn('tag=', url)
        self.assertIn('enrichment_status=permission_denied', url)
        self.page.on('dialog', lambda dialog: dialog.accept('自定义,<img onerror=alert(1)>'))
        self.page.locator('[data-edit-tags="11"]').click()
        sent = self.requests('/api/kb/11/tags')[-1]
        self.assertEqual(sent['method'], 'PATCH')
        self.assertEqual(sent['body']['tags'], ['自定义', '<img onerror=alert(1)>'])
        self.assertEqual(self.page.locator('[onerror]').count(), 0)

    def test_ttl_setting_preserves_secret_redaction(self):
        self.page.evaluate("switchTab('SETTINGS')")
        self.assertEqual(self.page.locator('#cfgDynamicTTL').input_value(), '48')
        self.assertEqual(self.page.locator('#cfgLlmApiKey').input_value(), '')
        self.page.locator('#cfgDynamicTTL').fill('72')
        self.page.evaluate('saveAllSettings()')
        request = [r for r in self.requests('/api/settings') if r['method'] == 'POST'][-1]
        self.assertEqual(request['body']['settings']['dynamic_ttl_hours'], '72')
        self.assertEqual(request['body']['settings']['llm_api_key'], '')

    def test_formal_manual_judgment_does_not_overwrite_numeric_rule(self):
        self.page.evaluate("""() => {
          report = [{creator_id: 11, nickname: '已入报告', recommend_level: '待核实',
            reason: '缺少粉丝数', manual_level: '', manual_reason: '', allow_missing: true}];
          currentSelectedTaskId = 7;
          switchTab('REPORT');
        }""")
        self.page.locator('[data-manual-level="11"]').select_option('高推荐')
        self.page.locator('[data-manual-reason="11"]').fill('人工复核说明')
        self.page.locator('[data-manual-reason="11"]').blur()
        self.page.evaluate('saveAllReportChanges()')
        row = self.requests('/api/results/batch_save')[-1]['body']['creators'][0]
        self.assertEqual(row['recommend_level'], '待核实')
        self.assertEqual(row['reason'], '缺少粉丝数')
        self.assertEqual(row['manual_level'], '高推荐')
        self.assertEqual(row['manual_reason'], '人工复核说明')
        self.assertIn('人工带缺失加入', self.page.locator('#reportTableBody').text_content())

    def test_task_confirmation_shows_final_target_and_numeric_conditions(self):
        self.page.evaluate("""() => appendChatMessageDOM({role: 'assistant', content: '确认', meta: {
          category: '食品', keyword: '测评', sample_count: 5, min_avg_likes: 100,
          min_followers: 10000, max_followers: 80000, min_max_likes: 1000}})""")
        self.assertIn('最终交付目标', self.page.locator('#chatMessages').text_content())
        for label in ('粉丝量下限', '粉丝量上限', '最高赞门槛'):
            self.assertIn(label, self.page.locator('#chatMessages').text_content())
        self.page.locator('[data-execute-card]').click()
        body = self.requests('/api/execute')[0]['body']
        for key, value in [('min_followers', 10000), ('max_followers', 80000), ('min_max_likes', 1000)]:
            self.assertEqual(body[key], value)
        self.assertNotIn('最多 5', self.page.locator('#chatMessages').text_content())

    def test_drawer_hostile_values_unknown_metrics_and_evidence_are_safe(self):
        self.page.evaluate("""() => {
          const hostile = '<img data-injected src=x onerror="window.injected=true">';
          Object.assign(creators[0], {nickname: hostile, reason: hostile,
            profile_url: 'javascript:alert(1)', tags: [hostile],
            sample_posts: [{desc: hostile, url: 'javascript:alert(1)', likes: null}]});
        }""")
        self.page.evaluate("creators[0].candidate_status = 'pending'")
        self.open_drawer()
        self.filter_candidates('pending')
        self.open_detail(11)
        self.assertEqual(self.page.locator('[data-injected], [onerror]').count(), 0)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.assertIn('待核实', self.page.locator('#candidateDetail').text_content())
        urls = self.page.locator('#candidateDetail a').evaluate_all('nodes => nodes.map(n => n.getAttribute("href"))')
        self.assertTrue(all(url == '#' or url.startswith(('https://', 'http://')) for url in urls))

    def test_mobile_drawer_is_framed_and_closes_back_to_chat(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open_drawer()
        rect = self.page.locator('#candidateDrawer').bounding_box()
        self.assertGreater(rect['height'], 200)
        self.assertGreaterEqual(rect['x'], 0)
        self.assertLessEqual(rect['x'] + rect['width'], 391)
        self.page.locator('#closeCandidateDrawerBtn').click()
        self.assertFalse(self.page.locator('#candidateDrawer').is_visible())
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')

    def test_failed_candidate_request_is_not_shown_as_empty_success(self):
        self.page.evaluate('failRequests = true')
        self.open_drawer()
        self.assertIn('加载失败', self.page.locator('#candidateList').text_content())
        self.assertTrue(self.page.locator('#confirmEnrichmentBtn').is_disabled())

    def test_pending_without_enrichment_can_be_explicitly_admitted_with_missing(self):
        self.open_drawer()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.assertEqual(self.requests('/enrich'), [])
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('[data-admit-missing="13"]').click()
        self.assertTrue(self.requests('/report')[0]['body']['allow_missing'])

    def test_failed_admission_stays_pending_and_can_retry(self):
        self.open_drawer()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.evaluate('failRequests = true')
        self.page.locator('[data-admit-missing="13"]').click()
        self.assertIn('加入失败', self.page.locator('#candidateActionStatus').text_content())
        self.assertFalse(self.page.locator('[data-admit-missing="13"]').is_disabled())

    def test_completed_enrichment_poll_refreshes_status_without_resubmitting(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('#submitEnrichmentBtn').click()
        self.page.evaluate("""() => {
          task.status = 'partial'; task.finished_at = '2026-11-05T01:03:00Z';
          creators[0].enrichment = {status: 'identity_unconfirmed', missing_fields: ['identity']};
          return checkGlobalStatus();
        }""")
        self.open_detail(11)
        self.assertIn('身份未确认', self.page.locator('#candidateDetail').text_content())
        self.assertEqual(len(self.requests('/enrich')), 1)
        self.assertTrue(any('task_id=7' in r['url'] for r in self.requests('/api/status')))

    def test_report_snapshot_metrics_are_read_only_and_missing_marker_is_visible(self):
        self.page.evaluate("""async () => {
          report = [{creator_id: 11, nickname: 'snapshot', recommend_level: '待核实',
            reason: '数值依据', followers: null, qualified: false, manual_include_missing: true}];
          currentSelectedTaskId = 7; switchTab('REPORT');
        }""")
        cell = self.page.locator('[onblur*="\'followers\'"]')
        self.assertEqual(cell.get_attribute('contenteditable'), 'false')
        self.assertEqual(cell.text_content(), '待核实')
        self.assertIn('人工带缺失加入', self.page.locator('#reportTableBody').text_content())
        self.assertIn('已核实达标 0', self.page.locator('#reportDeliverySummary').text_content())
        self.assertIn('缺口 5', self.page.locator('#reportDeliverySummary').text_content())

    def test_information_list_is_not_a_matching_report(self):
        self.page.evaluate("""async () => {
          task = {...task, counts: undefined, requirements: {report_kind: 'information'}};
          report = [{creator_id: 11, nickname: '资料', recommend_level: '资料清单',
            reason: '无需求，不提供匹配评级', report_kind: 'information', data_list: true}];
          currentSelectedTaskId = 7; switchTab('REPORT');
        }""")
        self.assertIn('无任务匹配评级', self.page.locator('#reportDeliverySummary').text_content())
        self.assertIn('资料清单', self.page.locator('#reportTableBody').text_content())
        self.assertTrue(self.page.locator('[data-manual-level="11"]').is_disabled())

    def test_report_load_failure_cannot_leave_previous_report_editable(self):
        self.page.evaluate("""async () => {
          report = [{creator_id: 11, nickname: 'previous'}];
          await loadTaskReport(7);
          failRequests = true;
          await loadTaskReport(8);
        }""")
        self.assertNotIn('previous', self.page.locator('#reportTableBody').text_content())
        self.assertIsNone(self.page.evaluate('currentSelectedTaskId'))

    def test_ttl_invalid_value_does_not_send_settings(self):
        self.page.evaluate("switchTab('SETTINGS')")
        self.page.locator('#cfgDynamicTTL').fill('0')
        self.page.evaluate('saveAllSettings()')
        self.assertFalse(any(r['method'] == 'POST' for r in self.requests('/api/settings')))

    def test_kb_new_brief_optional_numeric_rules_send_a_complete_task_card(self):
        self.page.evaluate("switchTab('KB')")
        self.page.locator('[data-kb-select="11"]').check()
        self.page.locator('#kbImportMode').select_option('new')
        self.page.locator('#kbImportBrief').fill('食品新品投放')
        self.page.locator('#kbStructuredRequirements').check()
        for field, value in [('kbTargetCount', '5'), ('kbMinFollowers', '10000'),
                             ('kbMaxFollowers', '80000'), ('kbMinAvgLikes', '1000'),
                             ('kbMinMaxLikes', '5000'), ('kbActiveDays', '14')]:
            self.page.locator('#' + field).fill(value)
        self.page.locator('#kbImportBtn').click()
        self.assertEqual(self.requests('/api/kb/import')[-1]['body']['requirements'], {
            'category': '未指定', 'keyword': '食品新品投放', 'sample_count': 5,
            'min_followers': 10000, 'max_followers': 80000, 'min_avg_likes': 1000,
            'min_max_likes': 5000, 'active_days': 14,
            'exclusions': [], 'content_preferences': []})
        self.assertIn('仅按数值规则', self.page.locator('#kbNumericRequirements').text_content())
        self.assertEqual(self.requests('/enrich'), [])

    def test_kb_invalid_numeric_range_is_not_submitted_and_brief_only_has_no_task_card(self):
        self.page.evaluate("switchTab('KB')")
        self.page.locator('[data-kb-select="11"]').check()
        self.page.locator('#kbImportMode').select_option('new')
        self.page.locator('#kbImportBrief').fill('新需求')
        self.page.locator('#kbStructuredRequirements').check()
        self.page.locator('#kbMinFollowers').fill('100')
        self.page.locator('#kbMaxFollowers').fill('10')
        self.page.locator('#kbImportBtn').click()
        self.assertEqual(self.requests('/api/kb/import'), [])
        self.assertIn('粉丝量上限', self.page.locator('#kbImportStatus').text_content())
        self.page.locator('#kbStructuredRequirements').uncheck()
        self.page.locator('#kbImportBtn').click()
        self.assertNotIn('requirements', self.requests('/api/kb/import')[-1]['body'])

    def test_snapshot_contacts_cannot_be_edited_but_copy_is_functional(self):
        self.page.evaluate("""async () => {
          report = [{creator_id: 11, nickname: '已核验', wechat: 'public-wechat', phone: 'public-phone'}];
          window.copied = []; window.copyToClipboard = value => copied.push(value);
          currentSelectedTaskId = 7; switchTab('REPORT');
        }""")
        for field in ('wechat', 'phone'):
            self.assertEqual(self.page.locator(f'[onblur*="\'{field}\'"]').get_attribute('contenteditable'), 'false')
        self.page.locator('[data-copy-wechat]').click()
        self.assertEqual(self.page.evaluate('copied'), ['public-wechat'])

    def load_xingtu_detail_fixture(self):
        self.page.evaluate("""async () => {
          creators[0].unique_id = '';
          creators[0].enrichment = {status: 'completed', identity_verified: true,
            audience: {gender: {male: .4, female: .6}, age: {'24-30': .4, '31-40': .6},
              region: {'浙江': .3, '广东': .2, '江苏': .1, '山东': .05}},
            contact: {status: '已获取', type: '微信', value: 'fixture_wechat',
              source_url: 'https://example.com/xingtu/contact',
              permission: 'session_visible', source: '星图后台'},
            profile: {unique_id: 'real_fixture_handle', uid: '123456789',
              sec_uid: 'MS4wFixture', star_id: '987654321'}};
          report = [structuredClone(creators[0])];
          window.copied = []; window.copyToClipboard = value => copied.push(value);
          await loadTaskReport(7);
        }""")

    def test_formal_report_renders_readable_ratio_portrait_and_matching_columns(self):
        self.load_xingtu_detail_fixture()
        self.page.evaluate("switchTab('REPORT')")
        self.page.wait_for_function('!reportLoading')
        portrait = self.page.locator('[data-report-audience="11"]')
        self.assertEqual(portrait.count(), 1)
        text = portrait.inner_text()
        for part in ('性别', '男 40%', '女 60%', '年龄', '24-30 40%', '31-40 60%',
                     '省份', '浙江 30%', '广东 20%', '江苏 10%', '另 1 项'):
            self.assertIn(part, text)
        self.assertNotIn('{', text)
        groups = self.page.locator('#excelTable thead tr').first.locator('th')
        headers = self.page.locator('#excelTable thead tr').last.locator('th')
        self.assertEqual(headers.count(), 26)
        self.assertEqual(groups.evaluate_all('nodes => nodes.reduce((n, x) => n + x.colSpan, 0)'), 26)
        self.assertEqual(self.page.locator('#reportTableBody tr').first.locator('td').count(), 26)
        self.page.evaluate('reportCreators = []; renderReportTable()')
        self.assertEqual(self.page.locator('#reportTableBody td').get_attribute('colspan'), '26')

    def test_candidate_and_evidence_modal_show_portrait_contact_and_real_identifiers(self):
        self.load_xingtu_detail_fixture()
        self.open_drawer()
        self.open_detail(11)
        detail = self.page.locator('#candidateDetail')
        for part in ('男 40%', '女 60%', '24-30 40%', '山东 5%', '已获取', '微信',
                     'fixture_wechat', '星图后台', '当前登录会话可见',
                     'real_fixture_handle', '123456789', 'MS4wFixture', '987654321'):
            self.assertIn(part, detail.inner_text())
        self.assertEqual(detail.get_by_role('link', name='联系来源', exact=True).get_attribute('href'),
                         'https://example.com/xingtu/contact')
        self.page.evaluate('openReportModal(11)')
        modal = self.page.locator('#detailModal')
        for part in ('粉丝画像', '男 40%', '女 60%', '山东 5%', 'fixture_wechat',
                     '星图后台', '当前登录会话可见', 'real_fixture_handle'):
            self.assertIn(part, modal.inner_text())
        self.assertEqual(modal.get_by_role('link', name='联系来源', exact=True).get_attribute('href'),
                         'https://example.com/xingtu/contact')
        self.assertEqual(self.requests('/report'), [])

    def test_report_verified_contact_is_read_only_copyable_and_has_visible_provenance(self):
        self.load_xingtu_detail_fixture()
        self.page.evaluate("switchTab('REPORT')")
        self.page.wait_for_function('!reportLoading')
        wechat = self.page.locator('[onblur*="\'wechat\'"]')
        self.assertEqual(wechat.inner_text(), 'fixture_wechat')
        self.assertEqual(wechat.get_attribute('contenteditable'), 'false')
        cell = wechat.locator('..').locator('..')
        for part in ('已获取', '星图后台'):
            self.assertIn(part, cell.inner_text())
        self.page.locator('[data-copy-wechat]').click()
        self.assertEqual(self.page.evaluate('copied'), ['fixture_wechat'])
        self.assertIn('real_fixture_handle', self.page.locator('[onblur*="\'unique_id\'"]').inner_text())
        self.assertIn('987654321', self.page.locator('[onblur*="\'star_id\'"]').inner_text())

    def test_missing_or_invalid_portrait_and_contact_are_explicit_without_fake_zeroes(self):
        self.page.evaluate("""() => {
          reportCreators = [{creator_id: 11, nickname: '缺失资料', enrichment: {
            audience: {gender: {male: null, female: false},
              age: {'24-30': '0.4', '31-40': 40}, region: {'浙江': -1}},
            contact: {status: '未获取', type: '微信', value: 'stale_hidden_value',
              source_url: 'javascript:alert(1)'}}}];
          renderReportTable(); openReportModal(11);
        }""")
        portrait = self.page.locator('[data-report-audience="11"]')
        self.assertEqual(portrait.count(), 1)
        self.assertEqual(portrait.inner_text().count('未获取'), 3)
        self.assertNotIn('%', portrait.inner_text())
        self.assertNotIn('stale_hidden_value', self.page.locator('#reportTableBody').inner_text())
        self.assertNotIn('stale_hidden_value', self.page.locator('#detailModal').inner_text())
        self.assertEqual(self.page.locator('[data-copy-wechat]').count(), 0)
        self.assertIn('未获取', self.page.locator('#detailModal').inner_text())
        self.assertEqual(self.page.locator('#detailModal a[href^="javascript:"]').count(), 0)
        self.page.evaluate('closeModal()')
        self.open_drawer()
        self.open_detail(12)
        detail = self.page.locator('#candidateDetail')
        self.assertEqual(detail.locator('[data-audience-dimension]').count(), 3)
        for dimension in ('gender', 'age', 'region'):
            self.assertIn('未获取', detail.locator(f'[data-audience-dimension="{dimension}"]').inner_text())
        self.assertIn('星图联系：未获取', detail.inner_text())

    def test_portrait_zero_share_flat_snapshot_and_api_labels_are_safe(self):
        self.page.evaluate("""() => {
          reportCreators = [{creator_id: 11, nickname: '安全测试', audience: {
            gender: {male: 0, female: 1}, age: {'24-30': .004},
            region: {'<img src=x onerror="window.injected=true">': .1}}}];
          renderReportTable(); openReportModal(11);
        }""")
        portrait = self.page.locator('[data-report-audience="11"]')
        self.assertEqual(portrait.count(), 1)
        text = portrait.inner_text()
        for part in ('男 0%', '女 100%', '24-30 0.4%'):
            self.assertIn(part, text)
        self.assertIn('<img src=x', text)
        self.assertEqual(self.page.locator('[data-report-audience] img').count(), 0)
        self.assertIsNone(self.page.evaluate('window.injected'))

    def test_portrait_desktop_cell_wraps_inside_horizontal_report_scroller(self):
        self.load_xingtu_detail_fixture()
        self.page.evaluate("switchTab('REPORT')")
        self.page.wait_for_function('!reportLoading')
        self.page.add_style_tag(content='#excelSheetContainer {width:100%; max-width:1180px; overflow:auto;}')
        for width in (1280, 1920):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                cell = self.page.locator('[data-report-audience="11"]')
                self.assertEqual(cell.count(), 1)
                self.assertGreaterEqual(cell.bounding_box()['width'], 260)
                self.assertLessEqual(cell.bounding_box()['width'], 320)
                self.assertTrue(cell.locator('[data-audience-dimension]').evaluate_all(
                    'nodes => nodes.every(n => n.scrollWidth <= n.clientWidth)'))
                scroller = self.page.locator('#excelSheetContainer')
                self.assertGreater(scroller.evaluate('n => n.scrollWidth'), scroller.evaluate('n => n.clientWidth'))

    def test_restored_flat_status_uses_persistent_time_and_disconnect_freezes_it(self):
        self.page.evaluate("""async () => {
          task = {...task, status: 'running', finished_at: null, elapsed_seconds: 3723};
          const original = fetch;
          window.fetch = async (url, options) => String(url).startsWith('/api/status')
            ? {ok: true, json: async () => ({...task, task_id: 7})} : original(url, options);
          await checkGlobalStatus();
        }""")
        self.assertIn('62:03', self.page.locator('#taskProgress').text_content())
        self.page.evaluate("window.fetch = async () => { throw Error('offline'); }; checkGlobalStatus()")
        text = self.page.locator('#taskProgress').text_content()
        self.page.wait_for_timeout(1100)
        self.assertEqual(self.page.locator('#taskProgress').text_content(), text)

    def test_selection_change_cancels_previously_confirmed_batch(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('[data-candidate-select="12"]').check()
        self.assertFalse(self.page.locator('#enrichmentConfirmation').is_visible())
        self.page.evaluate('submitCandidateEnrichment()')
        self.assertEqual(self.requests('/enrich'), [])

    def test_admission_shows_pending_reasons_instead_of_generic_success(self):
        self.page.evaluate("""() => {
          const original = fetch;
          window.fetch = async (url, options) => String(url).endsWith('/report')
            ? {ok: true, json: async () => ({admitted: [], pending: [12], details: {'12': ['unstructured_brief']}})}
            : original(url, options);
        }""")
        self.open_drawer()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.locator('[data-admit-missing="13"]').click()
        self.assertIn('待处理 1', self.page.locator('#candidateActionStatus').text_content())
        self.assertIn('unstructured_brief', self.page.locator('#candidateActionStatus').text_content())

    def test_kb_numeric_and_time_filters_omit_blanks_and_send_real_values(self):
        self.page.evaluate("switchTab('KB')")
        query = parse_qs(urlsplit(self.requests('/api/kb')[-1]['url']).query)
        self.assertNotIn('min_followers', query)
        self.assertNotIn('updated_within_days', query)
        for field, value in [('kbFilterMinFollowers', '100'), ('kbFilterMaxFollowers', '90000'),
                             ('kbFilterMinAvgLikes', '0'), ('kbFilterUpdatedDays', '7')]:
            self.page.locator('#' + field).fill(value)
        self.page.evaluate('loadKnowledgeBase()')
        query = parse_qs(urlsplit(self.requests('/api/kb')[-1]['url']).query)
        self.assertEqual({key: query[key] for key in ('min_followers', 'max_followers', 'min_avg_likes', 'updated_within_days')},
                         {'min_followers': ['100'], 'max_followers': ['90000'],
                          'min_avg_likes': ['0'], 'updated_within_days': ['7']})
        self.page.locator('#kbFilterUpdatedDays').fill('0')
        previous = len(self.requests('/api/kb'))
        self.page.evaluate('loadKnowledgeBase()')
        self.assertEqual(len(self.requests('/api/kb')), previous)
        self.assertIn('筛选条件', self.page.locator('#kbFilterStatus').text_content())

    def test_tag_source_and_basis_are_visible_without_html_execution(self):
        self.page.evaluate("""() => {
          creators[0].tags = [{name: '数值采集记录', source: 'automatic', basis: '<img onerror=alert(1)>'}];
          switchTab('KB');
        }""")
        tag = self.page.locator('#kbTableBody span').filter(has_text='数值采集记录')
        self.assertIn('automatic', tag.get_attribute('title'))
        self.assertIn('<img onerror=alert(1)>', tag.get_attribute('title'))
        self.assertEqual(self.page.locator('[onerror]').count(), 0)

    def test_save_selection_persists_flags_and_reload_restores_without_enriching(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.filter_candidates('pending')
        self.open_detail(13)
        self.page.locator('[data-candidate-exclude="13"]').check()
        self.page.locator('#candidateDetailBack').click()
        self.assertEqual(self.requests('/selection'), [])
        self.page.locator('#saveCandidateSelectionBtn').click()
        request = self.requests('/selection')[-1]
        self.assertEqual(request['method'], 'PATCH')
        self.assertEqual(request['body'], {'creator_ids': [11], 'excluded_ids': [13]})
        self.assertIn('已选 1', self.page.locator('#taskProgress').text_content())
        self.page.evaluate('candidateChoices.clear(); loadCandidates(7)')
        self.filter_candidates('eligible')
        self.assertTrue(self.page.locator('[data-candidate-select="11"]').is_checked())
        self.filter_candidates('pending')
        self.open_detail(13)
        self.assertTrue(self.page.locator('[data-candidate-exclude="13"]').is_checked())
        self.assertEqual(self.requests('/enrich'), [])

    def test_save_empty_selection_is_supported_and_disabled_during_search(self):
        self.page.evaluate("task.status = 'running'")
        self.open_drawer()
        self.assertTrue(self.page.locator('#saveCandidateSelectionBtn').is_disabled())
        self.page.evaluate("task.status = 'completed'; loadCandidates(7)")
        self.page.locator('#saveCandidateSelectionBtn').click()
        self.assertEqual(self.requests('/selection')[-1]['body'], {'creator_ids': [], 'excluded_ids': []})

    def test_numeric_post_id_provides_a_real_evidence_link_without_accepting_script_urls(self):
        self.page.evaluate("creators[0].sample_posts = [{aweme_id: '7123456789012345678', desc: '作品'}]")
        self.open_drawer()
        self.open_detail(11)
        self.assertEqual(self.page.locator('#candidateDetail a[href^="https://www.douyin.com/video/"]').first.get_attribute('href'),
                         'https://www.douyin.com/video/7123456789012345678')

    def test_pending_enrichment_does_not_switch_drawer_back_after_navigation(self):
        self.page.evaluate("""() => {
          const original = fetch;
          window.fetch = (url, options) => String(url).endsWith('/enrich')
            ? new Promise(resolve => { window.releaseEnrich = async () => resolve(await original(url, options)); })
            : original(url, options);
        }""")
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('#submitEnrichmentBtn').click()
        self.page.evaluate('openCandidateDrawer(8)')
        self.page.evaluate('releaseEnrich()')
        self.page.wait_for_function('!enrichmentSubmitting')
        self.assertEqual(self.page.evaluate('candidateTaskId'), 8)
        self.assertEqual(len(self.requests('/enrich')), 1)

    def test_report_render_preserves_readable_columns_and_horizontal_scrolling(self):
        self.page.evaluate("""() => {
          report = [{creator_id: 11, nickname: '合成食品账号', recommend_level: '高推荐',
            reason: '粉丝量和近期均赞达到明确数值门槛', manual_level: '', manual_reason: '',
            followers: 10000, avg_likes: 5000, max_likes: 10000}];
          currentSelectedTaskId = 7; switchTab('REPORT');
        }""")
        # Bound the offline fixture like the real report pane, without a CDN.
        self.page.add_style_tag(content='''
          #excelSheetContainer { width: 100%; max-width: 1180px; overflow: auto; }
        ''')
        for width, height in [(1600, 1000), (390, 844)]:
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': height})
                cells = self.page.locator('#reportTableBody tr').first.locator('td')
                for column, minimum in [(1, 112), (2, 220), (3, 112), (4, 220),
                                        (6, 150), (8, 100), (12, 100), (13, 100),
                                        (14, 100), (16, 100), (17, 100)]:
                    self.assertGreaterEqual(cells.nth(column).bounding_box()['width'], minimum)
                self.assertAlmostEqual(cells.first.bounding_box()['width'], 48, delta=1)
                self.assertGreaterEqual(self.page.locator('[data-manual-level="11"]').bounding_box()['width'], 112)
                self.assertGreaterEqual(self.page.locator('[data-manual-reason="11"]').bounding_box()['width'], 220)
                self.assertGreaterEqual(self.page.locator('#excelTable').bounding_box()['width'], 3200)
                scroller = self.page.locator('#excelSheetContainer')
                self.assertGreater(scroller.evaluate('n => n.scrollWidth'), scroller.evaluate('n => n.clientWidth'))
                scroller.evaluate('n => n.scrollLeft = 300')
                self.assertGreater(scroller.evaluate('n => n.scrollLeft'), 0)

    def test_candidate_post_update_and_collection_dates_are_not_conflated(self):
        self.page.evaluate("creators[1].updated_at = '2026-11-03'")
        self.open_drawer()
        def value(creator_id, label):
            self.open_detail(creator_id)
            text = self.page.locator('#candidateDetail dl > div').filter(
                has=self.page.locator('dt', has_text=label)).locator('dd').text_content()
            self.page.locator('#candidateDetailBack').click()
            return text
        self.assertEqual(value(11, '作品更新时间'), '2026-10-31')
        self.assertEqual(value(11, '资料采集时间'), '2026-11-04')
        self.assertEqual(value(12, '作品更新时间'), '待核实')
        self.assertEqual(value(12, '资料采集时间'), '2026-11-03')

    def test_failed_report_load_disables_export_and_never_requests_latest_fallback(self):
        self.page.evaluate('loadTaskReport(7)')
        self.page.evaluate('failRequests = true; loadTaskReport(8)')
        export = self.page.locator('button[onclick="downloadCurrentReportExcel()"]')
        self.assertTrue(export.is_disabled())
        self.page.evaluate('downloadCurrentReportExcel()')
        self.assertEqual(self.requests('/api/export'), [])

    def test_edits_during_pending_save_remain_dirty_and_are_not_lost(self):
        self.page.evaluate("""() => {
          report = [{creator_id: 11, nickname: '快照', manual_reason: ''}];
          currentSelectedTaskId = 7; switchTab('REPORT');
          const original = fetch;
          window.fetch = (url, options) => String(url) === '/api/results/batch_save'
            ? new Promise(resolve => { window.releaseSave = async () => {
                window.fetch = original; resolve(await original(url, options));
              }; }) : original(url, options);
        }""")
        reason = self.page.locator('[data-manual-reason="11"]')
        reason.fill('第一版')
        reason.blur()
        self.page.evaluate('() => { window.pendingSave = saveAllReportChanges(); }')
        reason.fill('保存期间的新修改')
        reason.blur()
        self.page.evaluate('releaseSave()')
        self.page.evaluate('pendingSave')
        self.assertTrue(self.page.evaluate('hasUnsavedChanges'))
        self.assertEqual(reason.input_value(), '保存期间的新修改')
        self.assertEqual(self.requests('/api/results/batch_save')[0]['body']['creators'][0]['manual_reason'], '第一版')
        self.page.evaluate('saveAllReportChanges()')
        self.assertEqual(self.requests('/api/results/batch_save')[-1]['body']['creators'][0]['manual_reason'], '保存期间的新修改')
        self.assertFalse(self.page.evaluate('hasUnsavedChanges'))

    def test_selection_refresh_failure_preserves_error_and_blocks_stale_candidate_actions(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.evaluate("""() => {
          const original = fetch;
          window.fetch = async (url, options) => {
            if (String(url).split('?')[0].endsWith('/candidates') && window.failCandidateLoad)
              return {ok: false, json: async () => ({error: 'fixture disconnected'})};
            const response = await original(url, options);
            if (String(url).endsWith('/selection')) window.failCandidateLoad = true;
            return response;
          };
        }""")
        self.page.locator('#saveCandidateSelectionBtn').click()
        self.page.wait_for_function("document.getElementById('candidateList').textContent.includes('加载失败')")
        self.assertIn('加载失败', self.page.locator('#candidateList').text_content())
        self.assertTrue(self.page.locator('#candidateList input').evaluate_all('nodes => nodes.every(n => n.disabled)'))
        self.assertTrue(self.page.locator('#confirmEnrichmentBtn').is_disabled())
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.evaluate("admitCandidate(11, document.createElement('button'))")
        self.assertEqual(self.requests('/report'), [])
        self.assertEqual(self.requests('/enrich'), [])

    def test_failed_kb_filter_invalidates_previous_selection_and_import(self):
        self.page.evaluate("switchTab('KB')")
        self.page.locator('[data-kb-select="11"]').check()
        self.page.locator('#kbImportTask').select_option('7')
        self.page.evaluate('failRequests = true')
        self.page.locator('#kbTagFilter').fill('新筛选')
        self.page.evaluate('loadKnowledgeBase()')
        self.assertTrue(self.page.locator('#kbImportBtn').is_disabled())
        self.assertEqual(self.page.evaluate('kbSelection.size'), 0)
        self.assertNotIn('候选甲', self.page.locator('#kbTableBody').text_content())
        self.assertIn('加载失败', self.page.locator('#kbFilterStatus').text_content())
        self.page.evaluate('selectAllKB(true); importKBSelection()')
        self.assertEqual(self.requests('/api/kb/import'), [])

    def test_delete_formal_entry_updates_qualified_delivery_and_gap(self):
        self.page.evaluate("""() => {
          report = Array.from({length: 5}, (_, i) => ({creator_id: i + 11,
            nickname: '合成账号' + i, qualified: true, recommend_level: '高推荐'}));
          task.counts = {...task.counts, report: 5, qualified: 5, target: 5};
          currentSelectedTaskId = 7; switchTab('REPORT');
          const original = fetch;
          window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (String(url) === '/api/results/delete_row') {
              report = report.filter(c => c.creator_id !== JSON.parse(options.body).creator_id);
              task.counts = {...task.counts, report: 4, qualified: 4};
              return {ok: true, json: async () => ({status: 'ok'})};
            }
            return response;
          };
        }""")
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.evaluate('deleteReportRow(11)')
        self.assertEqual(self.page.locator('#reportTableBody tr').count(), 4)
        self.assertIn('已核实达标 4', self.page.locator('#reportDeliverySummary').text_content())
        self.assertIn('缺口 1', self.page.locator('#reportDeliverySummary').text_content())

    def test_expired_completed_enrichment_requires_explicit_missing_exception(self):
        self.page.evaluate("""() => {
          creators[0].enrichment = {status: 'completed', identity_verified: true,
            median_play_30d: 1000, audience: {gender: {}, age: {}, region: {}}, missing_fields: []};
          const original = fetch;
          window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (String(url).endsWith('/report')) return {ok: true, json: async () =>
              JSON.parse(options.body).allow_missing ? {admitted: [11], pending: []}
              : {admitted: [], pending: [11], details: {'11': ['enrichment.expired']}}};
            return response;
          };
        }""")
        self.open_drawer()
        self.open_detail(11)
        self.page.locator('[data-admit-missing="11"]').click()
        self.assertEqual(len(self.requests('/report')), 1)
        self.assertFalse(self.requests('/report')[0]['body']['allow_missing'])
        self.assertIn('enrichment.expired', self.page.locator('#candidateActionStatus').text_content())
        override = self.page.locator('[data-force-admit-missing="11"]')
        self.assertEqual(override.count(), 1)
        self.assertIn('带缺失加入', override.text_content())
        self.page.on('dialog', lambda dialog: dialog.accept())
        override.click()
        self.assertEqual(len(self.requests('/report')), 2)
        self.assertTrue(self.requests('/report')[-1]['body']['allow_missing'])

    def test_ime_enter_does_not_send_until_composition_is_finished(self):
        input_box = self.page.locator('#chatInput')
        input_box.fill('正在输入中文')
        input_box.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        self.assertEqual(self.requests('/api/chat'), [])
        self.assertEqual(input_box.input_value(), '正在输入中文')
        input_box.dispatch_event('keydown', {'key': 'Enter', 'keyCode': 229})
        self.assertEqual(self.requests('/api/chat'), [])
        input_box.dispatch_event('keydown', {'key': 'Enter'})
        self.assertEqual(len(self.requests('/api/chat')), 1)

    def test_mobile_drawer_traps_focus_inerts_background_and_restores_opener(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('#progressCandidatesBtn').click()
        drawer = self.page.locator('#candidateDrawer')
        self.assertEqual(drawer.get_attribute('role'), 'dialog')
        self.assertEqual(drawer.get_attribute('aria-modal'), 'true')
        self.assertTrue(self.page.locator('body > aside').evaluate('n => n.inert'))
        self.assertTrue(self.page.locator('#viewCHAT').evaluate('n => n.inert'))
        self.page.locator('#saveCandidateSelectionBtn').focus()
        self.page.keyboard.press('Tab')
        # The footer review action is the next focusable when pending leads exist.
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'reviewPendingBtn')
        self.page.keyboard.press('Tab')
        self.assertEqual(self.page.evaluate('document.activeElement.title'), '刷新候选')
        self.page.keyboard.press('Shift+Tab')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'reviewPendingBtn')
        self.page.locator('#closeCandidateDrawerBtn').click()
        self.assertFalse(self.page.locator('body > aside').evaluate('n => n.inert'))
        self.assertFalse(self.page.locator('#viewCHAT').evaluate('n => n.inert'))
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'progressCandidatesBtn')

    def test_desktop_drawer_is_nonmodal_and_resize_releases_mobile_inert_state(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open_drawer()
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.page.wait_for_function("document.getElementById('candidateDrawer').getAttribute('aria-modal') !== 'true'")
        self.assertNotEqual(self.page.locator('#candidateDrawer').get_attribute('aria-modal'), 'true')
        self.assertFalse(self.page.locator('#viewCHAT').evaluate('n => n.inert'))
        self.page.locator('#chatInput').focus()
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'chatInput')


    def test_compact_two_line_list_fits_eight_rows_and_details_stay_closed(self):
        self.open_drawer()
        self.page.evaluate("""() => {
          const make = (id) => {
            const topic = {topic_status: 'matched', method: 'text_llm',
              posts: Array.from({length: 6}, (_, i) => ({aweme_id: 'p' + (i + 1),
                decision: 'yes', evidence_id: 'p' + (i + 1)})),
              account: {account_type: 'independent_creator', evidence_ids: ['p1', 'p2'],
                reason: 'Synthetic personal creator across posts'},
              creator_pattern: 'consistent_creator', sample_count: 6, matched_count: 6,
              unknown_count: 0, evidence_ids: ['p1', 'p2'], reason: '持续食品方向',
              prompt_version: 'fixture', model: 'fixture-model', error_code: null,
              truncated_ids: [], ratio: 1, min_samples: 5, min_ratio: 0.6};
            return {creator_id: id, nickname: id % 3 === 0
                      ? '这是一个非常非常长的达人昵称用于测试省略号效果' + id : '候选' + id,
                    followers: id % 2 ? 123456 : null, avg_likes: id % 2 ? 8600 : null,
                    max_likes: id % 2 ? 30000 : null, reason: '合成数值通过',
                    candidate_status: 'eligible', profile_url: 'https://example.com/' + id,
                    latest_post_date: '2026-11-04', captured_at: '2026-11-05',
                    assessment: {overall_status: 'eligible', numeric: {status: 'passed',
                      failed_fields: [], missing_fields: [], reason_codes: [], details: []},
                      topic, assessment: {reason: '长理由'.repeat(20)}},
                    sample_posts: [{aweme_id: '7123456789012345678', desc: '证据作品', likes: 10}]};
          };
          creators = Array.from({length: 12}, (_, i) => make(100 + i));
          task.counts = {candidates: 12, discovered: 12, eligible: 12, pending: 0, rejected: 0,
            prequalified: 12, topic_reviewed: 12, selected: 0, enriched: 0, report: 0,
            qualified: 0, target: 5};
          candidateCreators = [];
          candidatesLoaded = false;
          return loadCandidates(7);
        }""")
        self.page.wait_for_function('candidateCreators.length === 12')
        for width, height, drawer_max in [(1440, 900, 360), (1920, 1080, 360)]:
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': height})
                drawer = self.page.locator('#candidateDrawer').bounding_box()
                self.assertGreaterEqual(drawer['width'], 320)
                self.assertLessEqual(drawer['width'], drawer_max)
                rows = self.page.locator('#candidateList .candidate-row')
                self.assertEqual(rows.count(), 12)
                heights = rows.evaluate_all('nodes => nodes.map(n => n.getBoundingClientRect().height)')
                self.assertTrue(all(68 <= value <= 72 for value in heights), heights)
                list_box = self.page.locator('#candidateList').bounding_box()
                visible = rows.evaluate_all("""(nodes, box) => nodes.filter(n => {
                  const r = n.getBoundingClientRect();
                  return r.top >= box.y - 1 && r.bottom <= box.y + box.height + 1;
                }).length""", list_box)
                self.assertGreaterEqual(visible, 8)
                self.assertTrue(self.page.locator('#candidateDetail').is_hidden())
                list_text = self.page.locator('#candidateList').text_content()
                for forbidden in ('方向审核依据', '受众', '带缺失加入报告', '审核提示词版本'):
                    self.assertNotIn(forbidden, list_text)
                self.assertIn('粉丝', list_text)
                self.assertIn('均赞', list_text)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        drawer = self.page.locator('#candidateDrawer').bounding_box()
        self.assertLessEqual(drawer['width'], 390)
        self.open_detail(100)
        self.assertIn('方向审核依据', self.page.locator('#candidateDetail').text_content())
        self.assertTrue(self.page.locator('#candidateDetail').is_visible())
        self.page.locator('#candidateDetailBack').click()
        self.assertTrue(self.page.locator('#candidateDetail').is_hidden())

    def test_switch_filters_keeps_selection_and_shows_empty_state(self):
        self.open_drawer()
        self.page.locator('[data-candidate-select="11"]').check()
        self.filter_candidates('pending')
        self.assertFalse(self.page.locator('[data-candidate-select="11"]').count())
        self.filter_candidates('eligible')
        self.assertTrue(self.page.locator('[data-candidate-select="11"]').is_checked())
        self.page.evaluate("""() => {
          task.counts = {...task.counts, eligible: 0, pending: 2};
          creators.forEach(c => c.candidate_status = 'pending');
          return loadCandidates(7);
        }""")
        empty = self.page.locator('#candidateList').text_content()
        self.assertIn('暂无通过数值与方向初筛', empty)
        self.assertIn('本轮共发现', empty)


if __name__ == '__main__':
    unittest.main()
