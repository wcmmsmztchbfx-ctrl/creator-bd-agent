"""Read the identity-bound detail page through the authorized browser session."""

import asyncio
import re
from urllib.parse import parse_qs, urlsplit

from app.xingtu import _api_error, _gate, _identifier, _mapping
from app.xingtu_detail_fields import verify_base, parse_fans, parse_spread, parse_prices


BASE_PATH = '/gw/api/author/get_author_base_info'
SPREAD_PATH = '/gw/api/data_sp/get_author_spread_info'
PRICE_PATH = '/gw/api/author/get_author_marketing_info'
FANS_PATH = '/gw/api/data_sp/get_author_fans_distribution'
CONTACT_PATH = '/gw/api/ggeneric/apply_contact_info'


def detail_response_matches(response, path, star_id):
    url = urlsplit(response.url)
    if url.scheme != 'https' or url.netloc != 'www.xingtu.cn' or url.path != path:
        return False
    query = parse_qs(url.query)
    if query.get('o_author_id') != [star_id] or query.get('platform_source') != ['1']:
        return False
    if path == FANS_PATH:
        return query.get('author_type') == ['1']
    if query.get('platform_channel') != ['1']:
        return False
    if path == SPREAD_PATH:
        return all(query.get(key) == [value] for key, value in
                   (('type', '1'), ('flow_type', '0'), ('only_assign', 'false'), ('range', '2')))
    return True


def contact_response_matches(response, star_id):
    url = urlsplit(response.url)
    if url.scheme != 'https' or url.netloc != 'www.xingtu.cn' or url.path != CONTACT_PATH:
        return False
    query = parse_qs(url.query)
    try:
        body = response.request.post_data_json if response.request.method == 'POST' else {}
    except Exception:
        body = {}
    identities = [str(value) for key in ('author_id', 'o_author_id', 'dest_id')
                  for value in query.get(key, [])]
    identities.extend(str(body[key]) for key in ('author_id', 'o_author_id', 'dest_id')
                      if isinstance(body, dict) and key in body)
    return bool(identities) and set(identities) == {star_id}


async def _payload(response):
    if response.status in (401, 403, 429):
        raise PermissionError('Detail access denied')
    if response.status != 200:
        raise ValueError('Detail HTTP failure')
    payload = await asyncio.wait_for(response.json(), 5)
    if not isinstance(payload, dict):
        raise ValueError('Invalid detail payload')
    if _api_error(payload) == 'permission_denied':
        raise PermissionError('Detail permission denied')
    base = _mapping(payload.get('base_resp'))
    if type(base.get('status_code')) is not int or base['status_code'] != 0:
        raise ValueError('Detail API did not succeed')
    return payload


def _finalize_core(result):
    core_missing = [f for f in result['missing_fields']
                    if f in ('identity', 'median_play_30d') or f.startswith('audience.')]
    result.update(status='unavailable' if core_missing else 'completed',
                  reason='audience_fields_missing' if core_missing else None)
    result['missing_fields'] = [f for f in result['missing_fields'] if f != 'contact']
    if result['contact'].get('value') is None:
        result['missing_fields'].append('contact')


