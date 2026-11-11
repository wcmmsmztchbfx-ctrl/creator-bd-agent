import os
import json
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlsplit

# Keep direct script invocation working alongside `python -m app.main`.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, BackgroundTasks, Body, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, Field

from app.db import (
    init_db, get_all_tasks, get_task_results, get_knowledge_base,
    get_all_settings, save_setting, save_chat_message, get_chat_history,
    get_connection, candidate_assessment_reusable,
    save_task, update_task_status, get_task, recover_interrupted_tasks, clear_chat_history
)
from app.crawler import execute_bd_search_task, refresh_candidate, review_existing_candidate, _normalize_queries, _source_plan
from app.chat import parse_and_respond_chat, get_system_prompt, test_llm_connection
from app.contracts import TaskCard
from app.exporter import export_task_to_excel
from app import workflow, qualification
from app.xingtu import enrich_creator
from app.platform_sessions import manager as platform_session_manager, SessionBusy

app = FastAPI(title="达人 BD 自动化 Agent")

current_running_task = {
    "is_running": False,
    "task_id": None,
    "keyword": "",
    "step": "",
    "message": ""
}
task_lock = threading.RLock()
TERMINAL_STATUSES = {"completed", "partial", "blocked", "failed", "interrupted", "stopped"}
ACTIVE_STATUSES = {"queued", "running", "stopping", "enriching"}


def redact_secrets(value, extra_key=""):
    keys = [get_all_settings().get("llm_api_key", ""), extra_key]

    def redact(item):
        if isinstance(item, str):
            for key in keys:
                if key:
                    item = item.replace(key, "[redacted]")
            return item
        if isinstance(item, dict):
            return {k: redact(v) for k, v in item.items() if k != "llm_api_key"}
        if isinstance(item, list):
            return [redact(v) for v in item]
        return item

    return redact(value)


def sync_running_status():
    task_id = current_running_task.get("task_id")
    if task_id is not None:
        task = workflow.task_state(task_id)
        if task:
            current_running_task["status"] = task["status"]
            current_running_task['step'] = task['status']
            current_running_task['message'] = task.get('message', '')
            current_running_task["is_running"] = task["status"] in ACTIVE_STATUSES
            current_running_task.update({k: task.get(k) for k in
                ('started_at', 'finished_at', 'stop_requested', 'elapsed_seconds', 'counts', 'progress')})
            if task["status"] in TERMINAL_STATUSES:
                current_running_task["message"] = task.get("message", "")
                current_running_task["step"] = task["status"]
        else:
            current_running_task.update(is_running=False, status="failed", step="任务记录不存在", message="任务记录不存在")
    else:
        current_running_task.update(is_running=False, status="idle")
    return redact_secrets(dict(current_running_task))

@app.on_event("startup")
def on_startup():
    init_db()
    workflow.init_workflow_db()
    recover_interrupted_tasks()
    with task_lock:
        sync_running_status()


@app.on_event('shutdown')
async def close_platform_sessions():
    await platform_session_manager.close()


class PlatformLoginRequest(BaseModel):
    replace: bool = False


class PlatformCancelRequest(BaseModel):
    expected_job_id: Optional[str] = None
    model_config = {'extra': 'forbid'}


class TaskVerificationRequest(BaseModel):
    model_config = {'extra': 'forbid'}


@app.get('/api/platforms/status')
def api_platform_status():
    return JSONResponse({'platforms': platform_session_manager.views()}, headers={'Cache-Control': 'no-store'})


def _platform_action_error(platform, request, *, cancel=False):
    if platform not in ('douyin', 'xingtu'):
        return JSONResponse(status_code=404, content={'error': '不支持的平台'})
    if (urlsplit(str(request.base_url)).hostname or '').lower() not in ('127.0.0.1', 'localhost', '::1'):
        return JSONResponse(status_code=403, content={'error': '平台登录管理仅允许本机地址访问。'})
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse(status_code=403, content={'error': '请从本地工作台地址打开登录管理。'})
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse(status_code=403, content={'error': '不允许跨站发起平台登录操作。'})
    if not cancel and sync_running_status()['is_running']:
        return JSONResponse(status_code=409, content={'error': '请先停止当前采集或补全任务，再验证或更换登录账号。'})
    return None


@app.post('/api/platforms/{platform}/check')
async def api_platform_check(platform: str, request: Request):
    with task_lock:
        error = _platform_action_error(platform, request)
        if error is not None:
            return error
        try:
            view = platform_session_manager.start_check(platform)
        except SessionBusy as exc:
            return JSONResponse(status_code=409, content={'error': str(exc)})
    return JSONResponse(status_code=202, content={'platform': view})


@app.post('/api/platforms/{platform}/login')
async def api_platform_login(platform: str, request: Request, body: PlatformLoginRequest):
    with task_lock:
        error = _platform_action_error(platform, request)
        if error is not None:
            return error
        try:
            view = platform_session_manager.start_login(platform, replace=body.replace)
        except SessionBusy as exc:
            return JSONResponse(status_code=409, content={'error': str(exc)})
    return JSONResponse(status_code=202, content={'platform': view})


@app.post('/api/platforms/{platform}/cancel')
async def api_platform_cancel(platform: str, request: Request, body: Optional[PlatformCancelRequest] = Body(None)):
    error = _platform_action_error(platform, request, cancel=True)
    if error is not None:
        return error
    try:
        if body is not None and body.expected_job_id is not None:
            view = await platform_session_manager.cancel(platform, expected_job_id=body.expected_job_id)
        else:
            view = await platform_session_manager.cancel(platform)
    except SessionBusy as exc:
        return JSONResponse(status_code=409, content={'error': str(exc)})
    return {'platform': view}


