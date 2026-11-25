"""Offline account eligibility and signup-order regressions; no real accounts."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import board_rules as B
import rules
from detail_cache import DetailCache
from monitor import Monitor
import gui_server as gs

NOW = 1900000000.0


def detail(**kwargs):
    return {"activityId": "1", "activityName": "模拟活动", "status": 1,
            "joinstartdate": NOW + 600, "joinenddate": NOW + 7200,
            "joinNum": 0, "joinmaxnum": 50, "canJoin": False,
            "ableJoinFlag": True, "signUpId": "0", **kwargs}


def item(raw=None, owner="A", reader="A", checked=NOW):
    return B.decorate(rules.normalize(B.stamp_detail(raw or detail(), owner, checked)), reader, now=NOW)


class EligibilityTests(unittest.TestCase):
    def test_future_eligible_is_visible_even_when_can_join_false(self):
        row = item()
        self.assertEqual(row["board_status"], "soon")
        self.assertEqual(row["eligibility"], "eligible")
        self.assertIs(row["can_join"], False)

    def test_current_open_uses_explicit_account_permission(self):
        row = item(detail(status=3, joinstartdate=NOW-60, canJoin="true"))
        self.assertEqual(row["board_status"], "open")

    def test_false_strings_are_not_truthy(self):
        row = item(detail(status=3, joinstartdate=NOW-60, canJoin="false", ableJoinFlag="false"))
        self.assertEqual(row["board_status"], "ineligible")
        self.assertIs(row["can_join"], False)

    def test_boolean_wire_formats(self):
        for value in (True, 1, "1", " true ", "TRUE"):
            self.assertIs(B.flag(value), True)
        for value in (False, 0, "0", " false ", "FALSE"):
            self.assertIs(B.flag(value), False)
        for value in (None, "", "unknown", 2, [], {}):
            self.assertIsNone(B.flag(value))

    def test_future_explicit_restriction_is_excluded(self):
        row = item(detail(ableJoinFlag=False, unableJoinReason="仅限计算机学院学生"))
        self.assertEqual(row["board_status"], "ineligible")
        self.assertIn("计算机", row["eligibility_reason"])

    def test_time_only_denial_is_unknown_not_ineligible(self):
        for reason in ("报名未开始", "活动尚未开始报名", "", "还没开始报名"):
            row = item(detail(ableJoinFlag=False, unableJoinReason=reason))
            self.assertEqual(row["eligibility"], "unknown", reason)

    def test_explicit_restriction_overrides_generic_ability_flag(self):
        row = item(detail(ableJoinFlag=True, unableJoinReason="年级不符合要求"))
        self.assertEqual(row["board_status"], "ineligible")

    def test_missing_permission_is_unknown(self):
        row = item(detail(ableJoinFlag=None, canJoin=None))
        self.assertEqual(row["board_status"], "unknown")

    def test_full_and_unlimited_and_unknown_quota(self):
        self.assertEqual(item(detail(joinNum=50))["board_status"], "full")
        self.assertEqual(item(detail(joinmaxnum="不限"))["board_status"], "soon")
        self.assertEqual(item(detail(joinmaxnum=None))["quota"], -2)

    def test_joined_is_excluded_and_injoin_ignored(self):
        self.assertEqual(item(detail(signUpId="987"))["board_status"], "joined")
        self.assertEqual(item(detail(injoin=True))["board_status"], "soon")

    def test_case_insensitive_signup_id(self):
        raw = detail()
        raw.pop("signUpId")
        raw["signupId"] = "987"
        self.assertTrue(item(raw)["joined_by_me"])

    def test_cancel_missing_id_does_not_keep_old_signup(self):
        raw = detail()
        raw.pop("signUpId")
        stamped = B.stamp_detail(raw, "A", NOW)
        merged = {**B.stamp_detail(detail(signUpId="987"), "A", NOW-10), **stamped}
        self.assertFalse(B.decorate(rules.normalize(merged), "A", now=NOW)["joined_by_me"])

    def test_other_account_never_inherits_rights_or_signup(self):
        row = item(detail(signUpId="987"), owner="A", reader="B")
        self.assertEqual(row["board_status"], "unknown")
        self.assertFalse(row["joined_by_me"])

    def test_legacy_shared_records_are_unknown(self):
        row = B.decorate(rules.normalize(detail(canJoin=True)), "A", now=NOW)
        self.assertEqual(row["board_status"], "unknown")

    def test_expired_and_future_dated_evidence_is_unknown(self):
        for checked in (NOW-301, NOW+1):
            self.assertEqual(item(checked=checked)["board_status"], "unknown")

    def test_deadline_overrides_open_status_and_permission(self):
        row = item(detail(status=3, joinstartdate=NOW-60, joinenddate=NOW, canJoin=True))
        self.assertEqual(row["board_status"], "closed")

    def test_start_boundary_needs_current_action_permission(self):
        row = item(detail(joinstartdate=NOW, canJoin=False))
        self.assertEqual(row["board_status"], "unknown")
        self.assertEqual(item(detail(joinstartdate=NOW, canJoin=True))["board_status"], "open")

    def test_beijing_string_seconds_and_milliseconds(self):
        expected = 1794391200.0  # 2026-11-11 18:00:00 +08:00
        self.assertEqual(B.epoch("2026-11-11 18:00:00"), expected)
        self.assertEqual(B.epoch(expected * 1000), expected)
        self.assertEqual(B.epoch(str(int(expected))), expected)
        self.assertEqual(B.epoch("2026-11-11T10:00:00Z"), expected)

    def test_join_window_fallback(self):
        row = item(detail(joinstartdate=None, joinenddate=None,
                          joindate="2026.11.11 18:00-2026.11.12 19:00"))
        self.assertEqual(row["sign_start"], B.epoch("2026-11-11 18:00"))
        self.assertEqual(row["sign_end"], B.epoch("2026-11-12 19:00"))

    def test_order_uses_signup_not_activity_or_deadline(self):
        rows = [item(detail(activityId="far", joinstartdate=NOW+900, startdateDate=NOW+1000)),
                item(detail(activityId="open", status=3, joinstartdate=NOW-30, canJoin=True)),
                item(detail(activityId="near", joinstartdate=NOW+60, startdateDate=NOW+90000)),
                item(detail(activityId="unknown", joinstartdate=None))]
        self.assertEqual([r["aid"] for r in sorted(rows, key=B.sort_key)], ["near", "far", "open", "unknown"])


class BoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def monitor(self, uid):
        mon = Monitor({"api": {"uid": uid, "token": "OFFLINE"},
                        "db": str(self.root / (uid + ".db")),
                        "detail_cache_path": str(self.root / (uid + ".details.db")),
                        "board_eligibility": True, "detail_ttl": 300,
                        "page_workers": 1, "detail_workers": 1, "max_pages": 1})
        self.addCleanup(mon.store.db.close)
        return mon

    def test_details_are_stamped_and_caches_do_not_cross_accounts(self):
        a, b = self.monitor("A"), self.monitor("B")
        with patch.object(a, "_thread_client", return_value=SimpleNamespace(detail=lambda aid: detail())), \
             patch.object(b, "_thread_client", return_value=SimpleNamespace(detail=lambda aid: detail(ableJoinFlag=False, unableJoinReason="年级不符"))):
            a._details_parallel(["1"])
            b._details_parallel(["1"])
        self.assertEqual(a.cache.get("1")["_eligibility_uid"], "A")
        self.assertEqual(b.cache.get("1")["_eligibility_uid"], "B")
        self.assertTrue(a.cache.get("1")["ableJoinFlag"])
        self.assertFalse(b.cache.get("1")["ableJoinFlag"])

    def test_fresh_detail_replaces_old_stored_signup(self):
        mon = self.monitor("A")
        old = B.stamp_detail(detail(signUpId="987"), "A", time.time()-30)
        mon.cache.put_many({"1": B.stamp_detail(detail(signUpId="0"), "A")})
        rows = mon._merge_and_normalize([{"activityId": "1", "status": 1}], {}, {"1": old})
        self.assertFalse(rows[0]["joined_by_me"])

    def test_existing_legacy_store_requests_account_detail(self):
        mon = self.monitor("A")
        raw = detail()
        mon.store.upsert("1", "模拟活动", "soon", 50, raw)
        mon.client = SimpleNamespace(activities=lambda **kw: {"list": [raw], "haveMore": False})
        with patch.object(mon, "_details_parallel", return_value={"1": B.stamp_detail(raw, "A")}) as fetch:
            result = mon.fetch_all()
        self.assertEqual(fetch.call_args.args[0], ["1"])
        self.assertEqual(result[0]["eligibility"], "eligible")

    def test_slim_keeps_all_account_fields(self):
        row = item()
        slim = gs._slim(row)
        for key in B.BOARD_KEYS:
            self.assertIn(key, slim)
        self.assertNotIn("raw", slim)
        self.assertEqual(slim["board_status"], "soon")

    def test_hub_patch_updates_status_and_signup_without_shared_mutation(self):
        hub = gs.Hub.__new__(gs.Hub)
        hub.uid, hub.cfg, hub.lock = "A", {}, threading.Lock()
        hub.items = [gs._slim(item())]
        hub._patch_item("1", detail(signUpId="77"))
        self.assertEqual(hub.items[0]["board_status"], "joined")
        hub._patch_item("1", detail(signUpId="0"))
        self.assertFalse(hub.items[0]["joined_by_me"])

    def test_per_account_cache_is_removed_by_original_account_delete(self):
        from users import Users
        users = Users(self.root)
        users.upsert(uid="123", account="fake", password="fake", token="fake")
        path = users.db_for("123") + ".details-v24.db"
        DetailCache(path).put_many({"1": detail()})
        users.delete_user("123")
        self.assertFalse(Path(path).exists())
