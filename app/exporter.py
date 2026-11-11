import os
import datetime
import json
import math
import xlsxwriter
from typing import List, Dict, Any

from app.runtime_paths import WORKSPACE

EXPORTS_DIR = str(WORKSPACE / "exports")

def sanitize_cell(val: Any) -> Any:
    """防范 Excel 公式注入：防止外部字符串以 =, +, -, @ 开头被 Excel 执行。"""
    if isinstance(val, str):
        if val and val[0] in ("=", "+", "-", "@"):
            return "'" + val
    return val


def _audience_summary(distribution, gender=False):
    if not isinstance(distribution, dict) or not distribution:
        return '未获取'
    labels = {'female': '女', 'male': '男', 'unknown': '未知'} if gender else {}
    parts = []
    for label, ratio in distribution.items():
        if not isinstance(label, str) or not label.strip():
            continue
        # Only the normalized ratio contract is supported; never guess raw units.
        if (isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
                and 0 <= ratio <= 1 and math.isfinite(ratio)):
            share = f'{ratio * 100:.2f}'.rstrip('0').rstrip('.') + '%'
        else:
            share = '未获取'
        parts.append(f'{labels.get(label, label)}：{share}')
    return '；'.join(parts) or '未获取'


def _contact_values(contact):
    contact = contact if isinstance(contact, dict) else {}
    status_labels = {'available': '已获取', 'fetched': '已获取', 'completed': '已获取',
                     'not_fetched': '未获取', 'unavailable': '未获取',
                     'permission_denied': '权限受限', 'not_found': '未找到', 'failed': '获取失败'}
    permission_labels = {'session_visible': '当前会话可见', 'not_fetched': '未获取',
                         'permission_denied': '权限受限', 'denied': '权限受限', 'allowed': '允许读取'}
    values = []
    for field, labels in (('status', status_labels), ('value', {}),
                          ('source_url', {}), ('permission', permission_labels)):
        value = contact.get(field)
        if value is None or value == '':
            text = '未获取'
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        values.append(labels.get(text, text))
    return values


