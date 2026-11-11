"""Read-only Xingtu content discovery using the project's saved session."""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.xingtu import STORAGE_STATE, TOOL_DIR, MARKET_URL, SEARCH_PATH, _api_error, _identifier, _number


class XingtuDiscoveryError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _recent_posts(raw):
    if isinstance(raw, str):
        if len(raw) > 1_000_000:
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    result, seen = [], set()
    for item in raw[:100]:
        if not isinstance(item, dict):
            continue
        identity = _identifier(item.get('item_id'))
        if not identity or identity in seen:
            continue
        seen.add(identity)
        stamp = _number(item.get('item_publish_time', item.get('item_create_time')))
        if stamp is not None and stamp > 10_000_000_000:
            stamp /= 1000
        if stamp is not None and not (0 < stamp <= datetime.now(timezone.utc).timestamp() + 60):
            stamp = None
        title = item.get('item_title')
        result.append({'aweme_id': identity, 'desc': title[:2000] if isinstance(title, str) else '',
            'create_time': stamp, 'statistics': {'digg_count': _number(item.get('like_cnt')),
                'comment_count': _number(item.get('comment_cnt'))}})
    return result


def parse_discovery_payload(payload, expected_page):
    if not isinstance(payload, dict):
        raise XingtuDiscoveryError('unsupported_response', '星图搜索响应结构无法识别。')
    error = _api_error(payload)
    if error:
        raise XingtuDiscoveryError(error, '星图客户权益不足或访问被拒绝，请检查当前账号权限。'
            if error == 'permission_denied' else '星图搜索接口未返回成功结果。')
    base = _mapping(payload.get('base_resp'))
    if type(base.get('status_code')) is not int or base['status_code'] != 0:
        raise XingtuDiscoveryError('unsupported_response', '星图响应缺少明确成功状态，不能当作零结果。')
    authors = payload.get('authors')
    if not isinstance(authors, list) or len(authors) > 100:
        raise XingtuDiscoveryError('unsupported_response', '星图未返回有效达人列表，不能当作零结果。')
    pagination = _mapping(payload.get('pagination'))
    if 'page' in pagination and str(pagination['page']) != str(expected_page):
        raise XingtuDiscoveryError('wrong_page', '星图返回的页码与当前请求不一致。')
    more = pagination.get('has_more')
    has_more = more if type(more) is bool else (bool(more) if type(more) is int and more in (0, 1) else None)
    normalized, skipped, seen = [], 0, set()
    captured = datetime.now(timezone.utc).isoformat()
    for author in authors:
        if not isinstance(author, dict):
            skipped += 1
            continue
        attrs = _mapping(author.get('attribute_datas'))
        star_id = _identifier(author.get('star_id')) or _identifier(author.get('author_id'))
        aliases = {_identifier(value) for value in (author.get('star_id'), author.get('author_id'),
                   attrs.get('id'), attrs.get('author_id'))}
        aliases.discard(None)
        if not star_id or len(aliases) != 1:
            skipped += 1
            continue
        if star_id in seen:
            continue
        seen.add(star_id)
        core = _identifier(attrs.get('core_user_id'))
        avatar = attrs.get('avatar_uri')
        nickname = attrs.get('nick_name')
        normalized.append({'source_platform': 'xingtu', 'star_id': star_id,
            'uid': core if core and core.isdigit() else None,
            'nickname': nickname[:300] if isinstance(nickname, str) and nickname.strip() else '星图账号待核实',
            'followers': _number(attrs.get('follower')),
            'median_play_30d': _number(attrs.get('item_vv_median_30d', attrs.get('vv_median_30d'))),
            'avatar_url': avatar if isinstance(avatar, str) and avatar.startswith('https://') else None,
            'profile_url': None, 'posts': _recent_posts(attrs.get('last_10_items')),
            'captured_at': captured, 'source_url': MARKET_URL})
    return {'authors': normalized, 'has_more': has_more, 'page': expected_page,
            'total_count': _number(pagination.get('total_count')), 'skipped_count': skipped}


def search_response_matches(response, keyword, page):
    parsed = urlsplit(response.url)
    if parsed.scheme != 'https' or parsed.netloc != 'www.xingtu.cn' or parsed.path != SEARCH_PATH:
        return False
    try:
        request = response.request
        body = request.post_data_json
        return (request.method == 'POST' and isinstance(body, dict)
            and _mapping(body.get('search_param')).get('keyword') == keyword
            and str(_mapping(body.get('page_param')).get('page')) == str(page))
    except Exception:
        return False


