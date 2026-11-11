"""Local, user-operated platform sign-in; never rotate accounts or solve gates."""

import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'douyin-cli-poc'
SPECS = {
    'douyin': {
        'state': TOOL / '.local-home' / '.dy' / 'cookies' / 'default.json',
        'url': 'https://creator.douyin.com/creator-micro/content/upload',
        'domains': ('douyin.com',),
    },
    'xingtu': {
        'state': TOOL / '.local-home' / '.xingtu' / 'storage_state.json',
        'url': 'https://www.xingtu.cn/ad/creator/market',
        'domains': ('xingtu.cn', 'oceanengine.com'),
    },
}
SESSION_NAMES = {'sessionid', 'sessionid_ss', 'sid_guard', 'star_sessionid', 'sid_ucp_sso_v1'}
logger = logging.getLogger(__name__)
LABELS = {'missing': '未登录', 'saved': '凭据已保存 · 待验证', 'authenticated': '已登录',
          'expired': '登录已失效', 'verification_required': '需要人工验证',
          'checking': '验证中', 'error': '验证失败'}
MESSAGES = {
    'missing': '已接入平台适配器，尚未检测到有效登录凭据。',
    'saved': '已检测到本地登录凭据，当前登录有效性及业务权限待验证。',
    'authenticated': '登录入口验证通过；搜索、画像和联系方式权限以实际操作返回为准。',
    'expired': '登录已失效或平台要求重新登录，原凭据仍保留。',
    'verification_required': '平台要求人工验证；不会自动解验证码或轮换账号。',
    'checking': '正在验证当前登录会话。',
    'error': '未能确认登录状态，请检查网络或重新验证；原凭据未删除。',
}


class SessionBusy(RuntimeError):
    pass


def _error_code(exc):
    message = str(exc).lower()
    for token, code in (
        ('execution context was destroyed', 'page_transition'),
        ('cannot find context', 'page_transition'),
        ('err_aborted', 'navigation_interrupted'),
        ('err_name_not_resolved', 'dns_error'),
        ('err_proxy_connection_failed', 'proxy_error'),
        ('err_connection_reset', 'connection_reset'),
        ('err_timed_out', 'navigation_timeout'),
        ('timeout', 'navigation_timeout'),
        ('target page, context or browser has been closed', 'window_closed'),
    ):
        if token in message:
            return code
    return 'login_failed'


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _valid_cookies(platform, state):
    if not isinstance(state, dict) or not isinstance(state.get('cookies'), list):
        return []
    now = time.time()
    result = []
    for cookie in state['cookies']:
        if not isinstance(cookie, dict) or cookie.get('name') not in SESSION_NAMES or not cookie.get('value'):
            continue
        domain = str(cookie.get('domain') or '').lstrip('.').lower()
        if not any(domain == allowed or domain.endswith('.' + allowed) for allowed in SPECS[platform]['domains']):
            continue
        expires = cookie.get('expires', -1)
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or (expires != -1 and expires <= now)):
            continue
        result.append(cookie)
    return result