def export_task_to_excel(task_name: str, keyword: str, creators: List[Dict[str, Any]]) -> str:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = "".join(c for c in task_name if c.isalnum() or c in ("-", "_")).strip() or "export"
    filename = f"{safe_name}_{timestamp}.xlsx"
    file_path = os.path.join(EXPORTS_DIR, filename)

    workbook = xlsxwriter.Workbook(file_path, {'strings_to_formulas': False, 'strings_to_urls': False})
    worksheet = workbook.add_worksheet("达人BD交付清单")

    # Group Header Formats
    grp_rec_fmt = workbook.add_format({
        "bold": True, "bg_color": "#0F766E", "font_color": "#FFFFFF",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 11
    })
    grp_profile_fmt = workbook.add_format({
        "bold": True, "bg_color": "#1E40AF", "font_color": "#FFFFFF",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 11
    })
    grp_commerce_fmt = workbook.add_format({
        "bold": True, "bg_color": "#6B21A8", "font_color": "#FFFFFF",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 11
    })
    grp_content_fmt = workbook.add_format({
        "bold": True, "bg_color": "#B45309", "font_color": "#FFFFFF",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 11
    })
    grp_bd_fmt = workbook.add_format({
        "bold": True, "bg_color": "#BE185D", "font_color": "#FFFFFF",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 11
    })

    # Sub-header format
    sub_header_fmt = workbook.add_format({
        "bold": True, "bg_color": "#F1F5F9", "font_color": "#1E293B",
        "align": "center", "valign": "vcenter", "border": 1, "font_size": 10
    })

    # Data cell formats
    text_fmt = workbook.add_format({"valign": "vcenter", "border": 1, "font_size": 10})
    detail_fmt = workbook.add_format({"valign": "vcenter", "border": 1, "font_size": 10, "text_wrap": True})
    center_fmt = workbook.add_format({"align": "center", "valign": "vcenter", "border": 1, "font_size": 10})
    num_fmt = workbook.add_format({"align": "right", "valign": "vcenter", "border": 1, "num_format": "#,##0", "font_size": 10})
    currency_fmt = workbook.add_format({"align": "right", "valign": "vcenter", "border": 1, "num_format": "¥#,##0", "font_size": 10})
    cpm_fmt = workbook.add_format({"align": "right", "valign": "vcenter", "border": 1, "num_format": "¥0.0", "font_size": 10})
    
    # Recommendation level styles
    high_rec_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#DCFCE7", "font_color": "#15803D", "font_size": 10
    })
    mid_rec_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#FEF9C3", "font_color": "#A16207", "font_size": 10
    })
    rej_rec_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#FEE2E2", "font_color": "#B91C1C", "font_size": 10
    })
    chk_rec_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#E2E8F0", "font_color": "#475569", "font_size": 10
    })

    # BD Status format
    bd_done_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#EDE9FE", "font_color": "#6D28D9", "font_size": 10
    })
    bd_contacted_fmt = workbook.add_format({
        "align": "center", "valign": "vcenter", "border": 1, "bold": True,
        "bg_color": "#E0F2FE", "font_color": "#0369A1", "font_size": 10
    })

    # Row 0: Dual-tier Category Header Merges
    worksheet.merge_range(0, 0, 0, 2, "推荐参考（数值规则；内容匹配待核实）", grp_rec_fmt)
    worksheet.merge_range(0, 3, 0, 7, "达人档案（入选时快照）", grp_profile_fmt)
    worksheet.merge_range(0, 8, 0, 12, "星图信息（缺失及权限以核验状态为准）", grp_commerce_fmt)
    worksheet.merge_range(0, 13, 0, 16, "近期作品与活跃表现", grp_content_fmt)
    worksheet.merge_range(0, 17, 0, 20, "商务联系与跟进", grp_bd_fmt)
    worksheet.merge_range(0, 21, 0, 39, "人工判断、核验状态、审核依据与来源", grp_rec_fmt)
    worksheet.merge_range(0, 40, 0, 42, "粉丝画像（比例）", grp_commerce_fmt)
    worksheet.merge_range(0, 43, 0, 46, "合作联系方式与权限", grp_bd_fmt)

    # Row 1: Sub-headers definition (title, width)
    sub_headers = [
        # 🎯 推荐研判
        ("规则参考等级", 14),
        ("核心推荐理由", 32),
        ("投放风险提示", 26),
        # 👤 达人档案
        ("达人昵称", 16),
        ("抖音号 (唯一)", 16),
        ("粉丝量", 12),
        ("核心垂类标签", 18),
        ("抖音主页链接", 32),
        # 💰 星图商业与报价
        ("星图达人 ID", 18),
        ("1-20s 报价 (￥)", 15),
        ("21-60s 报价 (￥)", 15),
        ("30天播放中位 (VV)", 16),
        ("预期 CPM (￥)", 14),
        # 📊 内容与活跃表现
        ("样本均赞", 12),
        ("样本最高赞", 12),
        ("最新发布日期", 14),
        ("活跃状态", 14),
        # 📱 商务建联与跟进
        ("商务微信号", 18),
        ("商务电话", 15),
        ("BD 跟进状态", 14),
        ("跟进备注说明", 28),
        ("人工最终评级", 15), ("人工最终理由", 30), ("加入方式", 18),
        ("计入符合要求人数", 18), ("缺失项", 32), ("报告快照时间", 25),
        ("星图采集时间", 25), ("星图来源", 40), ("粉丝画像", 45), ("星图核验状态", 22),
        # Text-assisted qualification provenance, appended after existing columns.
        ("审核方法", 18), ("方向审核结论", 14), ("可判读样本数", 12),
        ("方向匹配数", 12), ("创作特征", 16), ("审核提示词版本", 16),
        ("审核模型", 18), ("审核证据作品 ID", 36), ("审核时间", 24),
        ("粉丝性别比例", 36), ("粉丝年龄比例", 48), ("粉丝地域比例", 56),
        ("合作联系方式状态", 20), ("合作联系方式", 36),
        ("合作联系方式来源", 48), ("合作联系方式权限", 22)
    ]

    for col_idx, (col_name, width) in enumerate(sub_headers):
        worksheet.write(1, col_idx, col_name, sub_header_fmt)
        worksheet.set_column(col_idx, col_idx, width)

    worksheet.set_row(0, 26)
    worksheet.set_row(1, 24)

    # Fill data starting at Row 2
    for row_idx, c in enumerate(creators, start=2):
        worksheet.set_row(row_idx, 22)
        level = c.get("recommend_level", "待核实")
        if level == "高推荐":
            level_fmt = high_rec_fmt
        elif level == "中推荐":
            level_fmt = mid_rec_fmt
        elif level == "淘汰":
            level_fmt = rej_rec_fmt
        else:
            level_fmt = chk_rec_fmt

        days = c.get("days_since_latest")
        if days is not None:
            active_label = "高活跃" if days <= 7 else ("正常更新" if days <= 30 else f"停更{days}天")
        else:
            active_label = "未知"

        bd_status = c.get("bd_status") or "未建联"
        cur_bd_fmt = center_fmt
        if bd_status in ("已定档", "脚本确认中"):
            cur_bd_fmt = bd_done_fmt
        elif bd_status in ("已加微", "样品寄送中"):
            cur_bd_fmt = bd_contacted_fmt

        # 🎯 推荐研判
        worksheet.write(row_idx, 0, sanitize_cell(level), level_fmt)
        worksheet.write(row_idx, 1, sanitize_cell(c.get("reason", "")), text_fmt)
        worksheet.write(row_idx, 2, sanitize_cell(c.get("risk", "")), text_fmt)

        # 👤 达人档案
        worksheet.write(row_idx, 3, sanitize_cell(c.get("nickname", "")), text_fmt)
        worksheet.write_string(row_idx, 4, sanitize_cell(str(c.get("unique_id") or "")), text_fmt)
        worksheet.write(row_idx, 5, int(c['followers']) if c.get('followers') is not None else None, num_fmt)
        worksheet.write(row_idx, 6, sanitize_cell(c.get("category_tag") or "待核实"), text_fmt)
        worksheet.write(row_idx, 7, sanitize_cell(c.get("profile_url", "")), text_fmt)

        # 💰 星图商业与报价
        worksheet.write_string(row_idx, 8, sanitize_cell(str(c.get("star_id") or "-")), text_fmt)
        worksheet.write(row_idx, 9, int(c['price_1_20']) if c.get('price_1_20') not in (None, '') else None, currency_fmt)
        worksheet.write(row_idx, 10, int(c['price_21_60']) if c.get('price_21_60') not in (None, '') else None, currency_fmt)
        worksheet.write(row_idx, 11, int(c['median_play_30d']) if c.get('median_play_30d') not in (None, '') else None, num_fmt)
        worksheet.write(row_idx, 12, float(c['expected_cpm']) if c.get('expected_cpm') not in (None, '') else None, cpm_fmt)

        # 📊 内容与活跃表现
        worksheet.write(row_idx, 13, int(c['avg_likes']) if c.get('avg_likes') is not None else None, num_fmt)
        worksheet.write(row_idx, 14, int(c['max_likes']) if c.get('max_likes') is not None else None, num_fmt)
        worksheet.write(row_idx, 15, sanitize_cell(str(c.get("latest_post_date", "-"))[:10]), center_fmt)
        worksheet.write(row_idx, 16, sanitize_cell(active_label), center_fmt)

        # 📱 商务建联与跟进
        worksheet.write_string(row_idx, 17, sanitize_cell(str(c.get("wechat") or "")), text_fmt)
        worksheet.write_string(row_idx, 18, sanitize_cell(str(c.get("phone") or "")), text_fmt)
        worksheet.write(row_idx, 19, sanitize_cell(bd_status), cur_bd_fmt)
        worksheet.write(row_idx, 20, sanitize_cell(c.get("bd_notes", "")), text_fmt)
        enrichment = c.get('enrichment') or {}
        missing = c.get('missing_fields') or []
        values = [c.get('manual_level') or '未评价', c.get('manual_reason') or '',
                  '带缺失加入' if c.get('allow_missing') else '核验通过',
                  '是' if c.get('qualified') else '否',
                  '、'.join(missing) if isinstance(missing,list) else str(missing),
                  c.get('snapshot_at') or c.get('admitted_at') or '', enrichment.get('captured_at') or '',
                  enrichment.get('source_url') or '',
                  json.dumps(enrichment.get('audience') or {},ensure_ascii=False),
                  enrichment.get('status') or '未补全']
        for col, value in enumerate(values, start=21):
            worksheet.write_string(row_idx, col, sanitize_cell(str(value)), text_fmt)
        assessment_record = c.get('assessment') or {}
        assessment = assessment_record.get('assessment') or assessment_record
        topic = c.get('topic_assessment') or assessment.get('topic') or {}
        method = c.get('assessment_method') or topic.get('method') or ''
        pattern_labels = {'consistent_creator': '持续创作',
                          'occasional_poster': '偶发/方向分散',
                          'uncertain': '不确定'}
        topic_labels = {'matched': '匹配', 'not_matched': '不符', 'unknown': '待核实'}
        audit_values = [
            '文本辅助审核（未观看视频）' if method == 'text_llm' else '未审核',
            topic_labels.get(topic.get('topic_status'), '待核实') if topic else '未审核',
            topic.get('sample_count') if topic else None,
            topic.get('matched_count') if topic else None,
            pattern_labels.get(topic.get('creator_pattern'), '不确定') if topic else '',
            c.get('assessment_prompt_version') or assessment.get('prompt_version') or topic.get('prompt_version') or '',
            c.get('assessment_model') or topic.get('model') or '',
            '、'.join(str(value) for value in (topic.get('evidence_ids') or [])),
            c.get('assessment_at') or assessment.get('assessed_at') or '',
        ]
        for col, value in enumerate(audit_values, start=31):
            if value is None:
                worksheet.write_blank(row_idx, col, None, center_fmt)
            else:
                worksheet.write_string(row_idx, col, sanitize_cell(str(value)), text_fmt)

        audience = enrichment.get('audience')
        audience = audience if isinstance(audience, dict) else {}
        detail_values = [_audience_summary(audience.get('gender'), gender=True),
                         _audience_summary(audience.get('age')),
                         _audience_summary(audience.get('region')),
                         *_contact_values(enrichment.get('contact'))]
        lines = 1
        for col, value in enumerate(detail_values, start=40):
            worksheet.write_string(row_idx, col, sanitize_cell(value), detail_fmt)
            width = sub_headers[col][1]
            lines = max(lines, math.ceil(len(value) * 2 / width), value.count('\n') + 1)
        worksheet.set_row(row_idx, max(22, min(180, lines * 15)))

    worksheet.freeze_panes(2, 4)
    if creators:
        worksheet.autofilter(1, 0, len(creators) + 1, len(sub_headers) - 1)
    workbook.close()
    return file_path