class XingtuDiscovery:
    def __init__(self, *, state_path=None, headless=True):
        self.state_path = state_path or STORAGE_STATE
        self.headless = headless
        self.runtime = self.browser = self.context = self.page = None
        self.keyword = None
        self.current_page = 0
        self.fingerprint = None

    async def __aenter__(self):
        if not self.state_path.is_file() or self.state_path.stat().st_size > 5_000_000:
            raise XingtuDiscoveryError('login_required', '项目星图登录凭据缺失或无效，请先登录星图。')
        self.fingerprint = hashlib.sha256(self.state_path.read_bytes()).digest()
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(TOOL_DIR / '.browsers'))
        try:
            self.runtime = await async_playwright().start()
            self.browser = await self.runtime.chromium.launch(headless=self.headless, timeout=12000)
            self.context = await self.browser.new_context(storage_state=str(self.state_path),
                viewport={'width': 1440, 'height': 900}, locale='zh-CN')
            self.page = await self.context.new_page()
            self.page.set_default_timeout(8000)
            await self.page.goto(MARKET_URL, wait_until='domcontentloaded', timeout=20000)
            return self
        except asyncio.CancelledError:
            await self.__aexit__(None, None, None)
            raise
        except Exception:
            await self.__aexit__(None, None, None)
            raise XingtuDiscoveryError('browser_unavailable', '星图搜索浏览器无法启动或页面加载失败。') from None

    async def __aexit__(self, *args):
        for resource, method in ((self.context, 'close'), (self.browser, 'close'), (self.runtime, 'stop')):
            if resource is not None:
                try:
                    await asyncio.wait_for(getattr(resource, method)(), 3)
                except Exception:
                    pass

    async def _gate(self):
        parsed = urlsplit(self.page.url)
        if parsed.scheme != 'https' or parsed.netloc != 'www.xingtu.cn' or not parsed.path.startswith('/ad/'):
            raise XingtuDiscoveryError('login_required', '星图会话需要重新登录，请在星图登录入口完成。')
        for selector in ('iframe[src*="captcha"]:visible', '[class*="captcha"]:visible',
                         ':text-is("安全验证"):visible', ':text-is("请先登录"):visible'):
            if await self.page.locator(selector).first.is_visible():
                raise XingtuDiscoveryError('verification_required', '星图要求登录或人工验证，已暂停检索。')

    async def search_page(self, keyword, page=1, *, identity=False):
        if (not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 200
                or type(page) is not int or not 1 <= page <= 5):
            raise ValueError('Invalid Xingtu query')
        if hashlib.sha256(self.state_path.read_bytes()).digest() != self.fingerprint:
            raise XingtuDiscoveryError('session_changed', '星图登录凭据发生变化，请重新发起任务。')
        await self._gate()
        if page > 1 and (self.keyword != keyword or self.current_page != page - 1):
            raise XingtuDiscoveryError('pagination_unavailable', '星图分页状态不连续，已保留当前候选。')
        try:
            if page == 1:
                # Content search is discovery; nickname search is not equivalent.
                toggle = self.page.get_by_text('昵称找人' if identity else '内容找人', exact=True)
                if await toggle.count() and await toggle.first.is_visible():
                    await toggle.first.click()
                placeholder = '输入达人昵称、抖音号或星图ID' if identity else '按内容关键词找达人'
                search = self.page.get_by_placeholder(placeholder, exact=True).first
                await search.wait_for(state='visible', timeout=8000)
                async with self.page.expect_response(lambda r: search_response_matches(r, keyword, page), timeout=12000) as pending:
                    await search.fill(keyword)
                    await search.press('Enter')
            else:
                next_page = self.page.locator('.el-pagination .btn-next').first
                # The response arrives before Vue finishes enabling pagination.
                # Wait for the actual control; never force-click disabled UI.
                await next_page.wait_for(state='visible', timeout=8000)
                async with self.page.expect_response(lambda r: search_response_matches(r, keyword, page), timeout=12000) as pending:
                    await next_page.click()
            response = await pending.value
            if response.status in (401, 403, 429):
                raise XingtuDiscoveryError('permission_denied', '星图搜索访问受限，请检查账号权限。')
            if response.status != 200:
                raise XingtuDiscoveryError('network_error', '星图搜索响应异常，已保留取得的数据。')
            result = parse_discovery_payload(await asyncio.wait_for(response.json(), 5), page)
            await self._gate()
            self.keyword, self.current_page = keyword, page
            return result
        except XingtuDiscoveryError:
            raise
        except Exception:
            await self._gate()
            raise XingtuDiscoveryError('search_unavailable', '星图未返回当前关键词的有效搜索响应，请检查登录、网络或页面变化。') from None

    async def lookup_creator(self, star_id):
        result = await self.search_page(star_id, identity=True)
        matches = [row for row in result['authors'] if row['star_id'] == star_id]
        if len(matches) != 1:
            raise XingtuDiscoveryError('identity_unconfirmed', '星图未返回唯一一致的账号身份，旧资料不视为已刷新。')
        return matches[0]
