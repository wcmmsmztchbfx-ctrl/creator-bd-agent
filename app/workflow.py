"""Persistent candidate selection, enrichment and immutable report evidence."""

import datetime
import json
import math
import re
from contextlib import contextmanager
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app import db


@contextmanager
def _connection():
    conn = db.get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now():
    return datetime.datetime.now().astimezone()


def _stamp():
    return _now().isoformat()


def _parse_time(value, naive_timezone=None):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None and naive_timezone is not None:
            parsed = parsed.replace(tzinfo=naive_timezone)
        return parsed.astimezone()
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_timestamp(value, naive_timezone=None):
    if not isinstance(value, str) or len(value) <= 10 or value[10] not in ('T', ' '):
        return None
    return _parse_time(value, naive_timezone)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _ttl():
    try:
        value = float(db.get_setting('dynamic_ttl_hours', '24'))
        return value if math.isfinite(value) and value > 0 else 24.0
    except (TypeError, ValueError):
        return 24.0


def _fresh(captured_at):
    captured = _parse_time(captured_at)
    return captured is not None and 0 <= (_now() - captured).total_seconds() <= _ttl() * 3600


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _task(task_id):
    task = db.get_task(task_id)
    if task is None:
        raise ValueError('Task not found')
    return task


def _information(requirements):
    return not requirements or requirements.get('report_kind') == 'information'


