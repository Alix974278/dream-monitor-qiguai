"""原报名流程的精确定时调度：提前准备、独立等待、有限重试。

发现线程只读本地计划，每个任务独立等待单调截止时间。on_send 只做内存
检查与计时；登录、活动获取、筛选和表单协议仍由原 Hub 负责。没有 NTP
共识时继续使用当前本机/内部基准，不增加启用门槛。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import threading
import time
from typing import Any, Callable, Mapping

from precision.clock import Clock

LEAD = 120.0                 # 提前发现，不提前发送报名
POLL = 1.0                   # 本地发现间隔，不决定发起时刻
FREEZE_BEFORE = 60.0
PREPARE_BEFORE = 30.0
GIVE_UP_AFTER = 300.0         # 保留原版迟到窗口
RETRY_DELAYS = (0.25, 0.5)
_TERMINAL = {"success", "failed", "unknown", "cancelled"}
_OWNED = ("claimed", "submitting")


class DispatchAborted(RuntimeError):
    """回调撤销发起；Hub 必须让此异常阻止真实 POST。"""


class _LocalAuthority:
    def __init__(self):
        self.clock = Clock()

    def freeze(self, key):
        return self.clock.freeze()

    def release(self, key):
        pass


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _same_version(row, job):
    return bool(row) and _finite(row.get("created")) == job.created and _finite(row.get("join_start")) == job.join_start


@dataclass
class _Job:
    uid: str
    aid: str
    name: str
    created: float
    join_start: float
    hub: Any
    cancel_event: Any
    key: str
    phase: str = "waiting"
    message: str = "计划已接管，等待进入准备窗口。"
    worker: Any = None
    context: Any = None
    frozen: Any = None
    deadline: float | None = None
    prefetched: bool = False
    prepare_note: str = ""
    attempts: int = 0
    sent_count: int = 0
    actual_send_at: float | None = None
    actual_send_wall: float | None = None
    actual_send_monotonic: float | None = None
    dispatch_lateness_ms: float | None = None
    last_send_at: float | None = None
    in_flight: bool = False
    needs_confirmation: bool = False
    result_status: str = ""
    sent_attempts: set[int] = field(default_factory=set)


class Scheduler:
    def __init__(self, users, hub_of: Callable[[str], object | None],
                 log: Callable[[str], None] | None = None, *, authority=None,
                 event_factory=None, max_tasks_per_user: int = 4, max_tasks: int = 32):
        self.users = users
        self.hub_of = hub_of
        self._log = log or (lambda message: None)
        self.authority = authority or _LocalAuthority()
        self.clock = self.authority.clock
        self.max_tasks_per_user = max(1, min(4, int(max_tasks_per_user)))
        self.max_tasks = max(1, min(64, int(max_tasks)))
        self._event_factory = event_factory or threading.Event
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._jobs: dict[tuple[str, str], _Job] = {}
        self._active: dict[str, _Job] = {}
        self._uncertain: set[tuple[str, str]] = set()
        self._deferred: set[str] = set()
        self._blocked_users: set[str] = set()
        self._recovered = False
        self._th = None
        self.last_tick = 0.0
        self.running = False

    def _log_safe(self, message):
        try:
            self._log(message)
        except Exception:
            pass

    def start(self):
        with self._lock:
            if self._th and self._th.is_alive():
                return
            self._stop.clear()
            self.running = True
            self._th = threading.Thread(target=self._loop, name="autosign", daemon=True)
            self._th.start()
        self._log_safe("精确定时报名已启动：提前准备，到点独立提交。")

    def stop(self, *, join_timeout: float = 0.0):
        self._stop.set()
        self._wake.set()
        self.running = False
        with self._lock:
            jobs = list(self._active.values())
        for job in jobs:
            self._cancel_job(job, "调度器已停止；不会发起后续报名。")
        cutoff = time.monotonic() + max(0.0, join_timeout)
        for thread in [self._th] + [job.worker for job in jobs]:
            if thread and thread is not threading.current_thread() and time.monotonic() < cutoff:
                thread.join(max(0.0, cutoff - time.monotonic()))

    close = stop

    def wake(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception as exc:
                self._log_safe(f"定时计划读取未完成：{type(exc).__name__}；稍后重试本地发现。")
            self._wake.wait(POLL)
        self.running = False

    def _live_locked(self, job):
        return (not self._stop.is_set() and not job.cancel_event.is_set()
                and self._jobs.get((job.uid, job.aid)) is job)

    def _mark(self, job, status, note, expected_statuses=_OWNED):
        return self.users.auto_mark_if_current(
            job.uid, job.aid, job.created, job.join_start, status, str(note)[:500],
            expected_statuses=expected_statuses,
        )

    def _cancel_job(self, job, message, *, persist=True):
        with self._lock:
            if job.phase in _TERMINAL and not job.in_flight:
                return
            job.cancel_event.set()
            job.phase = "cancelled"
            job.needs_confirmation = job.needs_confirmation or (job.uid, job.aid) in self._uncertain
            job.message = ("已停止后续操作；已发出的请求仍需人工核验，不能重新自动提交。"
                           if job.needs_confirmation else message)
            status = "unknown" if job.needs_confirmation else "cancelled"
        if persist:
            try:
                self._mark(job, status, job.message)
            except Exception:
                pass  # 取消事件已生效；旧接管标记重启后仍不会重发

    def cancel(self, uid, aid):
        key = (str(uid), str(aid))
        with self._lock:
            jobs = [job for job in self._active.values() if (job.uid, job.aid) == key]
            current = self._jobs.get(key)
            if current is not None and not any(job is current for job in jobs):
                jobs.append(current)
        for job in jobs:
            self._cancel_job(job, "计划已取消，不会发送报名请求。")
        self.wake()
        return self.snapshot(str(uid)).get(str(aid))

    def with_user_removed(self, uid, remove_callback):
        """串行化删人和任务发现；活跃 worker 期间拒绝删除，防止误发。

        不改任何正常报名热路径；仅管理员删除时持有这个保护锁。
        """
        uid = str(uid)
        with self._tick_lock:
            with self._lock:
                if any(job.uid == uid for job in self._active.values()):
                    raise RuntimeError("此账号还有正在等待或执行的报名任务，请先在该账号取消任务，等待其退出后再删除。")
                self._blocked_users.add(uid)
            try:
                result = remove_callback()
            except BaseException:
                with self._lock:
                    self._blocked_users.discard(uid)
                self.wake()
                raise
            with self._lock:
                for key in list(self._jobs):
                    if key[0] == uid:
                        del self._jobs[key]
                self._uncertain = {key for key in self._uncertain if key[0] != uid}
                self._deferred = {key for key in self._deferred if not key.startswith(uid + ':')}
                # 账号及其任务已从数据库删除，解除屏蔽；否则同一人重新登录后
                # 新建的定时任务会被调度器一直跳过（停在“等待中”到点不提交）。
                self._blocked_users.discard(uid)
            self.wake()
            return result

    def _reconcile(self):
        with self._lock:
            jobs = [job for job in self._active.values() if job.phase not in _TERMINAL]
        for job in jobs:
            row = self.users.auto_get(job.uid, job.aid)
            if not _same_version(row, job) or row.get("status") not in _OWNED:
                self._cancel_job(job, "计划已经被删除或更新，旧任务已停止。", persist=False)

    def tick(self, now: float | None = None) -> list[dict]:
        """发现并接管计划，立即返回；网络与截止等待由各任务独立执行。"""
        if self._stop.is_set():
            return []
        with self._tick_lock:
            if self._stop.is_set():
                return []
            if not self._recovered:
                self.users.auto_recover_claimed()
                self._recovered = True
            self._reconcile()
            now = self.clock.now() if now is None else float(now)
            if not math.isfinite(now):
                raise ValueError("发现时间必须是有效数值")
            self.last_tick = now
            started = []
            for row in self.users.auto_due(now, LEAD):
                uid, aid = str(row.get("uid") or ""), str(row.get("aid") or "")
                start, created = _finite(row.get("join_start")), _finite(row.get("created"))
                if not uid or not aid or start is None or created is None:
                    continue
                key = f"{uid}:{aid}:{created!r}:{start!r}"
                job = _Job(uid, aid, str(row.get("name") or aid), created, start,
                           None, self._event_factory(), key)
                with self._lock:
                    if uid in self._blocked_users:
                        continue
                    previous = self._jobs.get((uid, aid))
                    if previous and previous.key == key and previous.phase not in _TERMINAL:
                        continue
                    uncertain = (uid, aid) in self._uncertain
                    limited = (len(self._active) >= self.max_tasks or
                               sum(item.uid == uid for item in self._active.values()) >= self.max_tasks_per_user)
                if uncertain:
                    job.phase = "unknown"
                    job.needs_confirmation = True
                    job.result_status = "unknown"
                    job.message = "本次运行中该活动有尚未确认的提交；请先核验，不会因重建计划再次发送。"
                    if self._mark(job, "unknown", job.message, ("pending", "retry")):
                        with self._lock:
                            self._jobs[(uid, aid)] = job
                    continue
                if now > start + GIVE_UP_AFTER:
                    self._mark(job, "failed", "超过原定开始时间 5 分钟，已停止此计划。", ("pending", "retry"))
                    continue
                if limited:
                    if key not in self._deferred:
                        self._mark(job, "pending", "等待定时任务空位；每账号最多同时处理 4 个活动。", ("pending", "retry"))
                        self._deferred.add(key)
                    continue
                hub = self.hub_of(uid)
                if hub is None:
                    continue  # 保持原版：账号尚未就绪时仍为 pending
                job.hub = hub
                if not self.users.auto_claim(uid, aid, created, start):
                    continue
                with self._lock:
                    if self._stop.is_set():
                        job.cancel_event.set()
                    self._jobs[(uid, aid)] = job
                    self._active[key] = job
                    job.worker = threading.Thread(target=self._run, args=(job,),
                                                  name=f"autosign-{aid}", daemon=True)
                    job.worker.start()
                    self._trim_locked()
                started.append({"uid": uid, "aid": aid, "scheduled": True})
            return started

    def _trim_locked(self):
        if len(self._jobs) > 512:
            for key, job in list(self._jobs.items()):
                if job.key not in self._active and job.phase in _TERMINAL:
                    self._jobs.pop(key, None)
                    if len(self._jobs) <= 384:
                        break
        if len(self._deferred) > 1024:
            self._deferred.clear()

    def _check_current(self, job):
        with self._lock:
            if not self._live_locked(job):
                return False
        row = self.users.auto_get(job.uid, job.aid)
        if not _same_version(row, job) or row.get("status") not in _OWNED:
            self._cancel_job(job, "计划已经被删除或更新，旧任务已停止。", persist=False)
            return False
        return True

    def _wait_until(self, job, deadline):
        while True:
            with self._lock:
                if not self._live_locked(job):
                    return False
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                return True
            if job.cancel_event.wait(min(remaining, threading.TIMEOUT_MAX)):
                return False

    def _freeze(self, job):
        while True:
            with self._lock:
                if not self._live_locked(job):
                    return False
            deadline = self.clock.deadline_monotonic(job.join_start)
            remaining = deadline - self.clock.monotonic()
            if remaining <= FREEZE_BEFORE:
                if not self._check_current(job):
                    return False
                frozen = self.authority.freeze(job.key)
                with self._lock:
                    job.frozen = frozen
                    job.deadline = frozen.deadline_monotonic(job.join_start)
                return True
            if job.cancel_event.wait(min(10.0, remaining - FREEZE_BEFORE)):
                return False

    def _prepare(self, job):
        with self._lock:
            if not self._live_locked(job):
                return False
            job.phase, job.in_flight = "preparing", True
        try:
            context = job.hub.prepare_signup(job.aid)
            with self._lock:
                job.context = context
                job.prefetched = (bool(_get(context, "prefetched", False))
                                  and self.clock.monotonic() < job.deadline)
                job.prepare_note = str(_get(context, "note", ""))[:500]
        except Exception:
            # 预读只是优化。没有生成 ctx 时到点再试一次，绝不编造空表单。
            with self._lock:
                job.prepare_note = "提前准备未完成；到点按原报名流程再尝试读取表单。"
        finally:
            with self._lock:
                job.in_flight = False
        with self._lock:
            if not self._live_locked(job):
                return False
            job.phase = "waiting"
            job.message = ("报名资料已提前准备完成，等待计划时刻。" if job.prefetched else
                           job.prepare_note or "等待计划时刻，届时按原版读取报名信息并提交。")
        return True

    def _on_send(self, job, attempt, event=None):
        """紧邻实际 POST；禁止数据库、Hub 查找或日志接收器 IO。"""
        with self._lock:
            if not self._live_locked(job):
                raise DispatchAborted("计划已取消或替换，未发送请求。")
            when = self.clock.monotonic()
            if when < job.deadline:
                raise DispatchAborted("尚未到达报名开始时间，已阻止提前提交。")
            if when > job.deadline + GIVE_UP_AFTER:
                raise DispatchAborted("已超过允许触发窗口，未补发请求。")
            if attempt in job.sent_attempts:
                raise DispatchAborted("同一次尝试不能重复发送。")
            wall = self.clock.system_now()
            if isinstance(event, Mapping):
                candidate = _finite(event.get("monotonic"))
                if candidate is not None and abs(candidate - when) <= 1:
                    when = max(job.deadline, candidate)
                candidate = _finite(event.get("wall"))
                if candidate is not None:
                    wall = candidate
            actual = job.frozen.epoch_from_monotonic(when)
            job.sent_attempts.add(attempt)
            job.sent_count += 1
            job.needs_confirmation = True
            job.last_send_at = actual
            self._uncertain.add((job.uid, job.aid))
            if job.actual_send_at is None:
                job.actual_send_at = actual
                job.actual_send_wall = wall
                job.actual_send_monotonic = when
                job.dispatch_lateness_ms = round(max(0.0, when - job.deadline) * 1000.0, 3)

    def _finish(self, job, status, message, result_status=None):
        with self._lock:
            if not self._live_locked(job):
                return
            job.phase = status
            job.message = str(message)[:500]
            job.result_status = str(result_status or status)
        try:
            self._mark(job, status, job.message)
        except Exception:
            self._log_safe("任务状态落盘未完成；持久化接管标记会阻止重启后重复发起。")
        self._log_safe(f"定时报名 {status}：{job.name[:40]}｜{job.message}")

    def _confirm(self, job):
        with self._lock:
            if not self._live_locked(job):
                return
            job.phase, job.in_flight = "confirming", True
        try:
            result = job.hub.confirm_signup(job.context)
        except Exception:
            result = {"status": "unknown", "msg": "暂时无法核验个人报名记录；不会自动重发。"}
        finally:
            with self._lock:
                job.in_flight = False
        with self._lock:
            if not self._live_locked(job):
                return
        sid = str(_get(result, "signup_id", "") or "")
        if _get(result, "status") == "confirmed" and sid.isdigit() and int(sid) > 0:
            with self._lock:
                job.needs_confirmation = False
                self._uncertain.discard((job.uid, job.aid))
            self._finish(job, "success", _get(result, "msg") or "已核实个人报名记录。", "confirmed")
        else:
            with self._lock:
                job.needs_confirmation = True
                self._uncertain.add((job.uid, job.aid))
            self._finish(job, "unknown", "尚未核实个人报名记录；请人工查看，不会自动重发。", "unknown")

    def _submit(self, job):
        for attempt in range(1, 4):
            with self._lock:
                if not self._live_locked(job):
                    return
                late = self.clock.monotonic() > job.deadline + GIVE_UP_AFTER
                job.attempts = attempt
                job.phase, job.in_flight = "submitting", True
                before_sent = job.sent_count
            if late:
                self._finish(job, "failed", "已错过原版允许的触发窗口，没有补发。")
                return
            try:
                result = job.hub.submit_prepared(
                    job.context, on_send=lambda event=None, n=attempt: self._on_send(job, n, event),
                )
            except DispatchAborted:
                if job.sent_count == before_sent:
                    self._finish(job, "failed", "本次发起已被取消或本地检查阻止，未发送请求。", "cancelled")
                    return
                result = {"status": "unknown"}
            except Exception:
                result = {"status": "unknown" if job.sent_count > before_sent else "failed",
                          "msg": "请求结果尚未明确，请核验。" if job.sent_count > before_sent else "到点报名流程未完成，未发起提交。"}
            finally:
                with self._lock:
                    job.in_flight = False
            status = str(_get(result, "status", "unknown"))
            message = str(_get(result, "msg", "") or _get(result, "message", "") or "接口未返回可确认的结果。")
            if status in {"not_open", "full", "auth_required", "manual_required", "rate_limited", "failed", "cancelled"}:
                with self._lock:
                    self._uncertain.discard((job.uid, job.aid))
                    job.needs_confirmation = False
            with self._lock:
                if not self._live_locked(job):
                    return
                job.result_status = status
            if status == "not_open" and attempt < 3:
                with self._lock:
                    job.phase = "retrying"
                    job.message = "接口明确表示尚未开放，进行下一次有限尝试。"
                if not self._wait_until(job, self.clock.monotonic() + RETRY_DELAYS[attempt - 1]):
                    return
                continue
            if status in {"pending_confirmation", "unknown", "confirmed", "not_joined"}:
                with self._lock:
                    job.needs_confirmation = True
                    self._uncertain.add((job.uid, job.aid))
                self._confirm(job)
                return
            self._finish(job, "failed", message, status)
            return

    def _run(self, job):
        try:
            if not self._freeze(job):
                return
            if not self._wait_until(job, job.deadline - PREPARE_BEFORE) or not self._check_current(job):
                return
            if not self._prepare(job):
                return
            # 先持久化，再精确等待；发起回调不得写 SQLite。
            if not self._mark(job, "submitting", "已进入定时提交等待；若进程中断，请核验后再重新启用。"):
                self._cancel_job(job, "计划已更新，旧任务不再发送。", persist=False)
                return
            if not self._wait_until(job, job.deadline):
                return
            if job.context is None:
                if not self._prepare(job):
                    return
                if job.context is None:
                    self._finish(job, "failed", "到点仍未取得原报名流程的准备上下文，未发送请求。")
                    return
            self._submit(job)
        except Exception:
            self._finish(job, "unknown" if job.sent_count else "failed",
                         "任务未完成；已发送的请求请先核验，不会自动重发。" if job.sent_count else
                         "任务准备或本地检查未完成，未发送报名请求。")
        finally:
            if job.context is not None:
                try:
                    job.hub.close_prepared(job.context)
                except Exception:
                    pass
            if job.frozen is not None:
                try:
                    self.authority.release(job.key)
                except Exception:
                    pass
            with self._lock:
                job.in_flight = False
                self._active.pop(job.key, None)
            self.wake()

    def snapshot(self, uid):
        with self._lock:
            result = {}
            for (owner, aid), job in self._jobs.items():
                if owner != str(uid):
                    continue
                result[aid] = {
                    "created": job.created, "join_start": job.join_start,
                    "timing": {
                        "phase": job.phase, "planned_at": job.join_start,
                        "actual_send_at": job.actual_send_at,
                        "actual_send_wall": job.actual_send_wall,
                        "actual_send_monotonic": job.actual_send_monotonic,
                        "dispatch_lateness_ms": job.dispatch_lateness_ms,
                        "last_send_at": job.last_send_at,
                        "attempts": job.attempts, "sent_count": job.sent_count,
                        "clock_locked": job.frozen is not None,
                        "clock_source": _get(job.frozen, "source"),
                        "clock_offset_ms": _get(job.frozen, "offset_ms"),
                        "clock_generation": _get(job.frozen, "generation"),
                        # prefetched 表示提前预读结果；到点补读后的上下文另行反映。
                        "prefetched": job.prefetched,
                        "context_ready": bool(_get(job.context, "prefetched", False)),
                        "prepared_at": _finite(_get(job.context, "prepared_at")),
                        "prepare_note": str(_get(job.context, "note", None) or job.prepare_note)[:500],
                        "in_flight": job.in_flight, "needs_confirmation": job.needs_confirmation,
                        "result_status": job.result_status, "message": job.message,
                    },
                }
            return result
