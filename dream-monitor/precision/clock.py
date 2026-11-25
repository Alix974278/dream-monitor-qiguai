"""内部 UTC 基准、可冻结的单调时钟与保守的时间质量策略。

可信的多提供商 NTP 结果由应用显式 apply；本模块不改 Windows 系统时间。
HTTP Date 仍然只是秒级诊断，绝不作为自动毫秒校时依据。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import ssl


MAX_MANUAL_OFFSET_MS = 300_000.0
MAX_NTP_OFFSET_MS = 86_400_000.0
SYNC_MAX_AGE_SECONDS = 900.0
SYNC_MAX_UNCERTAINTY_MS = 100.0
OSCILLATOR_DRIFT_PPM = 50.0
DATE_NOTICE = (
    "HTTPS Date 只有秒级精度，也可能来自 CDN 或代理；"
    "这里只给出保守估计，不代表报名服务器的精确时钟，不会自动应用。"
)


def _finite(value: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数值")
    return result


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


@dataclass(frozen=True)
class FrozenClock:
    """已经冻结的 UTC/monotonic 映射；后来 apply 的校时不会改变它。"""

    anchor_wall: float
    anchor_monotonic: float
    offset_ms: float
    generation: int
    source: str
    sampled_at: float | None = None
    sampled_monotonic: float | None = None
    uncertainty_at_sample_ms: float | None = None
    providers: tuple[str, ...] = ()
    rtt_ms: float | None = None
    max_age_seconds: float = SYNC_MAX_AGE_SECONDS
    max_uncertainty_ms: float = SYNC_MAX_UNCERTAINTY_MS
    drift_ppm: float = OSCILLATOR_DRIFT_PPM
    _monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def monotonic(self) -> float:
        return float(self._monotonic())

    def now(self) -> float:
        return self.epoch_from_monotonic(self.monotonic())

    def deadline_monotonic(self, epoch: float) -> float:
        return self.anchor_monotonic + _finite(epoch, "计划时间") - self.anchor_wall - self.offset_ms / 1000.0

    deadline = deadline_monotonic

    def epoch_from_monotonic(self, when: float) -> float:
        return self.anchor_wall + _finite(when, "单调时间") - self.anchor_monotonic + self.offset_ms / 1000.0

    def quality(self, horizon_seconds: float = 0.0) -> dict:
        horizon = _finite(horizon_seconds, "校时有效时长")
        if horizon < 0:
            raise ValueError("校时有效时长不能为负数")
        mono = self.monotonic()
        age = None if self.sampled_monotonic is None else max(0.0, mono - self.sampled_monotonic)
        uncertainty = None if age is None or self.uncertainty_at_sample_ms is None else (
            self.uncertainty_at_sample_ms + age * self.drift_ppm / 1000.0)
        projected = None if uncertainty is None else uncertainty + horizon * self.drift_ppm / 1000.0
        valid_for = 0.0
        if uncertainty is not None and age is not None:
            error_budget = (self.max_uncertainty_ms - uncertainty) * 1000.0 / self.drift_ppm if self.drift_ppm > 0 else math.inf
            valid_for = max(0.0, min(self.max_age_seconds - age, error_budget))
        if self.source != "ntp" or self.sampled_monotonic is None or self.uncertainty_at_sample_ms is None:
            ready, reason = False, "尚未取得可用的多提供商 NTP 校时，请等待自动同步。"
        elif len(set(self.providers)) < 2:
            ready, reason = False, "有效来源不足两个独立提供商，不能自动信任单一来源。"
        elif mono < self.sampled_monotonic - 0.01:
            ready, reason = False, "单调时间出现异常，当前校时不可用。"
        elif age is not None and age > self.max_age_seconds:
            ready, reason = False, "NTP 样本已经过期，请重新同步。"
        elif age is not None and age + horizon > self.max_age_seconds:
            ready, reason = False, "现有样本不能覆盖所需时间，请在冻结计划前重新同步。"
        elif projected is None or projected > self.max_uncertainty_ms:
            ready, reason = False, "网络与本机漂移带来的时间不确定度过大，请重新同步。"
        else:
            ready, reason = True, "不同提供商的 NTP 样本已通过交叉校验。"
        return {
            "ready": ready, "reason": reason, "source": self.source,
            "generation": self.generation, "offset_ms": self.offset_ms,
            "age_seconds": age, "sample_age_seconds": age,
            "uncertainty_ms": uncertainty, "projected_uncertainty_ms": projected,
            "horizon_seconds": horizon, "valid_for_seconds": valid_for if ready else 0.0,
            "max_age_seconds": self.max_age_seconds, "max_uncertainty_ms": self.max_uncertainty_ms,
            "provider_count": len(set(self.providers)),
        }

    def ready_for_seconds(self, horizon_seconds: float = 0.0) -> bool:
        return bool(self.quality(horizon_seconds)["ready"])

    def to_dict(self) -> dict:
        return {**self.quality(), "sampled_at": self.sampled_at,
                "sampled_monotonic": self.sampled_monotonic, "rtt_ms": self.rtt_ms,
                "providers": list(self.providers)}


class Clock:
    """An epoch/monotonic anchor that is not moved by later OS clock steps.

    Automatic samples may update the live mapping used for distant tasks.
    Freeze a ``FrozenClock`` before the critical window; its epoch/monotonic
    conversion is unaffected by later samples or operating-system clock steps.
    The application coordinates sample application and freezing with one lock.
    """

    def __init__(
        self,
        manual_offset_ms: float = 0.0,
        *,
        wall_time: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._wall = wall_time or time.time
        self._mono = monotonic or time.monotonic
        self._lock = threading.RLock()
        before = self._mono()
        self._anchor_wall = float(self._wall())
        after = self._mono()
        self._anchor_mono = (before + after) / 2.0
        self._offset_ms = 0.0
        self._manual_offset_ms = 0.0
        self._generation = 0
        self._sync: dict | None = None
        self._provider_details: tuple[dict, ...] = ()
        self.set_manual_offset_ms(manual_offset_ms)

    def monotonic(self) -> float:
        return float(self._mono())

    def system_now(self) -> float:
        return float(self._wall())

    def now(self) -> float:
        with self._lock:
            return self._anchor_wall + self.monotonic() - self._anchor_mono + self._offset_ms / 1000.0

    def base_now(self) -> float:
        """未加校正的固定基准；NTP 采样始终相对此基准，不累加旧 offset。"""
        return self.epoch_from_monotonic(self.monotonic(), offset_ms=0.0)

    @property
    def offset_ms(self) -> float:
        with self._lock:
            return self._offset_ms

    @property
    def manual_offset_ms(self) -> float:
        with self._lock:
            return self._manual_offset_ms

    def set_manual_offset_ms(self, offset_ms: float) -> dict:
        value = _finite(offset_ms, "手动时钟校正")
        if abs(value) > MAX_MANUAL_OFFSET_MS:
            raise ValueError("手动校正请保持在正负 5 分钟内；更大偏差应先修正电脑日期和时区")
        with self._lock:
            self._offset_ms = value
            self._manual_offset_ms = value
            self._sync = None
            self._provider_details = ()
            self._generation += 1
        return self.status()

    def freeze(self) -> FrozenClock:
        with self._lock:
            sync = self._sync or {}
            return FrozenClock(
                anchor_wall=self._anchor_wall, anchor_monotonic=self._anchor_mono,
                offset_ms=self._offset_ms, generation=self._generation,
                source="ntp" if self._sync else ("manual" if self._manual_offset_ms else "system"),
                sampled_at=sync.get("sampled_at"), sampled_monotonic=sync.get("sampled_monotonic"),
                uncertainty_at_sample_ms=sync.get("uncertainty_ms"), providers=sync.get("providers", ()),
                rtt_ms=sync.get("rtt_ms"), _monotonic=self._mono,
            )

    def quality(self, horizon_seconds: float = 0.0) -> dict:
        return self.freeze().quality(horizon_seconds)

    def ready_for_seconds(self, horizon_seconds: float = 0.0) -> bool:
        return self.freeze().ready_for_seconds(horizon_seconds)

    def apply_sync(self, result: Any) -> dict:
        """应用交叉校验结果。调用者应与任务冻结共用互斥锁。

        服务可重新创建，因此不把结果的 generation 当成全局序号；按样本单调
        时间防止旧结果覆盖新结果，本 Clock 的 generation 始终独立递增。
        """
        if _value(result, "status") != "ready":
            raise ValueError("只有通过交叉校验的 NTP 结果才能自动应用")
        offset = _finite(_value(result, "offset_ms"), "NTP 校正")
        if abs(offset) > MAX_NTP_OFFSET_MS:
            raise ValueError("电脑日期偏差超过一天，不能自动信任此次校时")
        uncertainty = _finite(_value(result, "uncertainty_ms"), "NTP 不确定度")
        if uncertainty < 0:
            raise ValueError("NTP 不确定度不能为负数")
        uncertainty = max(1.0, uncertainty)
        sample_mono = _finite(_value(result, "sampled_monotonic"), "NTP 样本单调时间")
        sampled_at = _finite(_value(result, "sampled_at"), "NTP 样本时间")
        rtt = _finite(_value(result, "rtt_ms"), "NTP 往返时间")
        if rtt < 0:
            raise ValueError("NTP 往返时间不能为负数")
        provider_details = []
        providers = []
        for provider in _value(result, "providers", ()):
            key = str(provider if isinstance(provider, str) else _value(provider, "provider", "")).strip().lower()
            if not key:
                raise ValueError("NTP 来源信息不完整")
            providers.append(key)
            if hasattr(provider, "to_dict"):
                provider_details.append(provider.to_dict())
            elif isinstance(provider, Mapping):
                provider_details.append(dict(provider))
            else:
                provider_details.append({"provider": key})
        if len(set(providers)) < 2:
            raise ValueError("至少需要两个独立提供商通过交叉校验")
        with self._lock:
            candidate = FrozenClock(self._anchor_wall, self._anchor_mono, offset, self._generation + 1, "ntp",
                                    sampled_at, sample_mono, uncertainty, tuple(sorted(set(providers))), rtt,
                                    _monotonic=self._mono)
            quality = candidate.quality()
            if not quality["ready"]:
                raise ValueError(quality["reason"])
            if self._sync is not None and sample_mono <= self._sync["sampled_monotonic"]:
                return {**self.status(), "applied": False, "apply_reason": "已有更新或相同时间的校时结果。"}
            self._offset_ms = offset
            self._manual_offset_ms = 0.0
            self._generation += 1
            self._sync = {"sampled_at": sampled_at, "sampled_monotonic": sample_mono,
                          "uncertainty_ms": uncertainty, "providers": tuple(sorted(set(providers))), "rtt_ms": rtt}
            self._provider_details = tuple(provider_details)
            return {**self.status(), "applied": True, "apply_reason": "已应用多提供商 NTP 校正，未修改系统时间。"}

    def deadline_monotonic(self, epoch: float) -> float:
        value = _finite(epoch, "计划时间")
        with self._lock:
            return self._anchor_mono + value - self._anchor_wall - self._offset_ms / 1000.0

    # A small convenience alias for callers which already use Clock.deadline().
    deadline = deadline_monotonic

    def epoch_from_monotonic(self, when: float, *, offset_ms: float | None = None) -> float:
        value = _finite(when, "单调时间")
        with self._lock:
            offset = self._offset_ms if offset_ms is None else _finite(offset_ms, "时钟校正")
            return self._anchor_wall + value - self._anchor_mono + offset / 1000.0

    def wait_until(self, deadline: float, cancel_event) -> bool:
        """Wait directly for a deadline; return False when cancelled.

        Event.wait makes cancellation immediate.  There is no fixed polling
        interval, busy spin, or early submission.  The loop only handles an
        interrupted/spurious wait or the platform's maximum timeout.
        """
        target = _finite(deadline, "截止时间")
        while not cancel_event.is_set():
            remaining = target - self.monotonic()
            if remaining <= 0:
                return True
            if cancel_event.wait(min(remaining, threading.TIMEOUT_MAX)):
                return False
        return False

    def status(self) -> dict:
        mono = self.monotonic()
        system_now = self.system_now()
        with self._lock:
            anchored_now = self._anchor_wall + mono - self._anchor_mono
            drift_ms = (system_now - anchored_now) * 1000.0
            quality = self.freeze().quality()
            return {
                **quality,
                "now": anchored_now + self._offset_ms / 1000.0,
                "system_now": system_now,
                "manual_offset_ms": self._manual_offset_ms,
                "offset_ms": self._offset_ms,
                "offset_vs_system_ms": (anchored_now + self._offset_ms / 1000.0 - system_now) * 1000.0,
                "system_drift_ms": round(drift_ms, 3),
                "anchor_age_seconds": max(0.0, mono - self._anchor_mono),
                "source": "ntp" if self._sync else ("manual" if self._manual_offset_ms else "system"),
                "sampled_at": (self._sync or {}).get("sampled_at"),
                "sampled_monotonic": (self._sync or {}).get("sampled_monotonic"),
                "rtt_ms": (self._sync or {}).get("rtt_ms"),
                "providers": [dict(provider) for provider in self._provider_details],
                "sources": [{"provider": provider["provider"], "host": provider["host"]}
                            for provider in self._provider_details if provider.get("host")],
                "notice": (
                    "NTP 校准通用 UTC；已冻结计划保持原单调截止时间，不会被后来同步移动。"
                    if self._sync else
                    "尚未取得多源校时结果；当前显示不代表已经校准，也不代表报名服务器的内部时间。"
                ),
            }


def estimate_http_date(
    date_header: str,
    *,
    sent_wall: float,
    sent_monotonic: float,
    received_monotonic: float,
    received_wall: float | None = None,
    source: str = "HTTPS Date",
    age_header: str | None = None,
) -> dict:
    """Estimate offset from one Date header, without applying it.

    The centre of the advertised second is used only as an estimate.  The
    reported uncertainty is at least +/-1000 ms, plus half the round trip.
    Unknown proxy behaviour can introduce additional, unbounded error; the
    notice deliberately does not claim this is an accuracy guarantee.
    """
    wall = _finite(sent_wall, "请求开始时间")
    before = _finite(sent_monotonic, "请求开始单调时间")
    after = _finite(received_monotonic, "请求结束单调时间")
    rtt = after - before
    if rtt < 0:
        raise ValueError("单调时间顺序错误")
    if received_wall is not None:
        wall_elapsed = _finite(received_wall, "请求结束时间") - wall
        if abs(wall_elapsed - rtt) > 0.25:
            raise ValueError("采样期间电脑时钟发生跳变，请重新采样")
    if age_header is not None:
        try:
            age = float(age_header)
        except (TypeError, ValueError) as exc:
            raise ValueError("响应缓存 Age 无法解释，不能用于估计时间") from exc
        if not math.isfinite(age) or age > 0 or age < 0:
            raise ValueError("响应来自缓存，不能用旧的 Date 估计当前时间")
    try:
        parsed = parsedate_to_datetime(str(date_header).strip())
        if parsed.tzinfo is None:
            raise ValueError("Date 缺少时区")
        server_second = parsed.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("响应没有可用的 HTTP Date") from exc
    midpoint = wall + rtt / 2.0
    return {
        "offset_ms": round((server_second + 0.5 - midpoint) * 1000.0, 3),
        "uncertainty_ms": round(1000.0 + rtt * 500.0, 3),
        "rtt_ms": round(rtt * 1000.0, 3),
        "sampled_at": wall + rtt,
        "server_date": parsed.isoformat(),
        "source": str(source),
        "precision": "seconds",
        "applied": False,
        "notice": DATE_NOTICE,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sample_https_date(url: str = "https://www.5idream.net/", *, timeout: float = 4.0) -> dict:
    """Read a public official HTTPS response header, without cookies or login.

    Redirects are not followed.  No credential-bearing URLs or arbitrary
    destinations are accepted.  This helper is optional; constructing Clock or
    importing this module never makes a network request.
    """
    parsed = urlsplit(str(url))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.5idream.net", "appdmkj.5idream.net"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("校时只读取官方 HTTPS 首页响应头")
    seconds = _finite(timeout, "请求超时")
    if not 0 < seconds <= 10:
        raise ValueError("校时请求超时需在 0 到 10 秒之间")
    request = Request(url, method="HEAD", headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    before = time.monotonic()
    wall = time.time()
    try:
        response = opener.open(request, timeout=seconds)
    except HTTPError as exc:
        # Error/redirect responses may still provide Date.  Only inspect their
        # headers; never follow their location or read a response body.
        response = exc
    try:
        after = time.monotonic()
        wall_after = time.time()
        headers = response.headers
        return estimate_http_date(
            headers.get("Date", ""),
            sent_wall=wall,
            sent_monotonic=before,
            received_monotonic=after,
            received_wall=wall_after,
            source=f"https://{parsed.hostname}/",
            age_header=headers.get("Age"),
        )
    finally:
        response.close()
