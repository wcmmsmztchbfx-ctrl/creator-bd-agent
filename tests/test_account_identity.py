import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import crawler, db, qualification
from app.contracts import TaskCard


def account(kind='independent_creator', ids=None):
    return {'account_type': kind, 'evidence_ids': ['p0', 'p1'] if ids is None else ids,
            'reason': '多条文本显示个人对不同品牌的食品实测分享'}


def topic(identity=None):
    result = {'topic_status': 'matched', 'creator_pattern': 'consistent_creator',
              'posts': [{'aweme_id': f'p{i}', 'decision': 'yes'} for i in range(6)],
              'sample_count': 6, 'matched_count': 6, 'ratio': 1.0}
    if identity is not None:
        result['account'] = identity
    return result


class AccountIdentityTests(unittest.TestCase):
    def test_broad_food_task_card_uses_creator_queries_before_confirmation(self):
        card = TaskCard(category='食品', keyword='食品', search_queries=['食品', '美食'])
        self.assertEqual(card.search_queries, ['食品测评', '美食分享', '美食制作'])

    def test_executor_uses_same_creator_queries_for_direct_call(self):
        self.assertEqual(crawler._normalize_queries(['食品', '美食'], '食品'),
                         ['食品测评', '美食分享', '美食制作'])

    def test_legacy_topic_match_without_creator_identity_is_not_eligible(self):
        result = qualification.combine_candidate_assessment({'status': 'passed'}, topic(), {})
        self.assertEqual(result['overall_status'], 'pending')
        self.assertIn('account_type_unknown', result['reason_codes'])

    def test_brand_is_rejected_even_when_food_topic_and_numbers_pass(self):
        result = qualification.combine_candidate_assessment(
            {'status': 'passed'}, topic(account('brand_store', ['creator'])), {})
        self.assertEqual(result['overall_status'], 'rejected')
        self.assertIn('account_type_excluded', result['reason_codes'])

    def test_readable_account_evidence_is_separate_from_topic_decision(self):
        data = topic(account('brand_store', ['p0', 'creator']))
        data['posts'][0]['decision'] = 'unknown'
        result = qualification.combine_candidate_assessment({'status': 'passed'}, data, {})
        self.assertEqual(result['overall_status'], 'rejected')

    def test_valid_personal_creator_is_eligible(self):
        result = qualification.combine_candidate_assessment(
            {'status': 'passed'}, topic(account()), {})
        self.assertEqual(result['overall_status'], 'eligible')

    def test_unbacked_personal_label_or_single_post_cannot_prove_creator_role(self):
        for ids in ([], ['invented'], ['creator'], ['p0'], ['p0', 'p0']):
            with self.subTest(ids=ids):
                result = qualification.combine_candidate_assessment(
                    {'status': 'passed'}, topic(account(ids=ids)), {})
                self.assertEqual(result['overall_status'], 'pending')

    def test_brand_rules_use_clear_role_not_food_name_or_enterprise_badge(self):
        for creator in (
            {'nickname': '阿宽官方旗舰店'},
            {'nickname': '喜之郎食品', 'signature': '官方自营 正品保障 日期新鲜'},
            {'nickname': '某品牌', 'signature': '本账号为食品品牌官方账号'},
        ):
            with self.subTest(creator=creator):
                result = qualification.classify_account_identity(creator)
                self.assertEqual(result['account_type'], 'brand_store')
        for creator in (
            {'nickname': '方姐食品', 'signature': '朴实无华，记录农村生活！'},
            {'nickname': '老王测评官方账号', 'signature': '分享我的食品实测，商务合作见简介'},
            {'nickname': '食品测评小组', 'signature': 'MCN签约创作者', 'enterprise_verify_reason': '某文化公司'},
            {'nickname': '老李', 'signature': '曾就职于某某食品有限公司，现在分享我的厨艺'},
            {'nickname': '老李测评', 'signature': '今天测评阿宽官方旗舰店买来的零食'},
            {'nickname': '张姐美食', 'signature': '不是品牌官方，不做厂家直销，只分享做饭'},
        ):
            with self.subTest(creator=creator):
                self.assertEqual(qualification.classify_account_identity(creator)['account_type'], 'unknown')

    def test_strong_brand_rule_overrides_model_personal_claim_in_build(self):
        result = qualification.build_assessment(
            {'nickname': '阿宽官方旗舰店'}, None, [], {}, {'status': 'unknown'},
            topic(account()), qualification.POLICY_DEFAULTS)
        self.assertEqual(result['overall_status'], 'rejected')
        self.assertEqual(result['topic']['account']['account_type'], 'brand_store')

    def test_unknown_account_identity_can_be_explicitly_reviewed_again(self):
        assessment = {'numeric': {'status': 'passed'}, 'topic_reviewed': True,
                      'topic': topic(), 'overall_status': 'pending',
                      'reason_codes': ['account_type_unknown']}
        self.assertFalse(db.candidate_assessment_reusable(assessment))

    def test_explicit_brand_rejection_is_reusable_without_model_review(self):
        assessment = {'numeric': {'status': 'passed'}, 'topic_reviewed': False,
                      'topic': topic(account('brand_store', ['creator'])),
                      'overall_status': 'rejected', 'reason_codes': ['account_type_excluded']}
        self.assertTrue(db.candidate_assessment_reusable(assessment))


class AccountReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_account_role_is_retained_with_valid_evidence(self):
        samples = [{'aweme_id': f'p{i}', 'desc': '我买了不同品牌零食，今天做口味对比'} for i in range(6)]
        output = {key: value for key, value in topic(account()).items()
                  if key in ('creator_pattern', 'posts', 'account')}
        settings = {'llm_enabled': 'true', 'llm_model': 'fixture'}
        with patch.object(db, 'get_all_settings', return_value=settings), patch(
            'app.chat.request_model_json', new=AsyncMock(return_value=output)):
            result = await qualification.review_creator_topic(
                {'nickname': '老王测评'}, samples, {}, qualification.POLICY_DEFAULTS)
        self.assertEqual(result['account']['account_type'], 'independent_creator')
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, {})['overall_status'], 'eligible')

    async def test_model_account_role_cannot_reference_invented_evidence(self):
        samples = [{'aweme_id': f'p{i}', 'desc': '我买了不同品牌零食，今天做口味对比'} for i in range(6)]
        output = {'posts': topic()['posts'], 'creator_pattern': 'consistent_creator',
                  'account': account(ids=['invented', 'p0'])}
        with patch.object(db, 'get_all_settings', return_value={'llm_enabled': 'true'}), patch(
            'app.chat.request_model_json', new=AsyncMock(return_value=output)):
            result = await qualification.review_creator_topic(
                {'nickname': '测评'}, samples, {}, qualification.POLICY_DEFAULTS)
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, {})['overall_status'], 'pending')


class AccountPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(self.temp) / 'kb.db')))
        db.init_db()
        self.task = db.save_task('food', '食品', {'min_avg_likes': 1000})
        self.creator = db.upsert_creator({'sec_uid': 'fixture-brand', 'nickname': '阿宽官方旗舰店'})
        db.save_recommendation(self.creator, self.task, {'hard_pass': None})

    async def test_explicit_brand_saves_rejection_without_fetching_posts_or_model(self):
        with patch.object(crawler, 'fetch_profile', new=AsyncMock(return_value={
                'nickname': '阿宽官方旗舰店', 'signature': '品牌自营店', 'follower_count': 20000})), patch.object(
                crawler, 'fetch_user_posts', new=AsyncMock()) as posts, patch.object(
                qualification, 'review_creator_topic', new=AsyncMock()) as model:
            await crawler.refresh_candidate(self.task, self.creator)
        posts.assert_not_awaited()
        model.assert_not_awaited()
        result = db.get_current_candidate_assessment(self.task, self.creator)
        self.assertEqual(result['overall_status'], 'rejected')
        self.assertEqual(result['topic']['account']['account_type'], 'brand_store')
        self.assertIn('account_type_excluded', result['assessment']['reason_codes'])

    async def test_existing_brand_rereview_does_not_charge_model_budget(self):
        db.save_snapshot(self.creator, self.task, {'sample_count': 6, 'avg_likes': 2000})
        db.save_sample_posts(self.creator, self.task, [
            {'aweme_id': f'p{i}', 'desc': '食品特卖', 'likes': 2000} for i in range(6)])
        budget = {'used': 0, 'limit': 1, 'exhausted': False}
        with patch.object(qualification, 'review_creator_topic', new=AsyncMock()) as model:
            result = await crawler.review_existing_candidate(self.task, self.creator, budget=budget)
        self.assertEqual(result['overall_status'], 'rejected')
        self.assertEqual(budget['used'], 0)
        model.assert_not_awaited()