def _search_verification_context(task):
    progress = task.get('progress') or {}

    def context(values):
        return {'pause_reason': 'platform_verification',
                **{key: values[key] for key in ('query', 'search_type', 'offset') if key in values}}

    saved = progress.get('search_verification')
    if isinstance(saved, dict) and saved.get('pause_reason') == 'platform_verification':
        return context(saved)
    if task.get('status') == 'blocked' or progress.get('pause_reason') == 'platform_verification':
        return context(progress)
    # Legacy discovery evidence can outlive replacing enrichment progress and
    # the chat UI's 30-message window. Read metadata only; never rewrite history.
    conn = get_connection()
    try:
        for row in conn.execute("SELECT meta_json FROM chat_messages WHERE role='assistant' AND meta_json IS NOT NULL ORDER BY id DESC"):
            try:
                meta = json.loads(row['meta_json'])
            except (ValueError, TypeError):
                continue
            if (not isinstance(meta, dict) or meta.get('task_finished') is not True
                    or type(meta.get('task_id')) is not int or meta['task_id'] != task.get('id')
                    or meta.get('stage') not in (None, 'discovery')):
                continue
            if meta.get('status') != 'blocked':
                # Old enrichment notices had no stage; only an explicitly
                # identified discovery finish can resolve earlier evidence.
                if meta.get('stage') == 'discovery':
                    return None
                continue
            saved = meta.get('search_verification')
            return context(saved if isinstance(saved, dict) else {})
    finally:
        conn.close()
    return None


@app.post('/api/tasks/{task_id}/verify')
async def api_task_verify(task_id: int, request: Request, body: TaskVerificationRequest):
    with task_lock:
        error = _platform_action_error('douyin', request)
        if error is not None:
            return error
        task = get_task(task_id)
        if task is None:
            return JSONResponse(status_code=404, content={'error': '任务不存在'})
        if ((task.get('progress') or {}).get('platform') == 'xingtu'
                or (task.get('requirements') or {}).get('discovery_mode') == 'xingtu'):
            return JSONResponse(status_code=409, content={'error': '此任务使用巨量星图，请通过星图登录入口检查当前会话与权益。'})
        progress = _search_verification_context(task)
        if task['status'] in ACTIVE_STATUSES or progress is None:
            return JSONResponse(status_code=409, content={'error': '此任务没有待处理的搜索人工验证。'})
        requirements = task.get('requirements') or {}
        try:
            if progress.get('query'):
                query, search_type = progress['query'], progress.get('search_type')
                offset = progress.get('offset', 0)
            else:
                queries = _normalize_queries(requirements.get('search_queries'),
                                             requirements.get('keyword') or task['keyword'])
                query, search_type = _source_plan(queries, requirements.get('discovery_mode', 'user'))[0]
                offset = 0
            view = platform_session_manager.start_verification({
                'task_id': task_id, 'query': query, 'search_type': search_type, 'offset': offset})
        except SessionBusy as exc:
            return JSONResponse(status_code=409, content={'error': str(exc)})
        except (ValueError, RuntimeError, TypeError, IndexError):
            return JSONResponse(status_code=400, content={'error': '任务检索上下文或本地抖音凭据不可用，请先检查任务与登录状态。'})
    return JSONResponse(status_code=202, content={'platforms': [view]}, headers={'Cache-Control': 'no-store'})

# --- Pydantic Request Models ---
class ChatRequest(BaseModel):
    message: str

class ExecuteTaskRequest(TaskCard):
    category: str = Field(default="未指定", min_length=1, max_length=100)
    sample_count: int = Field(default=5, ge=1, le=50, strict=True, validation_alias=AliasChoices("count", "sample_count"))
    min_avg_likes: int = Field(default=0, ge=0, le=1_000_000_000_000, strict=True)
    active_days: Optional[int] = Field(default=None, ge=1, le=3650, strict=True)
    task_name: Optional[str] = None

class SettingsRequest(BaseModel):
    settings: Dict[str, str]
    clear_llm_api_key: bool = False

class BatchSaveResultsRequest(BaseModel):
    task_id: int
    creators: List[Dict[str, Any]]

class AddRowRequest(BaseModel):
    task_id: int

class DeleteRowRequest(BaseModel):
    task_id: int
    creator_id: int

class CreatorSelection(BaseModel):
    creator_ids: List[int] = Field(min_length=1, max_length=500)
    excluded_ids: List[int] = Field(default_factory=list, max_length=500)
    allow_missing: bool = False

class ReviewRequest(BaseModel):
    creator_ids: Optional[List[int]] = Field(default=None, max_length=500)

class SelectionState(BaseModel):
    creator_ids: List[int] = Field(default_factory=list, max_length=500)
    excluded_ids: List[int] = Field(default_factory=list, max_length=500)

class KBImportRequest(CreatorSelection):
    task_id: Optional[int] = None
    brief: str = Field(default='', max_length=2000)
    requirements: Optional[TaskCard] = None

class TagsRequest(BaseModel):
    tags: List[str] = Field(default_factory=list, max_length=30)

# --- API Endpoints ---

