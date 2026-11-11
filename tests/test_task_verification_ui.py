"""Offline task verification and current-session enrichment UI contracts."""
import re
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


UI = Path(__file__).resolve().parents[1] / 'app' / 'ui.html'


class TaskVerificationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1800)
        self.page.route('**/*', lambda route: route.abort())
        self.page.clock.install()
        self.page.evaluate("""() => {
          window.lucide = {createIcons() {}};
          window.requests = [];
          window.verificationJobSequence = 0;
          window.cancelledJobs = [];
          window.task = {id: 7, status: 'completed', keyword: '食品', counts: {
            discovered: 1, eligible: 1, selected: 0, report: 0, target: 5}, progress: {}};
          window.creators = [{creator_id: 11, nickname: '候选甲', candidate_status: 'eligible',
            followers: 20000, avg_likes: 1234, sample_posts: [], tags: []}];
          window.messages = [];
          window.report = [];
          window.views = Object.fromEntries(['douyin', 'xingtu'].map(platform => [platform, {
            platform, connected: true, status: 'saved', label: '凭据待验证', busy: false,
            login: {status: 'idle', message: ''}}]));
          window.fetch = async (url, options = {}) => {
            const path = String(url).split('?')[0];
            requests.push({url: String(url), method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null});
            let data = {};
            if (path.endsWith('/verify')) {
              if (window.holdVerify) await new Promise(r => window.releaseVerify = r);
              if (window.verifyError) return {ok: false, json: async () => ({error: {message: verifyError}})};
              Object.assign(views.douyin, {busy: true, status: 'verification_required',
                login: {status: 'waiting', kind: 'search_verification', task_id: Number(path.split('/')[3]),
                  job_id: 'verification-job-' + (++verificationJobSequence),
                  message: '请在任务搜索页完成安全验证'}});
              data = window.verifyAcceptedBody || {platforms: [views.douyin]};
            } else if (path === '/api/platforms/douyin/cancel') {
              const body = JSON.parse(options.body);
              if (body.expected_job_id && body.expected_job_id !== views.douyin.login.job_id) {
                return {ok: false, status: 409,
                  json: async () => ({error: {message: '验证任务已变更，请刷新状态后重试'}})};
              }
              cancelledJobs.push(views.douyin.login.job_id);
              Object.assign(views.douyin, {busy: false,
                login: {...views.douyin.login, status: 'cancelled', message: '已取消人工验证'}});
              data = window.cancelAcceptedBody || {platform: views.douyin};
            } else if (path === '/api/platforms/status') {
              if (window.statusOffline) throw Error('状态暂时离线');
              data = {platforms: views};
            } else if (path === '/api/status') data = {task, is_running: task.status === 'enriching'};
            else if (path === '/api/chat/history') data = {messages};
            else if (path.endsWith('/candidates')) data = {task, creators, summary: task.counts};
            else if (path.endsWith('/enrich')) {
              if (window.holdEnrich) await new Promise(r => window.releaseEnrich = r);
              task = {...task, status: 'enriching', finished_at: null};
              data = {success: true};
            } else if (path === '/api/results') {
              if (window.holdReport) await new Promise(r => window.releaseReport = r);
              const id = Number(new URL(url, 'https://offline.invalid').searchParams.get('task_id')) || task.id;
              const selected = {...task, id};
              data = {creators: report, selected_task: selected, tasks: [selected]};
            } else if (path === '/api/settings') data = {settings: {}};
            else if (path === '/api/kb') data = {creators: []};
            return {ok: true, status: path.endsWith('/verify') ? 202 : 200,
              json: async () => structuredClone(data)};
          };
        }""")
        html = re.sub(r'<script src="[^"]+"></script>', '', UI.read_text(encoding='utf-8'))
        html = re.sub(r'@import url\([^;]+;', '', html)
        self.page.set_content(html)

    def requests(self, path):
        return self.page.evaluate("p => requests.filter(r => r.url.split('?')[0] === p)", path)

    def open_verification(self, task_id=7):
        self.page.evaluate("""id => renderChatMessages([{role: 'assistant', content: '检索受阻',
          meta: {task_finished: true, task_id: id, status: 'blocked', counts: {discovered: 0}}}])""", task_id)
        self.page.get_by_role('button', name='查看受阻详情', exact=True).click()

    def submit_enrichment(self):
        self.page.evaluate('openCandidateDrawer(7)')
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('#submitEnrichmentBtn').click()

    def finish_enrichment(self, status='completed', rows=1):
        self.page.evaluate("""async ({status, rows}) => {
          task = {...task, status, finished_at: '2026-11-06T01:00:00Z',
            counts: {...task.counts, report: rows}};
          report = rows ? [{creator_id: 11, nickname: '正式条目', qualified: true}] : [];
          messages = [{role: 'assistant', content: '本轮补全结束', meta: {
            task_finished: true, task_id: 7, stage: 'enrichment', status, counts: task.counts}}];
          await checkGlobalStatus();
        }""", {'status': status, 'rows': rows})

    def test_blocked_action_opens_actual_historical_task_and_reads_status_after_acceptance(self):
        self.open_verification(9)
        self.assertEqual(self.requests('/api/tasks/9/verify'), [
            {'url': '/api/tasks/9/verify', 'method': 'POST', 'body': {}}])
        self.assertEqual(self.requests('/api/tasks/7/verify'), [])
        self.assertEqual(self.requests('/api/tasks/9/candidates'), [])
        panel = self.page.locator('#taskVerification')
        self.assertTrue(panel.is_visible())
        self.assertIn('#9', panel.inner_text())
        self.assertIn('请在任务搜索页完成安全验证', panel.inner_text())
        self.assertIn('重新执行原任务', panel.inner_text())
        self.assertIn('验证期间请保持窗口打开；成功后保存会话，超时/取消保留旧凭据；采集不会自动重启', panel.inner_text())
        self.assertNotIn('验证窗口不会自动关闭', panel.inner_text())
        self.assertTrue(self.page.locator('#cancelTaskVerificationBtn').is_enabled())
        self.assertEqual(len(self.requests('/api/platforms/status')), 2)

    def test_verification_polls_to_success_without_closing_or_restarting(self):
        self.open_verification()
        self.page.evaluate("Object.assign(views.douyin, {busy: false, login: {...views.douyin.login, status: 'succeeded', message: '验证已完成'}})")
        self.page.clock.fast_forward(1600)
        panel = self.page.locator('#taskVerification')
        self.assertTrue(panel.is_visible())
        self.assertIn('验证已完成', panel.inner_text())
        self.assertIn('重新执行原任务', panel.inner_text())
        count = len(self.requests('/api/platforms/status'))
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        posts = self.page.evaluate("requests.filter(r => r.method === 'POST').map(r => r.url)")
        self.assertEqual(posts, ['/api/tasks/7/verify'])

    def test_cancel_accepts_success_only_then_reads_status(self):
        self.page.evaluate('window.cancelAcceptedBody = {success: true}')
        self.open_verification()
        self.page.locator('#cancelTaskVerificationBtn').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel')[0]['body'],
                         {'expected_job_id': 'verification-job-1'})
        self.assertIn('已取消人工验证', self.page.locator('#taskVerification').inner_text())
        self.assertTrue(self.page.locator('#retryTaskVerificationBtn').is_enabled())
        count = len(self.requests('/api/platforms/status'))
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/status')), count)

    def assert_stale_cancel_is_rejected(self, task_id):
        self.open_verification(7)
        self.page.evaluate("""id => {
          views.douyin.login = {...views.douyin.login, task_id: id,
            job_id: 'replacement-job-' + id, status: 'waiting'};
          views.douyin.busy = true;
        }""", task_id)
        self.page.locator('#cancelTaskVerificationBtn').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel')[-1]['body'],
                         {'expected_job_id': 'verification-job-1'})
        self.assertIn('请刷新', self.page.locator('#taskVerification').inner_text())
        self.assertEqual(self.page.evaluate('cancelledJobs'), [])
        self.assertTrue(self.page.evaluate('views.douyin.busy'))
        self.assertEqual(self.page.evaluate('views.douyin.login.task_id'), task_id)
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/douyin/cancel')), 1)

    def test_stale_cached_cancel_cannot_stop_a_different_task(self):
        self.assert_stale_cancel_is_rejected(9)

    def test_stale_cached_cancel_cannot_stop_a_same_task_retry(self):
        self.assert_stale_cancel_is_rejected(7)

    def test_task_cancel_without_job_token_refuses_and_asks_for_refresh(self):
        self.open_verification()
        self.page.evaluate("delete views.douyin.login.job_id; loadPlatformStatus()")
        self.page.locator('#cancelTaskVerificationBtn').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel'), [])
        self.assertIn('刷新', self.page.locator('#taskVerification').inner_text())
        self.assertTrue(self.page.evaluate('views.douyin.busy'))

    def test_retry_same_task_cancels_with_the_new_job_token(self):
        self.open_verification()
        self.page.evaluate("Object.assign(views.douyin, {busy: false, login: {...views.douyin.login, status: 'failed'}})")
        self.page.clock.fast_forward(1600)
        self.page.locator('#retryTaskVerificationBtn').click()
        self.page.locator('#cancelTaskVerificationBtn').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel')[-1]['body'],
                         {'expected_job_id': 'verification-job-2'})
        self.assertEqual(self.page.evaluate('cancelledJobs'), ['verification-job-2'])

    def test_generic_platform_cancel_sends_the_cached_job_token(self):
        self.open_verification()
        self.page.evaluate("switchTab('SETTINGS')")
        self.page.locator('[data-platform="douyin"] [data-platform-action="cancel"]').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel')[-1]['body'],
                         {'expected_job_id': 'verification-job-1'})
        self.assertEqual(self.page.evaluate('cancelledJobs'), ['verification-job-1'])

    def test_generic_stale_cancel_rejection_never_retries_without_token(self):
        self.open_verification()
        self.page.evaluate("switchTab('SETTINGS')")
        self.page.evaluate("views.douyin.login = {...views.douyin.login, task_id: 9, job_id: 'replacement-job-9'}")
        self.page.locator('[data-platform="douyin"] [data-platform-action="cancel"]').click()
        self.assertEqual(self.requests('/api/platforms/douyin/cancel')[-1]['body'],
                         {'expected_job_id': 'verification-job-1'})
        self.assertIn('请刷新', self.page.locator('[data-platform="douyin"]').inner_text())
        self.assertEqual(self.page.evaluate('cancelledJobs'), [])
        self.assertTrue(self.page.evaluate('views.douyin.busy'))
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/douyin/cancel')), 1)

    def test_verify_conflict_is_visible_and_retryable(self):
        self.page.evaluate("window.verifyError = '另一个任务正在使用浏览器'")
        self.open_verification()
        self.assertIn('另一个任务正在使用浏览器', self.page.locator('#taskVerification').inner_text())
        self.page.evaluate('verifyError = null')
        self.page.locator('#retryTaskVerificationBtn').click()
        self.assertEqual(len(self.requests('/api/tasks/7/verify')), 2)
        self.assertIn('请在任务搜索页完成安全验证', self.page.locator('#taskVerification').inner_text())

    def test_duplicate_click_is_locked_and_navigation_does_not_cancel_browser(self):
        self.page.evaluate('window.holdVerify = true')
        self.open_verification()
        self.page.get_by_role('button', name='查看受阻详情', exact=True).click()
        self.assertEqual(len(self.requests('/api/tasks/7/verify')), 1)
        self.page.evaluate("switchTab('KB'); releaseVerify()")
        self.page.wait_for_function('requests.filter(r => r.url === "/api/platforms/status").length === 2')
        self.page.clock.fast_forward(5000)
        self.assertEqual(self.page.evaluate('currentTab'), 'KB')
        self.assertEqual(self.requests('/api/platforms/douyin/cancel'), [])
        self.assertEqual(self.requests('/api/execute'), [])

    def test_failed_verification_poll_is_visible_and_manual_refresh_recovers(self):
        self.open_verification()
        self.page.evaluate('window.statusOffline = true')
        self.page.clock.fast_forward(1600)
        self.assertIn('状态暂时离线', self.page.locator('#taskVerification').inner_text())
        count = len(self.requests('/api/platforms/status'))
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        self.page.evaluate('statusOffline = false')
        self.page.locator('#refreshTaskVerificationBtn').click()
        self.assertIn('请在任务搜索页完成安全验证', self.page.locator('#taskVerification').inner_text())

    def test_completed_verification_can_open_a_different_task_without_an_extra_retry(self):
        self.open_verification(7)
        self.page.evaluate("Object.assign(views.douyin, {busy: false, login: {...views.douyin.login, status: 'succeeded', message: '验证已完成'}})")
        self.page.clock.fast_forward(1600)
        self.open_verification(9)
        self.assertEqual(len(self.requests('/api/tasks/9/verify')), 1)
        self.assertIn('#9', self.page.locator('#taskVerification').inner_text())

    def test_acceptance_does_not_require_any_platform_envelope(self):
        self.page.evaluate('window.verifyAcceptedBody = {success: true}')
        self.open_verification()
        self.assertIn('请在任务搜索页完成安全验证', self.page.locator('#taskVerification').inner_text())
        self.assertTrue(self.page.locator('#cancelTaskVerificationBtn').is_enabled())

    def test_unrelated_job_cannot_be_displayed_or_cancelled_as_this_tasks_verification(self):
        self.open_verification(7)
        self.page.evaluate("views.douyin.login = {kind: 'search_verification', task_id: 9, status: 'waiting', message: '另一个任务的私有进度'}")
        self.page.clock.fast_forward(1600)
        panel = self.page.locator('#taskVerification')
        self.assertNotIn('另一个任务的私有进度', panel.inner_text())
        self.assertIn('不属于', panel.inner_text())
        self.assertTrue(self.page.locator('#cancelTaskVerificationBtn').is_hidden())
        self.page.evaluate('runTaskVerification(true)')
        self.assertEqual(self.requests('/api/platforms/douyin/cancel'), [])
        count = len(self.requests('/api/platforms/status'))
        self.page.clock.fast_forward(5000)
        self.assertEqual(len(self.requests('/api/platforms/status')), count)

    def test_submitted_enrichment_opens_its_actual_report_once(self):
        self.submit_enrichment()
        self.finish_enrichment()
        self.assertEqual(self.page.evaluate('currentTab'), 'REPORT')
        self.page.wait_for_function('currentSelectedTaskId === 7')
        self.assertEqual(self.page.locator('#reportTableBody tr').count(), 1)
        self.assertIn('正式条目', self.page.locator('#reportTableBody').inner_text())
        self.assertEqual([r['url'] for r in self.requests('/api/results')], ['/api/results?task_id=7'])
        self.page.evaluate('checkGlobalStatus()')
        self.page.evaluate('loadChatHistory()')
        self.assertEqual(len(self.requests('/api/results')), 1)

    def test_enrichment_outcomes_use_batch_results_not_presence_of_records(self):
        for failed, rows, expected in (
            (5, 0, '星图补全无成功'),
            (3, 2, '星图补全部分成功'),
            (0, 5, '星图补全全部成功'),
        ):
            with self.subTest(failed=failed):
                self.page.evaluate("""({failed, rows}) => {
                  task = {...task, id: 21, status: 'completed', counts: {
                    selected: 5, enriched: 5, report: rows}, progress: {
                    enrichment_requested: 5, enrichment_processed: 5, enrichment_failed: failed}};
                  displayTaskProgress(task);
                  renderChatMessages([{role: 'assistant', content: '本轮处理结束', meta: {
                    task_finished: true, task_id: 21, stage: 'enrichment', status: task.status,
                    counts: task.counts, progress: task.progress}}]);
                }""", {'failed': failed, 'rows': rows})
                self.assertIn(expected, self.page.locator('#taskProgressText').inner_text())
                card = self.page.locator('#chatMessages')
                self.assertIn(expected, card.inner_text())
                self.assertIn(f'成功 {5 - failed}/5', card.inner_text())
                self.assertIn(f'正式报告 {rows} 条', card.inner_text())
                self.assertNotIn('星图补全已结束', card.inner_text())
                self.assertEqual(card.get_by_role('button', name='查看正式报告', exact=True).count(),
                                 1 if rows else 0)
                if failed:
                    self.assertNotIn('bg-emerald-50', card.inner_html())

    def test_successful_platform_enrichment_does_not_claim_report_admission(self):
        self.page.evaluate("""() => {
          task = {...task, status: 'partial', counts: {report: 0}, progress: {
            enrichment_requested: 5, enrichment_processed: 5, enrichment_failed: 0,
            admission_pending: 5}};
          displayTaskProgress(task);
          renderChatMessages([{role: 'assistant', content: '资料未通过正式报告核验', meta: {
            task_finished: true, task_id: 7, stage: 'enrichment', status: task.status,
            counts: task.counts, progress: task.progress}}]);
        }""")
        text = self.page.locator('#chatMessages').inner_text()
        self.assertIn('成功 5/5', text)
        self.assertIn('正式报告 0 条', text)
        self.assertIn('暂无正式报告条目', text)
        self.assertEqual(self.page.get_by_role('button', name='查看正式报告', exact=True).count(), 0)

    def test_incomplete_batch_counters_do_not_invent_success_from_report_or_records(self):
        for progress in ({}, {'enrichment_requested': 5, 'enrichment_processed': 2,
                              'enrichment_failed': 3}):
            with self.subTest(progress=progress):
                self.page.evaluate("""progress => renderChatMessages([{role: 'assistant',
                  content: '计数待核对', meta: {task_finished: true, task_id: 21,
                    stage: 'enrichment', status: 'completed', progress,
                    counts: {selected: 5, enriched: 5, report: 2}}}])""", progress)
                text = self.page.locator('#chatMessages').inner_text()
                self.assertIn('本轮补全成功数待同步', text)
                self.assertIn('正式报告 2 条', text)
                self.assertNotIn('全部成功', text)
                self.assertNotIn('部分成功', text)
                self.assertNotIn('bg-emerald-50', self.page.locator('#chatMessages').inner_html())

    def test_report_nav_from_current_task_does_not_reuse_old_report_and_export(self):
        for rows in (0, 2):
            with self.subTest(rows=rows):
                self.page.evaluate("""async rows => {
                  await loadTaskReport(14);
                  switchTab('CHAT');
                  task = {...task, id: 21, status: 'partial', counts: {report: rows},
                    progress: {enrichment_requested: 5, enrichment_processed: 5,
                      enrichment_failed: 5 - rows}};
                  report = rows ? [{creator_id: 11, nickname: '当前任务正式条目'}] : [];
                  displayTaskProgress(task);
                  const original = fetch;
                  window.fetch = async (url, options) => {
                    const response = await original(url, options);
                    return String(url).startsWith('/api/export?')
                      ? {ok: true, blob: async () => new Blob(['offline fixture'])} : response;
                  };
                  requests = [];
                  switchTab('REPORT');
                }""", rows)
                self.page.wait_for_function('!reportLoading')
                self.assertEqual([r['url'] for r in self.requests('/api/results')],
                                 ['/api/results?task_id=21'])
                self.assertEqual(self.page.locator('#reportTaskSelect').input_value(), '21')
                self.assertEqual(self.page.evaluate('currentSelectedTaskId'), 21)
                self.page.evaluate('downloadCurrentReportExcel()')
                self.assertEqual([r['url'] for r in self.requests('/api/export')],
                                 ['/api/export?task_id=21'])
                self.assertEqual(self.requests('/api/tasks/21/report'), [])

    def test_submitted_current_task_overrides_old_report_but_history_links_stay_explicit(self):
        self.page.evaluate("""async () => {
          await loadTaskReport(14);
          task.id = 21;
          await openCandidateDrawer(21);
        }""")
        self.page.locator('[data-candidate-select="11"]').check()
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('#submitEnrichmentBtn').click()
        self.page.evaluate("""async () => {
          task = {...task, status: 'completed', counts: {report: 1}, progress: {
            enrichment_requested: 1, enrichment_processed: 1, enrichment_failed: 0}};
          report = [{creator_id: 11, nickname: '当前任务正式条目'}];
          await checkGlobalStatus();
        }""")
        self.page.wait_for_function('currentSelectedTaskId === 21 && !reportLoading')
        self.assertEqual(self.page.locator('#reportTaskSelect').input_value(), '21')
        self.page.evaluate("""() => {
          switchTab('CHAT');
          renderChatMessages([{role: 'assistant', content: '历史补全', meta: {
            task_finished: true, task_id: 14, stage: 'enrichment', status: 'completed',
            counts: {report: 1}}}]);
        }""")
        self.page.get_by_role('button', name='查看正式报告', exact=True).click()
        self.page.wait_for_function('currentSelectedTaskId === 14 && !reportLoading')

    def test_zero_success_batch_with_preexisting_report_does_not_auto_navigate(self):
        self.submit_enrichment()
        self.page.evaluate("""async () => {
          task = {...task, status: 'partial', counts: {report: 1}, progress: {
            enrichment_requested: 1, enrichment_processed: 1, enrichment_failed: 1}};
          await checkGlobalStatus();
        }""")
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])

    def test_progress_header_distinguishes_enrichment_from_stale_search_step(self):
        for status, expected in (
            ('enriching', '星图补全中'),
            ('completed', '星图补全暂无正式条目'),
            ('partial', '星图补全暂无正式条目'),
            ('failed', '星图补全失败'),
            ('interrupted', '星图补全中断'),
            ('stopped', '星图补全已停止'),
        ):
            with self.subTest(status=status):
                self.page.evaluate("""status => displayTaskProgress({id: 14, status,
                  step: 'searching', counts: {report: 0},
                  progress: status === 'enriching' ? {} : {enrichment_requested: 1}})""", status)
                text = self.page.locator('#taskProgressText').inner_text()
                self.assertTrue(text.startswith(expected + ' · '), text)
                self.assertNotIn('检索', text)
                self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
                self.assertEqual(self.requests('/api/results'), [])

    def test_discovery_header_without_enrichment_marker_retains_search_phase(self):
        for status, expected in (('partial', '检索未完整完成'), ('completed', '候选待选择')):
            with self.subTest(status=status):
                self.page.evaluate("""status => displayTaskProgress({id: 14, status,
                  counts: {report: 0}, progress: {}})""", status)
                self.assertTrue(self.page.locator('#taskProgressText').inner_text().startswith(expected + ' · '))
                self.assertEqual(self.requests('/api/results'), [])

    def test_zero_rows_or_failed_enrichment_never_forces_report(self):
        self.submit_enrichment()
        self.finish_enrichment(status='partial', rows=0)
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])
        self.assertIn('暂无正式报告条目', self.page.locator('#chatMessages').inner_text())
        self.assertEqual(self.page.get_by_role('button', name='查看正式报告', exact=True).count(), 0)

    def test_failed_enrichment_with_preexisting_rows_stays_in_candidates(self):
        self.submit_enrichment()
        self.finish_enrichment(status='failed', rows=1)
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])

    def test_partial_enrichment_with_actual_rows_can_open_report(self):
        self.submit_enrichment()
        self.finish_enrichment(status='partial', rows=1)
        self.assertEqual(self.page.evaluate('currentTab'), 'REPORT')

    def test_old_history_and_reload_never_auto_navigate(self):
        self.finish_enrichment()
        self.page.evaluate('loadChatHistory()')
        self.page.evaluate('loadChatHistory()')
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])
        self.assertIn('星图补全结果待核实', self.page.locator('#chatMessages').inner_text())
        self.assertNotIn('搜索已结束', self.page.locator('#chatMessages').inner_text())
        self.page.get_by_role('button', name='查看正式报告', exact=True).click()
        self.page.wait_for_function('currentSelectedTaskId === 7')

    def test_later_tab_navigation_wins_over_enrichment_completion(self):
        self.submit_enrichment()
        self.page.evaluate("switchTab('KB')")
        self.finish_enrichment()
        self.assertEqual(self.page.evaluate('currentTab'), 'KB')
        self.assertEqual(self.requests('/api/results'), [])

    def test_navigation_while_submission_pending_does_not_get_stolen(self):
        self.page.evaluate('window.holdEnrich = true')
        self.submit_enrichment()
        self.page.evaluate('openCandidateDrawer(9)')
        self.page.evaluate('releaseEnrich()')
        self.page.wait_for_function('!enrichmentSubmitting')
        self.finish_enrichment()
        self.assertEqual(self.page.evaluate('candidateTaskId'), 9)
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])

    def test_selected_report_task_is_preserved_after_user_navigation(self):
        self.submit_enrichment()
        self.page.evaluate("goToReportTab(9)")
        self.page.wait_for_function('currentSelectedTaskId === 9')
        before = len(self.requests('/api/results'))
        self.finish_enrichment()
        self.assertEqual(self.page.evaluate('currentSelectedTaskId'), 9)
        self.assertEqual(len(self.requests('/api/results')), before)

    def test_status_request_started_before_submission_cannot_finish_new_enrichment(self):
        self.page.evaluate("""() => {
          task.counts.report = 1;
          const original = fetch;
          window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (String(url).startsWith('/api/status') && !window.heldStatus) {
              window.heldStatus = true;
              await new Promise(r => window.releaseStatus = r);
            }
            return response;
          };
          checkGlobalStatus();
        }""")
        self.submit_enrichment()
        self.page.evaluate('releaseStatus()')
        self.page.wait_for_function('!statusChecking')
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])
        self.finish_enrichment(rows=0)
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')

    def test_fast_completion_before_first_poll_still_refreshes_enrichment_card(self):
        self.submit_enrichment()
        self.page.evaluate("""() => {
          task.status = 'completed';
          task.counts.report = 0;
          messages = [{role: 'assistant', content: '没有粉丝画像，未生成正式条目', meta: {
            task_finished: true, task_id: 7, stage: 'enrichment', status: 'completed', counts: task.counts}}];
          return loadCandidates(7);
        }""")
        self.page.evaluate('checkGlobalStatus()')
        self.assertIn('星图补全暂无正式条目', self.page.locator('#chatMessages').inner_text())
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')

    def test_previous_completion_cannot_consume_a_new_batch_during_history_refresh(self):
        self.submit_enrichment()
        self.page.evaluate("""() => {
          const original = fetch;
          window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (String(url) === '/api/chat/history' && !window.heldHistory) {
              window.heldHistory = true;
              await new Promise(r => window.releaseHistory = r);
            }
            return response;
          };
          task = {...task, status: 'completed', counts: {...task.counts, report: 1}};
          checkGlobalStatus();
        }""")
        self.page.wait_for_function('Boolean(window.releaseHistory)')
        # A new selection on the same task is a new batch, not the previous terminal response.
        self.page.locator('#confirmEnrichmentBtn').click()
        self.page.locator('#submitEnrichmentBtn').click()
        self.page.evaluate('releaseHistory()')
        self.page.wait_for_function('!statusChecking')
        self.assertEqual(self.page.evaluate('currentTab'), 'CHAT')
        self.assertEqual(self.requests('/api/results'), [])
        self.finish_enrichment()
        self.assertEqual(self.page.evaluate('currentTab'), 'REPORT')

    def test_candidate_failure_reason_is_readable_and_inert(self):
        self.page.evaluate("""creators[0].enrichment = {status: 'unavailable',
          reason: 'browser_or_saved_session_unavailable', identity_verified: false}""")
        self.page.evaluate('openCandidateDrawer(7)')
        self.page.locator('[data-candidate-detail="11"]').first.click()
        text = self.page.locator('#candidateDetail').inner_text()
        self.assertIn('浏览器或已保存的登录会话不可用', text)
        self.page.evaluate("""() => {
          creators[0].enrichment.reason = '<img src=x onerror="window.injected=true">';
          return loadCandidates(7);
        }""")
        self.assertIn('<img', self.page.locator('#candidateDetail').inner_text())
        self.assertEqual(self.page.locator('#candidateDetail img').count(), 0)
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))

    def test_customer_entitlement_required_shows_readable_reason(self):
        self.page.evaluate("""creators[0].enrichment = {status: 'permission_denied',
          reason: 'customer_entitlement_required', identity_verified: false}""")
        self.page.evaluate('openCandidateDrawer(7)')
        self.page.locator('[data-candidate-detail="11"]').first.click()
        text = self.page.locator('#candidateDetail').inner_text()
        self.assertIn('星图客户权益等级不足，无法读取更多数据', text)
        self.assertNotIn('customer_entitlement_required', text)

    def test_audience_and_identity_failures_show_specific_readable_reasons(self):
        self.page.evaluate('openCandidateDrawer(7)')
        self.page.locator('[data-candidate-detail="11"]').first.click()
        for reason, expected in (
            ('audience_permission_denied', '星图客户权益不足，无法获取粉丝画像'),
            ('audience_schema_not_verified', '粉丝画像尚未获取'),
            ('identity_unconfirmed', '账号身份未确认'),
        ):
            with self.subTest(reason=reason):
                self.page.evaluate("""reason => {
                  creators[0].enrichment = {status: 'unavailable', reason, identity_verified: false};
                  return loadCandidates(7);
                }""", reason)
                self.assertIn(expected, self.page.locator('#candidateDetail').inner_text())


if __name__ == '__main__':
    unittest.main()
