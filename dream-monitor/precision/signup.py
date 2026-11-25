"""A small timed-signup layer around the original DreamClient and form builder.

Login, activity discovery, form interpretation, request parameters and encryption
remain in the original project. Only this private client skips the ordinary
inter-request delay. The scheduler owns deadlines, bounded retries and task
generations; this module never retries a possibly delivered submission.
"""

from __future__ import annotations

import copy
import math
import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Callable

from dmclient import ApiError, AuthExpired, DreamClient, PATH_SIGNUP_SUBMIT


class _Stopped(Exception):
    def __init__(self, status: str, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class _Prepared:
    """Intentionally opaque: no dataclass/asdict/to_dict containing credentials."""

    __slots__ = (
        "prepared_at", "prefetched", "note", "_hub", "_source", "_uid",
        "_token", "_ua", "_jpush", "_client", "_aid", "_builder", "_now",
        "_monotonic", "_is_current", "_data", "_result", "_lock", "_closed",
        "_session_closed",
    )

    def __repr__(self) -> str:
        return f"<PreparedSignup prefetched={self.prefetched}>"


def _result(status: str, code: str = "", msg: str = "", signup_id: str = "",
            retry_after: float | None = None) -> dict:
    return {"status": status, "code": code, "msg": msg,
            "signup_id": signup_id, "retry_after": retry_after}


def _code(value) -> str:
    value = str(value or "")
    return value if re.fullmatch(r"[-A-Za-z0-9_]{1,40}", value) else "API_ERROR"


def _message(ctx: _Prepared, value) -> str:
    """Keep platform explanations, but do not emit known local credentials."""
    text = str(value or "")
    cfg = ctx._source.cfg
    secrets = [ctx._token, ctx._uid, ctx._jpush]
    secrets.extend(str(cfg.get(k) or "") for k in ("account", "password", "uuid"))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "[已隐藏]")
    return text[:240]


def _assert_current(ctx: _Prepared) -> None:
    if ctx._closed.is_set():
        raise _Stopped("cancelled", "CANCELLED", "任务已关闭，未继续发送报名请求。")
    source = getattr(getattr(ctx._hub, "mon", None), "client", None)
    current = (source is ctx._source and str(source.uid) == ctx._uid
               and str(source.token) == ctx._token and source.ua == ctx._ua
               and str(source.jpush) == ctx._jpush)
    if current and ctx._is_current is not None:
        try:
            current = bool(ctx._is_current())
        except Exception:
            current = False
    if not current:
        raise _Stopped("auth_required", "AUTH_CHANGED", "账号或登录会话已变化，请重新设置此任务。")


def _stop_result(exc: _Stopped) -> dict:
    return _result(exc.status, exc.code, exc.message)


def _close_session(ctx: _Prepared) -> None:
    if not ctx._session_closed:
        ctx._session_closed = True
        try:
            ctx._client.sess.close()
        except Exception:
            pass


def _read_form(ctx: _Prepared) -> None:
    _assert_current(ctx)
    info = ctx._client.signup_form(ctx._aid)
    _assert_current(ctx)
    # Use exactly the caller's original _build_signup_data, including its normal
    # no-fields representation. A failed request never becomes an empty form.
    ctx._data = ctx._builder(info, ctx._hub.name or "")
    ctx.prefetched = True
    ctx.prepared_at = ctx._now()


def prepare(hub, aid: str, build_data: Callable, clock,
            *, is_current: Callable[[], bool] | None = None) -> _Prepared:
    """Read the original form ahead of time; a failed prefetch remains usable.

    `clock` supplies now()/monotonic(), or may be a callable returning UTC epoch
    seconds. `is_current` is an optional in-memory Hub/generation predicate.
    It must not perform network or database work.
    """
    aid = str(aid or "").strip()
    if not aid:
        raise ValueError("缺少活动 ID")
    source = hub.mon.client
    source_uid, source_token = str(source.uid), str(source.token)
    source_ua, source_jpush = source.ua, str(source.jpush)
    cfg = copy.deepcopy(source.cfg)
    cfg.update(uid=source_uid, token=source_token,
               version=source.version, jPushId=source_jpush, request_delay=0)
    client = DreamClient(cfg)
    # _ua_json has time-dependent defaults. Reuse the exact current fingerprint,
    # not a newly generated variant, and retain any ordinary session cookies.
    client.ua = source_ua
    client.sess.headers.clear()
    client.sess.headers.update(source.sess.headers)
    client.sess.cookies.update(copy.deepcopy(source.sess.cookies))
    client.sess.trust_env = source.sess.trust_env
    client.delay = 0

    ctx = _Prepared()
    ctx._hub, ctx._source = hub, source
    ctx._uid, ctx._token = source_uid, source_token
    ctx._ua, ctx._jpush = source_ua, source_jpush
    ctx._client, ctx._aid, ctx._builder = client, aid, build_data
    ctx._now = getattr(clock, "now", clock if callable(clock) else time.time)
    ctx._monotonic = getattr(clock, "monotonic", time.monotonic)
    ctx._is_current = is_current
    ctx._data, ctx._result = None, None
    ctx._lock, ctx._closed = threading.Lock(), threading.Event()
    ctx._session_closed = False
    ctx.prepared_at, ctx.prefetched, ctx.note = None, False, ""
    client.timestamp_clock = ctx._now
    original_timeout = client.timeout
    try:
        try:
            configured_timeout = float(original_timeout)
        except (TypeError, ValueError):
            configured_timeout = 3.0
        client.timeout = min(3.0, configured_timeout) if configured_timeout > 0 else 3.0
        _read_form(ctx)
        ctx.note = "已按原版读取报名表单，到点直接提交。"
    except _Stopped as exc:
        ctx._result = _stop_result(exc)
        ctx.note = exc.message
    except Exception as exc:
        suffix = f"（业务码 {_code(exc.code)}）" if isinstance(exc, ApiError) else ""
        ctx.note = f"提前读取报名表单未成功{suffix}；到点按原版读取报名表单后再提交。"
    finally:
        # A short speculative read must not change the original timeout used by
        # a deadline-time fallback, submission, or later read-only confirmation.
        client.timeout = original_timeout
    return ctx