@app.get("/api/kb")
def api_get_kb(search: str = "", tag: str = "", enrichment_status: str = "",
               min_followers: Optional[int] = Query(None, ge=0),
               max_followers: Optional[int] = Query(None, ge=0),
               min_avg_likes: Optional[int] = Query(None, ge=0),
               updated_within_days: Optional[int] = Query(None, ge=1, le=3650)):
    """达人知识库全量列表"""
    creators = workflow.get_kb(search, tag, enrichment_status)
    if min_followers is not None:
        creators = [c for c in creators if c.get('followers') is not None and c['followers'] >= min_followers]
    if max_followers is not None:
        creators = [c for c in creators if c.get('followers') is not None and c['followers'] <= max_followers]
    if min_avg_likes is not None:
        creators = [c for c in creators if c.get('latest_avg_likes') is not None and c['latest_avg_likes'] >= min_avg_likes]
    if updated_within_days is not None:
        def recent(c):
            try:
                stamp = datetime.fromisoformat(str(c.get('updated_at'))).astimezone()
                return 0 <= (datetime.now().astimezone()-stamp).total_seconds() <= updated_within_days * 86400
            except (TypeError, ValueError):
                return False
        creators = [c for c in creators if recent(c)]
    return redact_secrets({"creators": creators, "total": len(creators)})


@app.patch('/api/kb/{creator_id}/tags')
def api_tags(creator_id: int, req: TagsRequest):
    if not any(c['creator_id'] == creator_id for c in get_knowledge_base()):
        return JSONResponse(status_code=404, content={'error': '达人不存在'})
    if any(not t.strip() or len(t.strip()) > 60 for t in req.tags):
        return JSONResponse(status_code=422, content={'error': '标签须为 1–60 字'})
    workflow.update_tags(creator_id, req.tags)
    return {'status': 'saved'}


@app.get('/api/tasks/{task_id}/candidates')
def api_candidates(task_id: int, view: str = Query('eligible')):
    if view not in ('eligible', 'pending', 'rejected', 'all'):
        return JSONResponse(status_code=422, content={'error': 'view 须为 eligible、pending、rejected 或 all'})
    task = workflow.task_state(task_id)
    if not task:
        return JSONResponse(status_code=404, content={'error': '任务不存在'})
    creators = workflow.get_candidates(task_id)
    summary = task.get('counts') or {}
    if view != 'all':
        creators = [row for row in creators if row.get('candidate_status') == view]
    return redact_secrets({'task': task, 'creators': creators, 'summary': summary, 'view': view})


@app.post('/api/tasks/{task_id}/stop')
def api_stop(task_id: int):
    with task_lock:
        if not get_task(task_id):
            return JSONResponse(status_code=404, content={'error': '任务不存在'})
        workflow.request_stop(task_id)
        return redact_secrets({'task': workflow.task_state(task_id), 'status': 'stop_requested'})


def selection_error(task_id, ids, require_eligible=False, excluded_ids=()):
    task = get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={'error': '任务不存在'})
    if task['status'] in ACTIVE_STATUSES:
        return JSONResponse(status_code=409, content={'error': '请先结束或停止当前任务，再确认选择'})
    candidates = {c['creator_id']: c for c in workflow.get_candidates(task_id)}
    if not (set(ids) | set(excluded_ids)).issubset(candidates):
        return JSONResponse(status_code=400, content={'error': '所选达人不属于本任务'})
    if require_eligible:
        ineligible = [cid for cid in ids
                      if candidates[cid].get('candidate_status') != 'eligible']
        if ineligible:
            return JSONResponse(status_code=409, content={
                'error': '所选候选人尚未通过当前数值与方向审核，请先补审或选择可选候选',
                'code': 'ineligible_selection', 'creator_ids': ineligible})


@app.patch('/api/tasks/{task_id}/selection')
def api_selection(task_id: int, req: SelectionState):
    with task_lock:
        error = selection_error(task_id, req.creator_ids, require_eligible=bool(req.creator_ids),
                                excluded_ids=req.excluded_ids)
        if error is not None:
            return error
        if set(req.creator_ids) & set(req.excluded_ids):
            return JSONResponse(status_code=400, content={'error': '选择与排除不能重叠'})
        workflow.set_candidate_selection(task_id, req.creator_ids, req.excluded_ids)
    return {'status': 'saved'}


def dynamic_metrics_stale(candidate):
    try:
        ttl = max(1, int(get_all_settings().get('dynamic_ttl_hours', '24'))) * 3600
        for field in ('updated_at', 'captured_at'):
            stamp = datetime.fromisoformat(str(candidate.get(field)).replace('Z','+00:00')).astimezone()
            if not 0 <= (datetime.now().astimezone() - stamp).total_seconds() <= ttl:
                return True
        return any(candidate.get(k) is None for k in ('followers','avg_likes','max_likes','days_since_latest'))
    except (ValueError, TypeError, OverflowError):
        return True


@app.post('/api/tasks/{task_id}/report')
def api_admit_report(task_id: int, req: CreatorSelection):
    with task_lock:
        error = selection_error(task_id, req.creator_ids)
        if error is not None:
            return error
        return redact_secrets(workflow.admit_report(task_id, req.creator_ids, req.allow_missing))


