"""Bounded, read-only Xingtu market adapter.

Search layouts include the current market's star_id/core_user_id response and
the legacy field map in docs/项目交接.md. Audience/contact schemas remain
unverified. Those fields deliberately stay unknown, so a successful search
alone cannot report completed. Saved credentials do not establish permission.
TTL, selection, persistence and report admission belong to the workflow.
"""

import asyncio
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
except ImportError:
    async_playwright = None
    PlaywrightTimeoutError = TimeoutError


TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "douyin-cli-poc"
STORAGE_STATE = TOOL_DIR / ".local-home" / ".xingtu" / "storage_state.json"
MARKET_URL = "https://www.xingtu.cn/ad/creator/market"
SEARCH_PATH = "/gw/api/gsearch/search_for_author_square"
LOOKUP_TIMEOUT_SECONDS = 50
OPERATION_TIMEOUT_MS = 8000
CLEANUP_TIMEOUT_SECONDS = 2
MAX_AUTHORS = 100
SEARCH_INPUT = 'input[placeholder*="关键词"], input[placeholder*="达人"], input[placeholder*="请输入"]'
GATE_SELECTOR = ('[class*="captcha"]:visible, [id*="captcha"]:visible, '
                 ':text-is("请先登录"):visible, :text-is("暂无权限"):visible, :text-is("无权限"):visible, '
                 ':text-is("访问受限"):visible, :text-is("安全验证"):visible')


def _empty_result():
    return {
        "status": "unavailable", "identity_verified": False,
        "median_play_30d": None,
        "audience": {"gender": None, "age": None, "region": None},
        "missing_fields": ["identity", "median_play_30d", "audience.gender", "audience.age", "audience.region"],
        "source_url": MARKET_URL,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "field_sources": {},
        "contact": {"status": "未获取", "value": None, "source_url": None, "permission": "not_fetched"},
    }


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    return value if value and value not in ("0", "-") else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _api_error(payload):
    # Error messages are used only for classification, never returned or logged.
    base = _mapping(payload.get("base_resp"))
    for container, keys in ((base, ("status_code",)), (payload, ("code", "status_code"))):
        for key in keys:
            if key not in container or container[key] in (0, "0", None, ""):
                continue
            message = " ".join(str(container.get(k, "")) for k in ("message", "msg", "status_message")).lower()
            denied = str(container[key]) in ("401", "403", "429") or any(
                token in message for token in ("permission", "unauthorized", "forbidden", "login", "log in",
                                               "captcha", "登录", "权限", "权益", "验证码", "风控", "访问受限"))
            return "permission_denied" if denied else "failed"
    return None


def _identity(author, creator):
    attrs = _mapping(author.get("attribute_datas"))
    base = _mapping(author.get("base_info"))
    star_ids = {_identifier(v) for v in (
        author.get("star_id"), author.get("author_id"), attrs.get("author_id"))}
    star_ids.discard(None)
    if len(star_ids) != 1:
        return None
    # Current market responses repeat star_id as attribute_datas.id. It is not
    # a Douyin ID; a conflicting repeat makes the entire mapping untrustworthy.
    repeated_star = _identifier(attrs.get("id"))
    if repeated_star is not None and repeated_star not in star_ids:
        return None
    if creator.get('source_platform') == 'xingtu':
        expected_star = _identifier(creator.get('star_id'))
        if expected_star is None or expected_star not in star_ids:
            return None
        expected_uid, actual_uid = _identifier(creator.get('uid')), _identifier(attrs.get('core_user_id'))
        if expected_uid is not None and actual_uid is not None and expected_uid != actual_uid:
            return None
        return expected_star, ['authors[].star_id']
    evidence = []
    for name, fields in (("unique_id", ("unique_id", "short_id")), ("sec_uid", ("sec_uid",))):
        expected = _identifier(creator.get(name))
        if expected is None:
            continue
        # author_id is Xingtu's namespace. Never compare it with Douyin IDs.
        for prefix, container in (("attribute_datas", attrs), ("base_info", base)):
            for field in fields:
                actual = _identifier(container.get(field))
                if actual is not None:
                    if actual != expected:
                        return None
                    evidence.append("authors[]." + prefix + "." + field)
    expected_uid = _identifier(creator.get("uid"))
    actual_uid = _identifier(attrs.get("core_user_id"))
    if expected_uid is not None and actual_uid is not None:
        if expected_uid != actual_uid:
            return None
        evidence.append("authors[].attribute_datas.core_user_id")
    return (star_ids.pop(), evidence) if evidence else None