def classify_page(platform, url, text, *, search_visible=False):
    """Require visible authenticated controls, not just a URL or a cookie file."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    expected = 'creator.douyin.com' if platform == 'douyin' else 'www.xingtu.cn'
    allowed = host == expected or any(host == domain or host.endswith('.' + domain)
                                    for domain in SPECS[platform]['domains'])
    if not allowed or parsed.scheme != 'https':
        return 'unknown'
    if any(part in parsed.path.lower() for part in ('captcha', '/verify')) or any(
            phrase in text for phrase in ('请完成安全验证', '请完成下列验证', '验证码中间页', '安全验证')):
        return 'verification_required'
    if any(part in parsed.path.lower() for part in ('/login', '/passport')) or any(
            phrase in text for phrase in ('扫码登录', '手机号登录', '请先登录', '登录后查看')):
        return 'expired'
    if platform == 'douyin' and host == expected and parsed.path.startswith('/creator-micro/'):
        markers = ('发布作品', '发布视频', '作品管理', '内容管理', '数据中心', '上传视频', '点击上传')
        if sum(marker in text for marker in markers) >= 2:
            return 'authenticated'
    if platform == 'xingtu' and host == expected and parsed.path.startswith('/ad/'):
        if search_visible and '达人广场' in text:
            return 'authenticated'
    return 'unknown'


class PlatformSessions:
    def __init__(self, *, state_paths=None, login_timeout=300):
        self.paths = state_paths or {key: value['state'] for key, value in SPECS.items()}
        self.login_timeout = login_timeout
        self.poll_interval = 1.0
        self.records = {}
        self.jobs = {}
        self.login_states = {}
        self._closing = False

    def _key(self, platform):
        if platform not in SPECS:
            raise ValueError('不支持的平台')
        return platform

    def _fingerprint(self, platform):
        try:
            with self.paths[platform].open('rb') as stream:
                stat = os.fstat(stream.fileno())
                if stat.st_size > 5_000_000:
                    return stat.st_mtime_ns, stat.st_size, 'oversized'
                content = stream.read(5_000_001)
            if len(content) > 5_000_000:
                return stat.st_mtime_ns, len(content), 'oversized'
            return stat.st_mtime_ns, stat.st_size, hashlib.sha256(content).hexdigest()
        except OSError:
            return None

    def _saved_status(self, platform):
        path = self.paths[platform]
        if not path.is_file():
            return 'missing'
        try:
            if path.stat().st_size > 5_000_000:
                return 'error'
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or not isinstance(state.get('cookies'), list):
                return 'error'
            if _valid_cookies(platform, state):
                return 'saved'
            if any(isinstance(c, dict) and c.get('name') in SESSION_NAMES for c in state['cookies']):
                return 'expired'
            return 'missing'
        except (OSError, ValueError, TypeError):
            return 'error'

    def busy(self):
        return any(not task.done() for task in self.jobs.values())

    def view(self, platform):
        self._key(platform)
        saved = self._saved_status(platform)
        record = self.records.get(platform) or {}
        if (record.get('fingerprint') != self._fingerprint(platform)
                or time.monotonic() - record.get('time', 0) > 900
                or (record.get('status') == 'authenticated' and saved != 'saved')):
            record = {}
        status = record.get('status', saved)
        job = self.jobs.get(platform)
        busy = job is not None and not job.done()
        return {'platform': platform, 'connected': True, 'status': status,
                'label': LABELS[status], 'message': MESSAGES[status],
                'has_saved_session': saved == 'saved', 'checked_at': record.get('checked_at'),
                'busy': busy, 'login': dict(self.login_states.get(platform) or {'status': 'idle', 'message': ''})}

    def views(self):
        return {platform: self.view(platform) for platform in SPECS}

    def _record(self, platform, status):
        self.records[platform] = {'status': status, 'fingerprint': self._fingerprint(platform),
                                 'checked_at': _stamp(), 'time': time.monotonic()}

    def _reserve(self, platform):
        self._key(platform)
        if self.busy():
            raise SessionBusy('已有平台登录或验证进行中，请先完成或取消。')

    def start_check(self, platform):
        self._reserve(platform)
        self.login_states[platform] = {'status': 'idle', 'message': '', 'job_id': uuid4().hex}
        if self._saved_status(platform) != 'saved':
            self.records.pop(platform, None)
            return self.view(platform)
        self._record(platform, 'checking')
        self.jobs[platform] = asyncio.create_task(self._run_check(platform))
        return self.view(platform)

    async def _run_check(self, platform):
        fingerprint = self._fingerprint(platform)
        try:
            result = await asyncio.wait_for(self._check(platform), 30)
            if self._fingerprint(platform) == fingerprint:
                self._record(platform, result if result in LABELS else 'error')
            else:
                self.records.pop(platform, None)
        except asyncio.CancelledError:
            self.records.pop(platform, None)
        except Exception:
            self._record(platform, 'error')

    def start_login(self, platform, *, replace=False):
        self._reserve(platform)
        self.login_states[platform] = {'status': 'opening', 'job_id': uuid4().hex,
                                       'message': '正在打开独立登录窗口，原凭据暂时保留。'}
        self.jobs[platform] = asyncio.create_task(self._run_login(platform, replace))
        return self.view(platform)

    def start_verification(self, target):
        self._reserve('douyin')
        query, search_type = target.get('query'), target.get('search_type')
        offset = target.get('offset', 0)
        if (not isinstance(query, str) or not query.strip() or len(query) > 200
                or search_type not in ('user', 'video') or isinstance(offset, bool)
                or not isinstance(offset, int) or offset < 0
                or self._saved_status('douyin') not in ('saved', 'expired')):
            raise ValueError('Invalid saved search or session')
        target = dict(target)
        self.login_states['douyin'] = {'status': 'opening', 'kind': 'search_verification',
                                       'job_id': uuid4().hex,
                                       'task_id': target['task_id'],
                                       'message': '正在使用项目已保存会话打开此任务的抖音搜索验证。'}
        self.jobs['douyin'] = asyncio.create_task(self._run_verification(target))
        return self.view('douyin')

    async def _run_verification(self, target):
        fingerprint = self._fingerprint('douyin')
        details = {'kind': 'search_verification', 'task_id': target['task_id'],
                   'job_id': self.login_states['douyin']['job_id']}
        try:
            result = await asyncio.wait_for(self._verify_search(target), self.login_timeout)
            if (not isinstance(result, dict) or result.get('status') != 'authenticated'
                    or self._fingerprint('douyin') != fingerprint):
                raise ValueError('Search not verified')
            # Keep the verified context alive and bind our own atomic write
            # without yielding between the fingerprint check, save, and bind.
            from app.douyin_browser import browser
            self.save_state('douyin', result.get('state'))
            browser.bind_saved_state()
            self._record('douyin', 'authenticated')
            self.login_states['douyin'] = dict(details, status='succeeded',
                message='此任务搜索返回真实结果且会话有效，凭据已保存；采集未自动重启。')
        except asyncio.CancelledError:
            self.login_states['douyin'] = dict(details, status='cancelled', message='已取消搜索验证，原凭据保持不变。')
        except asyncio.TimeoutError:
            self.login_states['douyin'] = dict(details, status='timeout', message='搜索验证超时，原凭据保持不变。')
        except Exception as exc:
            from app.douyin_browser import browser

            await browser.close()
            code = _error_code(exc)
            logger.warning('Task search verification failed: task=%s code=%s exception=%s',
                           target['task_id'], code, type(exc).__name__)
            self.login_states['douyin'] = dict(details, status='failed', error_code=code,
                message='未确认此任务搜索可用，原凭据保持不变。')

    async def _verify_search(self, target):
        from app.douyin_browser import browser

        self.login_states['douyin'] = {**self.login_states.get('douyin', {}),
            'status': 'waiting', 'kind': 'search_verification',
            'task_id': target['task_id'],
            'message': '请在此任务搜索窗口手动完成验证；不会自动解验证码或重启采集。'}
        try:
            return await browser.verify(target)
        except asyncio.CancelledError:
            # Shutdown owns cleanup; ordinary cancellation (including wait_for
            # timeout) must release the shared window before propagating.
            if not self._closing:
                await browser.close()
            raise

    async def _run_login(self, platform, replace):
        fingerprint = self._fingerprint(platform)
        details = {'job_id': self.login_states[platform]['job_id']}
        try:
            result = await asyncio.wait_for(self._login(platform, replace), self.login_timeout)
            if (not isinstance(result, dict) or result.get('status') != 'authenticated'
                    or self._fingerprint(platform) != fingerprint):
                raise ValueError('Login not verified')
            # Commit after browser cleanup, with no await between validation
            # and replacement. Cancelling a waiting window preserves old state.
            self.save_state(platform, result.get('state'))
            self._record(platform, 'authenticated')
            self.login_states[platform] = {**details, 'status': 'succeeded', 'message': '登录已验证，新凭据已保存。'}
        except asyncio.CancelledError:
            self.login_states[platform] = {**details, 'status': 'cancelled', 'message': '已取消登录，原凭据保持不变。'}
        except asyncio.TimeoutError:
            self.login_states[platform] = {**details, 'status': 'timeout', 'message': '登录超时，原凭据保持不变，可重新打开窗口。'}
        except Exception as exc:
            code = _error_code(exc)
            logger.warning('Platform login failed: platform=%s code=%s exception=%s',
                           platform, code, type(exc).__name__)
            self.login_states[platform] = {**details, 'status': 'failed', 'error_code': code,
                                           'message': '未能完成登录验证，原凭据保持不变。'}

    async def wait(self, platform):
        task = self.jobs.get(platform)
        if task is not None:
            await task

    async def cancel(self, platform, expected_job_id=None):
        self._key(platform)
        task = self.jobs.get(platform)
        # Capture and check the generation before yielding or cancelling anything.
        if (expected_job_id is not None
                and expected_job_id != self.login_states.get(platform, {}).get('job_id')):
            raise SessionBusy('平台登录或验证任务已变更，请刷新状态后重试。')
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if task is not None and self.jobs.get(platform) is task and task.cancelled():
            self.records.pop(platform, None)
            state = self.login_states.get(platform, {})
            if state.get('status') in ('opening', 'waiting'):
                self.login_states[platform] = {**state, 'status': 'cancelled',
                    'message': ('已取消搜索验证，原凭据保持不变。' if state.get('kind') == 'search_verification'
                                else '已取消登录，原凭据保持不变。')}
        return self.view(platform)

    async def close(self):
        from app.douyin_browser import browser

        self._closing = True
        try:
            for platform in list(self.jobs):
                await self.cancel(platform)
        finally:
            try:
                await browser.close()
            finally:
                self._closing = False

    def save_state(self, platform, state):
        self._key(platform)
        if not _valid_cookies(platform, state):
            raise ValueError('No valid platform session')
        path = self.paths[platform]
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.pending-login-', suffix='.json', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def _observe(self, platform, page):
        text = await page.locator('body').inner_text(timeout=3000)
        title = await page.title()
        search_visible = False
        if platform == 'xingtu':
            search_visible = await page.locator(
                'input[placeholder*="关键词"], input[placeholder*="达人"], input[placeholder*="请输入"]'
            ).first.is_visible()
        return classify_page(platform, page.url, text + '\n' + title, search_visible=search_visible)

    async def _browse(self, platform, *, login=False, replace=False):
        from playwright.async_api import async_playwright
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(TOOL / '.browsers'))
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=not login, timeout=12000)
            try:
                options = {'viewport': {'width': 1440, 'height': 900}, 'locale': 'zh-CN'}
                if not replace and self._saved_status(platform) == 'saved':
                    options['storage_state'] = str(self.paths[platform])
                context = await browser.new_context(**options)
                page = await context.new_page()
                if login:
                    self.login_states[platform] = {**self.login_states.get(platform, {}),
                        'status': 'waiting', 'message': '请在已打开的浏览器手动登录或验证；成功前不会替换旧凭据。'}
                transient = {'page_transition', 'navigation_interrupted', 'navigation_timeout'}
                try:
                    await page.goto(SPECS[platform]['url'], wait_until='commit', timeout=20000)
                except Exception as exc:
                    if not login or _error_code(exc) not in transient or page.is_closed():
                        raise
                attempts = 0
                authenticated_observations = 0
                while True:
                    if page.is_closed():
                        return 'unknown'
                    try:
                        result = await self._observe(platform, page)
                    except Exception as exc:
                        if not login or _error_code(exc) not in transient or page.is_closed():
                            raise
                        result = 'unknown'
                    if result == 'authenticated':
                        candidate_state = await context.storage_state()
                        if not _valid_cookies(platform, candidate_state):
                            # Unauthenticated SPA shells can show dashboard
                            # labels before the sign-in dialog has mounted.
                            result = 'unknown'
                        else:
                            authenticated_observations += 1
                            if authenticated_observations >= 2:
                                return {'status': result, 'state': candidate_state} if login else result
                    if result != 'authenticated':
                        authenticated_observations = 0
                    if not login and (result != 'unknown' or attempts >= 5):
                        if result != 'authenticated':
                            return result
                    attempts += 1
                    await asyncio.sleep(self.poll_interval)
            finally:
                try:
                    await asyncio.wait_for(browser.close(), 3)
                except Exception:
                    pass

    async def _check(self, platform):
        return await self._browse(platform)

    async def _login(self, platform, replace):
        return await self._browse(platform, login=True, replace=replace)


manager = PlatformSessions()