async def background_enrich_worker(task_id: int, creator_ids: list[int]):
    creator_ids = list(dict.fromkeys(creator_ids))
    processed, succeeded, admitted, failed, skipped, admission_pending = 0, 0, 0, 0, 0, 0
    reasons = Counter()
    failure_details = []
    verification_progress = {}
    reason_labels = {
        'customer_entitlement_required': '星图客户权益等级不足，无法读取更多数据',
        'audience_schema_not_verified': '粉丝画像尚未获取/接口未核实',
        'audience_permission_denied': '星图客户权益不足',
        'douyin_identifier_required': '缺少可核验的抖音标识',
        'browser_or_saved_session_unavailable': '浏览器或已保存会话不可用',
        'login_or_permission_challenge': '平台要求登录或人工验证',
        'market_access_denied': '星图市场访问受限',
        'search_access_denied': '星图搜索访问受限',
        'search_api_rejected': '星图搜索接口拒绝请求',
        'bounded_lookup_timeout': '平台查询超时',
        'market_http_error': '星图市场响应异常',
        'search_http_error': '星图搜索响应异常',
        'unsupported_search_response': '平台响应格式无法核验',
        'no_results_on_requested_search_page': '当前搜索页未找到匹配账号',
        'no_unique_douyin_identity_mapping': '星图账号身份未能唯一匹配',
        'browser_or_response_failure': '平台读取异常',
    }

    def enrichment_reason(data):
        reason = str(data.get('reason') or '')
        if reason in reason_labels:
            return reason_labels[reason]
        if reason.startswith(('identity_', 'douyin_identity_', 'xingtu_identity_')):
            return '星图账号身份匹配失败'
        if reason.startswith(('audience_', 'fans_', 'fan_')):
            return '星图粉丝详情查询失败'
        fallback = {'permission_denied': '平台权限受限', 'not_found': '未找到匹配账号',
                    'identity_unconfirmed': '身份尚未核验', 'unavailable': '平台数据暂不可用'}
        return fallback.get(data.get('status'), '平台读取异常')

    def progress():
        return {**verification_progress, 'enrichment_processed': processed,
                'enrichment_requested': len(creator_ids), 'enrichment_succeeded': succeeded,
                'enrichment_admitted': admitted, 'enrichment_failed': failed,
                'enrichment_skipped': skipped,
                'enrichment_unprocessed': len(creator_ids) - succeeded - failed - skipped,
                'admission_pending': admission_pending, 'enrichment_failure_reasons': dict(reasons),
                'enrichment_failure_details': failure_details}

    def summary():
        reason_summary = '；'.join(f'{label} {count} 位' for label, count in sorted(reasons.items()))
        return (f'本轮星图补全成功 {succeeded}/{len(creator_ids)} 位，补全失败 {failed} 位，'
                f'跳过 {skipped} 位，未处理 {len(creator_ids) - succeeded - failed - skipped} 位；'
                f'本轮新增正式报告 {admitted} 位，报告待处理 {admission_pending} 位。'
                + (f'原因（同一账号可有多项）：{reason_summary}。' if reason_summary else ''))

    def admission_reason(code):
        if code == 'identity_verified':
            return '身份尚未核验'
        if code == 'median_play_30d':
            return '近30天播放中位数缺失'
        if code.startswith('audience.'):
            return '粉丝画像不完整'
        if code == 'enrichment.expired':
            return '星图数据时间缺失或已过期'
        if code == 'enrichment.source_url':
            return '星图数据来源缺失或无效'
        if code.startswith(('overall_status:', 'topic:')) or code == 'hard_conditions':
            return '候选资格或方向审核未通过'
        if code == 'excluded':
            return '候选已被排除'
        return '报告核验资料不完整'

    try:
        search_context = _search_verification_context(get_task(task_id) or {})
        if search_context is not None:
            verification_progress['search_verification'] = search_context
        candidates = {c['creator_id']: c for c in workflow.get_candidates(task_id)}
        report_ids = {c['creator_id'] for c in workflow.get_report(task_id)}
        for cid in creator_ids:
            if workflow.should_stop(task_id):
                break
            update_task_status(task_id, 'enriching', f'正在核验星图 {processed + 1}/{len(creator_ids)}',
                               progress())
            if dynamic_metrics_stale(candidates[cid]):
                try:
                    await refresh_candidate(task_id, cid)
                    candidates[cid] = next(c for c in workflow.get_candidates(task_id) if c['creator_id'] == cid)
                except RuntimeError:
                    failed += 1
                    admission_pending += 1
                    reasons['候选资料刷新失败'] += 1
                    failure_details.append({'creator_id': cid, 'reasons': ['候选资料刷新失败']})
                    continue
            if workflow.should_stop(task_id):
                break
            current = next((c for c in workflow.get_candidates(task_id) if c['creator_id'] == cid), None)
            if not current or current.get('candidate_status') != 'eligible':
                skipped += 1
                admission_pending += 1
                reasons['候选资格或方向审核未通过'] += 1
                failure_details.append({'creator_id': cid, 'reasons': ['候选资格或方向审核未通过']})
                continue
            candidates[cid] = current
            try:
                data = await enrich_creator(candidates[cid])
            except Exception:
                data = {'status': 'failed', 'reason': 'browser_or_response_failure',
                        'identity_verified': False, 'missing_fields': ['星图读取失败']}
            workflow.save_enrichment(task_id, cid, data)
            candidate_reasons = set()
            if data.get('status') != 'completed':
                failed += 1
                candidate_reasons.add(enrichment_reason(data))
            else:
                succeeded += 1
            processed += 1
            reasons.update(candidate_reasons)
            detail = {'creator_id': cid, 'reasons': sorted(candidate_reasons)}
            failure_details.append(detail)
            with task_lock:
                admission = workflow.admit_report(task_id, [cid])
            # admit_report also returns existing members; only new rows count this round.
            new_ids = set(admission['admitted']) - report_ids
            admitted += len(new_ids)
            report_ids.update(new_ids)
            admission_pending += len(admission['pending'])
            if cid in admission['pending']:
                admission_reasons = {admission_reason(str(code))
                    for code in admission.get('details', {}).get(cid, [])}
                reasons.update(admission_reasons - candidate_reasons)
                candidate_reasons.update(admission_reasons)
            detail['reasons'] = sorted(candidate_reasons)
            if not candidate_reasons:
                failure_details.pop()
        stopped = workflow.should_stop(task_id)
        report_count = len(workflow.get_report(task_id))
        status = ('stopped' if stopped else 'failed' if failed and not succeeded and not admitted
                  else 'partial' if failed or admission_pending else 'completed')
        update_task_status(task_id, status, summary()
                           + f'正式报告当前 {report_count} 位。' + ('已手动停止。' if stopped else ''),
                           progress())
    except Exception:
        stopped = workflow.should_stop(task_id)
        update_task_status(task_id, 'stopped' if stopped else 'failed', summary()
                           + ('已手动停止。' if stopped else '星图处理异常停止，已保存取得的数据，请查看候选状态。'),
                           progress())
    finally:
        task = workflow.task_state(task_id)
        save_chat_message('assistant', task['message'],
                          {'task_finished': True, 'stage': 'enrichment', 'task_id': task_id,
                           'status': task['status'], 'counts': task['counts'], 'progress': task['progress']})
        with task_lock:
            sync_running_status()