def _source(field):
    # Visible to the existing session, not a claim that a field is public.
    return {"source_url": MARKET_URL, "field": field, "permission": "session_visible"}


def _parse_search(payload, creator, result):
    if not isinstance(payload, dict):
        result.update(status="failed", reason="unsupported_search_response")
        return result
    error = _api_error(payload)
    if error:
        entitlement = any("权益等级过低" in str(container.get(key, ""))
                          for container in (payload, _mapping(payload.get("base_resp")))
                          for key in ("message", "msg", "status_message"))
        result.update(status=error, reason="customer_entitlement_required" if entitlement else "search_api_rejected")
        return result
    authors = payload.get("authors")
    if not isinstance(authors, list) or len(authors) > MAX_AUTHORS or any(not isinstance(a, dict) for a in authors):
        result.update(status="failed", reason="unsupported_search_response")
        return result
    if not authors:
        result.update(status="not_found", reason="no_results_on_requested_search_page")
        return result
    matches = [(a, _identity(a, creator)) for a in authors]
    matches = [(a, identity) for a, identity in matches if identity is not None]
    if len(matches) != 1:
        result.update(status="identity_unconfirmed", reason="no_unique_douyin_identity_mapping")
        return result
    author, (star_id, evidence) = matches[0]
    result.update(identity_verified=True, star_id=star_id)
    result["missing_fields"].remove("identity")
    result["field_sources"]["identity"] = _source(evidence)
    attrs, hot = _mapping(author.get("attribute_datas")), _mapping(author.get("hot_item_info"))
    for container, key, field in (
        (attrs, "item_vv_median_30d", "authors[].attribute_datas.item_vv_median_30d"),
        (attrs, "vv_median_30d", "authors[].attribute_datas.vv_median_30d"),
        (hot, "median_play_over_30d", "authors[].hot_item_info.median_play_over_30d"),
    ):
        value = _number(container.get(key))
        if value is not None:
            result["median_play_30d"] = value
            result["missing_fields"].remove("median_play_30d")
            result["field_sources"]["median_play_30d"] = _source(field)
            break
    prices = author.get("price_info")
    if isinstance(prices, list):
        for video_type, name in ((1, "price_1_20"), (2, "price_21_60")):
            values = [_number(p.get("price")) for p in prices if isinstance(p, dict)
                      and not isinstance(p.get("video_type"), bool) and str(p.get("video_type")) == str(video_type)]
            if len(values) == 1 and values[0] is not None:
                result[name] = values[0]
                result["field_sources"][name] = _source(f"authors[].price_info[video_type={video_type}].price")
    # No verified audience mapping exists locally. Do not infer it from arbitrary
    # JSON keys or turn a successful search/median into a completed enrichment.
    result.update(status="unavailable", reason="audience_schema_not_verified")
    return result


def _search_response(response, keyword):
    url = urlsplit(response.url)
    if url.scheme != "https" or url.netloc != "www.xingtu.cn" or url.path != SEARCH_PATH:
        return False
    try:
        request = response.request
        body = request.post_data_json
        return (request.method == "POST" and isinstance(body, dict)
                and _mapping(body.get("search_param")).get("keyword") == keyword
                and str(_mapping(body.get("page_param")).get("page")) == "1")
    except Exception:
        return False


async def _gate(page):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.netloc != "www.xingtu.cn":
        return True
    if any(part in url.path.lower() for part in ("login", "passport", "captcha", "verify")):
        return True
    return await page.locator(GATE_SELECTOR).first.is_visible()


async def _close(resource, method):
    if resource is not None:
        try:
            await asyncio.wait_for(getattr(resource, method)(), CLEANUP_TIMEOUT_SECONDS)
        except Exception:
            pass


