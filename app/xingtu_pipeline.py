"""Xingtu-first discovery, reusing existing qualification and evidence storage."""

from app import db, qualification
from app.xingtu_discovery import XingtuDiscovery, XingtuDiscoveryError


def creator_profile(row, existing=None):
    profile = {'nickname': row['nickname'], 'star_id': row['star_id'],
        'follower_count': row.get('followers'), 'median_play_30d': row.get('median_play_30d'),
        'profile_url': row.get('profile_url') or (existing or {}).get('profile_url'),
        'avatar_url': row.get('avatar_url')}
    if row.get('uid'):
        profile['uid'] = row['uid']
    # Missing Xingtu biography must not erase a known brand exclusion.
    for field in ('signature', 'unique_id'):
        if (existing or {}).get(field):
            profile[field] = existing[field]
    return profile


async def execute_xingtu_search_task(keyword, max_count=5, custom_task_name=None, *, task_id=None,
                                    progress_callback=None, **options):
    from app import crawler

    queries = crawler._normalize_queries(options.get('search_queries'), keyword)
    requirements = {'keyword': keyword, 'category': options.get('category', ''), 'sample_count': max_count,
        'discovery_mode': 'xingtu', 'search_queries': queries,
        'min_avg_likes': options.get('min_avg_likes', 0), 'min_followers': options.get('min_followers', 0),
        'max_followers': options.get('max_followers'), 'min_max_likes': options.get('min_max_likes', 0),
        'active_days': options.get('active_days'), 'content_preferences': options.get('content_preferences') or [],
        'exclusions': options.get('exclusions') or [], 'topic_min_samples': options.get('topic_min_samples', 5),
        'topic_min_ratio': options.get('topic_min_ratio', .6)}
    if task_id is None:
        task_id = db.save_task(custom_task_name or f'{keyword}星图达人检索', keyword, requirements=requirements)
    settings = db.get_all_settings()
    policy = qualification.policy_from_requirements(requirements, settings)
    budget = {'limit': policy['max_reviews_per_run'], 'used': 0, 'exhausted': False}
    progress = {'platform': 'xingtu', 'requested': max_count, 'candidates': 0, 'processed': 0,
        'saved': 0, 'failed': 0, 'missing_posts': 0, 'prequalified': 0, 'pages': 0,
        'eligible': 0, 'pending': 0, 'rejected': 0, 'topic_reviews': 0,
        'topic_budget': policy['max_reviews_per_run']}
    seen, partial_reasons = {}, set()

    def report(status, message):
        progress['topic_reviews'] = budget['used']
        db.update_task_status(task_id, status, message, dict(progress))
        if progress_callback:
            progress_callback(message, dict(progress))

    try:
        report('running', '正在使用项目已保存的星图会话检索达人。')
        async with XingtuDiscovery() as source:
            for query in queries:
                for page in range(1, 4):
                    if crawler.stop_requested(task_id) or budget['exhausted']:
                        break
                    progress.update(query=query, search_type='xingtu', offset=page - 1)
                    report('running', f'星图内容检索【{query}】第 {page} 页；最终交付目标 {max_count} 位。')
                    result = await source.search_page(query, page)
                    progress['pages'] += 1
                    if result.get('skipped_count'):
                        partial_reasons.add('部分星图结果缺少一致的账号标识，未错误合并')
                    for row in result['authors']:
                        star_id = row['star_id']
                        if star_id in seen:
                            db.save_discovery_source(task_id, seen[star_id], 'xingtu', query)
                            continue
                        try:
                            cid = db.upsert_xingtu_creator({key: value for key, value in row.items()
                                if key in ('star_id', 'uid', 'nickname', 'followers', 'median_play_30d',
                                           'profile_url', 'avatar_url') and value is not None})
                        except ValueError:
                            partial_reasons.add('部分星图账号标识冲突，保留原档案且未错误合并')
                            continue
                        seen[star_id] = cid
                        db.save_discovery_source(task_id, cid, 'xingtu', query)
                        db.save_recommendation(cid, task_id, {'level': '待核实', 'hard_pass': None,
                            'reason': '已在星图发现账号，数值与跨作品方向审核待完成。',
                            'risk': '星图收录不等于符合要求；完整画像及合作意愿尚待核实。',
                            'missing_fields': '方向审核、完整星图画像、合作联系方式'})
                        progress['candidates'] = len(seen)
                        if budget['exhausted'] or crawler.stop_requested(task_id):
                            continue  # Keep discoveries even when model budget is exhausted.
                        existing = crawler._candidate_creator(task_id, cid)
                        report('running', f'正在审核第 {progress["processed"] + 1} 位星图候选，已发现 {len(seen)} 位。')
                        try:
                            assessment = await crawler._collect_candidate(existing['sec_uid'], cid, task_id,
                                settings, keyword, progress, requirements, policy, budget,
                                _star_profile=creator_profile(row, existing), _star_posts=row.get('posts') or [])
                            state = assessment.get('overall_status', 'pending')
                            progress[state if state in ('eligible', 'rejected') else 'pending'] += 1
                        except Exception:
                            progress['failed'] += 1
                            progress['pending'] += 1
                            db.save_recommendation(cid, task_id, {'level': '待核实', 'hard_pass': None,
                                'reason': '星图资料处理未完成，已保留发现账号供后续核实。',
                                'risk': '不视为完整审核结果', 'missing_fields': '星图数值或方向审核'})
                        progress['processed'] += 1
                    if result.get('has_more') is not True:
                        if result.get('has_more') is None:
                            partial_reasons.add('星图分页信息未确认，不能认定搜索已穷尽')
                        break
                    if page == 3:
                        partial_reasons.add('已达到本轮每个关键词三页的扫描上限，仍有更多星图结果')
                if crawler.stop_requested(task_id) or budget['exhausted']:
                    break
        if budget['exhausted']:
            partial_reasons.add('内容审核预算已用完，取得的候选已保留')
        if progress['failed'] or progress['missing_posts']:
            partial_reasons.add('部分账号资料或近期作品数据仍需核实')
        if crawler.stop_requested(task_id):
            report('stopped', f'星图检索已手动停止，已保留 {len(seen)} 位候选。')
        else:
            report('partial' if partial_reasons else 'completed',
                f'星图检索结束：发现 {len(seen)} 位，可选 {progress["eligible"]} 位，'
                f'待核实 {progress["pending"]} 位，不符合 {progress["rejected"]} 位。'
                + ('；'.join(sorted(partial_reasons)) + '。' if partial_reasons else '')
                + '候选数不等于最终交付数，勾选确认后再补全完整画像与报告。')
    except XingtuDiscoveryError as exc:
        progress['access_code'] = exc.code
        progress['pause_reason'] = 'xingtu_access'
        status = 'blocked' if exc.code in ('login_required', 'verification_required') else ('partial' if seen else 'failed')
        report(status, str(exc) + f' 已保留 {len(seen)} 位星图候选；不会自动切换检索平台。')
    except Exception:
        report('partial' if seen else 'failed', f'星图检索异常停止，已保留 {len(seen)} 位账号；没有回退抖音搜索。')
    return task_id