@app.post('/api/tasks/{task_id}/enrich')
def api_enrich(task_id: int, req: CreatorSelection, background_tasks: BackgroundTasks):
    with task_lock:
        if platform_session_manager.busy():
            return JSONResponse(status_code=409, content={'error': '平台正在登录或验证，请先完成或取消后再开始补全。'})
        error = selection_error(task_id, req.creator_ids, require_eligible=True,
                                excluded_ids=req.excluded_ids)
        if error is not None:
            return error
        if set(req.creator_ids) & set(req.excluded_ids):
            return JSONResponse(status_code=400, content={'error': '选择与排除不能重叠'})
        if sync_running_status()['is_running']:
            return JSONResponse(status_code=409, content={'error': '另一个任务仍在执行'})
        workflow.set_candidate_selection(task_id, req.creator_ids, req.excluded_ids)
        # Explicit user action starts a new stage; previous stop is not inherited.
        with get_connection() as conn:
            conn.execute('UPDATE tasks SET stop_requested=0, finished_at=NULL WHERE id=?', (task_id,))
        update_task_status(task_id, 'enriching', '已确认选择，准备星图补全')
        task = get_task(task_id)
        current_running_task.update(is_running=True, task_id=task_id, keyword=task['keyword'],status='enriching')
        background_tasks.add_task(background_enrich_worker, task_id, list(dict.fromkeys(req.creator_ids)))
    return {'status': 'queued', 'task_id': task_id}


@app.post('/api/kb/import')
def api_kb_import(req: KBImportRequest):
    with task_lock:
        return import_kb_selection(req)


def import_kb_selection(req: KBImportRequest):
    valid = {c['creator_id'] for c in get_knowledge_base()}
    if not set(req.creator_ids).issubset(valid):
        return JSONResponse(status_code=400, content={'error': '所选达人不存在'})
    task_id = req.task_id
    if task_id is not None and not get_task(task_id):
        return JSONResponse(status_code=404, content={'error': '任务不存在'})
    if task_id is not None and get_task(task_id)['status'] in ACTIVE_STATUSES:
        return JSONResponse(status_code=409, content={'error': '目标任务仍在执行，请结束后再导入'})
    if task_id is None:
        brief = req.brief.strip()
        requirements = req.requirements.model_dump() if req.requirements else {}
        requirements.update(report_kind='recommendation' if brief else 'information', brief=brief)
        if brief and req.requirements is None:
            requirements['unstructured_brief'] = True
        task_id = save_task(brief[:80] if brief else '知识库资料清单', brief[:200] if brief else '',
                            requirements=requirements, status='completed')
    workflow.attach_kb(task_id, req.creator_ids)
    information = get_task(task_id)['requirements'].get('report_kind') == 'information'
    result = workflow.admit_report(task_id, req.creator_ids, allow_missing=req.allow_missing or information)
    return redact_secrets({'task_id': task_id, **result})

@app.get("/api/tasks")
def api_get_tasks():
    """所有历史任务列表"""
    return redact_secrets({"tasks": get_all_tasks()})

@app.get("/api/results")
def api_get_results(task_id: Optional[int] = Query(None)):
    """获取指定任务（或最新任务）的推荐结果"""
    tasks = get_all_tasks()
    task_id = task_id or (tasks[0]['id'] if tasks else None)
    creators = workflow.get_report(task_id) if task_id else []
    tasks = get_all_tasks()
    selected_task = None
    if tasks:
        if task_id:
            for t in tasks:
                if t["id"] == task_id:
                    selected_task = t
                    break
        else:
            selected_task = tasks[0]

    return redact_secrets({
        "selected_task": workflow.task_state(selected_task['id']) if selected_task else None,
        "tasks": tasks,
        "creators": creators,
        "running_status": api_get_status()
    })

@app.post("/api/results/batch_save")
def api_batch_save_results(req: BatchSaveResultsRequest):
    """保存或更新任务中的达人表格数据（单元格修改入库）"""
    workflow.update_report(req.task_id, req.creators)
    return {"status": "ok", "message": f"成功保存 {len(req.creators)} 条达人数据"}

