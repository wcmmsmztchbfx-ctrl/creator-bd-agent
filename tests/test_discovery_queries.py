import unittest

from app.discovery import propose_creator_queries


class CreatorQueryTests(unittest.TestCase):
    def test_broad_food_defaults_cover_distinct_creator_roles(self):
        cases = {
            '食品': ['食品测评', '美食分享', '美食制作'],
            '美食': ['美食分享', '美食制作', '美食测评'],
            '零食': ['零食测评', '零食分享', '零食制作'],
        }
        for keyword, expected in cases.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(propose_creator_queries(keyword), expected)

    def test_llm_bare_category_queries_receive_creator_defaults(self):
        for queries in ([], ['食品'], ['食品', '美食', '零食']):
            with self.subTest(queries=queries):
                self.assertEqual(propose_creator_queries('食品', queries),
                                 ['食品测评', '美食分享', '美食制作'])

    def test_specific_subtype_keyword_is_not_broadened(self):
        self.assertEqual(propose_creator_queries('控糖零食'), ['控糖零食'])

    def test_specific_explicit_query_is_not_replaced(self):
        self.assertEqual(propose_creator_queries('食品', ['控糖零食']), ['控糖零食'])

    def test_mixed_explicit_queries_are_not_replanned(self):
        self.assertEqual(propose_creator_queries('食品', ['食品', '控糖零食']),
                         ['食品', '控糖零食'])

    def test_exact_creator_names_are_preserved(self):
        for name in ('小林的食品日记', '食品研究所', 'Chef Alice'):
            with self.subTest(name=name):
                self.assertEqual(propose_creator_queries(name), [name])
                self.assertEqual(propose_creator_queries('食品', [name]), [name])

    def test_specific_keyword_is_not_lost_to_bare_llm_queries(self):
        self.assertEqual(propose_creator_queries('控糖零食', ['食品']), ['控糖零食'])

    def test_subtype_preferences_replace_only_broad_defaults(self):
        for queries in (None, ['食品']):
            with self.subTest(queries=queries):
                self.assertEqual(propose_creator_queries(
                    '食品', queries, ['控糖零食', '家常美食']), ['控糖零食', '家常美食'])

    def test_preference_without_category_keeps_food_scope(self):
        self.assertEqual(propose_creator_queries('食品', content_preferences=['配料表解读']),
                         ['食品 配料表解读'])

    def test_specific_queries_take_precedence_over_preferences(self):
        self.assertEqual(propose_creator_queries('食品', ['小林的食品日记'], ['控糖零食']),
                         ['小林的食品日记'])

    def test_specific_keyword_takes_precedence_over_preferences(self):
        self.assertEqual(propose_creator_queries('控糖零食', content_preferences=['实测']),
                         ['控糖零食'])

    def test_unknown_categories_are_not_expanded(self):
        for keyword in ('摄影', '美妆', 'food', '食品饮料', '美食博主'):
            with self.subTest(keyword=keyword):
                self.assertEqual(propose_creator_queries(keyword), [keyword])
                self.assertEqual(propose_creator_queries(keyword, content_preferences=['分享']),
                                 [keyword])

    def test_whitespace_and_duplicates_do_not_consume_query_budget(self):
        self.assertEqual(propose_creator_queries(
            '食品', [' 控糖零食 ', '', '控糖零食', ' 家常菜 ', '探店', '烘焙']),
            ['控糖零食', '家常菜', '探店'])

    def test_preferences_are_deduplicated_and_bounded(self):
        self.assertEqual(propose_creator_queries('食品', content_preferences=[
            ' 控糖零食 ', '', '控糖零食', '家常美食', '零食制作', '烘焙']),
            ['控糖零食', '家常美食', '零食制作'])

    def test_empty_input_does_not_invent_a_category(self):
        self.assertEqual(propose_creator_queries('  ', ['', ' ']), [])
        self.assertEqual(propose_creator_queries('', ['控糖零食']), ['控糖零食'])

    def test_empty_preferences_do_not_disable_broad_defaults(self):
        self.assertEqual(propose_creator_queries(' 食品 ', [' 食品 ', ''], ['', ' ']),
                         ['食品测评', '美食分享', '美食制作'])

    def test_repeated_calls_are_deterministic_and_do_not_mutate_inputs(self):
        queries = [' 食品 ', '食品']
        preferences = [' 控糖零食 ', '控糖零食']
        first = propose_creator_queries('食品', queries, preferences)
        first.append('caller mutation')
        self.assertEqual(propose_creator_queries('食品', queries, preferences), ['控糖零食'])
        self.assertEqual(queries, [' 食品 ', '食品'])
        self.assertEqual(preferences, [' 控糖零食 ', '控糖零食'])

    def test_broad_female_fashion_defaults_target_creator_content(self):
        for keyword in ('女装', '女装穿搭'):
            for queries in (None, [keyword], ['女装', '女装穿搭']):
                with self.subTest(keyword=keyword, queries=queries):
                    self.assertEqual(propose_creator_queries(keyword, queries),
                                     ['女生穿搭分享', '女生日常穿搭', '女生一衣多穿'])

    def test_specific_fashion_queries_and_names_are_preserved(self):
        for query in ('女装穿搭分享', '日常穿搭', '一衣多穿', '通勤女装',
                      '小林女装穿搭日记', '女装穿搭官方店'):
            with self.subTest(query=query):
                self.assertEqual(propose_creator_queries('女装', [query], ['分享']),
                                 [query])
                self.assertEqual(propose_creator_queries('女装', ['女装', query]),
                                 ['女装', query])

    def test_specific_fashion_keyword_is_not_broadened(self):
        for keyword in ('通勤女装', '小个子女装', '上海女装', '30岁女装', '男装穿搭'):
            with self.subTest(keyword=keyword):
                self.assertEqual(propose_creator_queries(keyword, ['女装']), [keyword])

    def test_fashion_preferences_keep_female_context_without_invented_traits(self):
        self.assertEqual(propose_creator_queries('女装', content_preferences=[
            '日常穿搭', '女生通勤穿搭', '一衣多穿']),
            ['女装 日常穿搭', '女生通勤穿搭', '女装 一衣多穿'])

    def test_fashion_query_budget_is_deterministic_and_inputs_are_unchanged(self):
        queries = [' 女装 ', '女装穿搭', '女装']
        preferences = [' 日常穿搭 ', '', '日常穿搭', '一衣多穿', '女生穿搭分享', '实穿']
        expected = ['女装 日常穿搭', '女装 一衣多穿', '女生穿搭分享']
        result = propose_creator_queries(' 女装穿搭 ', queries, preferences)
        self.assertEqual(result, expected)
        result.append('caller mutation')
        self.assertEqual(propose_creator_queries(' 女装穿搭 ', queries, preferences), expected)
        self.assertEqual(queries, [' 女装 ', '女装穿搭', '女装'])
        self.assertEqual(preferences,
                         [' 日常穿搭 ', '', '日常穿搭', '一衣多穿', '女生穿搭分享', '实穿'])

    def test_fashion_task_card_preserves_explicit_modes_and_constraints(self):
        from app.contracts import TaskCard

        for mode in ('xingtu', 'user', 'video', 'hybrid'):
            with self.subTest(mode=mode):
                card = TaskCard(category='女装', keyword='女装', discovery_mode=mode,
                                min_followers=5000, max_followers=20000,
                                min_avg_likes=100, min_max_likes=500, active_days=30,
                                sample_count=7, content_preferences=['一衣多穿'],
                                exclusions=['直播切片'])
                self.assertEqual(card.search_queries, ['女装 一衣多穿'])
                self.assertEqual(card.discovery_mode, mode)
                self.assertEqual((card.min_followers, card.max_followers), (5000, 20000))
                self.assertEqual((card.min_avg_likes, card.min_max_likes,
                                  card.active_days, card.sample_count), (100, 500, 30, 7))
                self.assertEqual(card.content_preferences, ['一衣多穿'])
                self.assertEqual(card.exclusions, ['直播切片'])

    def test_fashion_task_card_defaults_to_xingtu(self):
        from app.contracts import TaskCard

        card = TaskCard(category='女装', keyword='女装')
        self.assertEqual(card.discovery_mode, 'xingtu')
        self.assertEqual(card.search_queries,
                         ['女生穿搭分享', '女生日常穿搭', '女生一衣多穿'])

    def test_xingtu_card_keeps_query_budget_and_existing_validation(self):
        from app.contracts import TaskCard
        from pydantic import ValidationError

        for changes in ({'search_queries': ['甲', '乙', '丙', '丁']},
                        {'min_followers': 100, 'max_followers': 99},
                        {'discovery_mode': 'unsupported'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                TaskCard(category='食品', keyword='食品', **changes)


if __name__ == '__main__':
    unittest.main()
