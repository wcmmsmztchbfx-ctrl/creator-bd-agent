"""LLM conversation boundary; errors never become a simulated search plan."""
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.contracts import ChatOutput, TaskCard
from app.db import get_all_settings, get_chat_history


PROMPT_PATH = Path(__file__).parent / 'prompts' / 'bd_assistant.md'


def get_system_prompt() -> dict:
    content = PROMPT_PATH.read_text(encoding='utf-8')
    return {'content': content, 'version': hashlib.sha256(content.encode()).hexdigest()[:12]}


def _error(code: str, message: str) -> dict:
    return {
        'intent': 'chat', 'reply': message, 'task_card': None,
        'engine': 'unavailable', 'error': {'code': code, 'message': message},
        'prompt_version': get_system_prompt()['version'],
    }


def _connection(settings: dict) -> tuple[str, dict, str]:
    base = str(settings.get('llm_base_url', '')).strip().rstrip('/')
    model = str(settings.get('llm_model', '')).strip()
    key = str(settings.get('llm_api_key', '')).strip()
    parts = urlsplit(base)
    local = parts.hostname == 'localhost'
    try:
        local = local or ipaddress.ip_address(parts.hostname or '').is_private
    except ValueError:
        pass
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or (parts.scheme == 'http' and not local)):
        raise ValueError('请在设置中填写有效的 HTTPS Base URL；本地模型可使用 HTTP。')
    if not model:
        raise ValueError('请在设置中填写模型名称。')
    if not key and not local:
        raise ValueError('请在设置中填写 API Key。')
    url = base if base.endswith('/chat/completions') else base + '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    return url, headers, model


class ModelFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


