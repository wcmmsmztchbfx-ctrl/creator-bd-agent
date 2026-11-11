import asyncio
import importlib
import json
import time
import unittest

try:
    module = importlib.import_module('app.xingtu_discovery')
except ModuleNotFoundError:
    module = None


def payload():
    return {'base_resp': {'status_code': 0}, 'authors': [{'star_id': 'star-123', 'attribute_datas': {
        'id': 'star-123', 'core_user_id': '123456', 'nick_name': 'Synthetic creator',
        'follower': '25000', 'vv_median_30d': '345678',
        'last_10_items': json.dumps([{'item_id': 'post-1', 'item_title': 'Synthetic food review',
            'like_cnt': '1200', 'comment_cnt': '14', 'vv': '999999',
            'item_publish_time': str(int(time.time()) - 86400)}])},
        'items': [{'item_id': 'wrong-evidence', 'vv': 99999999}]}],
        'pagination': {'page': 1, 'limit': 20, 'has_more': False, 'total_count': 1}}


class DiscoveryParsingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'Xingtu discovery is not implemented')

    def test_current_live_shape_keeps_identity_and_uses_likes_not_plays(self):
        result = module.parse_discovery_payload(payload(), 1)
        row = result['authors'][0]
        self.assertEqual(row['star_id'], 'star-123')
        self.assertEqual(row['uid'], '123456')
        self.assertNotIn('sec_uid', row)
        self.assertEqual(row['followers'], 25000)
        self.assertEqual(row['median_play_30d'], 345678)
        self.assertEqual(row['posts'][0]['statistics']['digg_count'], 1200)
        self.assertEqual(row['posts'][0]['aweme_id'], 'post-1')
        self.assertEqual(row['source_platform'], 'xingtu')

    def test_missing_or_incomplete_recent_items_do_not_borrow_representative_plays(self):
        data = payload()
        del data['authors'][0]['attribute_datas']['last_10_items']
        row = module.parse_discovery_payload(data, 1)['authors'][0]
        self.assertEqual(row['posts'], [])
        self.assertEqual(row['median_play_30d'], 345678)

    def test_conflicting_identity_is_not_admitted(self):
        data = payload()
        data['authors'][0]['attribute_datas']['id'] = 'another-star'
        result = module.parse_discovery_payload(data, 1)
        self.assertEqual(result['authors'], [])
        self.assertEqual(result['skipped_count'], 1)

    def test_all_star_aliases_must_agree(self):
        data = payload()
        data['authors'][0]['author_id'] = 'conflicting-top-alias'
        data['authors'][0]['attribute_datas']['author_id'] = 'conflicting-attribute-alias'
        self.assertEqual(module.parse_discovery_payload(data, 1)['authors'], [])

    def test_unknown_numeric_values_are_not_zero(self):
        data = payload()
        data['authors'][0]['attribute_datas'].update(follower='--', vv_median_30d='NaN')
        row = module.parse_discovery_payload(data, 1)['authors'][0]
        self.assertIsNone(row['followers'])
        self.assertIsNone(row['median_play_30d'])

    def test_permission_error_is_not_empty_success(self):
        with self.assertRaises(module.XingtuDiscoveryError) as error:
            module.parse_discovery_payload({'base_resp': {'status_code': 10003,
                'status_message': '您的客户权益等级过低，暂时无法访问更多数据'}, 'authors': []}, 1)
        self.assertEqual(error.exception.code, 'permission_denied')

    def test_wrong_page_and_unknown_shape_fail_closed(self):
        for data in ({}, {'authors': []}, {'authors': 'invalid'}, {**payload(), 'base_resp': {'status_code': False}},
                     {**payload(), 'pagination': {'page': 2}}):
            with self.assertRaises(RuntimeError):
                module.parse_discovery_payload(data, 1)

    def test_missing_pagination_is_unknown_not_exhausted(self):
        data = payload()
        del data['pagination']
        result = module.parse_discovery_payload(data, 1)
        self.assertIsNone(result['has_more'])


if __name__ == '__main__':
    unittest.main()
