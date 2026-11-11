import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from app import db, qualification


def task(**changes):
    result = {'category': 'food', 'keyword': 'food reviews',
              'content_preferences': [], 'exclusions': [],
              'topic_min_samples': 5, 'topic_min_ratio': 0.6}
    result.update(changes)
    return result


def samples(count=6):
    return [{'aweme_id': f'p{i}', 'desc': 'Food ingredient review and tasting',
             'post_date': f'2026-11-05T12:{i:02d}:00+08:00'} for i in range(count)]


def output(posts):
    return {'posts': [{'aweme_id': p['aweme_id'], 'decision': 'yes',
                       'evidence_id': p['aweme_id']} for p in posts],
            'creator_pattern': 'consistent_creator',
            'account': {'account_type': 'independent_creator',
                        'evidence_ids': [p['aweme_id'] for p in posts[:2]],
                        'reason': 'Synthetic personal creator across posts'}}


def condition(requirement_id, decision='matched', evidence_ids=None):
    return {'requirement_id': requirement_id, 'decision': decision,
            'evidence_ids': ['p0'] if evidence_ids is None else evidence_ids,
            'reason': 'Public text evidence'}


class QualificationFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = {'llm_enabled': 'true', 'llm_model': 'fixture',
                         'single_post_sample': '10'}
        self.enterContext(patch.object(db, 'get_all_settings', return_value=self.settings))

    async def review(self, requirements=None, posts=None, response=None, creator=None, policy=None):
        requirements = task() if requirements is None else requirements
        posts = samples() if posts is None else posts
        policy = qualification.policy_from_requirements(requirements, self.settings) if policy is None else policy
        with patch('app.chat.request_model_json', new=AsyncMock(
                return_value=output(posts) if response is None else response)) as model:
            result = await qualification.review_creator_topic(
                creator or {'nickname': 'fixture'}, posts, requirements, policy)
        return result, model, policy

    async def test_missing_explicit_preference_never_becomes_eligible(self):
        result, _, policy = await self.review(task(content_preferences=['ingredient analysis']))
        assessment = qualification.combine_candidate_assessment({'status': 'passed'}, result, policy)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertIn('explicit_condition_unknown', assessment['reason_codes'])
        self.assertEqual(result['account']['account_type'], 'independent_creator')
        self.assertEqual([item['decision'] for item in result.get('condition_decisions', [])], ['unknown'])

    async def test_text_conditions_have_individual_evidence_and_exclusion_failure_rejects(self):
        req = task(content_preferences=['ingredient analysis'], exclusions=['sponsored ads'])
        response = output(samples())
        response['condition_decisions'] = [condition('content_preferences:0'),
                                          condition('exclusions:0', 'not_matched')]
        result, _, policy = await self.review(req, response=response)
        self.assertEqual(result['topic_status'], 'matched')
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, policy)['overall_status'], 'rejected')
        response['condition_decisions'][1]['decision'] = 'matched'
        result, _, policy = await self.review(req, response=response)
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, policy)['overall_status'], 'eligible')

    async def test_visual_claims_remain_unknown_even_when_model_claims_match(self):
        for requirement in ('必须真人出镜', 'real person on camera', '原创视频', 'high video quality'):
            with self.subTest(requirement=requirement):
                response = output(samples())
                response['condition_decisions'] = [condition('content_preferences:0')]
                result, _, policy = await self.review(
                    task(content_preferences=[requirement]), response=response)
                self.assertEqual([item['decision'] for item in result.get('condition_decisions', [])], ['unknown'])
                self.assertEqual(qualification.combine_candidate_assessment(
                    {'status': 'passed'}, result, policy)['overall_status'], 'pending')
                self.assertEqual(result['account']['account_type'], 'independent_creator')

    async def test_condition_cannot_pass_without_real_readable_evidence(self):
        for evidence in ([], ['invented-id']):
            with self.subTest(evidence=evidence):
                response = output(samples())
                response['condition_decisions'] = [condition('content_preferences:0', evidence_ids=evidence)]
                result, _, policy = await self.review(
                    task(content_preferences=['ingredient analysis']), response=response)
                self.assertNotEqual(qualification.combine_candidate_assessment(
                    {'status': 'passed'}, result, policy)['overall_status'], 'eligible')

    async def test_personal_creator_passes_when_no_explicit_conditions(self):
        result, _, policy = await self.review()
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, policy)['overall_status'], 'eligible')

    async def test_legacy_protocol_without_account_stays_pending(self):
        response = output(samples())
        response.pop('account')
        result, _, policy = await self.review(response=response)
        assessment = qualification.combine_candidate_assessment({'status': 'passed'}, result, policy)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertIn('account_type_unknown', assessment['reason_codes'])

    async def test_blank_recent_posts_stay_unknown_in_ten_post_denominator(self):
        posts = samples(10)
        for post in posts[5:]:
            post['desc'] = ''
        result, _, policy = await self.review(posts=posts, response=output(posts))
        self.assertEqual(result['sample_count'], 10)
        self.assertEqual(result['matched_count'], 5)
        self.assertEqual(result['unknown_count'], 5)
        self.assertEqual(result['ratio'], 0.5)
        self.assertEqual(result['account']['account_type'], 'independent_creator')
        self.assertEqual(qualification.combine_candidate_assessment(
            {'status': 'passed'}, result, policy)['overall_status'], 'pending')

    async def test_recent_unreadable_samples_are_not_replaced_with_older_readable_ones(self):
        posts = samples(15)
        for post in posts[5:]:
            post['desc'] = '#food' if int(post['aweme_id'][1:]) % 2 else '\ufffd\ufffd'
        result, _, _ = await self.review(posts=posts, response=output(posts[5:]))
        self.assertEqual(result['sample_count'], 10)
        self.assertEqual(result['unknown_count'], 10)
        self.assertEqual(result['ratio'], 0.0)
        self.assertEqual({p['aweme_id'] for p in result['posts']}, {f'p{i}' for i in range(5, 15)})

    async def test_confirmed_minimum_is_not_reduced_by_fetch_limit(self):
        self.settings['single_post_sample'] = '1'
        result, model, policy = await self.review(posts=samples(1))
        self.assertEqual(policy['min_samples'], 5)
        self.assertEqual(result['topic_status'], 'unknown')
        self.assertIn(result['error_code'], ('invalid_policy', 'insufficient_samples'))
        model.assert_not_awaited()

    async def test_combination_uses_current_policy_not_embedded_weaker_minimum(self):
        result, _, policy = await self.review()
        result.update(sample_count=1, matched_count=1, min_samples=1)
        assessment = qualification.combine_candidate_assessment({'status': 'passed'}, result, policy)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertIn('topic_insufficient_samples', assessment['reason_codes'])

    async def test_build_cannot_skip_conditions_with_legacy_policy_and_topic(self):
        result, _, _ = await self.review()
        built = qualification.build_assessment(
            {}, None, samples(), task(content_preferences=['必须真人出镜']),
            {'status': 'passed'}, result, dict(qualification.POLICY_DEFAULTS))
        self.assertEqual(built['overall_status'], 'pending')
        self.assertIn('explicit_condition_unknown', built['reason_codes'])

    async def test_contact_redaction_covers_all_model_text_and_does_not_mutate_input(self):
        contacts = 'fixture@example.invalid +86 138-0013-8000 商务微信: fixture_wx42'
        req = task(category='food ' + contacts, keyword=contacts,
                   content_preferences=[contacts], exclusions=[contacts])
        posts = samples()
        posts[0]['desc'] += ' ' + contacts
        creator = {'nickname': contacts, 'signature': contacts, 'unique_id': contacts,
                   'contacts': ['PRIVATE-CONTACT'], 'cookies': 'COOKIE-SECRET',
                   'chat_history': ['CHAT-SECRET']}
        originals = copy.deepcopy((req, posts, creator))
        _, model, _ = await self.review(req, posts, creator=creator)
        messages = model.await_args.args[1]
        text = json.dumps(messages, ensure_ascii=False)
        for secret in ('fixture@example.invalid', '138-0013-8000', 'fixture_wx42',
                       'PRIVATE-CONTACT', 'COOKIE-SECRET', 'CHAT-SECRET'):
            self.assertNotIn(secret, text)
        self.assertIn('Food ingredient review', text)
        self.assertEqual((req, posts, creator), originals)

    async def test_contact_redaction_handles_unspaced_chinese_and_business_v(self):
        _, model, _ = await self.review(creator={
            'signature': '商务V: vendor_42 电话010-88886666 手机13800138000 food reviews'})
        sent = json.dumps(model.await_args.args[1], ensure_ascii=False)
        for secret in ('vendor_42', '010-88886666', '13800138000'):
            self.assertNotIn(secret, sent)

    async def test_malformed_runtime_policy_is_unknown_without_spending(self):
        for changes in ({'min_samples': 'oops'}, {'min_ratio': float('nan')},
                        {'sample_limit': True}, {'min_samples': -1}):
            with self.subTest(changes=changes):
                result, model, _ = await self.review(policy=dict(qualification.POLICY_DEFAULTS, **changes))
                self.assertEqual(result['topic_status'], 'unknown')
                self.assertEqual(result['error_code'], 'invalid_policy')
                model.assert_not_awaited()

    async def test_review_cannot_weaken_confirmed_ratio_with_legacy_policy(self):
        posts = samples(6)
        response = output(posts)
        response['posts'][0]['decision'] = 'no'
        result, _, _ = await self.review(task(topic_min_ratio=1.0), posts, response,
                                         policy=dict(qualification.POLICY_DEFAULTS))
        self.assertEqual(result['min_ratio'], 1.0)
        self.assertNotEqual(result['topic_status'], 'matched')

    async def test_forged_condition_evidence_cannot_pass_combination(self):
        response = output(samples())
        response['condition_decisions'] = [condition('content_preferences:0')]
        result, _, policy = await self.review(task(content_preferences=['ingredient analysis']), response=response)
        result['condition_decisions'][0]['evidence_ids'] = ['invented-id']
        assessment = qualification.combine_candidate_assessment({'status': 'passed'}, result, policy)
        self.assertEqual(assessment['overall_status'], 'pending')
        self.assertIn('explicit_condition_unknown', assessment['reason_codes'])

    def test_public_policy_validator_rejects_conflicting_and_nonfinite_policy(self):
        validate = getattr(qualification, 'validate_review_policy', None)
        self.assertTrue(callable(validate), 'Expose validate_review_policy(policy) -> list[str]')
        self.assertEqual(validate(dict(qualification.POLICY_DEFAULTS)), [])
        self.assertTrue(validate(dict(qualification.POLICY_DEFAULTS, sample_limit=1)))
        self.assertTrue(validate(dict(qualification.POLICY_DEFAULTS, min_ratio=float('nan'))))


if __name__ == '__main__':
    unittest.main()
