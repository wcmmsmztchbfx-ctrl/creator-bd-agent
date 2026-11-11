import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from app import exporter


class ExportTests(unittest.TestCase):
    def export_cells(self, rows):
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            output = exporter.export_task_to_excel('测试', '食品', rows)
            with zipfile.ZipFile(output) as workbook:
                strings = ET.fromstring(workbook.read('xl/sharedStrings.xml'))
                shared = [''.join(node.itertext()) for node in strings.findall('s:si', ns)]
                sheet = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))
        cells = {}
        for cell in sheet.findall('.//s:c', ns):
            value = cell.findtext('s:v', namespaces=ns)
            cells[cell.attrib['r']] = shared[int(value)] if cell.get('t') == 's' else value
        return sheet, cells, ns

    def test_valid_zero_commercial_metrics_are_numeric_not_blank(self):
        sheet, cells, ns = self.export_cells([{'price_1_20': 0, 'price_21_60': 0,
                                             'median_play_30d': 0, 'expected_cpm': 0}])
        for ref in ('J3', 'K3', 'L3', 'M3'):
            self.assertEqual(cells.get(ref), '0', ref)
            self.assertNotEqual(sheet.find(f'.//s:c[@r="{ref}"]', ns).get('t'), 's')

    def test_absent_commercial_metrics_stay_blank_and_existing_units_stay_unchanged(self):
        _, cells, _ = self.export_cells([{}, {'price_1_20': 1200, 'price_21_60': 2400,
                                             'median_play_30d': 12000, 'expected_cpm': 35.5}])
        for ref in ('J3', 'K3', 'L3', 'M3'):
            self.assertIsNone(cells.get(ref), ref)
        for ref, value in {'J4': '1200', 'K4': '2400', 'L4': '12000', 'M4': '35.5'}.items():
            self.assertEqual(cells[ref], value)

    def test_readable_columns_append_without_moving_existing_columns(self):
        sheet, cells, ns = self.export_cells([{}])
        headers = [cells[c.attrib['r']] for c in sheet.findall('.//s:row[@r="2"]/s:c', ns)]
        self.assertEqual(headers[:40], [
            '规则参考等级', '核心推荐理由', '投放风险提示', '达人昵称', '抖音号 (唯一)', '粉丝量',
            '核心垂类标签', '抖音主页链接', '星图达人 ID', '1-20s 报价 (￥)', '21-60s 报价 (￥)',
            '30天播放中位 (VV)', '预期 CPM (￥)', '样本均赞', '样本最高赞', '最新发布日期',
            '活跃状态', '商务微信号', '商务电话', 'BD 跟进状态', '跟进备注说明', '人工最终评级',
            '人工最终理由', '加入方式', '计入符合要求人数', '缺失项', '报告快照时间', '星图采集时间',
            '星图来源', '粉丝画像', '星图核验状态', '审核方法', '方向审核结论', '可判读样本数',
            '方向匹配数', '创作特征', '审核提示词版本', '审核模型', '审核证据作品 ID', '审核时间'])
        self.assertEqual(headers[40:], ['粉丝性别比例', '粉丝年龄比例', '粉丝地域比例',
                                        '合作联系方式状态', '合作联系方式', '合作联系方式来源',
                                        '合作联系方式权限'])
        self.assertEqual(sheet.find('s:autoFilter', ns).get('ref'), 'A2:AU3')

    def test_normalized_audience_ratios_are_readable_percentages_including_zero(self):
        import copy
        row = {'enrichment': {'audience': {
            'gender': {'female': 0.625, 'male': 0.375, 'unknown': 0},
            'age': {'18-24': 0.5, '25-34': 0.125, '其他': 0.0037},
            'region': {'上海': 1, '浙江': 0}}}}
        original = copy.deepcopy(row)
        _, cells, _ = self.export_cells([row])
        self.assertEqual(cells.get('AO3'), '女：62.5%；男：37.5%；未知：0%')
        self.assertEqual(cells.get('AP3'), '18-24：50%；25-34：12.5%；其他：0.37%')
        self.assertEqual(cells.get('AQ3'), '上海：100%；浙江：0%')
        self.assertIn('0.625', cells['AD3'])
        self.assertEqual(row, original)

    def test_missing_or_unverified_audience_is_not_guessed_as_percentages(self):
        rows = [{}, {'enrichment': {'audience': {'gender': None, 'age': {}, 'region': []}}},
                {'enrichment': {'audience': {'gender': {'female': 60, 'male': '40%'},
                    'age': {'18-24': True, '25-34': -0.5},
                    'region': {'上海': float('nan'), '浙江': float('inf')}}}}]
        _, cells, _ = self.export_cells(rows)
        for row in (3, 4):
            for col in ('AO', 'AP', 'AQ'):
                self.assertEqual(cells.get(f'{col}{row}'), '未获取')
        self.assertEqual(cells.get('AO5'), '女：未获取；男：未获取')
        self.assertEqual(cells.get('AP5'), '18-24：未获取；25-34：未获取')
        self.assertEqual(cells.get('AQ5'), '上海：未获取；浙江：未获取')

    def test_contact_status_value_source_and_permission_are_exported(self):
        rows = [{'enrichment': {'contact': {'status': 'available', 'value': '微信：synthetic-bd',
                    'source_url': 'https://www.xingtu.cn/ad/creator/123', 'permission': 'session_visible'}}},
                {'enrichment': {'contact': {'status': 'permission_denied', 'value': None,
                    'source_url': None, 'permission': 'permission_denied'}}}, {}]
        _, cells, _ = self.export_cells(rows)
        for ref, expected in {'AR3': '已获取', 'AS3': '微信：synthetic-bd',
                              'AT3': 'https://www.xingtu.cn/ad/creator/123', 'AU3': '当前会话可见',
                              'AR4': '权限受限', 'AS4': '未获取', 'AT4': '未获取', 'AU4': '权限受限',
                              'AR5': '未获取', 'AS5': '未获取', 'AT5': '未获取', 'AU5': '未获取'}.items():
            self.assertEqual(cells.get(ref), expected, ref)

    def test_appended_external_text_stays_inert_in_excel(self):
        row = {'enrichment': {'audience': {
                    'gender': {'=HYPERLINK("bad")': 0.5},
                    'age': {'+SUM(1,2)': 0.25}, 'region': {'-DDE': 0}},
                'contact': {'status': '=1+1', 'value': '+SUM(1,2)',
                            'source_url': '@HYPERLINK("bad")', 'permission': '-DDE'}}}
        sheet, cells, ns = self.export_cells([row])
        for col in ('AO', 'AP', 'AQ', 'AR', 'AS', 'AT', 'AU'):
            self.assertTrue(cells.get(f'{col}3', '').startswith("'"), col)
        self.assertEqual(sheet.findall('.//s:f', ns), [])
        self.assertEqual(sheet.findall('.//s:hyperlink', ns), [])

    def test_nested_frozen_assessment_provenance_is_exported(self):
        payload = {'assessed_at': '2026-11-05T12:34:56+08:00', 'prompt_version': 'frozen-prompt',
                   'topic': {'method': 'text_llm', 'model': 'frozen-model', 'topic_status': 'matched'}}
        row = {'nickname': 'fixture', 'assessment': {'assessment': payload}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            output = exporter.export_task_to_excel('fixture', 'food', [row])
            with zipfile.ZipFile(output) as workbook:
                text = workbook.read('xl/sharedStrings.xml').decode()
        for value in ('2026-11-05T12:34:56+08:00', 'frozen-prompt', 'frozen-model'):
            self.assertIn(value, text)

    def test_snapshot_provenance_and_manual_decision_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter,'EXPORTS_DIR',tmp):
            row={'nickname':'合成','manual_level':'中推荐','manual_reason':'人工判断',
                 'allow_missing':True,'qualified':False,'missing_fields':['粉丝画像'],
                 'snapshot_at':'2026-11-05T10:00:00Z','enrichment':{'source_url':'https://www.xingtu.cn/','audience':{'age':{'18-24':0.4}}}}
            first=exporter.export_task_to_excel('测试','食品',[row])
            second=exporter.export_task_to_excel('测试','食品',[row])
            self.assertNotEqual(first,second)
            with zipfile.ZipFile(first) as z:
                text=z.read('xl/sharedStrings.xml').decode()
                for value in ['人工最终评级','人工判断','带缺失加入','粉丝画像','2026-11-05T10:00:00Z']:
                    self.assertIn(value,text)

    def test_export_does_not_claim_ai_decision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            output = exporter.export_task_to_excel('测试', '食品', [{'nickname': '合成测试', 'recommend_level': '待核实'}])
            with zipfile.ZipFile(output) as z:
                text = z.read('xl/sharedStrings.xml').decode()
                self.assertNotIn('AI 决策', text)
                self.assertIn('规则', text)

    def test_missing_numbers_are_not_exported_as_zero(self):
        from xml.etree import ElementTree as ET
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            output = exporter.export_task_to_excel('测试', '食品', [{'nickname': '=HYPERLINK("bad")', 'followers': None, 'avg_likes': None}])
            with zipfile.ZipFile(output) as z:
                xml = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                cells = {c.attrib['r']: c for c in xml.findall('.//s:c', ns)}
                for ref in ('F3', 'N3', 'O3'):
                    self.assertIsNone(cells[ref].find('s:v', ns))
                self.assertEqual(xml.findall('.//s:f', ns), [])


    def test_audit_method_topic_evidence_and_time_are_appended(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(exporter, 'EXPORTS_DIR', tmp):
            row = {'nickname': '合成', 'recommend_level': '可选',
                   'assessment_method': 'text_llm', 'assessment_prompt_version': 'v1',
                   'assessment_model': 'review-model', 'assessment_at': '2026-11-05T20:00:00+08:00',
                   'topic_assessment': {'topic_status': 'matched', 'sample_count': 6,
                                        'matched_count': 5, 'creator_pattern': 'consistent_creator',
                                        'evidence_ids': ['p1', 'p2']}}
            output = exporter.export_task_to_excel('测试', '食品', [row])
            with zipfile.ZipFile(output) as z:
                text = z.read('xl/sharedStrings.xml').decode()
                for value in ['文本辅助审核（未观看视频）', '匹配', '持续创作', 'p1、p2',
                              'review-model', '2026-11-05T20:00:00+08:00']:
                    self.assertIn(value, text)
                self.assertIn('审核方法', text)


if __name__ == '__main__':
    unittest.main()