async def enrich_creator(creator: dict) -> dict:
    """Read one ID search with existing isolated storage state; never log in.

    Each call owns its browser. Lookup is bounded to 50s plus at most 6s of
    cleanup, with no retries, pagination, profile credential reads or messages.
    Only parsed, identity-bound fields leave this module; exceptions, cookies,
    raw responses and contact-like strings are never printed or returned.
    """
    result = _empty_result()
    if not isinstance(creator, dict):
        result.update(status="identity_unconfirmed", reason="douyin_identifier_required")
        return result
    native_xingtu = creator.get('source_platform') == 'xingtu'
    keyword = (_identifier(creator.get('star_id')) if native_xingtu else
               _identifier(creator.get("unique_id")) or _identifier(creator.get("sec_uid")))
    if keyword is None or len(keyword) > 256:
        result.update(status="identity_unconfirmed", reason="douyin_identifier_required")
        return result
    if async_playwright is None or not STORAGE_STATE.is_file():
        result["reason"] = "browser_or_saved_session_unavailable"
        return result
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(TOOL_DIR / ".browsers"))
    runtime = browser = context = None

    async def lookup():
        nonlocal runtime, browser, context
        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch(headless=True, timeout=OPERATION_TIMEOUT_MS)
        context = await browser.new_context(storage_state=str(STORAGE_STATE), viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        page.set_default_timeout(OPERATION_TIMEOUT_MS)
        navigation = await page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=12000)
        if navigation and navigation.status in (401, 403, 429):
            result.update(status="permission_denied", reason="market_access_denied")
            return result
        if navigation and navigation.status >= 400:
            result.update(status="failed", reason="market_http_error")
            return result
        if await _gate(page):
            result.update(status="permission_denied", reason="login_or_permission_challenge")
            return result
        if native_xingtu:
            await page.locator(SEARCH_INPUT).first.wait_for(state='visible', timeout=OPERATION_TIMEOUT_MS)
            await page.get_by_text('昵称找人', exact=True).first.click()
            search = page.get_by_placeholder('输入达人昵称、抖音号或星图ID', exact=True)
        else:
            search = page.locator(SEARCH_INPUT).first
        try:
            await search.wait_for(state="visible", timeout=OPERATION_TIMEOUT_MS)
            async with page.expect_response(lambda response: _search_response(response, keyword),
                                            timeout=OPERATION_TIMEOUT_MS) as pending:
                await search.fill(keyword)
                await search.press("Enter")
            response = await pending.value
        except (TimeoutError, PlaywrightTimeoutError):
            if await _gate(page):
                result.update(status="permission_denied", reason="login_or_permission_challenge")
            else:
                result.update(status="unavailable", reason="bounded_lookup_timeout")
            return result
        if response.status in (401, 403, 429) or await _gate(page):
            result.update(status="permission_denied", reason="search_access_denied")
            return result
        if response.status != 200:
            result.update(status="failed", reason="search_http_error")
            return result
        parsed = _parse_search(await response.json(), creator, result)
        if native_xingtu and parsed.get('identity_verified'):
            from app.xingtu_details import enrich_details
            return await enrich_details(page, creator, parsed)
        return parsed

    try:
        return await asyncio.wait_for(lookup(), LOOKUP_TIMEOUT_SECONDS)
    except Exception as exc:
        # Playwright has its own TimeoutError type; never include exception text.
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
            if result.get('status') == 'completed':
                if result['contact'].get('value') is None:
                    result['contact'].update(permission='bounded_timeout')
            else:
                result.update(status="unavailable", reason="bounded_lookup_timeout")
        elif isinstance(exc, (FileNotFoundError, ImportError, PermissionError)) or "Executable doesn't exist at" in str(exc):
            result.update(status="unavailable", reason="browser_or_saved_session_unavailable")
        else:
            result.update(status="failed", reason="browser_or_response_failure")
        return result
    finally:
        await _close(context, "close")
        await _close(browser, "close")
        await _close(runtime, "stop")