def _retry_after(exc: Exception, now: float) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _error(ctx: _Prepared, exc: Exception, *, sent: bool, form: bool = False) -> dict:
    if isinstance(exc, _Stopped):
        return _stop_result(exc)
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    code = _code(exc.code) if isinstance(exc, ApiError) else "NETWORK_ERROR"
    if http_status == 429 or code == "429":
        return _result("rate_limited", "429", "平台限流，已停止继续提交。",
                       retry_after=_retry_after(exc, ctx._now()))
    if isinstance(exc, AuthExpired) or code in {"10004", "10005", "10007"}:
        return _result("auth_required", code, "登录会话已失效，请在原登录页处理后重新设置任务。")
    if isinstance(exc, ApiError) and code != "BADJSON":
        msg = _message(ctx, exc.msg)
        if not form and code == "2000011":
            return _result("pending_confirmation", code, "平台提示已报名，正在只读核验个人报名记录。")
        if code == "2000166":
            return _result("full", code, msg or "平台返回名额已满。")
        if any(s in msg for s in ("报名未开始", "报名尚未开始", "还未开始报名", "还没开始报名",
                                   "不在报名时间", "未到报名时间", "尚未到报名时间")):
            return _result("not_open", code, msg)
        prefix = "读取报名表单失败：" if form else "报名失败："
        return _result("failed", code, prefix + (msg or f"平台业务码 {code}"))
    if sent:
        return _result("unknown", code, "请求已尝试发送，但未获得可确认的结果；仅查询个人报名记录，不重复提交。")
    return _result("failed", code, "读取报名表单未成功，本次未发送报名提交。")


def _signup_id(data) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get("signUpId")
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return text if text.isascii() and text.isdigit() and int(text) > 0 else ""


def submit(ctx: _Prepared, on_send: Callable[[dict], None] | None = None) -> dict:
    """Submit once. Only an explicit not_open result permits another call.

    An exception from on_send propagates unchanged and prevents the POST. The
    callback receives timings only, never signed parameters or credentials.
    """
    with ctx._lock:
        try:
            _assert_current(ctx)
            if ctx._result is not None and ctx._result["status"] != "not_open":
                return dict(ctx._result)
            if not ctx.prefetched:
                try:
                    _read_form(ctx)
                    ctx.note = "已在提交前沿用原版读取报名表单。"
                except Exception as exc:
                    ctx._result = _error(ctx, exc, sent=False, form=True)
                    return dict(ctx._result)
            sent = False
            callback_error = None

            def before_request(path, params):
                nonlocal sent, callback_error
                if path != PATH_SIGNUP_SUBMIT:
                    return
                _assert_current(ctx)
                event = {"wall": ctx._now(), "monotonic": ctx._monotonic(),
                         "timestamp": int(params["timestamp"]), "activity_id": ctx._aid}
                if on_send is not None:
                    try:
                        on_send(event)
                    except BaseException as exc:
                        callback_error = exc
                        raise
                _assert_current(ctx)
                sent = True

            ctx._client.before_request = before_request
            try:
                data = ctx._client.submit(ctx._aid, data=ctx._data)
                ctx._result = _result("pending_confirmation", "100",
                                      "提交接口已接受，正在只读核验个人报名记录。",
                                      signup_id=_signup_id(data))
            except Exception as exc:
                if exc is callback_error:
                    raise
                ctx._result = _error(ctx, exc, sent=sent)
            finally:
                ctx._client.before_request = None
            return dict(ctx._result)
        except _Stopped as exc:
            ctx._result = _stop_result(exc)
            return dict(ctx._result)
        finally:
            if ctx._closed.is_set():
                _close_session(ctx)


def confirm(ctx: _Prepared) -> dict:
    """Only a positive personal signUpId in original detail confirms success."""
    with ctx._lock:
        try:
            _assert_current(ctx)
            data = ctx._client.detail(ctx._aid)
            _assert_current(ctx)
            if isinstance(data, dict):
                for key in ("activityId", "id"):
                    value = data.get(key)
                    if value is not None and str(value).strip() and str(value).strip() != ctx._aid:
                        return _result("unknown", "ACTIVITY_MISMATCH", "返回的活动编号与当前任务不一致，未判为报名成功。")
            sid = _signup_id(data)
            if sid:
                ctx._result = _result("confirmed", "100", "已核验个人报名记录。", signup_id=sid)
                return dict(ctx._result)
            return _result("unknown", "100", "尚未查到个人报名编号；结果未确认，不重复提交。")
        except _Stopped as exc:
            return _result("unknown", exc.code, exc.message)
        except Exception as exc:
            code = _code(exc.code) if isinstance(exc, ApiError) else "NETWORK_ERROR"
            return _result("unknown", code, "个人报名记录暂未查明；保留未知结果，不重复提交。")
        finally:
            if ctx._closed.is_set():
                _close_session(ctx)


def close(ctx: _Prepared) -> None:
    """Prevent future requests; never wait for a pending network response."""
    ctx._closed.set()
    if ctx._lock.acquire(blocking=False):
        try:
            _close_session(ctx)
        finally:
            ctx._lock.release()
