"""One ordinary, retained browser for human verification and discovery.

No generated signatures, hidden HTTP retries or CAPTCHA solver. Only the
project's authorized state is loaded; unrelated browser profiles are untouched.
"""

import asyncio
import copy
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from playwright.async_api import async_playwright


TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'douyin-cli-poc'
PATHS = {'user': '/aweme/v1/web/discover/search/', 'video': '/aweme/v1/web/search/item/'}
CHALLENGES = {'verify_check', 'captcha', 'login_check', 'login_required'}


class BrowserSearchBlocked(RuntimeError):
    pass


def response_matches(url, query, search_type, offset):
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    return (parsed.scheme == 'https' and parsed.netloc == 'www.douyin.com'
            and parsed.path == PATHS.get(search_type) and params.get('keyword') == [query]
            and params.get('offset', ['0']) == [str(offset)])


def validate_payload(payload, search_type):
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item.lower() in CHALLENGES:
            raise BrowserSearchBlocked('抖音当前搜索需要人工验证，已保留同一浏览器现场。')
    if not isinstance(payload, dict) or type(payload.get('status_code')) is not int or payload['status_code'] != 0:
        raise RuntimeError('抖音搜索未返回可确认的成功响应。')
    fields = ('user_list', 'data') if search_type == 'user' else ('data',)
    present = [payload[key] for key in fields if key in payload]
    if not present or any(not isinstance(value, list) for value in present):
        raise RuntimeError('抖音搜索响应格式尚未识别，不能当作零结果。')
    from app.crawler import normalize_search_authors
    authors = normalize_search_authors(payload, search_type)
    if any(present) and not any(isinstance(a.get('sec_uid'), str) and a['sec_uid'].strip() for a in authors):
        raise RuntimeError('搜索返回内容没有可核验的账号标识。')
    return payload