@app.post("/api/results/add_row")
def api_add_row(req: AddRowRequest):
    """增补一行空白达人"""
    return JSONResponse(status_code=409, content={'error': '正式报告不创建无来源空行，请从候选或知识库导入'})

@app.post("/api/results/delete_row")
def api_delete_row(req: DeleteRowRequest):
    """删除指定达人行"""
    workflow.remove_report_entry(req.task_id, req.creator_id)
    return {"status": "ok"}

@app.get("/api/chat/history")
def api_get_chat_history():
    """获取对话历史"""
    messages = get_chat_history()
    # 如果没有历史记录，注入一条友好的系统欢迎语
    if not messages:
        welcome_meta = {
            "is_welcome": True,
            "suggestions": [
                "帮我找5位食品测评达人，均赞1万以上",
                "寻找近两周活跃的休闲零食腰部KOC",
                "挖掘食品安全科普类高互动博主"
            ]
        }
        return {"messages": [{
            "id": 0,
            "role": "assistant",
            "content": "您好！这里是达人 BD 工作台。启用并配置大模型后，可通过对话整理需求；确认任务卡后开始检索候选，您再次勾选确认后才尝试星图补全。推荐依据为数值规则，不代表内容质量判断；完整星图画像及平台权限尚待验证。未完成核验的候选不会自动成为正式报告。",
            "meta": welcome_meta,
            "created_at": "刚刚"
        }]}
    # Historical status cards use current blocked state; stored chat stays intact.
    blocked_notices = {}
    for message in messages:
        meta = message.get('meta') or {}
        task_id = meta.get('task_id')
        if not meta.get('task_finished') or not isinstance(task_id, int) or isinstance(task_id, bool):
            continue
        if task_id not in blocked_notices:
            task = get_task(task_id)
            blocked_notices[task_id] = search_completion_notice(task_id) if task and task['status'] == 'blocked' else None
        notice = blocked_notices[task_id]
        if notice:
            message['content'], current_meta = notice
            message['meta'] = {**meta, **current_meta}
    return redact_secrets({"messages": messages})


@app.delete("/api/chat/history")
def api_clear_chat_history():
    clear_chat_history()
    return {"status": "cleared"}


@app.get("/api/chat/prompt")
def api_get_prompt():
    return get_system_prompt()


@app.post("/api/llm/test")
async def api_test_llm(overrides: Optional[Dict[str, str]] = Body(None)):
    overrides = dict(overrides or {})
    if not overrides.get("llm_api_key", "").strip():
        overrides.pop("llm_api_key", None)
    try:
        result = await test_llm_connection(overrides=overrides)
    except Exception:
        result = {"ok": False, "message": "模型连接测试失败，请检查配置或稍后重试", "model": overrides.get("llm_model", "")}
    return redact_secrets(result, overrides.get("llm_api_key", ""))

@app.post("/api/chat")
async def api_send_chat(req: ChatRequest):
    """用户在首页发送自然语言需求"""
    user_msg = req.message.strip()
    if not user_msg:
        return JSONResponse(status_code=400, content={"error": "消息不能为空"})

    history = get_chat_history()
    save_chat_message("user", user_msg)
    try:
        parsed = await parse_and_respond_chat(user_msg, history=history)
    except Exception:
        parsed = {
            "intent": "chat", "reply": "模型服务暂不可用，请检查设置后重试。未创建找人任务。",
            "task_card": None, "engine": "unavailable",
            "error": {"code": "chat_failed", "message": "模型请求失败"},
            "prompt_version": get_system_prompt()["version"]
        }
    if parsed.get("error") or parsed.get("engine") != "llm":
        parsed["task_card"] = None
    parsed = redact_secrets(parsed)

    # 保存 Agent 响应
    parsed["message_id"] = save_chat_message("assistant", parsed.get("reply", ""), parsed.get("task_card"))
    return parsed

def search_completion_notice(task_id: int):
    task = workflow.task_state(task_id) or {'status': 'failed', 'message': '任务记录不存在'}
    counts = task.get('counts') or {}
    state = task['status']
    labels = {'completed': '本轮候选检索结束', 'partial': '本轮检索未完整完成',
              'blocked': '检索受阻，需要人工验证', 'failed': '执行失败',
              'interrupted': '执行中断', 'stopped': '已停止检索'}
    outcome = ''
    if state == 'blocked':
        outcome = ('尚未取得任何候选账号。' if counts.get('discovered', 0) == 0
                   else f'已发现 {counts["discovered"]} 个账号，但后续检索未完成。')
    content = f'找人任务【{task.get("keyword", "")}】{labels.get(state, "状态待核实")}。{outcome}{task.get("message", "")}'
    meta = {'task_finished': True, 'stage': 'discovery', 'task_id': task_id,
                     'keyword': task.get('keyword', ''), 'status': state, 'counts': counts,
                     'pause_reason': (task.get('progress') or {}).get('pause_reason')}
    if (task.get('requirements') or {}).get('discovery_mode') == 'xingtu':
        meta.update(platform='xingtu', access_code=(task.get('progress') or {}).get('access_code'))
    context = (task.get('progress') or {}).get('search_verification')
    if isinstance(context, dict):
        meta['search_verification'] = context
    return content, meta


