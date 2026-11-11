import os
from pathlib import Path
from zoneinfo import ZoneInfo

TOOL_DIR = Path(__file__).resolve().parent.parent / 'tools' / 'douyin-cli-poc'
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(TOOL_DIR / '.browsers'))

import asyncio
import json
import re
import datetime
import subprocess
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright

from app import qualification
from app.contracts import TopicAssessment
from app.discovery import propose_creator_queries
from app.douyin_browser import browser as douyin_browser, BrowserSearchBlocked
from app.db import (
    save_task, upsert_creator, save_snapshot,
    save_contacts, save_sample_posts, save_recommendation,
    get_all_settings, update_task_status, get_task, get_task_results,
    save_discovery_source, get_discovery_sources, save_candidate_assessment,
    get_current_candidate_assessment, get_observation_posts,
    candidate_assessment_reusable,
)


def extract_contacts(signature: str) -> List[Dict[str, str]]:
    """从达人个人简介中提取公开商务微信、手机号、邮箱。"""
    contacts = []
    if not signature:
        return contacts

    wx_patterns = [
        r"(?:商务|合作|广告|工作|VX|vx|wx|微信)[：:\s\+]*([a-zA-Z0-9_\-]{6,25})",
        r"(?:带货|招商)[：:\s\+]*([a-zA-Z0-9_\-]{6,25})",
    ]
    for pat in wx_patterns:
        matches = re.findall(pat, signature, re.IGNORECASE)
        for m in matches:
            if m.lower() not in ("douyin", "tiktok", "wechat", "notice", "message", "please"):
                contacts.append({"type": "微信", "value": m, "source": "主页简介"})

    phone_matches = re.findall(r"(?:电话|手机|联系方式|TEL|Tel)[：:\s]*?(1[3-9]\d{9})", signature)
    for p in phone_matches:
        contacts.append({"type": "电话", "value": p, "source": "主页简介"})

    email_matches = re.findall(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", signature)
    for em in email_matches:
        contacts.append({"type": "邮箱", "value": em, "source": "主页简介"})

    seen = set()
    dedup = []
    for c in contacts:
        if c["value"] not in seen:
            seen.add(c["value"])
            dedup.append(c)
    return dedup


class PostFetchError(RuntimeError):
    pass


class PostFetchUnavailable(PostFetchError):
    pass


def _set_diag(diagnostics, status, reason='', count=0, method=''):
    if isinstance(diagnostics, dict):
        diagnostics.update(status=status, reason=reason, count=count, method=method)


def _run_python(script: str, args: List[str], timeout: int = 60) -> Any:
    env = {**os.environ, 'HOME': str(TOOL_DIR / '.local-home'),
           'PLAYWRIGHT_BROWSERS_PATH': str(TOOL_DIR / '.browsers')}
    try:
        result = subprocess.run(
            [str(TOOL_DIR / '.venv/bin/python'), '-c', script, *args],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise PostFetchError('作品接口查询超时') from None
    except OSError:
        raise PostFetchUnavailable('作品接口工具无法启动') from None
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise PostFetchError('作品接口返回格式异常') from None
    if result.returncode or not isinstance(payload, dict) or payload.get('error'):
        raise PostFetchError(str(payload.get('error') if isinstance(payload, dict) else '') or '作品接口查询失败')
    return payload


def _post_response_items(data: dict) -> list:
    if not isinstance(data, dict):
        raise PostFetchUnavailable('未取得有效作品响应')
    message = str(data.get('status_msg') or data.get('message') or '')
    if data.get('status_code') in (401, 403) or re.search(
            r'permission|private|login|captcha|登录|权限|验证|私密', message, re.IGNORECASE):
        raise PostFetchUnavailable('作品读取被权限、登录或验证码阻断')
    if data.get('status_code', 0) not in (0, None) or data.get('error'):
        raise PostFetchError('作品接口返回错误，未取得成功作品响应')
    if not isinstance(data.get('aweme_list'), list):
        raise PostFetchUnavailable('作品响应缺少有效作品列表')
    return data['aweme_list']


async def _fetch_user_posts_api(sec_uid: str, count: int = 20, *, stop_check=None) -> List[Dict[str, Any]]:
    script = '''import json,sys
from dy_cli.engines.api_client import DouyinAPIClient
client=DouyinAPIClient.from_config(None)
try:
 data=client.get_user_posts(sys.argv[1],max_cursor=int(sys.argv[3]),count=int(sys.argv[2]))
 print(json.dumps({"pages":[data]}))
except Exception as exc:
 print(json.dumps({"error":str(exc)[:300]}))
 sys.exit(1)
finally:
 client.close()
'''
    posts: List[Dict[str, Any]] = []
    cursor = 0
    seen_cursors = {cursor}
    for _ in range(3):
        if stop_check and stop_check():
            break
        payload = await asyncio.to_thread(_run_python, script, [sec_uid, str(count), str(cursor)])
        pages = payload.get('pages')
        if not isinstance(pages, list) or not pages:
            raise PostFetchUnavailable('接口未返回作品响应')
        for page in pages:
            posts.extend(_post_response_items(page))
        page = pages[-1]
        if not page.get('has_more'):
            break
        nxt = page.get('max_cursor')
        if isinstance(nxt, bool) or not isinstance(nxt, int) or nxt in seen_cursors:
            break
        seen_cursors.add(nxt)
        cursor = nxt
    return posts


async def _fetch_user_posts_playwright(sec_uid: str, *, stop_check=None) -> List[Dict[str, Any]]:
    if stop_check and stop_check():
        return []
    cookie_file = str(TOOL_DIR / '.local-home' / '.dy' / 'cookies' / 'default.json')
    if not Path(cookie_file).is_file():
        raise PostFetchUnavailable('抖音登录凭据缺失，无法读取作品')
    posts: List[Dict[str, Any]] = []
    successful_response = False
    response_error = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=cookie_file,
                                                viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            async def on_resp(resp):
                nonlocal successful_response, response_error
                if "aweme/v1/web/aweme/post" not in resp.url:
                    return
                try:
                    if resp.status in (401, 403):
                        raise PostFetchUnavailable('作品页面响应无访问权限或需要登录')
                    if resp.status != 200:
                        raise PostFetchError('作品页面接口读取失败')
                    posts.extend(_post_response_items(await resp.json()))
                    successful_response = True
                except PostFetchError as exc:
                    response_error = exc
                except Exception:
                    response_error = PostFetchError('作品页面响应无法解析')

            page.on("response", on_resp)
            if stop_check and stop_check():
                return posts
            await page.goto(f"https://www.douyin.com/user/{sec_uid}",
                            wait_until="commit", timeout=20000)
            for _ in range(16):
                if successful_response or response_error or (stop_check and stop_check()):
                    break
                await page.wait_for_timeout(500)
            if not successful_response and not response_error:
                # Some profile layouts need a scroll before the post request.
                for _ in range(2):
                    if stop_check and stop_check():
                        break
                    try:
                        await page.evaluate('window.scrollBy(0, 900)')
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                    if successful_response or response_error:
                        break
            final_url = str(page.url or '')
            if stop_check and stop_check():
                return posts
            if not posts and ('login' in final_url or 'captcha' in final_url):
                raise PostFetchUnavailable('作品读取被登录或验证码阻断')
            await context.close()
            if not successful_response:
                raise response_error or PostFetchUnavailable('页面未捕获成功的作品响应，不能判断账号无作品')
        finally:
            await browser.close()
    return posts


async def fetch_user_posts(sec_uid: str, pw_browser=None, diagnostics=None) -> List[Dict[str, Any]]:
    """Read recent posts via the API client first, then the logged-in page.

    ``diagnostics`` distinguishes a real empty account from a failed read.  The
    caller must not turn a network failure into a normal empty sample list.
    An optional ``task_id`` entry enables stop checks between subrequests.
    """

    task_id = diagnostics.get('task_id') if isinstance(diagnostics, dict) else None
    stop_check = (lambda: stop_requested(task_id)) if task_id is not None else None
    if stop_check and stop_check():
        _set_diag(diagnostics, 'stopped', '手动停止，未开始作品请求')
        return []
    try:
        posts = await _fetch_user_posts_api(sec_uid, count=20, stop_check=stop_check)
        if stop_check and stop_check():
            _set_diag(diagnostics, 'stopped', '手动停止，已保留取得的作品', len(posts), 'api')
            return _dedupe_posts(posts)
        _set_diag(diagnostics, 'ok' if posts else 'empty',
                  '' if posts else '账号当前没有公开作品', len(posts), 'api')
        return _dedupe_posts(posts)
    except PostFetchUnavailable as api_error:
        api_message = str(api_error)
    except PostFetchError as api_error:
        api_message = str(api_error)
    if stop_check and stop_check():
        _set_diag(diagnostics, 'stopped', '手动停止，未开始页面回退请求')
        return []
    try:
        posts = await _fetch_user_posts_playwright(sec_uid, stop_check=stop_check)
        if stop_check and stop_check():
            _set_diag(diagnostics, 'stopped', '手动停止，已保留取得的作品', len(posts), 'playwright')
            return _dedupe_posts(posts)
        _set_diag(diagnostics, 'ok' if posts else 'empty',
                  ('' if posts else '账号当前没有公开作品'), len(posts), 'playwright')
        return _dedupe_posts(posts)
    except PostFetchUnavailable as page_error:
        _set_diag(diagnostics, 'unavailable', f'{api_message}；{page_error}', 0, 'none')
        return []
    except Exception:
        _set_diag(diagnostics, 'failed', f'{api_message}；页面作品读取失败', 0, 'none')
        return []


def _dedupe_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        aid = str(post.get('aweme_id') or post.get('id') or '')
        if not aid or aid in seen:
            continue
        seen.add(aid)
        result.append(post)
    return result


def evaluate_recommendation(
    keyword: str,
    nickname: str,
    followers: int,
    avg_likes: float,
    max_likes: int,
    days_since_latest: Optional[float],
    sample_posts: List[Dict[str, Any]],
    contacts: List[Dict[str, str]],
    star_atlas: int,
    dormant_limit: int = 60,
    active_limit: int = 14,
    min_avg_likes: int = 0,
    active_days: Optional[int] = None,
    min_followers: int = 0,
    max_followers: Optional[int] = None,
    min_max_likes: int = 0,
) -> Dict[str, Any]:
    """Compatibility wrapper over the shared numeric rule.

    The old level labels remain for frozen reports, but they never replace the
    structured ``overall_status`` used to build the candidate list.
    """

    now = datetime.datetime.now().astimezone()
    requirements = {
        'keyword': keyword, 'min_avg_likes': min_avg_likes, 'active_days': active_days,
        'min_followers': min_followers, 'max_followers': max_followers,
        'min_max_likes': min_max_likes,
    }
    metrics = {
        'followers': followers, 'avg_likes': avg_likes, 'max_likes': max_likes,
        'days_since_latest': days_since_latest, 'sample_count': len(sample_posts or []),
        'captured_at': now.isoformat(), 'updated_at': now.isoformat(),
    }
    numeric = qualification.evaluate_numeric_conditions(metrics, requirements, now=now)
    base_missing = ['星图指标与粉丝画像']
    if not contacts:
        base_missing.append('公开商务联系渠道')
    risk = '当前仅为数值与公开文本初筛结果，内容适配、星图数据和履约能力尚未核实；联系方式仍需复核。'
    if numeric['status'] == 'failed':
        return {'level': '淘汰', 'hard_pass': False,
                'overall_status': 'rejected',
                'reason': '规则检查：' + '；'.join(numeric['details'] or ['已知硬条件不达标']),
                'risk': risk, 'missing_fields': '、'.join(base_missing)}
    evidence_missing = []
    if not sample_posts:
        evidence_missing.append('近期作品数据')
    if avg_likes is None or max_likes is None or days_since_latest is None:
        evidence_missing.append('近期作品数值')
    if numeric['status'] == 'unknown' or evidence_missing:
        return {'level': '待核实', 'hard_pass': None, 'overall_status': 'pending',
                'reason': '规则检查：必要数值缺失或作品样本不足，不能判断均赞及活跃条件。',
                'risk': risk,
                'missing_fields': '、'.join(base_missing + list(dict.fromkeys(evidence_missing + numeric['missing_fields'])))}
    level = '高推荐' if avg_likes >= 8000 and days_since_latest <= active_limit else '中推荐'
    return {'level': level, 'hard_pass': True, 'overall_status': 'eligible',
            'reason': f'规则参考：样本均赞 {avg_likes:,.0f}，最高赞 {max_likes:,}，最后更新距今 {days_since_latest:.1f} 天；已获取数据符合本次数值条件，内容方向待文本审核。',
            'risk': risk, 'missing_fields': '、'.join(base_missing)}


async def _cli(*args) -> dict:
    env = {**os.environ, 'HOME': str(TOOL_DIR / '.local-home'),
           'PLAYWRIGHT_BROWSERS_PATH': str(TOOL_DIR / '.browsers')}
    cmd = [str(TOOL_DIR / '.venv' / 'bin' / 'dy'), *args, '--json-output']
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True,
                                         env=env, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError('抖音 CLI 查询超时，任务已停止；请核对登录和网络。') from None
    except OSError:
        raise RuntimeError('抖音 CLI 无法启动，请检查工具安装和执行路径。') from None
    try:
        envelope = json.loads(result.stdout)
        if result.returncode or envelope.get('ok') is not True:
            code = envelope.get('error', {}).get('code', '')
            if code == 'not_authenticated':
                raise RuntimeError('抖音未登录或登录已失效，请先完成登录验证。')
            raise RuntimeError('抖音 CLI 查询失败，未取得有效数据；请检查登录、权限和接口状态。')
        data = envelope['data']
        if not isinstance(data, dict) or data.get('status_code', 0) not in (0, None):
            raise RuntimeError('抖音平台未返回可用数据，可能需要登录验证或页面接口已变化。')
        return data
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('抖音 CLI 返回格式异常，未将其当作成功结果。') from None


async def fetch_profile(sec_uid: str) -> dict:
    profile = await _cli('profile', sec_uid)
    if not profile.get('nickname'):
        raise RuntimeError('达人主页未返回有效身份信息。')
    if profile.get('sec_uid') and profile['sec_uid'] != sec_uid:
        raise RuntimeError('达人身份标识不一致，需要人工复核。')
    return profile


class SearchAccessBlocked(RuntimeError):
    pass


def _validated_search_page(payload: dict) -> dict:
    nil = payload.get('search_nil_info') or {}
    markers = [nil.get(key, '') for key in ('search_nil_type', 'search_nil_item')] if isinstance(nil, dict) else []
    if any(str(value).lower() in ('verify_check', 'captcha', 'login_check', 'login_required')
           for value in markers):
        raise SearchAccessBlocked('抖音搜索被登录或验证码验证拦截，请先在采集浏览器完成人工验证，再重试；这不是搜索无结果。')
    return payload


async def search_page(keyword: str, offset: int = 0, count: int = 20,
                      search_type: str = 'user') -> dict:
    if search_type not in ('user', 'video', 'general', 'atlas'):
        raise ValueError('不支持的搜索类型')
    if search_type in ('user', 'video'):
        try:
            payload = await douyin_browser.search(keyword, offset, count, search_type=search_type)
            return _validated_search_page(payload)
        except BrowserSearchBlocked as exc:
            raise SearchAccessBlocked(str(exc)) from None
    if offset == 0:
        return _validated_search_page(await _cli('search', keyword, '--type', search_type, '--count', str(count)))
    # The installed CLI exposes count only; reuse its API client for pagination
    # with the same search type as the first page.
    script = '''import json,sys
from dy_cli.engines.api_client import DouyinAPIClient
client=DouyinAPIClient.from_config(None)
try:
 print(json.dumps(client.search(sys.argv[1],search_type=sys.argv[4],offset=int(sys.argv[2]),count=int(sys.argv[3]))))
finally:
 client.close()
'''
    env = {**os.environ, 'HOME': str(TOOL_DIR / '.local-home'),
           'PLAYWRIGHT_BROWSERS_PATH': str(TOOL_DIR / '.browsers')}
    try:
        result = await asyncio.to_thread(subprocess.run,
            [str(TOOL_DIR / '.venv/bin/python'), '-c', script, keyword, str(offset), str(count), search_type],
            env=env, capture_output=True, text=True, timeout=90)
        payload = json.loads(result.stdout)
        if result.returncode or not isinstance(payload, dict) or payload.get('status_code', 0) != 0:
            raise ValueError('invalid page')
        return _validated_search_page(payload)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RuntimeError('抖音分页查询失败，已保留本轮候选；请检查登录、网络及平台限制。') from None


def _author_fields(info: dict) -> dict:
    avatar = None
    for key in ('avatar_medium', 'avatar_thumb', 'avatar_larger'):
        value = info.get(key)
        if isinstance(value, dict):
            urls = value.get('url_list') or []
            if urls:
                avatar = urls[0]
                break
    followers = info.get('follower_count', info.get('followers'))
    return {
        'sec_uid': info.get('sec_uid') or info.get('sec_user_id') or '',
        'nickname': info.get('nickname') or '',
        'followers': followers if isinstance(followers, (int, float)) and not isinstance(followers, bool) else None,
        'signature': info.get('signature') or '',
        'unique_id': info.get('unique_id') or info.get('short_id') or '',
        'uid': info.get('uid') or '',
        'avatar_url': avatar,
        'total_likes': info.get('total_favorited'),
        'aweme_count': info.get('aweme_count'),
    }


def normalize_search_authors(payload: dict, search_type: str) -> list[dict]:
    """Normalize user and video search responses without treating nicknames as IDs.

    Supported user shapes: ``user_list[].user_info`` (current CLI), nested
    ``data[].user_list[].user_info`` and a direct ``data[]`` user object.
    Supported video shape: ``data[].aweme_info.author``.
    """

    payload = payload if isinstance(payload, dict) else {}
    authors: list[dict] = []
    seen = set()

    def add(info, source_post_id=''):
        if not isinstance(info, dict):
            return
        normalized = _author_fields(info)
        if (not isinstance(normalized['sec_uid'], str) or not normalized['sec_uid'].strip()
                or normalized['sec_uid'] in seen):
            return
        seen.add(normalized['sec_uid'])
        normalized['source_post_id'] = str(source_post_id or '')
        normalized['search_type'] = search_type
        authors.append(normalized)

    if search_type == 'user':
        for user in payload.get('user_list') or []:
            if isinstance(user, dict):
                add(user.get('user_info') or user)
        for item in payload.get('data') or []:
            if not isinstance(item, dict):
                continue
            for user in item.get('user_list') or []:
                if isinstance(user, dict):
                    add(user.get('user_info') or user)
            direct = item.get('user_info') or item.get('user')
            if isinstance(direct, dict):
                add(direct)
            elif item.get('sec_uid') or item.get('sec_user_id'):
                add(item)
        return authors

    for item in payload.get('data') or []:
        if not isinstance(item, dict):
            continue
        aweme = item.get('aweme_info') or item
        author = aweme.get('author') if isinstance(aweme, dict) else None
        if isinstance(author, dict):
            add(author, aweme.get('aweme_id') or item.get('aweme_id') or '')
    return authors


def _normalize_queries(search_queries, keyword: str) -> list[str]:
    if not search_queries:
        return propose_creator_queries(keyword)
    queries = [str(item).strip() for item in search_queries]
    if len(queries) > 3 or not queries or any(not item or len(item) > 200 for item in queries):
        raise RuntimeError('查询词须为 1–3 个、每个 1–200 字；已拒绝执行。')
    return propose_creator_queries(keyword, queries)


def _source_plan(queries: list[str], discovery_mode: str) -> list[tuple[str, str]]:
    mode = discovery_mode if discovery_mode in ('user', 'hybrid', 'video') else 'user'
    if mode == 'video':
        return [(query, 'video') for query in queries]
    if mode == 'hybrid':
        return [(query, 'user') for query in queries] + [(query, 'video') for query in queries]
    return [(query, 'user') for query in queries]


def _next_offset(result: dict, offset: int, returned: int, page_size: int) -> Optional[int]:
    # If the platform returns a cursor, it must actually advance.  Falling
    # back to offset arithmetic on a stuck cursor would loop forever.
    if 'cursor' in result and result.get('cursor') is not None:
        value = result.get('cursor')
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > offset else None
        if isinstance(value, str) and value.isdigit():
            return int(value) if int(value) > offset else None
        return None
    for key in ('offset',):
        value = result.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > offset:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > offset:
            return int(value)
    return None


def stop_requested(task_id: int) -> bool:
    task = get_task(task_id)
    return bool(task and task.get('stop_requested'))


def _recommendation_from_assessment(assessment: dict, numeric: dict, topic: dict) -> dict:
    status = assessment.get('overall_status') or 'pending'
    missing = list(dict.fromkeys(list(numeric.get('missing_fields') or []) +
                                 ([topic.get('error_code')] if topic.get('error_code') else [])))
    return {
        'level': '可选' if status == 'eligible' else ('淘汰' if status == 'rejected' else '待核实'),
        'hard_pass': (True if numeric.get('status') == 'passed'
                      else (False if numeric.get('status') == 'failed' else None)),
        'overall_status': status,
        'reason': assessment.get('reason') or '审核结果待核实',
        'risk': '文本辅助审核，未观看视频；不代表内容质量、星图画像或投放效果。',
        'missing_fields': '、'.join(missing),
    }


async def execute_bd_search_task(
    keyword: str, max_count: int = 5, custom_task_name: Optional[str] = None, *,
    min_avg_likes: int = 0, active_days: Optional[int] = None,
    min_followers: int = 0, max_followers: Optional[int] = None, min_max_likes: int = 0,
    task_id: Optional[int] = None, progress_callback=None,
    discovery_mode: str = 'user', search_queries: Optional[List[str]] = None,
    topic_min_samples: int = 5, topic_min_ratio: float = 0.6,
    category: str = '', content_preferences: Optional[List[str]] = None,
    exclusions: Optional[List[str]] = None,
) -> int:
    if discovery_mode == 'xingtu':
        from app.xingtu_pipeline import execute_xingtu_search_task
        return await execute_xingtu_search_task(keyword, max_count, custom_task_name,
            task_id=task_id, progress_callback=progress_callback, min_avg_likes=min_avg_likes,
            active_days=active_days, min_followers=min_followers, max_followers=max_followers,
            min_max_likes=min_max_likes, search_queries=search_queries,
            topic_min_samples=topic_min_samples, topic_min_ratio=topic_min_ratio,
            category=category, content_preferences=content_preferences, exclusions=exclusions)
    settings = get_all_settings()
    task_name = custom_task_name or f"{keyword}达人挖掘_{datetime.datetime.now().strftime('%m%d_%H%M')}"
    queries = _normalize_queries(search_queries, keyword)
    requirements = {
        'category': category, 'keyword': keyword, 'sample_count': max_count,
        'min_avg_likes': min_avg_likes, 'active_days': active_days,
        'min_followers': min_followers, 'max_followers': max_followers,
        'min_max_likes': min_max_likes, 'discovery_mode': discovery_mode,
        'search_queries': queries, 'topic_min_samples': topic_min_samples,
        'topic_min_ratio': topic_min_ratio,
        'content_preferences': list(content_preferences or []),
        'exclusions': list(exclusions or []),
    }
    policy = qualification.policy_from_requirements(requirements, settings)
    if task_id is None:
        task_id = save_task(task_name, keyword, requirements=requirements)
    progress = {'requested': max_count, 'candidates': 0, 'processed': 0, 'saved': 0,
                'failed': 0, 'missing_posts': 0, 'prequalified': 0, 'pages': 0,
                'eligible': 0, 'pending': 0, 'rejected': 0, 'topic_reviews': 0,
                'topic_budget': policy['max_reviews_per_run']}
    budget = {'limit': policy['max_reviews_per_run'], 'used': 0, 'exhausted': False}
    partial_reason = ''
    seen = {}

    def report(status, message):
        progress['topic_reviews'] = budget['used']
        update_task_status(task_id, status, message, dict(progress))
        if progress_callback:
            progress_callback(message, dict(progress))

    try:
        for query, search_type in _source_plan(queries, discovery_mode):
            offset = 0
            duplicate_pages = 0
            while not stop_requested(task_id):
                report('running', f"正在检索【{query}】{'用户' if search_type == 'user' else '视频'}来源第 {progress['pages'] + 1} 页；最终交付目标 {max_count} 位，不限制候选数。")
                if stop_requested(task_id):
                    break
                result = await search_page(query, offset, 20, search_type=search_type)
                progress['pages'] += 1
                authors = normalize_search_authors(result, search_type)
                candidates = []
                for author in authors:
                    uid = author.get('sec_uid')
                    if not isinstance(uid, str) or not uid:
                        continue
                    if uid in seen:
                        save_discovery_source(task_id, seen[uid], search_type, query,
                                              author.get('source_post_id', ''))
                        continue
                    # Discovery identity is not a new observation of retained
                    # follower metrics; only the later profile read updates them.
                    creator_id = upsert_creator({
                        'sec_uid': uid, 'nickname': author.get('nickname') or '身份资料待获取',
                        'profile_url': f'https://www.douyin.com/user/{uid}',
                        **({'unique_id': author['unique_id']} if author.get('unique_id') else {}),
                        **({'signature': author['signature']} if author.get('signature') else {}),
                        **({'avatar_url': author['avatar_url']} if author.get('avatar_url') else {}),
                    })
                    save_discovery_source(task_id, creator_id, search_type, query,
                                          author.get('source_post_id', ''))
                    seen[uid] = creator_id
                    save_recommendation(creator_id, task_id, {
                        'level': '待核实', 'hard_pass': None,
                        'reason': '已发现账号；主页、近期作品与方向审核待完成。',
                        'risk': '尚未核验身份、数值条件与内容方向',
                        'missing_fields': '主页、作品、方向审核、星图数据',
                    })
                    candidates.append((uid, creator_id))
                progress['candidates'] = len(seen)
                if not candidates:
                    duplicate_pages += 1
                    if duplicate_pages >= 3 and result.get('has_more'):
                        partial_reason = '连续三页未发现新账号，已暂停重复检索；可调整关键词重新查找。'
                        break
                else:
                    duplicate_pages = 0
                for uid, creator_id in candidates:
                    if stop_requested(task_id):
                        break
                    if budget['exhausted']:
                        break
                    report('running', f"正在读取第 {progress['processed'] + 1} 位候选；已发现 {len(seen)} 位。")
                    try:
                        outcome = await _collect_candidate(
                            uid, creator_id, task_id, settings, keyword, progress,
                            requirements, policy, budget)
                        progress['processed'] += 1
                        status = outcome.get('overall_status')
                        if status == 'eligible':
                            progress['eligible'] += 1
                        elif status == 'rejected':
                            progress['rejected'] += 1
                        else:
                            progress['pending'] += 1
                    except Exception:
                        progress['failed'] += 1
                        progress['processed'] += 1
                        save_recommendation(creator_id, task_id, {
                            'level': '待核实', 'hard_pass': None,
                            'reason': '主页或作品读取失败，保留已发现账号供后续重试。',
                            'risk': '无法判断硬条件与方向', 'missing_fields': '主页或作品、方向审核、星图数据'})
                report('running', f"已处理 {progress['processed']} 位；可选 {progress['eligible']} 位，待核实 {progress['pending']} 位，不符合 {progress['rejected']} 位；内容审核 {budget['used']}/{budget['limit']} 次。")
                if budget['exhausted']:
                    partial_reason = '审核预算已用完，已保留全部已取得资料；确认后可继续审核。'
                    progress['pause_reason'] = 'topic_review_budget'
                    break
                if result.get('pagination_status') == 'unknown':
                    partial_reason = '已取得当前浏览器可见账号，但分页信息未确认；不能判定搜索已穷尽。'
                    progress['pause_reason'] = 'browser_pagination_unknown'
                    break
                if stop_requested(task_id) or not result.get('has_more'):
                    break
                next_offset = _next_offset(result, offset, len(authors), 20)
                if next_offset is None or next_offset <= offset:
                    partial_reason = '平台分页游标未前进，已保留结果并停止，避免重复采集。'
                    break
                offset = next_offset
            if budget['exhausted'] or stop_requested(task_id):
                break
        stopped = stop_requested(task_id)
        if not progress['saved'] and not stopped and not budget['exhausted']:
            raise RuntimeError('候选详情均未获取，已保留发现的账号；请核对登录及平台状态。')
        partial = bool(progress['failed'] or progress['missing_posts'] or partial_reason)
        report('stopped' if stopped else ('partial' if partial else 'completed'),
               f"{'已手动停止。' if stopped else '本轮检索结束。'}发现 {len(seen)} 位，可选 {progress['eligible']} 位、待核实 {progress['pending']} 位、不符合 {progress['rejected']} 位；"
               f"资料失败 {progress['failed']} 位，作品不完整 {progress['missing_posts']} 位。{partial_reason}"
               '请查看候选；默认不展示已知不达标账号，原始线索仍保留在知识库。')
        return task_id
    except SearchAccessBlocked as exc:
        progress['pause_reason'] = 'platform_verification'
        progress.update(query=query, search_type=search_type, offset=offset)
        progress['search_verification'] = {
            'pause_reason': 'platform_verification', 'query': query,
            'search_type': search_type, 'offset': offset}
        outcome = ('尚未取得任何候选账号。' if not seen
                   else f'已发现 {len(seen)} 个账号，已有线索保留，但检索尚未完成。')
        report('blocked', str(exc) + ' 检索已暂停。' + outcome)
        return task_id
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '采集任务异常停止，请核对配置和平台状态。'
        report('partial' if progress['saved'] else 'failed', message)
        raise RuntimeError(message) from None


async def _collect_candidate(uid, creator_id, task_id, settings, keyword, progress,
                             requirements, policy, budget, *, _star_profile=None, _star_posts=None):
    if stop_requested(task_id):
        return {'overall_status': 'pending'}
    from_xingtu = _star_profile is not None
    profile = _star_profile if from_xingtu else await fetch_profile(uid)
    now = datetime.datetime.now().astimezone()
    profile_data = {'sec_uid': uid, 'nickname': profile['nickname'],
                    'profile_url': profile.get('profile_url') if from_xingtu else f'https://www.douyin.com/user/{uid}'}
    if from_xingtu:
        profile_data.update(source_platform='xingtu', star_id=profile['star_id'],
                            median_play_30d=profile.get('median_play_30d'))
        if profile.get('avatar_url'):
            profile_data['avatar_url'] = profile['avatar_url']
    for source, destination in (('uid', 'uid'), ('follower_count', 'followers'),
        ('total_favorited', 'total_likes'), ('aweme_count', 'aweme_count'),
        ('signature', 'signature'), ('city', 'city')):
        if source in profile:
            profile_data[destination] = profile[source]
    if 'unique_id' in profile or 'short_id' in profile:
        profile_data['unique_id'] = profile.get('unique_id') or profile.get('short_id')
    if 'avatar_medium' in profile:
        avatar = (profile.get('avatar_medium') or {}).get('url_list') or [None]
        profile_data['avatar_url'] = avatar[0]
    if 'commerce_user_info' in profile:
        profile_data['star_atlas'] = (profile.get('commerce_user_info') or {}).get('star_atlas')
    creator_id = upsert_creator(profile_data)
    creator = {'creator_id': creator_id, **profile_data}
    sources = get_discovery_sources(task_id, creator_id)

    early = qualification.evaluate_numeric_conditions(
        {'followers': profile_data.get('followers'), 'updated_at': now.isoformat()},
        requirements, now=now)
    identity_excluded = qualification.classify_account_identity(creator)['account_type'] in ('brand_store', 'institution')
    if early['status'] == 'failed' or identity_excluded:
        snapshot_id = save_snapshot(creator_id, task_id, {
            'sample_count': 0, 'avg_likes': None, 'max_likes': None,
            'latest_post_date': None, 'days_since_latest': None,
        })
        topic = qualification.unknown_topic_assessment(
            policy, 'not_reviewed',
            '明确为品牌/店铺自营账号，未继续读取作品或调用模型。' if identity_excluded
            else '已知数值硬条件不达标，未继续读取作品或调用内容审核。')
        assessment = qualification.build_assessment(
            creator, snapshot_id, [], requirements, early, topic, policy, sources)
        assessment['post_fetch'] = {'status': 'skipped', 'reason': 'account_type_excluded' if identity_excluded else 'numeric_failed'}
        save_candidate_assessment(task_id, creator_id, snapshot_id, assessment)
        save_recommendation(creator_id, task_id,
                            _recommendation_from_assessment(assessment, early, topic))
        progress['saved'] += 1
        return assessment

    if stop_requested(task_id):
        snapshot_id = save_snapshot(creator_id, task_id, {
            'sample_count': 0, 'avg_likes': None, 'max_likes': None,
            'latest_post_date': None, 'days_since_latest': None,
        })
        numeric = qualification.evaluate_numeric_conditions(
            {'followers': profile_data.get('followers'), 'updated_at': now.isoformat()},
            requirements, now=now)
        topic = qualification.unknown_topic_assessment(policy, 'stopped', '手动停止：已保留主页资料，尚未读取作品。')
        assessment = qualification.build_assessment(creator, snapshot_id, [], requirements, numeric, topic, policy, sources)
        save_candidate_assessment(task_id, creator_id, snapshot_id, assessment)
        save_recommendation(creator_id, task_id, _recommendation_from_assessment(assessment, numeric, topic))
        progress['saved'] += 1
        progress['missing_posts'] += 1
        return assessment

    diagnostics = {'task_id': task_id}
    if from_xingtu:
        posts = list(_star_posts or [])
        diagnostics.update(status='ok' if posts else 'unavailable', method='xingtu_recent_items', count=len(posts))
    else:
        posts = await fetch_user_posts(uid, None, diagnostics=diagnostics)
    diagnostics.pop('task_id', None)
    post_status = diagnostics.get('status') or ('ok' if posts else 'empty')

    deduped = _dedupe_posts(posts)
    deduped.sort(key=lambda item: item.get('create_time') or 0, reverse=True)
    sample_posts = deduped[:policy['sample_limit']]
    likes: List[float] = []
    complete_statistics = bool(sample_posts)
    for post in sample_posts:
        value = (post.get('statistics') or {}).get('digg_count')
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            likes.append(float(value))
        else:
            complete_statistics = False
    avg = sum(likes) / len(likes) if complete_statistics and likes else None
    highest = max(likes) if complete_statistics and likes else None
    timestamps = [post['create_time'] for post in sample_posts
                  if isinstance(post.get('create_time'), (int, float))
                  and not isinstance(post.get('create_time'), bool) and post['create_time'] > 0]
    dt = (datetime.datetime.fromtimestamp(max(timestamps), datetime.timezone.utc).astimezone()
          if timestamps else None)
    age_days = max(0.0, (now - dt).total_seconds() / 86400) if dt else None
    latest_post_date = dt.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') if dt else None
    if not sample_posts or avg is None or dt is None:
        progress['missing_posts'] += 1

    snapshot_id = save_snapshot(creator_id, task_id, {
        'sample_count': len(likes), 'avg_likes': avg, 'max_likes': highest,
        'latest_post_date': latest_post_date,
        'days_since_latest': int(age_days) if age_days is not None else None,
    })
    contacts = extract_contacts(profile.get('signature', ''))
    save_contacts(creator_id, contacts)
    samples = [{
        'aweme_id': post.get('aweme_id'), 'desc': str(post.get('desc', ''))[:2000],
        'likes': (post.get('statistics') or {}).get('digg_count'),
        'comments': (post.get('statistics') or {}).get('comment_count'),
        'date': datetime.datetime.fromtimestamp(post['create_time'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
        if isinstance(post.get('create_time'), (int, float)) and not isinstance(post.get('create_time'), bool)
        and post['create_time'] > 0 else None,
    } for post in sample_posts]
    save_sample_posts(creator_id, task_id, samples)

    metrics = {
        'followers': profile.get('follower_count', profile.get('followers')),
        'avg_likes': avg, 'max_likes': highest,
        'days_since_latest': age_days, 'latest_post_date': latest_post_date,
        'sample_count': len(likes), 'captured_at': now.isoformat(),
        'updated_at': now.isoformat(),
    }
    numeric = qualification.evaluate_numeric_conditions(metrics, requirements, now=now)
    topic = None
    topic_reviewed = False
    budget_exhausted = False
    if stop_requested(task_id):
        topic = qualification.unknown_topic_assessment(
            policy, 'stopped', '手动停止：已保留作品资料，未开始内容审核。',
            sample_count=len(sample_posts))
    elif numeric['status'] == 'failed':
        topic = qualification.unknown_topic_assessment(
            policy, 'not_reviewed', '已知数值硬条件不达标，未调用内容审核。')
    elif numeric['status'] == 'passed':
        review_posts, _ = qualification.normalize_review_posts(sample_posts, policy['sample_limit'])
        if len(review_posts) < policy['min_samples']:
            topic = qualification.unknown_topic_assessment(
                policy, 'insufficient_samples',
                f'可判读作品仅 {len(review_posts)} 条，少于 {policy["min_samples"]} 条，证据不足。',
                sample_count=len(review_posts))
        elif stop_requested(task_id):
            topic = qualification.unknown_topic_assessment(
                policy, 'stopped', '手动停止，未开始内容审核。',
                sample_count=len(review_posts))
        elif budget['exhausted'] or budget['used'] >= budget['limit']:
            budget['exhausted'] = True
            budget_exhausted = True
            topic = qualification.unknown_topic_assessment(
                policy, 'budget_exhausted', '本轮内容审核预算已用完，账号保留为待核实。',
                sample_count=len(review_posts))
        else:
            budget['used'] += 1
            progress['topic_reviews'] = budget['used']
            topic = await qualification.review_creator_topic(creator, sample_posts, requirements, policy)
            topic_reviewed = True
    else:
        topic = qualification.unknown_topic_assessment(
            policy, 'numeric_unknown', '数值不完整，保留为待核实；未消耗内容审核预算。',
            sample_count=len(qualification.normalize_review_posts(sample_posts, policy['sample_limit'])[0]))

    assessment = qualification.build_assessment(
        creator, snapshot_id, sample_posts, requirements, numeric, topic, policy, sources)
    assessment['post_fetch'] = dict(diagnostics) if diagnostics else {'status': post_status}
    assessment['topic_reviewed'] = topic_reviewed
    save_candidate_assessment(task_id, creator_id, snapshot_id, assessment)
    save_recommendation(creator_id, task_id, _recommendation_from_assessment(assessment, numeric, topic))
    progress['saved'] += 1
    if numeric['status'] == 'passed':
        progress['prequalified'] += 1
    assessment['budget_exhausted'] = budget_exhausted
    return assessment


def _candidate_creator(task_id: int, creator_id: int) -> dict:
    candidate = next((row for row in get_task_results(task_id) if row['creator_id'] == creator_id), None)
    if not candidate:
        raise ValueError('任务或候选不存在')
    return candidate


async def review_existing_candidate(task_id: int, creator_id: int, settings: Optional[dict] = None,
                                    policy: Optional[dict] = None, *, budget: Optional[dict] = None) -> dict:
    """Review stored evidence; reserve shared budget before invoking topic review.

    ``budget`` is a mutable ``{used, limit, exhausted}`` mapping. Reservations
    survive model and persistence errors; completed reusable outcomes cost zero.
    """

    task = get_task(task_id)
    if not task:
        raise ValueError('任务不存在')
    existing = get_current_candidate_assessment(task_id, creator_id)
    candidate = _candidate_creator(task_id, creator_id)
    requirements = task.get('requirements') or {}
    settings = settings or get_all_settings()
    policy = policy or qualification.policy_from_requirements(requirements, settings)
    observed = get_observation_posts(creator_id, task_id, candidate.get('observation_id'),
                                     limit=policy['sample_limit'])
    posts = observed.get('posts') or []
    if existing and not observed.get('legacy_fallback'):
        completed = existing.get('assessment') or {}
        if candidate_assessment_reusable(completed):
            return completed
    if budget is None:
        budget = {'used': 0, 'limit': policy['max_reviews_per_run'], 'exhausted': False}
    now = datetime.datetime.now().astimezone()
    metrics = {
        'followers': candidate.get('followers'), 'avg_likes': candidate.get('avg_likes'),
        'max_likes': candidate.get('max_likes'),
        'days_since_latest': candidate.get('days_since_latest'),
        'latest_post_date': candidate.get('latest_post_date'),
        'captured_at': candidate.get('captured_at'), 'updated_at': candidate.get('updated_at'),
    }
    numeric = qualification.evaluate_numeric_conditions(metrics, requirements, now=now)
    topic_reviewed = False
    if stop_requested(task_id):
        topic = qualification.unknown_topic_assessment(
            policy, 'stopped', '手动停止，未开始内容审核。')
    elif qualification.classify_account_identity(candidate)['account_type'] in ('brand_store', 'institution'):
        topic = qualification.unknown_topic_assessment(
            policy, 'not_reviewed', '公开资料已明确为品牌或店铺自营账号，未消耗模型审核预算。')
    elif numeric['status'] == 'failed':
        topic = qualification.unknown_topic_assessment(
            policy, 'not_reviewed', '已知数值硬条件不达标，不进入内容审核。')
    elif observed.get('legacy_fallback'):
        topic = qualification.unknown_topic_assessment(
            policy, 'legacy_observation', '历史作品无法关联当前观察，需重新采集后审核。')
    elif numeric['status'] != 'passed':
        topic = qualification.unknown_topic_assessment(
            policy, 'numeric_unknown', '必要数值缺失或过期，未消耗内容审核预算。')
    elif not posts:
        topic = qualification.unknown_topic_assessment(
            policy, 'no_posts', '当前观察没有可判读作品，无法审核内容方向。')
    else:
        review_posts, _ = qualification.normalize_review_posts(posts, policy['sample_limit'])
        if len(review_posts) < policy['min_samples']:
            topic = qualification.unknown_topic_assessment(
                policy, 'insufficient_samples', '当前观察作品样本不足，无法审核内容方向。',
                sample_count=len(review_posts))
        elif budget['exhausted'] or budget['used'] >= budget['limit']:
            budget['exhausted'] = True
            topic = qualification.unknown_topic_assessment(
                policy, 'budget_exhausted', '本轮内容审核预算已用完，账号保留为待核实。',
                sample_count=len(review_posts))
        elif stop_requested(task_id):
            topic = qualification.unknown_topic_assessment(
                policy, 'stopped', '手动停止，未开始内容审核。')
        else:
            budget['used'] += 1
            topic = await qualification.review_creator_topic(candidate, posts, requirements, policy)
            topic_reviewed = True
    sources = get_discovery_sources(task_id, creator_id)
    assessment = qualification.build_assessment(
        candidate, candidate.get('observation_id'), posts, requirements, numeric, topic, policy, sources)
    assessment['post_fetch'] = {'status': 'stored', 'count': len(posts),
                                'legacy_fallback': observed.get('legacy_fallback', False)}
    assessment['topic_reviewed'] = topic_reviewed
    assessment['budget_exhausted'] = topic.get('error_code') == 'budget_exhausted'
    save_candidate_assessment(task_id, creator_id, candidate.get('observation_id'), assessment)
    save_recommendation(creator_id, task_id, _recommendation_from_assessment(assessment, numeric, topic))
    return assessment


async def refresh_candidate(task_id: int, creator_id: int):
    task = get_task(task_id)
    candidate = _candidate_creator(task_id, creator_id)
    if not task:
        raise ValueError('任务或候选不存在')
    if stop_requested(task_id):
        return
    requirements = task['requirements']
    settings = get_all_settings()
    policy = qualification.policy_from_requirements(requirements, settings)
    budget = {'limit': policy['max_reviews_per_run'], 'used': 0, 'exhausted': False}
    progress = {'saved': 0, 'missing_posts': 0, 'prequalified': 0}
    try:
        if candidate.get('source_platform') == 'xingtu' or str(candidate.get('sec_uid', '')).startswith('xingtu:'):
            from app import db as database
            from app.xingtu_discovery import XingtuDiscovery
            from app.xingtu_pipeline import creator_profile
            if not candidate.get('star_id'):
                raise RuntimeError('缺少星图账号标识，不能错误转用抖音标识。')
            async with XingtuDiscovery() as source:
                row = await source.lookup_creator(candidate['star_id'])
            if stop_requested(task_id):
                return
            refreshed_id = database.upsert_xingtu_creator({k: row[k] for k in
                ('star_id', 'uid', 'nickname', 'followers', 'median_play_30d', 'avatar_url')
                if row.get(k) is not None})
            if refreshed_id != creator_id:
                raise RuntimeError('星图刷新账号身份不一致。')
            return await _collect_candidate(candidate['sec_uid'], creator_id, task_id, settings,
                task['keyword'], progress, requirements, policy, budget,
                _star_profile=creator_profile(row, candidate), _star_posts=row.get('posts') or [])
        await _collect_candidate(candidate['sec_uid'], creator_id, task_id, settings,
                                 task['keyword'], progress, requirements, policy, budget)
    except Exception:
        save_recommendation(creator_id, task_id, {
            'level': '待核实', 'hard_pass': None,
            'reason': '动态资料刷新失败，旧资料不视为新核验结果。',
            'risk': '旧数据可能过期', 'missing_fields': '动态数值待刷新'})
        platform = '星图' if candidate.get('source_platform') == 'xingtu' else '抖音'
        raise RuntimeError(platform + '动态资料刷新失败') from None


async def parse_and_respond_chat(user_msg: str) -> Dict[str, Any]:
    from app.chat import parse_and_respond_chat as respond
    return await respond(user_msg)