def init_workflow_db():
    db.init_db()
    with _connection() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS candidate_selection (
                task_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, creator_id)
            );
            CREATE TABLE IF NOT EXISTS creator_enrichments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                UNIQUE (task_id, creator_id)
            );
            CREATE TABLE IF NOT EXISTS formal_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (task_id, creator_id)
            );
            CREATE TABLE IF NOT EXISTS creator_tags (
                creator_id INTEGER PRIMARY KEY,
                tags_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS enrichment_creator_idx
                ON creator_enrichments (creator_id, id);
        ''')
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('dynamic_ttl_hours','24')")


def task_state(task_id):
    task = db.get_task(task_id)
    if task is None:
        return None
    candidates = get_candidates(task_id)
    report = get_report(task_id)
    start = _parse_time(task.get('started_at'))
    finish = _parse_time(task.get('finished_at'))
    task['stop_requested'] = bool(task['stop_requested'])
    task['elapsed_seconds'] = max(0, ((finish or _now()) - start).total_seconds()) if start else 0
    task['active'] = task['status'] in db.ACTIVE_TASK_STATUSES
    task['stopping'] = task['status'] == 'stopping'
    task['is_running'] = task['active']
    requirements = task['requirements']
    target = requirements.get('target_count', requirements.get('count', requirements.get('sample_count', 5)))
    discovered = len(candidates)
    eligible = sum(row.get('candidate_status') == 'eligible' for row in candidates)
    pending = sum(row.get('candidate_status') == 'pending' for row in candidates)
    rejected = sum(row.get('candidate_status') == 'rejected' for row in candidates)
    numeric_passed = sum(
        (row.get('assessment') or {}).get('numeric', {}).get('status') == 'passed'
        or (not row.get('assessment') and row.get('hard_pass') == 1)
        for row in candidates)
    topic_reviewed = sum(bool((row.get('assessment') or {}).get('topic_reviewed')) for row in candidates)
    task['counts'] = dict(
        candidates=discovered, discovered=discovered,
        eligible=eligible, pending=pending, rejected=rejected,
        prequalified=numeric_passed, topic_reviewed=topic_reviewed,
        selected=sum(row['selected'] for row in candidates),
        enriched=sum(row['enrichment'] is not None for row in candidates),
        report=len(report), qualified=sum(bool(row['qualified']) for row in report),
        target=0 if _information(requirements) else target,
    )
    return task


def request_stop(task_id):
    _task(task_id)
    with _connection() as conn:
        conn.execute("""UPDATE tasks SET stop_requested=1, status='stopping', updated_at=?
                        WHERE id=? AND status IN ('queued','running','stopping','enriching')""",
                     (_stamp(), task_id))
    return task_state(task_id)


def should_stop(task_id):
    task = db.get_task(task_id)
    return task is None or bool(task['stop_requested'])


def _tags(conn, creator_id):
    row = conn.execute('SELECT tags_json FROM creator_tags WHERE creator_id=?', (creator_id,)).fetchone()
    return json.loads(row['tags_json']) if row else []


def _identity_confirmed(data):
    return (data.get('identity_verified') is True and data.get('status') != 'identity_unconfirmed'
            and not {'identity', 'identity_verified'}.intersection(_missing_list(data.get('missing_fields'))))


def _enrichment(conn, task_id, creator_id):
    rows = conn.execute('''SELECT task_id,data_json FROM creator_enrichments WHERE creator_id=?
                           ORDER BY saved_at DESC,id DESC''', (creator_id,)).fetchall()
    invalidated = False
    for row in rows:
        data = json.loads(row['data_json'])
        if row['task_id'] == task_id:
            if not invalidated:
                return data
            break
        if not _identity_confirmed(data) or data.get('status') != 'completed':
            invalidated = True
    if rows:
        latest = json.loads(rows[0]['data_json'])
        if not _enrichment_problems(latest):
            return latest
    return None


def get_candidates(task_id):
    if db.get_task(task_id) is None:
        return []
    rows = db.get_task_results(task_id)
    with _connection() as conn:
        for item in rows:
            cid = item['creator_id']
            flags = conn.execute('SELECT selected,excluded FROM candidate_selection WHERE task_id=? AND creator_id=?',
                                 (task_id, cid)).fetchone()
            item.update(selected=bool(flags['selected']) if flags else False,
                        excluded=bool(flags['excluded']) if flags else False,
                        enrichment=_enrichment(conn, task_id, cid), tags=_tags(conn, cid))
            assessment = db.get_current_candidate_assessment(task_id, cid)
            item['assessment'] = assessment
            item['candidate_status'] = (assessment or {}).get('overall_status') or 'pending'
            item['discovery_sources'] = db.get_discovery_sources(task_id, cid)
            # Detail-only identity display must not rewrite the inputs of the
            # frozen content assessment and invalidate an otherwise valid review.
            _apply_detail_profile(item, item['enrichment'])
    return rows


def set_candidate_selection(task_id, creator_ids, excluded_ids=[]):
    _task(task_id)
    selected, excluded = set(creator_ids), set(excluded_ids)
    valid = {row['creator_id'] for row in get_candidates(task_id)}
    if (selected | excluded) - valid:
        raise ValueError('Creator is not attached to this task')
    with _connection() as conn:
        for cid in valid:
            conn.execute('''INSERT INTO candidate_selection VALUES (?,?,?,?,?)
                            ON CONFLICT(task_id,creator_id) DO UPDATE SET
                            selected=excluded.selected, excluded=excluded.excluded, updated_at=excluded.updated_at''',
                         (task_id, cid, int(cid in selected and cid not in excluded), int(cid in excluded), _stamp()))
    return get_candidates(task_id)


def _apply_detail_profile(item, enrichment):
    if not enrichment or not _identity_confirmed(enrichment):
        return
    profile = enrichment.get('profile')
    if not isinstance(profile, dict) or profile.get('star_id') != item.get('star_id'):
        return
    if item.get('uid') and profile.get('uid') != item['uid']:
        return
    for field in ('unique_id', 'uid'):
        if isinstance(profile.get(field), str) and profile[field].strip():
            item[field] = profile[field]
    sec_uid = profile.get('sec_uid')
    if isinstance(sec_uid, str) and re.fullmatch(r'[A-Za-z0-9_\-]{5,256}', sec_uid):
        item['profile_url'] = 'https://www.douyin.com/user/' + sec_uid
    contact = enrichment.get('contact') or {}
    value = contact.get('value')
    if (contact.get('status') == '已获取' and contact.get('type') == '微信'
            and contact.get('permission') == 'session_visible'
            and contact.get('source_url') == enrichment.get('source_url')
            and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_\-]{3,128}', value)):
        item['wechat'] = value


def save_enrichment(task_id, creator_id, data):
    _task(task_id)
    with _connection() as conn:
        if not conn.execute('SELECT 1 FROM recommendations WHERE task_id=? AND creator_id=?',
                            (task_id, creator_id)).fetchone():
            raise ValueError('Creator is not attached to this task')
        payload = _json_safe(dict(data))
        payload.setdefault('status', 'unavailable')
        payload.setdefault('identity_verified', False)
        payload.setdefault('median_play_30d', None)
        payload.setdefault('audience', {'gender': None, 'age': None, 'region': None})
        payload.setdefault('missing_fields', [])
        payload.setdefault('source_url', '')
        payload.setdefault('captured_at', None)
        profile = payload.get('profile')
        if isinstance(profile, dict) and _identity_confirmed(payload):
            row = conn.execute('SELECT * FROM creators WHERE id=?', (creator_id,)).fetchone()
            native = row['source_platform'] == 'xingtu'
            bound = (native and row['star_id'] and payload.get('star_id') == row['star_id']
                     and profile.get('star_id') == row['star_id']
                     and (not row['uid'] or profile.get('uid') == row['uid'])
                     and _source_url(payload.get('source_url'))
                     and urlsplit(payload['source_url']).hostname == 'www.xingtu.cn')
            if bound:
                contact = payload.get('contact') or {}
                value = contact.get('value')
                if (contact.get('status') == '已获取' and contact.get('type') == '微信'
                        and contact.get('permission') == 'session_visible'
                        and contact.get('source_url') == payload['source_url']
                        and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_\-]{3,128}', value)
                        and not conn.execute('SELECT 1 FROM contacts WHERE creator_id=? AND contact_value=?',
                                             (creator_id, value)).fetchone()):
                    conn.execute('''INSERT INTO contacts
                        (creator_id,contact_type,contact_value,source,verified,created_at)
                        VALUES (?,?,?,?,?,?)''', (creator_id, '微信', value, '星图后台', 1, _stamp()))
            elif native:
                payload.update(status='identity_unconfirmed', identity_verified=False,
                               reason='identity_persistence_mismatch')
                payload['missing_fields'] = list(dict.fromkeys([*payload['missing_fields'], 'identity']))
        conn.execute('''INSERT INTO creator_enrichments (task_id,creator_id,data_json,saved_at) VALUES (?,?,?,?)
                        ON CONFLICT(task_id,creator_id) DO UPDATE SET data_json=excluded.data_json,
                        saved_at=excluded.saved_at''', (task_id, creator_id, _dump(payload), _stamp()))
    return payload


def get_enrichment(task_id, creator_id):
    with _connection() as conn:
        return _enrichment(conn, task_id, creator_id)


def _distribution(value):
    return isinstance(value, dict) and bool(value) and all(
        isinstance(label, str) and bool(label.strip()) and _number(share) and share >= 0
        for label, share in value.items()
    )


def _source_url(value):
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (parsed.port is None or parsed.port > 0))
    except ValueError:
        return False


def _enrichment_problems(data):
    if not data:
        return ['enrichment']
    missing = []
    if data.get('status') != 'completed':
        missing.append('enrichment.status:' + str(data.get('status')))
    if not _identity_confirmed(data):
        missing.append('identity_verified')
    if not _number(data.get('median_play_30d')) or data['median_play_30d'] < 0:
        missing.append('median_play_30d')
    audience = data.get('audience')
    for key in ('gender', 'age', 'region'):
        if not isinstance(audience, dict) or not _distribution(audience.get(key)):
            missing.append('audience.' + key)
    core = {'identity', 'identity_verified', 'median_play_30d', 'audience',
            'audience.gender', 'audience.age', 'audience.region', 'source_url', 'captured_at',
            '星图', '星图数据', '星图指标与粉丝画像'}
    missing.extend('enrichment.missing:' + field for field in _missing_list(data.get('missing_fields'))
                   if field in core)
    if not _source_url(data.get('source_url')):
        missing.append('enrichment.source_url')
    if _parse_timestamp(data.get('captured_at')) is None or not _fresh(data.get('captured_at')):
        missing.append('enrichment.expired')
    return missing


def _numeric_rule(item, requirements):
    """Reevaluate task-specific numeric evidence with the shared tri-state rule."""
    from app import qualification

    metrics = dict(item)
    if item.get('latest_post_date'):
        metrics['latest_post_date'] = item.get('latest_post_date')
    numeric = qualification.evaluate_numeric_conditions(metrics, requirements, now=_now())
    missing = list(numeric['missing_fields'])
    if requirements.get('unstructured_brief'):
        missing.append('\u9700\u6c42\u5c1a\u672a\u7ed3\u6784\u5316\u786e\u8ba4')
    passed = True if numeric['status'] == 'passed' else (False if numeric['status'] == 'failed' else None)
    return passed, list(numeric['failed_fields']), list(dict.fromkeys(missing))


def _missing_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip() and v]
    return [v.strip() for v in re.split('[,;、，；\\n]', str(value)) if v.strip()] if value else []


def admit_report(task_id, creator_ids, allow_missing=False):
    task = _task(task_id)
    candidates = {row['creator_id']: row for row in get_candidates(task_id)}
    outcome = {'admitted': [], 'pending': [], 'details': {}}
    information = _information(task['requirements'])
    with _connection() as conn:
        for cid in dict.fromkeys(creator_ids):
            if conn.execute('SELECT 1 FROM formal_reports WHERE task_id=? AND creator_id=?', (task_id, cid)).fetchone():
                outcome['admitted'].append(cid)
                continue
            item = candidates.get(cid)
            if item is None:
                outcome['pending'].append(cid)
                outcome['details'][cid] = ['not_attached']
                continue
            assessment = item.get('assessment') or {}
            candidate_status = item.get('candidate_status') or 'pending'
            numeric, failures, missing = _numeric_rule(item, task['requirements'])
            enrichment_problems = _enrichment_problems(item['enrichment'])
            problems = enrichment_problems + missing + failures
            known_failure = candidate_status == 'rejected' or numeric is False
            if not information:
                # The structured assessment is the single admission gate.  A
                # legacy hard_pass flag or a high recommendation label cannot
                # replace it, and a known numeric failure is never admitted as
                # a "missing data" exception.
                if candidate_status != 'eligible':
                    problems.append('overall_status:' + candidate_status)
                if numeric is not True:
                    problems.append('hard_conditions')
                topic = assessment.get('topic') or {}
                if assessment and topic.get('error_code'):
                    problems.append('topic:' + str(topic['error_code']))
            if item['excluded']:
                problems.append('excluded')
            problems = list(dict.fromkeys(problems))
            if not information and problems and (not allow_missing or known_failure):
                outcome['pending'].append(cid)
                outcome['details'][cid] = problems
                continue
            snapshot = dict(item)
            enrichment = item['enrichment'] or {}
            original_missing = _missing_list(item.get('missing_fields'))
            if not enrichment_problems:
                original_missing = [field for field in original_missing
                                    if field not in ('星图', '星图数据', '星图指标与粉丝画像')]
            topic = assessment.get('topic') or {}
            numeric_data = assessment.get('numeric') or {}
            assessment_payload = assessment.get('assessment') or assessment
            assessment_missing = list(numeric_data.get('missing_fields') or [])
            if topic.get('error_code'):
                assessment_missing.append('direction:' + str(topic['error_code']))
            snapshot.update(
                task_id=task_id, requirements=task['requirements'], task_name=task['name'],
                qualified=bool(not information and candidate_status == 'eligible'
                               and numeric is True and not problems),
                allow_missing=bool(allow_missing), manual_include_missing=bool(allow_missing and problems),
                candidate_status=candidate_status,
                assessment=assessment,
                assessment_method=(topic.get('method') or '') if assessment else '',
                assessment_prompt_version=(assessment_payload.get('prompt_version') or ''),
                assessment_model=(topic.get('model') or ''),
                assessment_at=(assessment_payload.get('assessed_at') or ''),
                discovery_sources=item.get('discovery_sources') or [],
                numeric_assessment=numeric_data,
                topic_assessment=topic,
                missing_fields=list(dict.fromkeys(original_missing +
                                                 _missing_list(enrichment.get('missing_fields')) +
                                                 problems + assessment_missing)),
                manual_level='', manual_reason='', data_list=information,
                report_kind='information' if information else 'recommendation',
                admitted_at=_stamp(),
            )
            for field in ('star_id', 'median_play_30d', 'price_1_20', 'price_21_60', 'expected_cpm'):
                snapshot[field] = enrichment.get(field) if _identity_confirmed(enrichment) else None
            snapshot['audience'] = enrichment.get('audience')
            snapshot['enrichment_captured_at'] = enrichment.get('captured_at')
            snapshot['enrichment_source_url'] = enrichment.get('source_url')
            if information:
                snapshot['recommend_level'] = '\u8d44\u6599\u6e05\u5355'
                snapshot['reason'] = '\u65e0\u9700\u6c42\uff0c\u4e0d\u63d0\u4f9b\u5339\u914d\u8bc4\u7ea7'
            if problems:
                snapshot['risk'] = '; '.join(filter(None, [item.get('risk'), 'Unverified: ' + ', '.join(problems)]))
            conn.execute('INSERT OR IGNORE INTO formal_reports (task_id,creator_id,snapshot_json,created_at) VALUES (?,?,?,?)',
                         (task_id, cid, _dump(_json_safe(snapshot)), snapshot['admitted_at']))
            outcome['admitted'].append(cid)
    return outcome


def get_report(task_id):
    with _connection() as conn:
        rows = conn.execute('SELECT snapshot_json FROM formal_reports WHERE task_id=? ORDER BY id', (task_id,)).fetchall()
    return [json.loads(row['snapshot_json']) for row in rows]


def update_report(task_id, items):
    _task(task_id)
    editable = {'manual_level', 'manual_reason', 'bd_status', 'bd_notes'}
    with _connection() as conn:
        for item in items:
            cid = item.get('creator_id')
            row = conn.execute('SELECT snapshot_json FROM formal_reports WHERE task_id=? AND creator_id=?',
                               (task_id, cid)).fetchone()
            if not row:
                continue
            saved = json.loads(row['snapshot_json'])
            saved.update({key: item[key] for key in editable if key in item})
            conn.execute('UPDATE formal_reports SET snapshot_json=? WHERE task_id=? AND creator_id=?',
                         (_dump(saved), task_id, cid))
    return get_report(task_id)


def remove_report_entry(task_id, creator_id):
    with _connection() as conn:
        conn.execute('DELETE FROM formal_reports WHERE task_id=? AND creator_id=?', (task_id, creator_id))


def update_tags(creator_id, tags):
    normalized = []
    seen = set()
    for tag in tags:
        value = dict(tag) if isinstance(tag, dict) else {'name': str(tag), 'source': 'manual'}
        name = str(value.get('name', '')).strip()
        if not name or name in seen:
            continue
        value['name'] = name
        value.setdefault('source', 'manual')
        normalized.append(value)
        seen.add(name)
    with _connection() as conn:
        if not conn.execute('SELECT 1 FROM creators WHERE id=?', (creator_id,)).fetchone():
            raise ValueError('Creator not found')
        conn.execute('''INSERT INTO creator_tags VALUES (?,?,?) ON CONFLICT(creator_id) DO UPDATE SET
                        tags_json=excluded.tags_json, updated_at=excluded.updated_at''',
                     (creator_id, _dump(normalized), _stamp()))
    return normalized


def get_kb(search='', tag='', enrichment_status=''):
    rows = db.get_knowledge_base(search)
    result = []
    with _connection() as conn:
        for item in rows:
            cid = item['creator_id']
            if not conn.execute('SELECT 1 FROM creator_tags WHERE creator_id=?',(cid,)).fetchone():
                has_sample = item.get('latest_avg_likes') is not None
                suggestion = {'name': '有数值采集记录' if has_sample else '资料待补全',
                              'source': 'automatic', 'basis': '依据本地采集记录；不代表内容适配或永久资格',
                              'created_at': _stamp()}
                conn.execute('INSERT INTO creator_tags VALUES (?,?,?)',(cid,_dump([suggestion]),_stamp()))
            item['tags'] = _tags(conn, cid)
            row = conn.execute('SELECT data_json FROM creator_enrichments WHERE creator_id=? ORDER BY saved_at DESC,id DESC LIMIT 1',
                               (cid,)).fetchone()
            item['enrichment'] = json.loads(row['data_json']) if row else None
            _apply_detail_profile(item, item['enrichment'])
            item['enrichment_status'] = item['enrichment']['status'] if row else 'unavailable'
            item['enrichment_fresh'] = _fresh(item['enrichment'].get('captured_at')) if row else False
            item['tasks'] = [dict(r) for r in conn.execute('''
                SELECT r.task_id,r.recommend_level,r.hard_pass,r.reason,r.risk,r.missing_fields,
                       COALESCE(cs.selected,0) AS selected,COALESCE(cs.excluded,0) AS excluded
                FROM recommendations r LEFT JOIN candidate_selection cs
                    ON cs.task_id=r.task_id AND cs.creator_id=r.creator_id
                WHERE r.creator_id=? AND r.id=(SELECT MAX(r2.id) FROM recommendations r2
                    WHERE r2.task_id=r.task_id AND r2.creator_id=r.creator_id)
                ORDER BY r.id DESC''', (cid,))]
            if tag and not any(value['name'] == tag for value in item['tags']):
                continue
            if enrichment_status and enrichment_status != item['enrichment_status']:
                continue
            result.append(item)
    return result


def attach_kb(task_id, creator_ids):
    task = _task(task_id)
    from app import qualification

    requirements = task['requirements']
    policy = qualification.policy_from_requirements(requirements)
    for cid in dict.fromkeys(creator_ids):
        with _connection() as conn:
            creator = conn.execute('SELECT * FROM creators WHERE id=?', (cid,)).fetchone()
            if creator is None:
                raise ValueError('Creator not found')
            exists = conn.execute('SELECT 1 FROM recommendations WHERE task_id=? AND creator_id=?', (task_id, cid)).fetchone()
            if exists:
                continue
            snapshot = conn.execute('SELECT * FROM snapshots WHERE creator_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
            item = dict(creator)
            posts = []
            observation_id = None
            if snapshot:
                item.update({key: snapshot[key] for key in ('sample_count', 'avg_likes', 'max_likes',
                             'latest_post_date', 'days_since_latest', 'captured_at')})
                copied = conn.execute('''INSERT INTO snapshots (creator_id,task_id,sample_count,avg_likes,max_likes,
                                latest_post_date,days_since_latest,captured_at) VALUES (?,?,?,?,?,?,?,?)''',
                             (cid, task_id, snapshot['sample_count'], snapshot['avg_likes'], snapshot['max_likes'],
                              snapshot['latest_post_date'], snapshot['days_since_latest'], snapshot['captured_at']))
                observation_id = copied.lastrowid
                posts, legacy = db._observation_posts(conn, cid, snapshot['task_id'], snapshot['id'])
                conn.executemany('''INSERT INTO sample_posts
                                    (creator_id,task_id,aweme_id,desc,likes,comments,post_date,observation_id)
                                    VALUES (?,?,?,?,?,?,?,?)''',
                                 [(cid, task_id, post['aweme_id'], post['desc'], post['likes'],
                                   post['comments'], post['post_date'], None if legacy else copied.lastrowid)
                                  for post in posts])
            numeric = qualification.evaluate_numeric_conditions(item, requirements, now=_now())
            topic = qualification.unknown_topic_assessment(
                policy, 'not_reviewed',
                '知识库导入未自动调用模型；需在候选中显式补审内容方向。')
            assessment = qualification.build_assessment(
                item, observation_id, posts, requirements, numeric, topic, policy, [])
            assessment['post_fetch'] = {'status': 'stored', 'count': len(posts), 'source': 'knowledge_base'}
            assessment_id = conn.execute('''INSERT INTO candidate_assessments
                (task_id, creator_id, observation_id, requirements_hash, policy_version, input_hash,
                 numeric_status, topic_status, overall_status, assessment_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (task_id, cid, observation_id, assessment['requirements_hash'],
                 assessment['policy_version'], assessment['input_hash'],
                 numeric['status'], topic['topic_status'], assessment['overall_status'],
                 _dump(_json_safe(assessment)), _stamp())).lastrowid
            level = '淘汰' if numeric['status'] == 'failed' else '待核实'
            reason = ('知识库导入：已知数值硬条件不达标。' if numeric['status'] == 'failed'
                      else '知识库导入：数值待复核或方向未审核；需显式补审后才能成为可选候选。')
            conn.execute('''INSERT INTO recommendations (task_id,creator_id,recommend_level,hard_pass,
                            reason,risk,missing_fields,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                         (task_id, cid, level, None if numeric['status'] != 'failed' else 0,
                          reason, '知识库历史数据，非当前审核结论',
                          ', '.join(numeric['missing_fields']), _stamp()))
    return get_candidates(task_id)
