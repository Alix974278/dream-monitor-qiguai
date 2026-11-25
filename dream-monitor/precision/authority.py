"""Automatic time for the original app; this module never handles accounts.

Sampling is finite and independent of the signup workers. A task takes a
frozen monotonic mapping before its critical minute. Unavailable NTP sources
are displayed as a downgrade, not a new prerequisite for the original UI.
"""
from __future__ import annotations

import threading

from .clock import Clock
from .time_sync import TimeSyncService


class TimeAuthority:
    def __init__(self, *, clock=None, service_factory=TimeSyncService,
                 refresh_interval=300.0):
        self.clock = clock or Clock()
        self._factory = service_factory
        self._interval = max(30.0, float(refresh_interval))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._service = None
        self._seen = None
        self._next = 0.0
        self._leases = set()
        self._error = ""
        self._closed = False

    def start(self):
        with self._lock:
            if self._closed or self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="public-time", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.maintain()
            except Exception:
                with self._lock:
                    self._error = "本轮自动校时未完成，将按有限间隔重新尝试。"
                    self._next = self.clock.monotonic() + self._interval
                    self._close_service()
            self._stop.wait(0.5)

    def _close_service(self):
        service, self._service = self._service, None
        self._seen = None
        if service is not None:
            service.close()

    def maintain(self):
        with self._lock:
            if self._closed or self._leases:
                return
            if self._service is None:
                self._service = self._factory(self.clock)
            service = self._service
            result = service.latest_result
            if result is not None:
                key = (id(service), result.generation, result.sampled_monotonic)
                if key != self._seen:
                    self._seen = key
                    if result.status == "ready":
                        applied = self.clock.apply_sync(result)
                        if applied.get("applied") is not False:
                            self._error = ""
                    else:
                        self._error = str(result.message or "时间源未通过交叉校验。")[:250]
            status = service.snapshot().get("status")
            if status != "syncing" and self.clock.monotonic() >= self._next:
                service.start()
                self._next = self.clock.monotonic() + self._interval

    def freeze(self, key):
        with self._lock:
            self._leases.add(str(key))
            self._close_service()
            return self.clock.freeze()

    def release(self, key):
        with self._lock:
            had_leases = bool(self._leases)
            self._leases.discard(str(key))
            if had_leases and not self._leases:
                self._next = 0.0

    def state(self):
        with self._lock:
            raw = self.clock.status()
            ready = bool(raw.get("ready"))
            source = raw.get("source", "system")
            locked = bool(self._leases)
            syncing = self._service is not None and self._service.snapshot().get("status") == "syncing"
            if ready:
                status = "ready"
                message = "已采用阿里云与腾讯云交叉验证后的时间，自动校正等待时刻。"
                if self._error:
                    message = "本轮校时未完成，继续使用仍在有效期内的上次校准结果。"
            elif source == "ntp":
                status = "degraded"
                message = "上次校时的质量有效期已过，暂沿用该计时基准；精度尚未重新确认。"
            else:
                status = "syncing" if syncing else "degraded"
                message = ("正在自动校时，暂按程序启动时的电脑时间计时。" if syncing else
                           "尚未取得多源校时结果，暂按程序启动时的电脑时间计时；无需手填时差。")
            if locked:
                message += " 定时任务已锁定计时基准，处理结束后恢复取样。"
            return {
                "ready": ready, "status": status, "source": source,
                "now": raw["now"], "offset_ms": raw["offset_vs_system_ms"],
                "uncertainty_ms": raw.get("uncertainty_ms"), "rtt_ms": raw.get("rtt_ms"),
                "sources": raw.get("sources", []), "last_sync_at": raw.get("sampled_at"),
                "age_seconds": raw.get("age_seconds"), "message": message,
                "failure_reason": self._error, "locked": locked,
            }

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self._close_service()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