async def background_search_worker(task_id: int, req: ExecuteTaskRequest):
    def on_progress(message, progress):
        task = get_task(task_id)
        if task and task['status'] in ACTIVE_STATUSES:
            update_task_status(task_id, task['status'], redact_secrets(message), progress)
        with task_lock:
            if current_running_task.get("task_id") == task_id:
                current_running_task.update(message=redact_secrets(message), step=redact_secrets(message), progress=progress)

    try:
        update_task_status(task_id, "running", "正在检索候选达人")
        with task_lock:
            if current_running_task.get("task_id") == task_id:
                current_running_task.update(status="running", step="正在检索候选达人", message="正在执行规则筛选")
        returned_id = await execute_bd_search_task(
            req.keyword, req.sample_count, custom_task_name=req.task_name,
            min_avg_likes=req.min_avg_likes, active_days=req.active_days, task_id=task_id,
            min_followers=req.min_followers, max_followers=req.max_followers, min_max_likes=req.min_max_likes,
            progress_callback=on_progress, discovery_mode=req.discovery_mode,
            search_queries=req.search_queries, topic_min_samples=req.topic_min_samples,
            topic_min_ratio=req.topic_min_ratio, category=req.category,
            content_preferences=req.content_preferences, exclusions=req.exclusions,
        )
        task = get_task(task_id)
        if returned_id != task_id or not task or task["status"] not in TERMINAL_STATUSES:
            update_task_status(task_id, "failed", "执行器未提供有效的最终任务状态")
    except Exception as exc:
        task = get_task(task_id)
        if not task or task["status"] != "partial":
            update_task_status(task_id, "failed", redact_secrets(str(exc)))
    finally:
        task = get_task(task_id) or {"status": "failed", "message": "任务记录不存在"}
        with task_lock:
            if current_running_task.get("task_id") == task_id:
                sync_running_status()
        content, meta = search_completion_notice(task_id)
        save_chat_message('assistant', redact_secrets(content), meta)

@app.post("/api/execute")
async def api_execute_task(req: ExecuteTaskRequest, background_tasks: BackgroundTasks):
    """启动自动化找人执行流水线"""
    with task_lock:
        if platform_session_manager.busy():
            return JSONResponse(status_code=409, content={'error': '平台正在登录或验证，请先完成或取消后再执行找人。'})
        if sync_running_status()["is_running"]:
            return JSONResponse(status_code=409, content={"error": "已有找人任务正在执行中，请稍候"})
        # Legacy cards remain readable, but every newly confirmed run now uses
        # Xingtu. Never silently retry the old Douyin discovery channel.
        req = req.model_copy(update={'discovery_mode': 'xingtu'})
        requirements = req.model_dump(exclude={"task_name"})
        task_id = save_task(req.task_name or req.keyword, req.keyword, requirements=requirements, status="queued")
        current_running_task.update(is_running=True, task_id=task_id, keyword=req.keyword, status="queued", step="等待执行", message="任务已排队", progress={})
        try:
            background_tasks.add_task(background_search_worker, task_id, req)
        except Exception:
            update_task_status(task_id, "failed", "任务排队失败")
            sync_running_status()
            return JSONResponse(status_code=503, content={"error": "任务排队失败", "task_id": task_id})
    return {"status": "queued", "keyword": req.keyword, "task_id": task_id}

async def background_review_worker(task_id: int, creator_ids: list[int]):
    settings = get_all_settings()
    task = get_task(task_id)
    policy = qualification.policy_from_requirements(task['requirements'] if task else {}, settings)
    budget = {'used': 0, 'limit': policy['max_reviews_per_run'], 'exhausted': False}
    progress = {'review_requested': len(creator_ids), 'review_processed': 0,
                'topic_reviews': 0, 'topic_budget': budget['limit'],
                'review_eligible': 0, 'review_pending': 0, 'review_rejected': 0,
                'review_failed': 0}
    processed = 0
    failed = 0
    pause = False
    try:
        for cid in dict.fromkeys(creator_ids):
            if workflow.should_stop(task_id):
                break
            rows = {row['creator_id']: row for row in workflow.get_candidates(task_id)}
            row = rows.get(cid)
            if row is None:
                continue
            if candidate_assessment_reusable(row.get('assessment')):
                continue
            if budget['used'] >= budget['limit']:
                budget['exhausted'] = True
                pause = True
                break
            update_task_status(task_id, 'running',
                               f'正在补审 {processed + 1}/{len(creator_ids)}；内容审核 {budget["used"]}/{budget["limit"]} 次',
                               dict(progress))
            try:
                assessment = await review_existing_candidate(task_id, cid, settings, policy, budget=budget)
            except Exception:
                failed += 1
                progress['review_failed'] = failed
                continue
            finally:
                progress['topic_reviews'] = budget['used']
            processed += 1
            status = assessment.get('overall_status') or 'pending'
            progress['review_processed'] = processed
            progress['review_' + ('eligible' if status == 'eligible' else
                                  ('rejected' if status == 'rejected' else 'pending'))] += 1
        stopped = workflow.should_stop(task_id)
        pause = pause or budget['exhausted']
        if pause:
            progress['pause_reason'] = 'topic_review_budget'
        remaining_pending = sum(row.get('candidate_status') == 'pending'
                                for row in workflow.get_candidates(task_id))
        status = ('stopped' if stopped else
                  ('partial' if pause or failed or remaining_pending else 'completed'))
        update_task_status(task_id, status,
                           f'补审结束：处理 {processed} 位，内容审核 {budget["used"]}/{budget["limit"]} 次，'
                           f'待核实 {remaining_pending} 位，失败 {failed} 位。' +
                           (' 审核预算已用完，可确认继续。' if pause else ''),
                           dict(progress))
    except Exception:
        update_task_status(task_id, 'failed', '补审异常停止，已保留取得的审核结果。')
    finally:
        state = workflow.task_state(task_id)
        save_chat_message('assistant', '候选补审已结束，请查看可选、待核实与不符合分类。',
                          {'task_finished': True, 'task_id': task_id,
                           'status': state['status'] if state else 'failed'})
        with task_lock:
            sync_running_status()