async def enrich_details(page, creator, result):
    star_id = _identifier(creator.get('star_id') or result.get('star_id'))
    if not star_id or not star_id.isdigit() or len(star_id) > 30:
        result.update(status='identity_unconfirmed', reason='identity_invalid_star_id')
        return result
    source = 'https://www.xingtu.cn/ad/creator/author-homepage/douyin-video/' + star_id
    loop = asyncio.get_running_loop()
    pending = {path: loop.create_future() for path in (BASE_PATH, SPREAD_PATH, PRICE_PATH)}

    def capture(response):
        for path, future in pending.items():
            if not future.done() and detail_response_matches(response, path, star_id):
                future.set_result(response)

    def provenance(field):
        return {'source_url': source, 'field': field, 'permission': 'session_visible'}

    page.on('response', capture)
    stage = 'identity'
    try:
        navigation = await page.goto(source, wait_until='domcontentloaded', timeout=15000)
        if navigation and navigation.status in (401, 403, 429) or await _gate(page):
            raise PermissionError('Detail login required')
        payload = await _payload(await asyncio.wait_for(asyncio.shield(pending[BASE_PATH]), 10))
        profile = verify_base(payload, creator, star_id)
        result.update(identity_verified=True, star_id=star_id, source_url=source, profile=profile)
        result['missing_fields'] = [field for field in result['missing_fields'] if field != 'identity']
        result['field_sources']['identity'] = provenance('get_author_base_info.id/core_user_id')
        stage = 'spread'
        try:
            spread = parse_spread(await _payload(await asyncio.wait_for(asyncio.shield(pending[SPREAD_PATH]), 8)))
            for field, value in spread.items():
                if value is not None:
                    result[field] = value
            if spread.get('median_play_30d') is not None:
                result['missing_fields'] = [f for f in result['missing_fields'] if f != 'median_play_30d']
                result['field_sources']['median_play_30d'] = provenance('get_author_spread_info.play_mid; range=2,type=1,flow_type=0')
            if spread.get('expected_cpm') is not None:
                result['field_sources']['expected_cpm'] = provenance('get_author_spread_info.expect_cpm.cpm_21_60 / 100')
        except (ValueError, PermissionError, TimeoutError):
            pass
        stage = 'audience'
        await page.get_by_text('连接用户', exact=True).first.click(timeout=8000)
        fans = page.get_by_text('粉丝画像', exact=True).first
        await fans.wait_for(state='visible', timeout=10000)
        async with page.expect_response(lambda r: detail_response_matches(r, FANS_PATH, star_id), timeout=10000) as response:
            await fans.click()
        fans_payload = await _payload(await response.value)
        result['audience'] = parse_fans(fans_payload)
        result['audience_kind'] = 'fans'
        result['audience_updated_at'] = fans_payload.get('last_update_time')
        result['audience_share_unit'] = 'ratio'
        for dimension in ('gender', 'age', 'region'):
            if result['audience'].get(dimension):
                field = 'audience.' + dimension
                result['missing_fields'] = [f for f in result['missing_fields'] if f != field]
                result['field_sources'][field] = provenance('get_author_fans_distribution.distributions.' + dimension)
        price_read = False
        async def read_price(wait=0):
            nonlocal price_read
            if price_read or not wait and not pending[PRICE_PATH].done():
                return
            try:
                response = await asyncio.wait_for(asyncio.shield(pending[PRICE_PATH]), wait or 1)
                prices = parse_prices(await _payload(response), star_id)
                price_read = True
                for field, value in prices.items():
                    if value is not None:
                        result[field] = value
                        result['field_sources'][field] = provenance('get_author_marketing_info.price_info.' + field)
            except (ValueError, PermissionError, TimeoutError):
                pass
        await read_price()
        # Core completeness is independent of optional contact/quote latency.
        # The outer deadline can safely return these verified fields.
        _finalize_core(result)
        # Only the explicit business-WeChat viewing action is allowed. Never
        # open online chat, project invitations or phone-call actions.
        stage = 'contact'
        try:
            await page.get_by_text('联系', exact=True).first.click(timeout=4000)
            view = page.get_by_text('查看微信', exact=True).first
            if await view.is_visible():
                async with page.expect_response(lambda r: contact_response_matches(r, star_id), timeout=5000) as response:
                    await view.click(timeout=3000)
                contact = await _payload(await response.value)
                value = contact.get('wechat')
                if contact.get('apply_result') is True and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_\-]{3,128}', value):
                    result['contact'] = {'status':'已获取', 'type':'微信', 'value':value,
                        'source_url':source, 'permission':'session_visible', 'source':'星图后台'}
                else:
                    result['contact'].update(status='未获取', permission='not_provided', source_url=source)
        except Exception:
            result['contact'].update(status='未获取', permission='unavailable', source_url=source)
        await read_price(wait=2)
        _finalize_core(result)
        return result
    except PermissionError:
        result.update(status='permission_denied', reason='audience_permission_denied' if stage == 'audience' else 'detail_permission_denied')
    except Exception:
        if stage == 'identity':
            result.update(identity_verified=False, status='identity_unconfirmed', reason='identity_detail_unconfirmed')
            if 'identity' not in result['missing_fields']:
                result['missing_fields'].append('identity')
        else:
            result.update(status='unavailable', reason='audience_detail_unavailable' if stage == 'audience' else 'detail_unavailable')
    finally:
        page.remove_listener('response', capture)
        for future in pending.values():
            if not future.done():
                future.cancel()
    return result
