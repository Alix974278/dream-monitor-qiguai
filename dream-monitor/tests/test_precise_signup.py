"""Offline tests of the original request methods with the timed thin layer.

Every Session is replaced before construction. No real account, DNS lookup,
network request, or identity file outside a temporary directory is used.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

import dmclient
from dmclient import DreamClient, PATH_DETAIL, PATH_SIGNUP_INFO, PATH_SIGNUP_SUBMIT
from precision import signup


def original_builder():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "gui_server.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_signup_data")
    scope = {"json": json}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "original_form_builder", "exec"), scope)
    return scope["_build_signup_data"]


class Clock:
    def now(self):
        return 1_700_000_000.125

    def monotonic(self):
        return 1234.5


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.text = "mock response"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("mock HTTP failure", response=self)

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def ok(data=None):
    return Response({"code": "100", "data": data or {}})


def error(code, message):
    return Response({"code": str(code), "msg": message})


class Router:
    def __init__(self):
        self.calls, self.sessions, self.scripts = [], [], {}
        self.guard = threading.Lock()

    def respond(self, path, *items):
        self.scripts[path] = list(items)

    def session(self):
        router = self

        class Session:
            def __init__(self):
                self.headers = {}
                self.cookies = requests.cookies.RequestsCookieJar()
                self.trust_env, self.closed = True, False

            def post(self, url, data, timeout):
                path = urlsplit(url).path
                with router.guard:
                    call = {"path": path, "params": copy.deepcopy(data["d"]),
                            "session": self, "timeout": timeout}
                    router.calls.append(call)
                    script = router.scripts.get(path, [])
                    item = script.pop(0) if script else ok()
                if isinstance(item, Exception):
                    raise item
                return item(call) if callable(item) else item

            def close(self):
                self.closed = True

        session = Session()
        self.sessions.append(session)
        return session

    def submitted(self):
        return [c for c in self.calls if c["path"] == PATH_SIGNUP_SUBMIT]


class PreciseSignupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = Router()
        for p in (
            patch.object(dmclient, "IDENTITY_PATH", str(Path(self.temp.name) / "identity.json")),
            patch.object(dmclient.requests, "Session", self.router.session),
            patch.object(dmclient.C, "make_d", side_effect=lambda p: copy.deepcopy(p)),
        ):
            p.start()
            self.addCleanup(p.stop)
        cfg = {"uid": "mock-uid", "token": "mock-secret-token", "account": "mock-account",
               "password": "mock-password", "uuid": "mock-fixed-uuid", "jPushId": "mock-fixed-jpush",
               "version": "4.9.5", "request_delay": 1.2, "timeout": 20}
        self.source = DreamClient(cfg)
        self.source.sess.cookies.set("mock-session-cookie", "same-session")
        self.hub = SimpleNamespace(mon=SimpleNamespace(client=self.source), name="测试姓名")
        self.builder = Mock(wraps=original_builder())
        self.clock = Clock()
        self.form = {"list": [{"key": "k1", "fullid": 7, "title": "姓名", "notNull": "true"}]}
        self.router.respond(PATH_SIGNUP_INFO, ok(self.form))

    def prepare(self, aid="test-001", **kwargs):
        ctx = signup.prepare(self.hub, aid, self.builder, self.clock, **kwargs)
        self.addCleanup(signup.close, ctx)
        return ctx

    def test_clone_keeps_original_identity_cookies_and_builder(self):
        before_cfg = copy.deepcopy(self.source.cfg)
        ctx = self.prepare()
        self.assertTrue(ctx.prefetched)
        self.assertEqual(ctx.prepared_at, self.clock.now())
        self.assertIsNot(ctx._client, self.source)
        self.assertIsNot(ctx._client.sess, self.source.sess)
        self.assertEqual(ctx._client.sess.headers, self.source.sess.headers)
        self.assertEqual(ctx._client.sess.cookies.get_dict(), self.source.sess.cookies.get_dict())
        self.assertEqual(ctx._client.ua, self.source.ua)
        self.assertEqual(ctx._client.jpush, self.source.jpush)
        self.assertEqual(ctx._client.token, self.source.token)
        self.assertEqual(ctx._client.cfg["uuid"], self.source.cfg["uuid"])
        self.assertEqual(ctx._client.delay, 0)
        self.assertEqual(self.router.calls[0]["timeout"], 3.0)
        self.assertEqual(ctx._client.timeout, 20)
        self.assertEqual(self.source.delay, 1.2)
        self.assertEqual(self.source.cfg, before_cfg)
        self.builder.assert_called_once_with(self.form, "测试姓名")
        self.assertNotIn("mock-secret", repr(ctx))
        self.assertFalse(hasattr(ctx, "to_dict"))
        self.assertFalse(hasattr(ctx, "__dict__"))

    def test_prefetched_submit_has_no_preceding_form_detail_or_sleep(self):
        ctx = self.prepare()
        events = []

        def on_send(event):
            self.assertEqual(len(self.router.submitted()), 0)
            self.assertEqual(set(event), {"wall", "monotonic", "timestamp", "activity_id"})
            events.append(event)

        with patch.object(dmclient.time, "sleep") as sleep:
            result = signup.submit(ctx, on_send)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual([c["path"] for c in self.router.calls], [PATH_SIGNUP_INFO, PATH_SIGNUP_SUBMIT])
        sleep.assert_not_called()
        self.assertEqual(events, [{"wall": self.clock.now(), "monotonic": self.clock.monotonic(),
                                   "timestamp": 1_700_000_000_125, "activity_id": "test-001"}])
        params = self.router.submitted()[0]["params"]
        self.assertEqual(set(params), {"activityId", "data", "remark", "useNumber", "jPushId",
                                       "uid", "token", "version", "timestamp", "signToken"})
        self.assertEqual(params["data"], original_builder()(self.form, "测试姓名"))
        self.assertEqual(params["useNumber"], "0")
        self.assertNotIn("timestamp", self.router.calls[0]["params"])
        self.assertEqual(params["jPushId"], self.source.jpush)

    def test_failed_prefetch_11111_falls_back_to_original_read_at_submission(self):
        self.router.respond(PATH_SIGNUP_INFO, error("11111", "暂不能获取"), ok(self.form))
        ctx = self.prepare()
        self.assertFalse(ctx.prefetched)
        self.assertIsNone(ctx.prepared_at)
        self.builder.assert_not_called()
        self.assertIn("到点按原版读取", ctx.note)
        self.assertNotIn("必填", ctx.note)
        result = signup.submit(ctx)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual([c["path"] for c in self.router.calls],
                         [PATH_SIGNUP_INFO, PATH_SIGNUP_INFO, PATH_SIGNUP_SUBMIT])
        self.assertEqual([c["timeout"] for c in self.router.calls], [3.0, 20, 20])
        self.builder.assert_called_once_with(self.form, "测试姓名")

    def test_prefetch_timeout_keeps_context_and_restores_original_timeout(self):
        self.router.respond(PATH_SIGNUP_INFO, requests.Timeout("mock-secret-token"), ok(self.form))
        ctx = self.prepare()
        self.assertFalse(ctx.prefetched)
        self.assertIsNone(ctx.prepared_at)
        self.assertIn("到点按原版读取", ctx.note)
        self.assertNotIn("mock-secret-token", ctx.note)
        self.assertEqual(ctx._client.timeout, 20)
        self.assertEqual(signup.submit(ctx)["status"], "pending_confirmation")
        self.assertEqual([c["timeout"] for c in self.router.calls], [3.0, 20, 20])

    def test_failed_fallback_never_becomes_empty_form_or_missing_fields_guess(self):
        self.router.respond(PATH_SIGNUP_INFO, error("11111", "平台原始说明"), error("11111", "平台原始说明"))
        ctx = self.prepare()
        result = signup.submit(ctx)
        self.assertEqual((result["status"], result["code"]), ("failed", "11111"))
        self.assertIn("平台原始说明", result["msg"])
        self.assertNotIn("必填", result["msg"])
        self.assertFalse(self.router.submitted())
        self.builder.assert_not_called()

    def test_read_only_confirmation_requires_positive_personal_id(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, ok({"signUpId": 123}))
        self.assertEqual(signup.submit(ctx)["status"], "pending_confirmation")
        self.router.respond(PATH_DETAIL, ok({"signUpId": 0, "injoin": True}), ok({"signUpId": "987"}))
        self.assertEqual(signup.confirm(ctx)["status"], "unknown")
        result = signup.confirm(ctx)
        self.assertEqual((result["status"], result["signup_id"]), ("confirmed", "987"))
        self.assertEqual(signup.submit(ctx)["status"], "confirmed")
        self.assertEqual(len(self.router.submitted()), 1)
        for call in self.router.calls:
            if call["path"] == PATH_DETAIL:
                self.assertNotIn("timestamp", call["params"])

    def test_confirmation_rejects_a_different_activity_record(self):
        ctx = self.prepare()
        signup.submit(ctx)
        for field in ("activityId", "id"):
            with self.subTest(field=field):
                self.router.respond(PATH_DETAIL, ok({field: "another-activity", "signUpId": "987"}))
                result = signup.confirm(ctx)
                self.assertEqual((result["status"], result["code"]), ("unknown", "ACTIVITY_MISMATCH"))
        self.router.respond(PATH_DETAIL, ok({"activityId": "test-001", "signUpId": "987"}))
        self.assertEqual(signup.confirm(ctx)["status"], "confirmed")
        self.assertEqual(len(self.router.submitted()), 1)

    def test_unknown_submit_timeout_is_not_repeated_even_after_empty_confirmation(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, requests.Timeout("url/token=mock-secret-token"))
        first = signup.submit(ctx)
        self.assertEqual(first["status"], "unknown")
        self.assertNotIn("mock-secret-token", first["msg"])
        self.assertEqual(signup.submit(ctx), first)
        self.assertEqual(signup.confirm(ctx)["status"], "unknown")
        self.assertEqual(signup.submit(ctx), first)
        self.assertEqual(len(self.router.submitted()), 1)

    def test_non_json_after_send_is_unknown_and_not_retried(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, Response(json.JSONDecodeError("invalid", "not json", 0)))
        result = signup.submit(ctx)
        self.assertEqual((result["status"], result["code"]), ("unknown", "BADJSON"))
        signup.submit(ctx)
        self.assertEqual(len(self.router.submitted()), 1)

    def test_already_joined_still_requires_confirmation(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, error("2000011", "你已经报过这个活动"))
        self.assertEqual(signup.submit(ctx)["status"], "pending_confirmation")
        self.assertEqual(signup.confirm(ctx)["status"], "unknown")
        signup.submit(ctx)
        self.assertEqual(len(self.router.submitted()), 1)

    def test_full_auth_and_business_errors_stop_without_automatic_retry(self):
        for code, expected in (("2000166", "full"), ("10005", "auth_required"), ("11111", "failed")):
            with self.subTest(code=code):
                ctx = self.prepare(code)
                self.router.respond(PATH_SIGNUP_SUBMIT, error(code, "平台原始说明"))
                before = len(self.router.submitted())
                self.assertEqual(signup.submit(ctx)["status"], expected)
                self.assertEqual(signup.submit(ctx)["status"], expected)
                self.assertEqual(len(self.router.submitted()), before + 1)

    def test_only_explicit_not_open_can_be_submitted_again_without_reloading_form(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, error("2000999", "报名尚未开始"), ok())
        self.assertEqual(signup.submit(ctx)["status"], "not_open")
        self.assertEqual(signup.submit(ctx)["status"], "pending_confirmation")
        self.assertEqual(len(self.router.submitted()), 2)
        self.assertEqual([c["path"] for c in self.router.calls].count(PATH_SIGNUP_INFO), 1)

    def test_http_429_keeps_retry_after_and_stops(self):
        ctx = self.prepare()
        self.router.respond(PATH_SIGNUP_SUBMIT, Response({}, status=429, headers={"Retry-After": "3"}))
        result = signup.submit(ctx)
        self.assertEqual((result["status"], result["retry_after"]), ("rate_limited", 3.0))
        self.assertEqual(signup.submit(ctx), result)
        self.assertEqual(len(self.router.submitted()), 1)

    def test_callback_exception_propagates_and_prevents_the_post(self):
        ctx = self.prepare()
        failure = RuntimeError("task cancelled")

        def cancel(event):
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            signup.submit(ctx, cancel)
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.router.submitted())
        signup.close(ctx)
        self.assertEqual(signup.submit(ctx)["status"], "cancelled")

    def test_token_change_and_replaced_hub_block_before_post(self):
        current = [True]
        ctx = self.prepare(is_current=lambda: current[0])
        current[0] = False
        self.assertEqual(signup.submit(ctx)["code"], "AUTH_CHANGED")
        self.assertFalse(self.router.submitted())
        current[0] = True
        ctx2 = self.prepare()
        self.source.token = "different-mock-token"
        self.assertEqual(signup.submit(ctx2)["code"], "AUTH_CHANGED")
        self.assertFalse(self.router.submitted())

    def test_identity_is_rechecked_in_actual_send_hook(self):
        current = [True]
        ctx = self.prepare(is_current=lambda: current[0])

        def encrypt_and_replace(params):
            current[0] = False
            return copy.deepcopy(params)

        with patch.object(dmclient.C, "make_d", side_effect=encrypt_and_replace):
            result = signup.submit(ctx)
        self.assertEqual(result["code"], "AUTH_CHANGED")
        self.assertFalse(self.router.submitted())

    def test_token_change_during_clone_construction_cannot_validate_stale_credentials(self):
        original_factory = self.router.session

        def change_token_while_constructing():
            self.source.token = "changed-during-clone"
            return original_factory()

        with patch.object(dmclient.requests, "Session", change_token_while_constructing):
            ctx = self.prepare()
        self.assertFalse(ctx.prefetched)
        self.assertIsNone(ctx.prepared_at)
        self.assertEqual(signup.submit(ctx)["code"], "AUTH_CHANGED")
        self.assertFalse(self.router.calls)

    def test_concurrent_calls_to_same_context_only_submit_once(self):
        ctx = self.prepare()
        entered, release = threading.Event(), threading.Event()
        results = []

        def slow(call):
            entered.set()
            self.assertTrue(release.wait(2))
            return ok()

        self.router.respond(PATH_SIGNUP_SUBMIT, slow)
        threads = [threading.Thread(target=lambda: results.append(signup.submit(ctx))) for _ in range(2)]
        threads[0].start()
        self.assertTrue(entered.wait(2))
        threads[1].start()
        release.set()
        for t in threads:
            t.join(2)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.router.submitted()), 1)

    def test_other_activity_uses_separate_session_and_is_not_blocked(self):
        ctx_a, ctx_b = self.prepare("A"), self.prepare("B")
        entered, release = threading.Event(), threading.Event()
        results = []

        def slow(call):
            entered.set()
            self.assertTrue(release.wait(2))
            return ok()

        self.router.respond(PATH_SIGNUP_SUBMIT, slow, ok())
        t = threading.Thread(target=lambda: results.append(signup.submit(ctx_a)))
        t.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(signup.submit(ctx_b)["status"], "pending_confirmation")
            self.assertTrue(t.is_alive())
            calls = self.router.submitted()
            self.assertIsNot(calls[0]["session"], calls[1]["session"])
        finally:
            release.set()
            t.join(2)
        self.assertFalse(t.is_alive())

    def test_close_blocks_future_reads_and_writes(self):
        ctx = self.prepare()
        count = len(self.router.calls)
        signup.close(ctx)
        self.assertEqual(signup.submit(ctx)["status"], "cancelled")
        self.assertEqual(signup.confirm(ctx)["status"], "unknown")
        self.assertEqual(len(self.router.calls), count)
        self.assertTrue(ctx._client.sess.closed)
        self.assertFalse(self.source.sess.closed)

    def test_original_client_without_hooks_retains_timestamp_and_error_behavior(self):
        self.source.delay = 0
        self.assertFalse(hasattr(self.source, "before_request"))
        self.assertFalse(hasattr(self.source, "timestamp_clock"))
        with patch.object(dmclient.time, "time", return_value=1234567.125):
            self.source.submit("default-test", data="[]")
        call = self.router.submitted()[0]
        self.assertEqual(call["params"]["timestamp"], "1234567125")
        self.router.respond(PATH_SIGNUP_INFO, error("11111", "读取阶段原始说明"))
        with self.assertRaises(dmclient.ApiError) as caught:
            self.source.signup_form("default-test")
        self.assertEqual((caught.exception.code, caught.exception.msg), ("11111", "读取阶段原始说明"))


if __name__ == "__main__":
    unittest.main()