@app.post('/api/tasks/{task_id}/review')
def api_review(task_id: int, req: ReviewRequest, background_tasks: BackgroundTasks):
    with task_lock:
        task = get_task(task_id)
        if not task:
            return JSONResponse(status_code=404, content={'error': '任务不存在'})
        if task['status'] in ACTIVE_STATUSES or sync_running_status()['is_running']:
            return JSONResponse(status_code=409, content={'error': '另一个任务仍在执行，请结束后再补审'})
        candidates = workflow.get_candidates(task_id)
        valid = {row['creator_id'] for row in candidates}
        if req.creator_ids is None:
            creator_ids = [row['creator_id'] for row in candidates
                           if row.get('candidate_status') == 'pending']
        else:
            creator_ids = list(dict.fromkeys(req.creator_ids))
            if not set(creator_ids).issubset(valid):
                return JSONResponse(status_code=400, content={'error': '所选达人不属于本任务'})
        if not creator_ids:
            return JSONResponse(status_code=400, content={'error': '本任务没有待补审的线索'})
        with get_connection() as conn:
            conn.execute('UPDATE tasks SET stop_requested=0, finished_at=NULL WHERE id=?', (task_id,))
        update_task_status(task_id, 'running', '已确认补审，准备读取已保存的观察证据')
        current_running_task.update(is_running=True, task_id=task_id,
                                    keyword=task['keyword'], status='running',
                                    step='补审候选', message='正在补审候选')
        background_tasks.add_task(background_review_worker, task_id, creator_ids)
    return {'status': 'queued', 'task_id': task_id, 'creator_ids': creator_ids}


@app.get("/api/status")
def api_get_status(task_id: Optional[int] = None):
    with task_lock:
        if task_id is not None:
            task = workflow.task_state(task_id)
            if not task:
                return JSONResponse(status_code=404,content={'error':'任务不存在'})
            return redact_secrets({**task, 'task_id': task_id, 'is_running': task['status'] in ACTIVE_STATUSES})
        return sync_running_status()

@app.get("/api/settings")
def api_get_settings():
    settings = get_all_settings()
    key = settings.pop("llm_api_key", "")
    settings["llm_api_key_configured"] = bool(key.strip())
    return {"settings": settings}

@app.post("/api/settings")
def api_save_settings(req: SettingsRequest):
    if 'dynamic_ttl_hours' in req.settings:
        try:
            ttl = int(req.settings['dynamic_ttl_hours'])
            if not 1 <= ttl <= 8760:
                raise ValueError()
        except (TypeError, ValueError):
            return JSONResponse(status_code=422, content={'error': '数据有效期须为 1–8760 小时'})
    merged = {**get_all_settings(), **req.settings}
    try:
        sample_limit = int(merged['single_post_sample'])
        if not 1 <= sample_limit <= 10:
            raise ValueError()
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={'error': '作品采样数须为 1–10'})
    requirements = [{}] + [task.get('requirements') or {} for task in get_all_tasks()]
    for confirmed in requirements:
        policy = qualification.policy_from_requirements(confirmed, merged)
        if qualification.validate_review_policy(policy):
            return JSONResponse(status_code=422, content={
                'error': '作品采样数不能低于已确认的方向审核样本下限',
                'min_samples': policy['min_samples']})
    for k, v in req.settings.items():
        if k in {"clear_llm_api_key", "llm_api_key_configured"}:
            continue
        if k == "llm_api_key" and (req.clear_llm_api_key or not v.strip()):
            continue
        save_setting(k, str(v))
    if req.clear_llm_api_key:
        save_setting("llm_api_key", "")
    return {"status": "saved"}

@app.get("/api/export")
def download_excel(task_id: Optional[int] = Query(None), recommend_level: str = "", search: str = ""):
    if task_id is None:
        return JSONResponse(status_code=400,content={'error':'请先明确选择一个正式报告任务'})
    if not get_task(task_id):
        return JSONResponse(status_code=404,content={'error':'任务不存在'})
    creators = workflow.get_report(task_id)
    if recommend_level and recommend_level != "ALL":
        creators = [c for c in creators if c.get("recommend_level") == recommend_level]
    search = search.strip().lower()
    if search:
        fields = ("nickname", "unique_id", "category_tag", "wechat", "reason")
        creators = [c for c in creators if any(search in str(c.get(field) or "").lower() for field in fields)]
    if not creators:
        return JSONResponse(status_code=404, content={"error": "没有可导出的达人记录"})
    
    tasks = get_all_tasks()
    t_name = f"任务_{task_id}_达人清单"
    t_kw = "食品"
    for t in tasks:
        if t["id"] == task_id or not task_id:
            t_name = t["name"]
            t_kw = t["keyword"]
            break

    safe_export = redact_secrets({'name':t_name,'keyword':t_kw,'creators':creators})
    excel_path = export_task_to_excel(safe_export['name'], safe_export['keyword'], safe_export['creators'])
    filename = os.path.basename(excel_path)
    return FileResponse(
        path=excel_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.get("/", response_class=HTMLResponse)
def index_page():
    return HTML_CONTENT

HTML_CONTENT = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8088)
