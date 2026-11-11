"""Synthetic, offline contracts for the verified Xingtu detail endpoints."""

import copy
import importlib
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:
    fields = importlib.import_module('app.xingtu_detail_fields')
except ModuleNotFoundError as exc:
    if exc.name != 'app.xingtu_detail_fields':
        raise
    fields = None


def base_payload():
    return {'base_resp': {'status_code': 0}, 'id': 'synthetic-star',
            'core_user_id': 'synthetic-uid', 'unique_id': 'synthetic-handle',
            'short_id': 'synthetic-short', 'sec_uid': 'synthetic-sec',
            'wechat': 'PRIVATE-SENTINEL', 'unknown': {'token': 'PRIVATE-SENTINEL'}}


def dimension(kind, pairs):
    return {'type': kind, 'distribution_list': [
        {'distribution_key': key, 'distribution_value': value} for key, value in pairs]}


def fans_payload():
    return {'base_resp': {'status_code': 0}, 'last_update_time': '1793923200',
            'distributions': [
                dimension(1, [('male', '212851'), ('female', '316361')]),
                dimension(2, [('18-23', '10'), ('24-30', '20'), ('31-40', '30'),
                              ('41-50', '30'), ('50+', '10')]),
                dimension(4, [('\u6d59\u6c5f', '3'), ('\u4e0a\u6d77', '1')]),
                dimension(256, [('\u676d\u5dde', '9000')])]}


def price_row(video_type=1, **changes):
    row = {'author_id': 'synthetic-star', 'video_type': video_type,
           'task_category': 1, 'enable': True, 'is_open': True, 'price': '1200'}
    row.update(changes)
    return row


class DetailFieldsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(fields, 'Xingtu detail pure parsers are not implemented')

    def test_base_returns_only_verified_identity_fields(self):
        data = base_payload()
        before = copy.deepcopy(data)
        result = fields.verify_base(data, {'star_id': 'synthetic-star', 'uid': 'synthetic-uid'},
                                    'synthetic-star')
        self.assertEqual(result, {'star_id': 'synthetic-star', 'uid': 'synthetic-uid',
                                 'unique_id': 'synthetic-handle', 'short_id': 'synthetic-short',
                                 'sec_uid': 'synthetic-sec'})
        self.assertEqual(data, before)

    def test_base_rejects_wrong_star_or_any_conflicting_known_identifier(self):
        for changes in ({'id': 'other-star'}, {'core_user_id': 'other-uid'},
                        {'unique_id': 'other-handle'}, {'short_id': 'other-short'},
                        {'sec_uid': 'other-sec'}):
            data = {**base_payload(), **changes}
            creator = {'star_id': 'synthetic-star', 'uid': 'synthetic-uid',
                       'unique_id': 'synthetic-handle', 'short_id': 'synthetic-short',
                       'sec_uid': 'synthetic-sec'}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fields.verify_base(data, creator, 'synthetic-star')
        with self.assertRaises(ValueError):
            fields.verify_base(base_payload(), {'star_id': 'other-star'}, 'synthetic-star')

    def test_base_does_not_conflate_star_uid_or_public_identifier_namespaces(self):
        with self.assertRaises(ValueError):
            fields.verify_base(base_payload(), {'uid': 'synthetic-star'}, 'synthetic-star')
        result = fields.verify_base(base_payload(), {'unique_id': 'synthetic-handle'}, 'synthetic-star')
        self.assertEqual(result['short_id'], 'synthetic-short')

    def test_base_requires_creator_identity_evidence(self):
        for creator in ({}, {'nickname': 'someone'}, None, [], {'uid': True}):
            with self.subTest(creator=creator), self.assertRaises(ValueError):
                fields.verify_base(base_payload(), creator, 'synthetic-star')

    def test_base_internal_storage_key_is_not_a_real_douyin_sec_uid(self):
        creator = {'source_platform': 'xingtu', 'star_id': 'synthetic-star',
                   'uid': 'synthetic-uid', 'sec_uid': 'xingtu:synthetic-star'}
        before = copy.deepcopy(creator)
        result = fields.verify_base(base_payload(), creator, 'synthetic-star')
        self.assertEqual(result['sec_uid'], 'synthetic-sec')
        self.assertEqual(creator, before)
        for changes in ({'sec_uid': 'xingtu:other-star'}, {'sec_uid': 'other-real-sec'},
                        {'source_platform': 'douyin'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fields.verify_base(base_payload(), {**creator, **changes}, 'synthetic-star')

    def test_base_never_returns_internal_storage_identifiers_as_real_ids(self):
        for name in ('core_user_id', 'unique_id', 'short_id', 'sec_uid'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fields.verify_base({**base_payload(), name: 'xingtu:synthetic-star'},
                                   {'star_id': 'synthetic-star'}, 'synthetic-star')

    def test_base_missing_expected_identifier_is_not_verified(self):
        for source, target in (('core_user_id', 'uid'), ('unique_id', 'unique_id'),
                               ('short_id', 'short_id'), ('sec_uid', 'sec_uid')):
            data = base_payload()
            expected = data.pop(source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                fields.verify_base(data, {target: expected}, 'synthetic-star')

    def test_base_never_synthesizes_missing_douyin_identifiers(self):
        result = fields.verify_base({'base_resp': {'status_code': 0}, 'id': 'synthetic-star'},
                                    {'star_id': 'synthetic-star'}, 'synthetic-star')
        self.assertEqual(result, {'star_id': 'synthetic-star'})

    def test_base_identifiers_are_real_nonempty_strings(self):
        for name in ('id', 'core_user_id', 'unique_id', 'short_id', 'sec_uid'):
            for invalid in (123, True, {}, [], ' ', '0', '-'):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                    fields.verify_base({**base_payload(), name: invalid},
                                       {'star_id': 'synthetic-star'}, 'synthetic-star')

    def test_all_parsers_require_explicit_integer_zero_status(self):
        for status in (None, {}, {'status_code': None}, {'status_code': False},
                       {'status_code': True}, {'status_code': '0'}, {'status_code': 0.0},
                       {'status_code': 1}, {'status_code': 403}):
            calls = ((fields.verify_base, ({**base_payload(), 'base_resp': status},
                                          {'star_id': 'synthetic-star'}, 'synthetic-star')),
                     (fields.parse_fans, ({**fans_payload(), 'base_resp': status},)),
                     (fields.parse_spread, ({'base_resp': status, 'play_mid': 123},)),
                     (fields.parse_prices, ({'base_resp': status, 'price_info': []}, 'synthetic-star')))
            for function, args in calls:
                with self.subTest(function=function.__name__, status=status), self.assertRaises(ValueError):
                    function(*args)

    def test_all_parsers_reject_nonobject_or_missing_status(self):
        for data in (None, [], 'invalid', {}, {'code': 0}, {'status_code': 0}):
            calls = ((fields.verify_base, (data, {'star_id': 'synthetic-star'}, 'synthetic-star')),
                     (fields.parse_fans, (data,)), (fields.parse_spread, (data,)),
                     (fields.parse_prices, (data, 'synthetic-star')))
            for function, args in calls:
                with self.subTest(function=function.__name__, data=data), self.assertRaises(ValueError):
                    function(*args)

    def test_fans_normalizes_each_dimensions_own_counts(self):
        data = fans_payload()
        before = copy.deepcopy(data)
        result = fields.parse_fans(data)
        self.assertAlmostEqual(result['gender']['male'], 0.4022036537342313)
        self.assertAlmostEqual(result['gender']['female'], 0.5977963462657687)
        self.assertEqual(result['age'], {'18-23': .1, '24-30': .2, '31-40': .3,
                                        '41-50': .3, '50+': .1})
        self.assertEqual(result['region'], {'\u6d59\u6c5f': .75, '\u4e0a\u6d77': .25})
        for ratios in result.values():
            self.assertAlmostEqual(sum(ratios.values()), 1)
            self.assertTrue(all(0 <= ratio <= 1 for ratio in ratios.values()))
        self.assertEqual(data, before)

    def test_fans_missing_dimensions_stay_unknown_and_city_is_not_province(self):
        for dimensions in ([], [dimension(256, [('\u676d\u5dde', '100')])]):
            self.assertEqual(fields.parse_fans({'base_resp': {'status_code': 0},
                                               'distributions': dimensions}),
                             {'gender': None, 'age': None, 'region': None})
        data = {'base_resp': {'status_code': 0}, 'distributions': [dimension(1, [('female', '10')])]}
        self.assertEqual(fields.parse_fans(data), {'gender': {'female': 1}, 'age': None, 'region': None})

    def test_fans_never_uses_arbitrary_audience_or_display_labels(self):
        data = {'base_resp': {'status_code': 0}, 'distributions': [],
                'audience': {'gender': {'female': .9}}, 'gender': {'male': .2}}
        self.assertIsNone(fields.parse_fans(data)['gender'])
        data['distributions'] = [{**dimension(256, [('city', 100)]),
                                  'type_display': '\u7701\u4efd\u5206\u5e03'}]
        self.assertIsNone(fields.parse_fans(data)['region'])
        with self.assertRaises(ValueError):
            fields.parse_fans({'base_resp': {'status_code': 0}, 'audience': data['audience']})

    def test_fans_duplicate_dimensions_or_keys_are_rejected(self):
        for kind in (1, 2, 4, 256):
            repeated = dimension(kind, [('key', '10')])
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                fields.parse_fans({'base_resp': {'status_code': 0},
                                   'distributions': [repeated, copy.deepcopy(repeated)]})
        with self.assertRaises(ValueError):
            fields.parse_fans({'base_resp': {'status_code': 0},
                               'distributions': [dimension(1, [('male', '10'), ('male', '20')])]})

    def test_fans_rejects_invalid_counts_instead_of_dropping_them(self):
        for value in ('NaN', float('nan'), 'Infinity', float('inf'), '-Infinity', -1, '-1',
                      True, False, None, '', '--', {}, [], '1e9999'):
            data = {'base_resp': {'status_code': 0},
                    'distributions': [dimension(1, [('male', value), ('female', '10')])]}
            with self.subTest(value=value), self.assertRaises(ValueError):
                fields.parse_fans(data)

    def test_fans_zero_total_is_invalid_but_an_individual_zero_is_real(self):
        for pairs in ([], [('male', '0'), ('female', 0)]):
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                fields.parse_fans({'base_resp': {'status_code': 0},
                                   'distributions': [dimension(1, pairs)]})
        result = fields.parse_fans({'base_resp': {'status_code': 0},
                                   'distributions': [dimension(1, [('male', 0), ('female', '10')])]})
        self.assertEqual(result['gender'], {'male': 0, 'female': 1})

    def test_fans_rejects_malformed_structure_and_keys(self):
        for dimensions in (None, {}, [None], [{'type': True}], [{'type': '1'}],
                           [{'type': 1, 'distribution_list': None}],
                           [{'type': 1, 'distribution_list': [None]}],
                           [dimension(1, [('', 1)])], [dimension(1, [(' ', 1)])],
                           [dimension(1, [(1, 1)])], [dimension(1, [(None, 1)])]):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                fields.parse_fans({'base_resp': {'status_code': 0}, 'distributions': dimensions})

    def test_fans_rejects_overflowing_totals(self):
        with self.assertRaises(ValueError):
            fields.parse_fans({'base_resp': {'status_code': 0},
                               'distributions': [dimension(1, [('male', 1e308), ('female', 1e308)])]})

    def test_numeric_underflow_does_not_turn_negative_or_nonzero_into_zero(self):
        for value in ('-1e-9999', '1e-9999'):
            calls = ((fields.parse_fans, ({'base_resp': {'status_code': 0},
                                          'distributions': [dimension(1, [('male', value), ('female', 10)])]},)),
                     (fields.parse_spread, ({'base_resp': {'status_code': 0}, 'play_mid': value},)),
                     (fields.parse_prices, ({'base_resp': {'status_code': 0},
                                            'price_info': [price_row(price=value)]}, 'synthetic-star')))
            for function, args in calls:
                with self.subTest(function=function.__name__, value=value), self.assertRaises(ValueError):
                    function(*args)

    def test_spread_uses_play_mid_and_verified_cpm_unit(self):
        data = {'base_resp': {'status_code': 0}, 'play_mid': '123456',
                'expect_vv': 999999, 'expect_cpm': {'cpm_21_60': 1622, 'cpm_1_20': 8888},
                'unverified_metric': 100}
        before = copy.deepcopy(data)
        self.assertEqual(fields.parse_spread(data), {'median_play_30d': 123456, 'expected_cpm': 16.22})
        self.assertEqual(data, before)

    def test_spread_does_not_borrow_expected_views_or_unknown_units(self):
        data = {'base_resp': {'status_code': 0}, 'expect_vv': 999999,
                'expect_cpm': {'cpm_1_20': 1622}}
        self.assertEqual(fields.parse_spread(data), {'median_play_30d': None, 'expected_cpm': None})
        self.assertEqual(fields.parse_spread({'base_resp': {'status_code': 0},
                                             'play_mid': 0, 'expect_cpm': {'cpm_21_60': '0'}}),
                         {'median_play_30d': 0, 'expected_cpm': 0})

    def test_spread_rejects_invalid_present_metrics(self):
        for value in ('NaN', float('inf'), -1, True, {}, [], '--'):
            for changes in ({'play_mid': value}, {'expect_cpm': {'cpm_21_60': value}}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    fields.parse_spread({'base_resp': {'status_code': 0}, **changes})
        with self.assertRaises(ValueError):
            fields.parse_spread({'base_resp': {'status_code': 0}, 'expect_cpm': []})

    def test_prices_match_author_task_and_duration_not_custom_or_collection(self):
        data = {'base_resp': {'status_code': 0}, 'price_info': [
            price_row(), price_row(2, price='2400'), price_row(71, price=7100),
            price_row(73, price=7300), price_row(author_id='other-star', price=99999),
            price_row(task_category=2, price=77777)]}
        before = copy.deepcopy(data)
        self.assertEqual(fields.parse_prices(data, 'synthetic-star'),
                         {'price_1_20': 1200, 'price_21_60': 2400})
        self.assertEqual(data, before)

    def test_prices_require_explicit_boolean_open_flags(self):
        for changes in ({'enable': False}, {'is_open': False}, {'enable': 1}, {'is_open': 1},
                        {'enable': 'true'}, {'is_open': 'true'}, {'enable': None}):
            with self.subTest(changes=changes):
                result = fields.parse_prices({'base_resp': {'status_code': 0},
                                              'price_info': [price_row(**changes)]}, 'synthetic-star')
                self.assertEqual(result, {'price_1_20': None, 'price_21_60': None})
        for absent in ('enable', 'is_open'):
            row = price_row()
            del row[absent]
            self.assertEqual(fields.parse_prices({'base_resp': {'status_code': 0}, 'price_info': [row]},
                                                 'synthetic-star')['price_1_20'], 1200)
        row = price_row()
        del row['enable'], row['is_open']
        self.assertIsNone(fields.parse_prices({'base_resp': {'status_code': 0}, 'price_info': [row]},
                                              'synthetic-star')['price_1_20'])

    def test_prices_missing_data_is_unknown_not_zero(self):
        for changes in ({}, {'price_info': None}, {'price_info': []},
                        {'price_info': [price_row(71), price_row(73)]}):
            self.assertEqual(fields.parse_prices({'base_resp': {'status_code': 0}, **changes},
                                                 'synthetic-star'),
                             {'price_1_20': None, 'price_21_60': None})
        self.assertEqual(fields.parse_prices({'base_resp': {'status_code': 0},
                                              'price_info': [price_row(price=0)]}, 'synthetic-star'),
                         {'price_1_20': 0, 'price_21_60': None})

    def test_prices_rejects_invalid_selected_values_and_duplicate_duration(self):
        for value in ('NaN', float('inf'), -1, True, {}, [], '--'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fields.parse_prices({'base_resp': {'status_code': 0},
                                     'price_info': [price_row(price=value)]}, 'synthetic-star')
        with self.assertRaises(ValueError):
            fields.parse_prices({'base_resp': {'status_code': 0},
                                 'price_info': [price_row(), price_row(price=2400)]}, 'synthetic-star')

    def test_prices_does_not_coerce_task_or_duration_types(self):
        for changes in ({'video_type': True}, {'video_type': '1'}, {'video_type': 1.0},
                        {'task_category': True}, {'task_category': '1'}, {'task_category': 1.0}):
            with self.subTest(changes=changes):
                result = fields.parse_prices({'base_resp': {'status_code': 0},
                                              'price_info': [price_row(**changes)]}, 'synthetic-star')
                self.assertEqual(result, {'price_1_20': None, 'price_21_60': None})

    def test_prices_rejects_malformed_containers_and_target(self):
        for rows in ({}, 'invalid', [None]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                fields.parse_prices({'base_resp': {'status_code': 0}, 'price_info': rows}, 'synthetic-star')
        for target in (None, True, 123, '', ' '):
            with self.subTest(target=target), self.assertRaises(ValueError):
                fields.parse_prices({'base_resp': {'status_code': 0}}, target)

    def test_errors_never_emit_or_include_payload(self):
        output = io.StringIO()
        data = {**base_payload(), 'base_resp': {'status_code': 403,
                                              'status_message': 'PRIVATE-SENTINEL'}}
        calls = ((fields.verify_base, (data, {'star_id': 'synthetic-star'}, 'synthetic-star')),
                 (fields.parse_fans, (data,)), (fields.parse_spread, (data,)),
                 (fields.parse_prices, (data, 'synthetic-star')))
        with redirect_stdout(output), redirect_stderr(output):
            for function, args in calls:
                with self.assertRaises(ValueError) as error:
                    function(*args)
                self.assertNotIn('PRIVATE-SENTINEL', str(error.exception))
                self.assertNotIn('synthetic-star', str(error.exception))
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
