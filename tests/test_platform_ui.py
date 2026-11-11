"""Offline UI contracts for platform authentication; no platform traffic leaves here."""
import re
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


UI = Path(__file__).resolve().parents[1] / "app" / "ui.html"


class PlatformUITests(unittest.TestCase):
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
        self.page.clock.install()
        self.page.evaluate("""() => {
          window.lucide = {createIcons() {}};
          window.requests = [];
          window.views = {
            douyin: {platform: 'douyin', connected: true, status: 'saved',
              label: '已检测到凭据，待验证', message: '请验证登录状态',
              has_saved_session: true, checked_at: null, busy: false,
              login: {status: 'idle', message: ''}},
            xingtu: {platform: 'xingtu', connected: true, status: 'missing',
              label: '尚未登录', message: '请登录巨量星图',
              has_saved_session: false, checked_at: null, busy: false,
              login: {status: 'idle', message: ''}}
          };
          window.fetch = async (url, options = {}) => {
            const path = String(url).split('?')[0];
            const request = {url: path, method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null};
            requests.push(request);
            if (path.startsWith('/api/platforms/')) {
              if (window.holdPlatform) await new Promise(resolve => window.releasePlatform = resolve);
              if (window.platformOffline) throw Error('离线，请重试');
              if (window.platformFailure) return {ok: false, status: 409,
                json: async () => ({error: {message: window.platformFailure}})};
              if (path === '/api/platforms/status') return {ok: true,
                json: async () => ({platforms: views})};
              const [, , , id, action] = path.split('/');
              views[id] = {...views[id], busy: action !== 'cancel',
                status: action === 'cancel' ? 'saved' : 'checking',
                label: action === 'cancel' ? '已检测到凭据，待验证' : '正在验证',
                login: {status: action === 'cancel' ? 'cancelled' : 'waiting',
                  message: action === 'cancel' ? '已取消登录' : '等待浏览器登录'}};
              return {ok: true, json: async () => ({platform: views[id]})};
            }
            let data = {};
            if (path === '/api/chat/history') data = {messages: []};
            if (path === '/api/status') data = {is_running: false};
            if (path === '/api/settings') data = {settings: {}};
            return {ok: true, json: async () => data};
          };
        }""")
        html = re.sub(r'<script src="[^"]+"></script>', '', UI.read_text(encoding="utf-8"))
        html = re.sub(r"@import url\([^;]+;", '', html)
        self.page.set_content(html)

    def open_settings(self):
        self.page.evaluate("switchTab('SETTINGS')")

    def card(self, platform="douyin"):
        return self.page.locator(f'[data-platform="{platform}"]')

    def button(self, name, platform="douyin"):
        return self.card(platform).get_by_role("button", name=name, exact=True)

    def requests(self, path):
        return self.page.evaluate("path => requests.filter(r => r.url === path)", path)

    def advance_poll(self):
        self.page.clock.fast_forward(1600)
        self.page.evaluate("() => Promise.resolve()")

    def test_initial_header_and_sidebar_fetch_real_states(self):
        self.assertEqual(len(self.requests('/api/platforms/status')), 1)
        for selector in ('#platformHeaderStatus', '#sidebarPlatformStatus'):
            text = self.page.locator(selector).text_content()
            self.assertIn('已检测到凭据，待验证', text)
            self.assertIn('尚未登录', text)
            self.assertNotIn('已登录', text)

    def test_xingtu_blocked_and_failed_receipts_use_current_platform_login(self):
        for status in ('blocked', 'failed'):
            with self.subTest(status=status):
                self.page.evaluate("""status => {
                  views.xingtu.busy = false;
                  switchTab('CHAT');
                  renderChatMessages([{role: 'assistant', content: '星图检索受阻', meta: {
                    task_finished: true, task_id: 9, platform: 'xingtu', status,
                    pause_reason: 'permission_denied', counts: {discovered: 0}}}]);
                }""", status)
                chat = self.page.locator('#chatMessages')
                self.assertIn('权益', chat.inner_text())
                self.assertNotIn('验证码', chat.inner_text())
                chat.get_by_role('button', name='查看星图登录与权限', exact=True).click()
                self.assertEqual(self.page.evaluate('currentTab'), 'SETTINGS')
                self.assertEqual(self.requests('/api/platforms/xingtu/login')[-1]['body'],
                                 {'replace': False})
                self.assertEqual(self.requests('/api/tasks/9/verify'), [])
                self.assertEqual(self.requests('/api/platforms/douyin/login'), [])
                self.assertEqual(len(self.requests('/api/platforms/xingtu/login')),
                                 1 if status == 'blocked' else 2)

    def test_settings_refresh_separates_connection_from_login(self):
        self.open_settings()
        self.assertEqual(len(self.requests('/api/platforms/status')), 2)
        text = self.card().text_content()
        self.assertIn('能力已连接', text)
        self.assertIn('已检测到凭据，待验证', text)
        self.assertNotIn('未接入', text)
        for platform in ('douyin', 'xingtu'):
            for action in ('验证登录', '重新登录', '更换账号'):
                self.assertTrue(self.button(action, platform).is_enabled())

    def test_all_backend_states_and_login_failures_render_without_inference(self):
        self.open_settings()
        for state in ('missing', 'saved', 'authenticated', 'expired',
                      'verification_required', 'checking', 'error'):
            self.page.evaluate("""state => {
              Object.assign(views.douyin, {status: state, label: '后端状态：' + state,
                message: '后端说明：' + state, busy: false, checked_at: '2026-11-05T12:00:00Z'});
              return loadPlatformStatus();
            }""", state)
            self.assertIn('后端状态：' + state, self.card().text_content())
            self.assertIn('后端说明：' + state, self.card().text_content())
            self.assertIn('2026-11-05T12:00:00Z', self.card().text_content())
        for state in ('failed', 'timeout', 'cancelled', 'succeeded'):
            self.page.evaluate("""state => {
              views.douyin.login = {status: state, message: '登录结果：' + state};
              return loadPlatformStatus();
            }""", state)
            self.assertIn('登录结果：' + state, self.card().text_content())

    def test_check_busy_lock_poll_completion_updates_everywhere(self):
        self.open_settings()
        self.button('验证登录').click()
        self.assertEqual(self.requests('/api/platforms/douyin/check')[0]['body'], {})
        self.assertTrue(self.button('取消').is_visible())
        for platform in ('douyin', 'xingtu'):
            self.assertTrue(self.button('重新登录', platform).is_disabled())
        self.page.evaluate("""Object.assign(views.douyin, {busy: false, status: 'authenticated',
          label: '登录已验证', message: '验证成功', login: {status: 'succeeded', message: '登录成功'}})""")
        self.advance_poll()
        for selector in ('#platformHeaderStatus', '#sidebarPlatformStatus', '#platformCards'):
            self.assertIn('登录已验证', self.page.locator(selector).text_content())
        count = len(self.requests('/api/platforms/status'))
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        self.assertEqual(self.button('取消').count(), 0)

    def test_renew_and_cancel_use_explicit_contract(self):
        self.open_settings()
        self.button('重新登录', 'xingtu').click()
        self.assertEqual(self.requests('/api/platforms/xingtu/login')[0]['body'], {'replace': False})
        self.button('取消', 'xingtu').click()
        self.assertEqual(self.requests('/api/platforms/xingtu/cancel')[0]['body'], {})
        self.assertIn('已取消登录', self.card('xingtu').text_content())
        self.assertTrue(self.button('重新登录').is_enabled())

    def test_switch_account_requires_confirmation_and_preservation_notice(self):
        self.open_settings()
        dialogs = []

        def dismiss(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()

        self.page.on('dialog', dismiss)
        self.button('更换账号').click()
        self.assertEqual(self.requests('/api/platforms/douyin/login'), [])
        self.assertIn('更换', dialogs[0])
        self.assertIn('新登录成功前保留现有凭据', dialogs[0])
        self.page.remove_listener('dialog', dismiss)
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.button('更换账号').click()
        self.assertEqual(self.requests('/api/platforms/douyin/login')[0]['body'], {'replace': True})

    def test_double_click_and_cross_platform_submission_are_locked(self):
        self.open_settings()
        self.page.evaluate("""() => {
          holdPlatform = true;
          const button = document.querySelector('[data-platform="douyin"] [data-platform-action="login"]');
          button.click(); button.click();
          document.querySelector('[data-platform="xingtu"] [data-platform-action="check"]').click();
        }""")
        self.assertEqual(len(self.requests('/api/platforms/douyin/login')), 1)
        self.assertEqual(self.requests('/api/platforms/xingtu/check'), [])
        self.page.evaluate('holdPlatform = false; releasePlatform()')

    def test_closed_settings_stops_polling_and_reopening_resumes(self):
        self.open_settings()
        self.button('验证登录').click()
        self.page.evaluate("switchTab('CHAT')")
        count = len(self.requests('/api/platforms/status'))
        self.advance_poll()
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        self.open_settings()
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count + 2)

    def test_failed_poll_is_visible_and_does_not_retry_forever(self):
        self.open_settings()
        self.button('验证登录').click()
        self.page.evaluate('platformOffline = true')
        self.advance_poll()
        self.assertIn('离线，请重试', self.page.locator('#platformStatusMessage').text_content())
        count = len(self.requests('/api/platforms/status'))
        self.advance_poll()
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        self.page.evaluate('platformOffline = false')
        self.page.locator('#refreshPlatformStatus').click()
        self.assertEqual(len(self.requests('/api/platforms/status')), count + 1)

    def test_closing_settings_during_poll_does_not_restart_timer(self):
        self.open_settings()
        self.button('验证登录').click()
        self.page.evaluate('holdPlatform = true')
        self.advance_poll()
        self.assertTrue(self.button('取消').is_disabled())
        self.page.evaluate("switchTab('CHAT'); holdPlatform = false; releasePlatform()")
        count = len(self.requests('/api/platforms/status'))
        self.advance_poll()
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count)

    def test_status_failure_disables_auth_and_manual_refresh_recovers(self):
        self.open_settings()
        self.page.evaluate('platformOffline = true; loadPlatformStatus()')
        self.assertIn('同步失败', self.page.locator('#platformHeaderStatus').text_content())
        self.assertTrue(self.button('重新登录').is_disabled())
        count = len(self.requests('/api/platforms/status'))
        self.advance_poll()
        self.assertEqual(len(self.requests('/api/platforms/status')), count)
        self.page.evaluate('platformOffline = false')
        self.page.locator('#refreshPlatformStatus').click()
        self.assertTrue(self.button('重新登录').is_enabled())

    def test_global_running_flag_locks_auth_without_a_task_id(self):
        self.open_settings()
        self.page.evaluate("""() => {
          const originalFetch = window.fetch;
          window.fetch = async (url, options) => String(url).startsWith('/api/status')
            ? {ok: true, json: async () => ({is_running: true})} : originalFetch(url, options);
          return checkGlobalStatus();
        }""")
        self.assertTrue(self.button('重新登录').is_disabled())
        self.assertIn('搜索任务运行中', self.page.locator('#platformStatusMessage').text_content())

    def test_conflict_error_is_clear_and_retryable(self):
        self.open_settings()
        self.page.evaluate("platformFailure = '搜索或其他认证任务正在运行'")
        self.button('重新登录').click()
        self.assertIn('搜索或其他认证任务正在运行', self.card().text_content())
        self.assertTrue(self.button('重新登录').is_enabled())
        self.page.evaluate('platformFailure = null')
        self.button('重新登录').click()
        self.assertEqual(len(self.requests('/api/platforms/douyin/login')), 2)

    def test_known_running_search_disables_auth_and_restores_after_completion(self):
        self.open_settings()
        self.page.evaluate("displayTaskProgress({id: 7, status: 'running', is_running: true})")
        for platform in ('douyin', 'xingtu'):
            for action in ('验证登录', '重新登录', '更换账号'):
                self.assertTrue(self.button(action, platform).is_disabled())
        self.page.evaluate("displayTaskProgress({id: 7, status: 'completed', is_running: false})")
        self.assertTrue(self.button('重新登录').is_enabled())

    def test_api_text_is_inert_and_unlisted_secrets_never_enter_dom(self):
        self.open_settings()
        self.page.evaluate("""() => {
          Object.assign(views.douyin, {label: '<img src=x onerror="window.injected=true">',
            message: '<script>window.injected=true</script>',
            cookie: 'SECRET_COOKIE', session_path: '/private/SECRET_PATH',
            login: {status: 'failed', message: '<b>登录失败</b>', token: 'SECRET_TOKEN'}});
          return loadPlatformStatus();
        }""")
        self.assertIn('<img', self.card().text_content())
        self.assertIn('<b>登录失败</b>', self.card().text_content())
        self.assertEqual(self.card().locator('img, script, b').count(), 0)
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        html = self.page.locator('body').inner_html()
        for secret in ('SECRET_COOKIE', 'SECRET_PATH', 'SECRET_TOKEN'):
            self.assertNotIn(secret, html)


if __name__ == '__main__':
    unittest.main()
