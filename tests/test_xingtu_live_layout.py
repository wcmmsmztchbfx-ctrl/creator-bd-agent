"""Anonymized shapes observed on the actual market response; no live requests."""

import unittest

from app import xingtu


class CurrentIdentityLayoutTests(unittest.TestCase):
    def setUp(self):
        self.creator = {"uid": "douyin-user-1", "unique_id": "public-handle-1", "sec_uid": "secure-user-1"}
        self.author = {"star_id": "star-user-1", "attribute_datas": {
            "id": "star-user-1", "core_user_id": "douyin-user-1", "vv_median_30d": "32000",
            "gender": "2", "province": "fictional-region"}}

    def test_current_core_user_mapping_verifies_only_same_namespace(self):
        result = xingtu._parse_search({"authors": [self.author]}, self.creator, xingtu._empty_result())
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["star_id"], "star-user-1")
        self.assertEqual(result["median_play_30d"], 32000)
        self.assertIn("authors[].attribute_datas.core_user_id", result["field_sources"]["identity"]["field"])
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(result["audience"], {"gender": None, "age": None, "region": None})

    def test_wrong_uid_never_passes_even_when_public_handle_matches(self):
        self.author["attribute_datas"].update(core_user_id="another-douyin-user", short_id="public-handle-1")
        self.assertIsNone(xingtu._identity(self.author, self.creator))

    def test_star_id_is_not_douyin_uid_and_conflicts_are_rejected(self):
        for changes in ({"core_user_id": "star-user-1"}, {"id": "other-star"}):
            with self.subTest(changes=changes):
                attrs = {**self.author["attribute_datas"], **changes}
                self.assertIsNone(xingtu._identity({**self.author, "attribute_datas": attrs}, self.creator))

    def test_missing_douyin_uid_cannot_verify_from_core_id_alone(self):
        self.assertIsNone(xingtu._identity(self.author, {"unique_id": "public-handle-1"}))

    def test_duplicate_matching_core_ids_remain_ambiguous(self):
        second = {"star_id": "star-user-2", "attribute_datas": {
            "id": "star-user-2", "core_user_id": "douyin-user-1"}}
        result = xingtu._parse_search({"authors": [self.author, second]}, self.creator, xingtu._empty_result())
        self.assertEqual(result["status"], "identity_unconfirmed")

    def test_customer_entitlement_error_is_permission_denied_not_parse_failure(self):
        result = xingtu._parse_search({"base_resp": {
            "status_code": 10003, "status_message": "您的客户权益等级过低，暂时无法访问更多数据"},
            "authors": [self.author]}, self.creator, xingtu._empty_result())
        self.assertEqual(result["status"], "permission_denied")
        self.assertEqual(result["reason"], "customer_entitlement_required")
        self.assertFalse(result["identity_verified"])


if __name__ == "__main__":
    unittest.main()