async def _complete(settings: dict, messages: list[dict], *, max_tokens=2000, json_mode=False, recover_chat=False) -> str:
    try:
        url, headers, model = _connection(settings)
    except ValueError as exc:
        raise ModelFailure('configuration', str(exc)) from None
    payload = {'model': model, 'messages': messages, 'temperature': 0.2, 'max_tokens': max_tokens}
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code in (400, 422) and json_mode and recover_chat:
                problem = response.text.lower()
                if 'response_format' in problem and any(word in problem for word in ('unsupported', 'not supported', 'unknown', '不支持')):
                    payload.pop('response_format', None)
                    response = await client.post(url, headers=headers, json=payload)
            if recover_chat and response.is_success and 'response_format' in payload:
                try:
                    choice = response.json()['choices'][0]
                    content = choice.get('message', {}).get('content')
                    blank = not isinstance(content, str) or not content.strip()
                except (ValueError, KeyError, IndexError, AttributeError, TypeError):
                    blank = False
                # Some compatible providers occasionally return empty JSON-mode
                # answers. One plain retry recovers chat, not an execution card.
                if blank and choice.get('finish_reason') != 'content_filter':
                    payload.pop('response_format', None)
                    response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise ModelFailure('timeout', '模型响应超时，请稍后重试或在设置中测试连接。') from None
    except httpx.RequestError:
        raise ModelFailure('connection_failed', '无法连接模型服务，请检查接口地址、网络和代理设置。') from None
    if response.status_code in (401, 403):
        raise ModelFailure('authentication_failed', '模型服务拒绝访问，请检查 API Key 和模型使用权限。')
    if response.status_code == 429:
        raise ModelFailure('rate_limited', '模型服务限流或额度不足，请稍后重试并检查额度。')
    if not response.is_success:
        raise ModelFailure('provider_error', f'模型服务返回 HTTP {response.status_code}，请检查接口和模型配置。')
    try:
        text = response.json()['choices'][0]['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('empty response')
        return text.strip()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelFailure('invalid_response', '模型没有返回有效回复，请确认接口兼容 Chat Completions。') from None


async def request_model_json(settings: dict, messages: list[dict], *, max_tokens=2000) -> dict:
    """Reusable JSON boundary for non-chat reviews.

    It reuses the same endpoint, timeout and error classification as chat, but
    callers pass only their own bounded data.  No conversation history is
    appended here.
    """

    raw = await _complete(settings, messages, max_tokens=max_tokens, json_mode=True)
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
    try:
        payload = json.loads(cleaned)
    except (ValueError, TypeError):
        raise ModelFailure('invalid_response', '模型未返回有效 JSON，审核结果未采用。') from None
    if not isinstance(payload, dict):
        raise ModelFailure('invalid_response', '模型返回的审核结果不是 JSON 对象。')
    return payload


async def parse_and_respond_chat(user_msg: str, history=None) -> dict:
    settings = get_all_settings()
    if str(settings.get('llm_enabled', 'false')).lower() != 'true':
        return _error('disabled', 'LLM 对话尚未启用。请到“接入与系统设置”配置模型、测试连接并开启 API；当前不会伪造 AI 回复或创建任务。')
    prompt = get_system_prompt()
    model_context = ('\n\n当前应用配置的接口模型标识：' + json.dumps(str(settings.get('llm_model','')),ensure_ascii=False)
                     + '。用户询问背后模型时可如实说明该配置标识；不要猜测未提供的模型架构或版本，不显示密钥。')
    messages = [{'role': 'system', 'content': prompt['content'] + model_context}]
    previous = get_chat_history() if history is None else history
    previous = list(previous)[-30:]
    if previous and previous[-1].get('role') == 'user' and previous[-1].get('content') == user_msg:
        previous = previous[:-1]
    for item in previous:
        if item.get('role') in ('user', 'assistant') and isinstance(item.get('content'), str):
            messages.append({'role': item['role'], 'content': item['content'][:8000]})
            card = item.get('meta')
            if item.get('role') == 'assistant' and isinstance(card, dict) and card.get('keyword'):
                messages[-1]['content'] += '\n此前待确认的需求卡：' + json.dumps(card, ensure_ascii=False)
    messages.append({'role': 'user', 'content': user_msg[:8000]})
    try:
        raw = await _complete(settings, messages, json_mode=True, recover_chat=True)
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if raw.startswith(('{', '[', '```')):
                raise ValueError('invalid structured reply') from None
            # A genuine plain model answer is usable chat, never an execution card.
            payload = {'intent':'chat', 'reply':raw, 'task_card':None}
        if isinstance(payload, dict) and payload.get('intent') in ('chat','clarify'):
            payload.setdefault('task_card', None)
        if isinstance(payload, dict) and 'intent' not in payload:
            if 'category' in payload and 'keyword' in payload:
                card = TaskCard.model_validate(payload).model_dump()
                payload = {'intent':'search', 'reply':'已整理找人参数，请确认下方任务卡；尚未开始采集。', 'task_card':card}
            elif set(payload).issubset({'task_card','reply'}) and isinstance(payload.get('task_card'),dict):
                card = TaskCard.model_validate(payload['task_card']).model_dump()
                payload = {'intent':'search','reply':payload.get('reply') or '请确认下方任务卡；尚未开始采集。','task_card':card}
            elif set(payload).issubset({'reply','task_card'}) and isinstance(payload.get('reply'),str):
                payload = {'intent':'chat','reply':payload['reply'],'task_card':None}
        result = ChatOutput.model_validate(payload).model_dump()
    except ModelFailure as exc:
        return _error(exc.code, exc.message)
    except (ValidationError, ValueError, TypeError):
        return _error('invalid_response', '模型回复未符合任务协议，未生成可执行任务。请重试；也可在设置中查看系统提示词和测试连接。')
    result.update(engine='llm', error=None, prompt_version=prompt['version'])
    result['model'] = settings.get('llm_model','')
    return result


async def test_llm_connection(overrides=None) -> dict:
    settings = dict(get_all_settings())
    if overrides:
        for key in ('llm_base_url', 'llm_model', 'llm_api_key'):
            if key in overrides and (key != 'llm_api_key' or str(overrides[key]).strip()):
                settings[key] = str(overrides[key])
    try:
        await _complete(settings, [
            {'role': 'system', 'content': 'This is a connectivity check. Reply with OK only.'},
            {'role': 'user', 'content': 'Reply OK.'},
        ], max_tokens=16)
    except ModelFailure as exc:
        return {'ok': False, 'message': exc.message, 'error': {'code': exc.code}, 'model': settings.get('llm_model', '')}
    return {'ok': True, 'message': '模型连接成功。该测试未发送聊天历史；启用 API 后可开始对话。', 'model': settings.get('llm_model', '')}