class DouyinBrowser:
    def __init__(self, *, state_path=None, headless=False, timeout=15):
        self.state_path = state_path or TOOL / '.local-home' / '.dy' / 'cookies' / 'default.json'
        self.headless = headless
        self.timeout = timeout
        self.poll_interval = 0.25
        self.lock = asyncio.Lock()
        self.runtime = self.browser = self.context = self.page = None
        self._fingerprint = None
        self._key = None
        self._cache = None
        self._responses = asyncio.Queue(maxsize=64)
        self._generation = 0
        self._request_generations = {}
        self._cleanup_task = None

    def _state_fingerprint(self):
        try:
            stat = self.state_path.stat()
            if stat.st_size > 5_000_000:
                return None
            return stat.st_mtime_ns, stat.st_size, hashlib.sha256(self.state_path.read_bytes()).digest()
        except OSError:
            return None

    def bind_saved_state(self):
        # Our own verified state commit must not look like an account replacement.
        self._fingerprint = self._state_fingerprint()

    async def _close_resources(self):
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._release_resources())
        # A second cancellation must not interrupt cleanup and orphan Chrome.
        await asyncio.shield(self._cleanup_task)

    async def _release_resources(self):
        browser, runtime = self.browser, self.runtime
        self._cache = self._key = self._fingerprint = None
        self._responses = asyncio.Queue(maxsize=64)
        for resource, method in ((browser, 'close'), (runtime, 'stop')):
            if resource is not None:
                try:
                    await asyncio.wait_for(getattr(resource, method)(), 3)
                except Exception:
                    pass
        if self.browser is browser:
            self.runtime = self.browser = self.context = self.page = None

    async def close(self):
        async with self.lock:
            await self._close_resources()

    async def _ensure(self):
        if self._cleanup_task is not None and not self._cleanup_task.done():
            await asyncio.shield(self._cleanup_task)
        fingerprint = self._state_fingerprint()
        if (self.context is not None and self.page is not None and not self.page.is_closed()
                and fingerprint == self._fingerprint):
            return
        await self._close_resources()
        if fingerprint is None:
            raise RuntimeError('项目抖音登录凭据缺失，请先登录。')
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(TOOL / '.browsers'))
        try:
            self.runtime = await async_playwright().start()
            self.browser = await self.runtime.chromium.launch(headless=self.headless, timeout=12000)
            self.context = await self.browser.new_context(storage_state=str(self.state_path),
                viewport={'width': 1440, 'height': 900}, locale='zh-CN')
            self.page = await self.context.new_page()
            self.page.on('framenavigated', self._on_navigation)
            self.page.on('request', self._on_request)
            self.page.on('response', self._on_response)
            self._fingerprint = fingerprint
        except asyncio.CancelledError:
            await self._close_resources()
            raise
        except Exception:
            await self._close_resources()
            raise RuntimeError('项目搜索浏览器无法启动，请检查本地凭据和浏览器安装。') from None

    def _on_navigation(self, frame):
        if frame == self.page.main_frame:
            self._generation += 1
            self._cache = None

    def _on_request(self, request):
        parsed = urlsplit(request.url)
        if parsed.netloc == 'www.douyin.com' and parsed.path in PATHS.values():
            if len(self._request_generations) >= 128:
                self._request_generations.clear()
            self._request_generations[id(request)] = self._generation

    def _on_response(self, response):
        parsed = urlsplit(response.url)
        if parsed.scheme == 'https' and parsed.netloc == 'www.douyin.com' and parsed.path in PATHS.values():
            if self._responses.full():
                self._responses.get_nowait()
            generation = self._request_generations.pop(id(response.request), None)
            if generation is not None:
                self._responses.put_nowait((generation, response))

    def _page_matches(self, query, search_type):
        parsed = urlsplit(self.page.url)
        return (parsed.scheme == 'https' and parsed.netloc == 'www.douyin.com'
                and unquote(parsed.path).rstrip('/') == '/search/' + query
                and parse_qs(parsed.query).get('type') == [search_type])

    async def _challenge_visible(self):
        selectors = ('iframe[src*="/verifycenter/"]:visible', '[class*="captcha"]:visible',
                     '[id*="captcha"]:visible')
        for selector in selectors:
            if await self.page.locator(selector).first.is_visible():
                return True
        for label in ('请完成下列验证后继续', '请完成安全验证', '扫码登录', '手机号登录'):
            if await self.page.get_by_text(label, exact=True).first.is_visible():
                return True
        return False

    async def _dom_payload(self, query, search_type):
        # Public result links are evidence of a loaded USER result page, not of
        # numeric eligibility, followers, topic fit or business permission.
        if search_type != 'user' or not self._page_matches(query, search_type):
            return None
        search = self.page.locator('input[placeholder="搜索你感兴趣的内容"]').first
        if not await search.is_visible() or await search.input_value() != query:
            return None
        links = await self.page.locator('ul a[href*="/user/"],[role="list"] a[href*="/user/"]').evaluate_all('''nodes => nodes
          .filter(n => {
            if (n.closest('nav,header,aside,[role="navigation"]') ||
                !n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return false;
            const row = n.closest('li,[role="listitem"]');
            const r = n.getBoundingClientRect();
            const left = Math.max(0,r.left), right = Math.min(innerWidth,r.right);
            const top = Math.max(0,r.top), bottom = Math.min(innerHeight,r.bottom);
            if (!row || right<=left || bottom<=top) return false;
            const hit = document.elementFromPoint((left+right)/2,(top+bottom)/2);
            return !!hit && row.contains(hit);
          }).slice(0, 100).map(n => n.href)''')
        ids = []
        for link in links:
            url = urlsplit(link)
            parts = url.path.strip('/').split('/')
            if (url.scheme == 'https' and url.netloc == 'www.douyin.com' and len(parts) == 2
                    and parts[0] == 'user' and parts[1].startswith('MS4wLjAB') and parts[1] not in ids):
                ids.append(parts[1])
        if len(ids) < 2:
            return None
        return {'status_code': 0, 'user_list': [{'user_info': {'sec_uid': uid}} for uid in ids[:100]],
                'pagination_status': 'unknown', 'source': 'browser_visible_user_results'}

    async def _collect(self, query, search_type, offset, *, manual=False):
        key = (query, search_type)
        if self._key != key or not self._page_matches(query, search_type):
            self._cache = None
            self._responses = asyncio.Queue(maxsize=64)
            self._key = key
            url = 'https://www.douyin.com/search/' + quote(query, safe='') + '?' + urlencode({'type': search_type})
            try:
                await self.page.goto(url, wait_until='domcontentloaded', timeout=20000)
            except Exception:
                if self.page.is_closed():
                    raise RuntimeError('搜索浏览器已关闭，请重新打开人工验证。') from None
                # Redirects/timeouts can leave a usable page; observe it below.
        deadline = time.monotonic() + self.timeout
        scrolls = 0
        while manual or time.monotonic() < deadline:
            if self.page.is_closed():
                raise RuntimeError('搜索浏览器已关闭，请重新打开人工验证。')
            blocked = False
            while not self._responses.empty():
                generation, response = self._responses.get_nowait()
                if (generation != self._generation or not self._page_matches(query, search_type)
                        or not response_matches(response.url, query, search_type, offset)):
                    continue
                if response.status in (401, 403, 429):
                    blocked = True
                    continue
                if response.status != 200:
                    continue
                try:
                    payload = validate_payload(await asyncio.wait_for(response.json(), 3), search_type)
                    from app.crawler import normalize_search_authors
                    if not manual or normalize_search_authors(payload, search_type):
                        if (not await self._challenge_visible() and generation == self._generation
                                and self._page_matches(query, search_type)):
                            return payload
                except BrowserSearchBlocked:
                    blocked = True
                except Exception:
                    continue
            try:
                if not self._page_matches(query, search_type):
                    await asyncio.sleep(self.poll_interval)
                    continue
                challenged = await self._challenge_visible()
                if not challenged and offset == 0:
                    generation = self._generation
                    payload = await self._dom_payload(query, search_type)
                    if payload and generation == self._generation and self._page_matches(query, search_type):
                        return payload
                if (blocked or challenged) and not manual:
                    raise BrowserSearchBlocked('抖音当前搜索需要人工验证，已保留同一浏览器现场。')
                if offset > 0 and not challenged and scrolls < 3:
                    await self.page.mouse.wheel(0, 1800)
                    scrolls += 1
            except BrowserSearchBlocked:
                raise
            except Exception:
                pass  # A SPA transition is not a failed human verification.
            await asyncio.sleep(self.poll_interval)
        raise RuntimeError('浏览器未捕获当前查询的有效搜索结果，不能当作零结果。')

    def _remember(self, query, search_type, offset, payload):
        self._cache = {'key': (query, search_type), 'offset': offset,
                       'time': time.monotonic(), 'generation': self._generation, 'payload': copy.deepcopy(payload)}

    async def verify(self, target):
        query, kind = target['query'], target['search_type']
        if kind not in PATHS or not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError('Invalid saved search')
        async with self.lock:
            await self._ensure()
            # Verify the visible initial page; blocked pagination remains an
            # explicit later operation rather than hidden replay during login.
            try:
                payload = await self._collect(query, kind, 0, manual=True)
            except BaseException:
                self._key = self._cache = None
                raise
            generation = self._generation
            state = await self.context.storage_state()
            from app.platform_sessions import _valid_cookies
            if (not _valid_cookies('douyin', state) or generation != self._generation
                    or not self._page_matches(query, kind)):
                raise RuntimeError('搜索结果可见，但没有有效登录会话，请重新登录。')
            self._remember(query, kind, 0, payload)
            return {'status': 'authenticated', 'state': state}

    async def search(self, query, offset=0, count=20, *, search_type='user'):
        if (search_type not in PATHS or not isinstance(query, str) or not query.strip() or len(query) > 200
                or type(offset) is not int or offset < 0 or type(count) is not int or count < 1):
            raise ValueError('Invalid search request')
        async with self.lock:
            await self._ensure()
            if await self._challenge_visible():
                self._cache = None
                raise BrowserSearchBlocked('当前浏览器仍需人工验证，已保留现场。')
            cached = self._cache
            if (cached and cached['key'] == (query, search_type) and cached['offset'] == offset
                    and self._key == (query, search_type) and self._page_matches(query, search_type)
                    and cached.get('generation') == self._generation and time.monotonic() - cached['time'] < 600):
                return copy.deepcopy(cached['payload'])
            try:
                payload = await self._collect(query, search_type, offset)
            except BrowserSearchBlocked:
                self._cache = None
                raise
            except BaseException:
                self._key = self._cache = None
                raise
            self._remember(query, search_type, offset, payload)
            return copy.deepcopy(payload)


browser = DouyinBrowser()
