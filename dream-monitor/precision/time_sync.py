"""有限的多提供商 NTP 采样，不改系统时间，不自动移动已冻结任务。

协议按 RFC 5905 的四时间戳计算；默认公网域名来自阿里云和腾讯云官方文档。
同一提供商无论采多少主机都只产生一个代表值。后台服务只测量，应用层在
安全时机调用 Clock.apply_sync。导入或构造任何对象都不会发出网络请求。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from itertools import combinations
import math
import secrets
import socket
import statistics
import struct
import threading
import time
from typing import Any, Callable, Iterable, Sequence


NTP_EPOCH = 2_208_988_800
NTP_ERA_SECONDS = 1 << 32
MIN_UNIX_TIME = 946_684_800.0       # 2000-01-01 UTC
MAX_UNIX_TIME = 4_102_444_800.0     # 2100-01-01 UTC
MAX_OFFSET_SECONDS = 86_400.0
MAX_RTT_SECONDS = 3.0
MAX_ROOT_DISTANCE_SECONDS = 10.0
CONSENSUS_FLOOR_MS = 10.0
MAX_SAMPLE_UNCERTAINTY_MS = 250.0
MAX_RESULT_UNCERTAINTY_MS = 100.0
MAX_BATCH_AGE_SECONDS = 30.0
DRIFT_PPM = 50.0
PROVIDER_LABELS = {"aliyun": "阿里云", "tencent": "腾讯云"}
NOTICE = "校准通用 UTC；网络不对称与本机漂移仍带来不确定度，不代表报名服务端时钟零误差。"
_DNS_SLOTS = threading.BoundedSemaphore(4)


class NtpError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class SyncCancelled(NtpError):
    def __init__(self):
        super().__init__("CANCELLED", "校时采样已取消。")


def _provider(provider: str, host: str) -> str:
    # 不允许把多个 Ali/Tencent 主机改个显示名就算成独立多数。
    lowered = str(host).rstrip(".").lower()
    if lowered == "aliyun.com" or lowered.endswith(".aliyun.com"):
        return "aliyun"
    if lowered == "tencent.com" or lowered.endswith(".tencent.com"):
        return "tencent"
    return str(provider).strip().lower()


@dataclass(frozen=True)
class NtpSource:
    provider: str
    host: str

    def __post_init__(self):
        host = str(self.host).strip().lower().rstrip(".")
        if (not host or len(host) > 253 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host)
                or "." not in host):
            raise ValueError("NTP 主机名格式不正确")
        provider = _provider(self.provider, host)
        if not provider:
            raise ValueError("NTP 来源必须注明提供商")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "provider", provider)


DEFAULT_SOURCES = (
    NtpSource("aliyun", "ntp.aliyun.com"),
    NtpSource("tencent", "ntp.tencent.com"),
    NtpSource("aliyun", "ntp1.aliyun.com"),
    NtpSource("tencent", "ntp1.tencent.com"),
)


@dataclass(frozen=True)
class NtpSample:
    provider: str
    host: str
    address: str
    offset_ms: float
    uncertainty_ms: float
    rtt_ms: float
    delay_ms: float
    sampled_at: float
    sampled_monotonic: float
    stratum: int = 2
    version: int = 4
    leap: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self), "label": PROVIDER_LABELS.get(self.provider, self.provider)}


@dataclass(frozen=True)
class ProviderEstimate:
    provider: str
    host: str
    offset_ms: float
    uncertainty_ms: float
    rtt_ms: float
    sample_count: int
    sampled_at: float
    sampled_monotonic: float
    stratum: int

    def to_dict(self) -> dict:
        return {**asdict(self), "label": PROVIDER_LABELS.get(self.provider, self.provider)}


@dataclass(frozen=True)
class RejectedSource:
    provider: str
    host: str
    code: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SyncResult:
    status: str
    message: str
    generation: int = 0
    offset_ms: float | None = None
    uncertainty_ms: float | None = None
    rtt_ms: float | None = None
    sampled_at: float | None = None
    sampled_monotonic: float | None = None
    providers: tuple[ProviderEstimate, ...] = ()
    samples: tuple[NtpSample, ...] = ()
    rejected: tuple[RejectedSource, ...] = ()

    def to_dict(self, *, now_monotonic: float | None = None) -> dict:
        age = None if now_monotonic is None or self.sampled_monotonic is None else max(0.0, now_monotonic - self.sampled_monotonic)
        return {
            "status": self.status, "message": self.message, "generation": self.generation,
            "offset_ms": self.offset_ms, "uncertainty_ms": self.uncertainty_ms, "rtt_ms": self.rtt_ms,
            "sampled_at": self.sampled_at, "sampled_monotonic": self.sampled_monotonic,
            "age_seconds": age, "sample_age_seconds": age,
            "provider_count": len(self.providers), "providers": [provider.to_dict() for provider in self.providers],
            "sources": ([{"provider": item.provider, "host": item.host} for item in self.providers]
                        if self.status == "ready" else []),
            "samples": [sample.to_dict() for sample in self.samples],
            "rejected": [item.to_dict() for item in self.rejected], "notice": NOTICE,
        }


def encode_timestamp(unix_time: float) -> bytes:
    value = float(unix_time)
    if not math.isfinite(value):
        raise ValueError("NTP 时间必须是有限数值")
    ntp = value + NTP_EPOCH
    seconds = math.floor(ntp)
    fraction = int((ntp - seconds) * (1 << 32))
    return struct.pack("!II", seconds % NTP_ERA_SECONDS, fraction)


def decode_timestamp(value: bytes, *, reference_unix: float) -> float:
    if len(value) != 8:
        raise NtpError("TIMESTAMP_LENGTH", "NTP 时间戳长度不正确。")
    seconds, fraction = struct.unpack("!II", value)
    # 以当前基准选最近 era，跨 2036 年溢出仍能正确解释 32 位秒数。
    era = round((float(reference_unix) + NTP_EPOCH - seconds) / NTP_ERA_SECONDS)
    return seconds + era * NTP_ERA_SECONDS - NTP_EPOCH + fraction / (1 << 32)


def build_request(sent_unix: float) -> bytes:
    packet = bytearray(48)
    packet[0] = (4 << 3) | 3             # LI=0, VN=4, mode=client
    timestamp = bytearray(encode_timestamp(sent_unix))
    # 仅扰动不足 60 ns 的末尾位；回应必须逐字节回显，降低陈旧应答碰撞。
    timestamp[-1] = secrets.randbits(8)
    packet[40:48] = timestamp
    return bytes(packet)


def parse_response(packet: bytes, request: bytes, *, source: NtpSource, address: str,
                   sent_unix: float, received_unix: float,
                   sent_monotonic: float, received_monotonic: float) -> NtpSample:
    """校验响应并使用 t1/t2/t3/t4 计算 theta 与 delta。"""
    if len(request) != 48:
        raise NtpError("REQUEST_LENGTH", "本地 NTP 请求长度不正确。")
    # 本客户端不协商扩展/MAC/NTS，未知额外载荷不假装已经验证。
    if len(packet) != 48:
        raise NtpError("PACKET_LENGTH", "NTP 响应不是已支持的 48 字节基础报文。")
    first, stratum, _poll, precision, root_delay_raw, root_disp_raw, reference_id, reference, originate, receive, transmit = struct.unpack(
        "!BBbbiI4s8s8s8s8s", packet)
    leap, version, mode = first >> 6, (first >> 3) & 7, first & 7
    if mode != 4:
        raise NtpError("MODE", "NTP 响应不是服务器模式。")
    if version not in (3, 4):
        raise NtpError("VERSION", "NTP 协议版本不受支持。")
    if originate != request[40:48]:
        raise NtpError("ORIGINATE", "NTP 回应与本次请求不匹配，可能是陈旧应答。")
    if stratum == 0:
        kiss = reference_id.decode("ascii", "replace").strip()
        code = "KOD_" + kiss if kiss in ("RATE", "DENY", "RSTR") else "STRATUM_ZERO"
        raise NtpError(code, "NTP 来源拒绝或限制了本次请求。")
    if leap == 3:
        raise NtpError("UNSYNCHRONIZED", "NTP 来源声明尚未同步。")
    if not 1 <= stratum <= 15:
        raise NtpError("STRATUM", "NTP 来源层级无效。")
    if receive == bytes(8) or transmit == bytes(8) or reference == bytes(8):
        raise NtpError("ZERO_TIMESTAMP", "NTP 来源缺少必要的时间戳。")
    inputs = (sent_unix, received_unix, sent_monotonic, received_monotonic)
    if not all(math.isfinite(float(value)) for value in inputs):
        raise NtpError("LOCAL_TIME", "本地时间采样无效。")
    rtt = received_monotonic - sent_monotonic
    if not 0 <= rtt <= MAX_RTT_SECONDS:
        raise NtpError("RTT", "NTP 往返耗时异常或过长。")
    if abs((received_unix - sent_unix) - rtt) > 0.002:
        raise NtpError("LOCAL_STEP", "NTP 采样期间本地基准不连续。")
    t1 = decode_timestamp(request[40:48], reference_unix=sent_unix)
    t2 = decode_timestamp(receive, reference_unix=sent_unix)
    t3 = decode_timestamp(transmit, reference_unix=received_unix)
    ref_time = decode_timestamp(reference, reference_unix=t3)
    if not all(MIN_UNIX_TIME <= value <= MAX_UNIX_TIME for value in (t1, t2, t3, received_unix)):
        raise NtpError("TIME_RANGE", "日期超出自动校时的合理范围，请检查电脑日期。")
    if t3 < t2 - 0.000001 or t3 - t2 > rtt + 0.002:
        raise NtpError("SERVER_ORDER", "NTP 服务器收发时间顺序或处理耗时异常。")
    if ref_time > t3 + 0.001 or t3 - ref_time > 86_400:
        raise NtpError("REFERENCE_TIME", "NTP 来源的参考时间异常或过旧。")
    root_delay = root_delay_raw / 65536.0
    dispersion = root_disp_raw / 65536.0
    if not -0.001 <= root_delay <= MAX_ROOT_DISTANCE_SECONDS or not 0 <= dispersion <= MAX_ROOT_DISTANCE_SECONDS:
        raise NtpError("ROOT_DISTANCE", "NTP 来源报告的误差范围异常。")
    if not -50 <= precision <= 0:
        raise NtpError("PRECISION", "NTP 来源报告的精度不合理。")
    delay = (received_unix - t1) - (t3 - t2)
    if delay < -0.002:
        raise NtpError("NEGATIVE_DELAY", "NTP 网络耗时计算结果无效。")
    delay = max(0.0, delay)
    offset = ((t2 - t1) + (t3 - received_unix)) / 2.0
    if abs(offset) > MAX_OFFSET_SECONDS:
        raise NtpError("OFFSET_RANGE", "电脑日期与来源相差超过一天，不自动应用此次样本。")
    uncertainty = max(0.001, delay / 2.0 + max(0.0, root_delay) / 2.0 + dispersion + 2.0 ** precision)
    return NtpSample(_provider(source.provider, source.host), source.host, address,
                     offset * 1000.0, uncertainty * 1000.0, rtt * 1000.0, delay * 1000.0,
                     received_unix, received_monotonic, stratum, version, leap)


class NtpClient:
    def __init__(self, clock, *, timeout: float = 1.25, resolver=None, socket_factory=None):
        self.clock = clock
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 3.0:
            raise ValueError("NTP 请求超时必须在 0 到 3 秒之间")
        self._resolver = resolver or socket.getaddrinfo
        self._socket_factory = socket_factory or socket.socket

    def _resolve(self, source: NtpSource, cancel_event, deadline: float):
        if cancel_event.is_set():
            raise SyncCancelled()
        # 系统 DNS 不能可靠地中断；用有界 daemon 解析槽避免关闭界面被它阻塞。
        if not _DNS_SLOTS.acquire(blocking=False):
            raise NtpError("DNS_BUSY", "域名解析仍在等待，已停止增加新的解析任务。")
        done = threading.Event()
        box = {}

        def resolve():
            try:
                box["addresses"] = self._resolver(source.host, 123, socket.AF_INET, socket.SOCK_DGRAM)
            except Exception:
                box["failed"] = True
            finally:
                _DNS_SLOTS.release()
                done.set()

        threading.Thread(target=resolve, name="ntp-dns", daemon=True).start()
        while not done.is_set():
            if cancel_event.is_set():
                raise SyncCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NtpError("DNS_TIMEOUT", "NTP 域名解析超时，网络可能无法访问该来源。")
            done.wait(min(remaining, 0.05))
        if cancel_event.is_set():
            raise SyncCancelled()
        addresses = box.get("addresses") or []
        usable = [item for item in addresses if item[0] == socket.AF_INET and item[1] == socket.SOCK_DGRAM]
        if not usable:
            raise NtpError("DNS_FAILED", "NTP 域名没有得到可用的 IPv4 地址。")
        return usable[0]

    def sample(self, source: NtpSource, cancel_event=None) -> NtpSample:
        cancel_event = cancel_event or threading.Event()
        deadline = time.monotonic() + self.timeout
        family, kind, protocol, _canonical, address = self._resolve(source, cancel_event, deadline)
        if cancel_event.is_set():
            raise SyncCancelled()
        channel = self._socket_factory(family, kind, protocol)
        try:
            # connected UDP 只接收目标地址/端口的报文，不接受任意发件人。
            channel.connect(address)
            if cancel_event.is_set():
                raise SyncCancelled()
            before = self.clock.monotonic()
            sent = self.clock.epoch_from_monotonic(before, offset_ms=0.0)
            request = build_request(sent)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NtpError("TIMEOUT", "NTP 请求超时。")
            channel.settimeout(min(remaining, 0.1))
            if cancel_event.is_set():
                raise SyncCancelled()
            channel.send(request)
            while True:
                if cancel_event.is_set():
                    raise SyncCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NtpError("TIMEOUT", "NTP 响应超时，UDP 123 可能被网络阻断。")
                channel.settimeout(min(remaining, 0.1))
                try:
                    packet = channel.recv(512)
                    after = self.clock.monotonic()
                    received = self.clock.epoch_from_monotonic(after, offset_ms=0.0)
                    break
                except socket.timeout:
                    continue
            if cancel_event.is_set():
                raise SyncCancelled()
            return parse_response(packet, request, source=source, address=str(address[0]),
                                  sent_unix=sent, received_unix=received,
                                  sent_monotonic=before, received_monotonic=after)
        except NtpError:
            raise
        except OSError as exc:
            raise NtpError("NETWORK", "NTP 网络请求未完成，未取得可验证时间。") from exc
        finally:
            channel.close()


def _compatible(left, right) -> bool:
    allowance = max(CONSENSUS_FLOOR_MS, left.uncertainty_ms + right.uncertainty_ms)
    return abs(left.offset_ms - right.offset_ms) <= allowance


def _cohort(values: Sequence[Any], minimum: int) -> tuple[Any, ...] | None:
    """最多八个候选，取唯一的最大两两一致组；平票不任意选时间。"""
    for size in range(len(values), minimum - 1, -1):
        matches = [group for group in combinations(values, size)
                   if all(_compatible(left, right) for left, right in combinations(group, 2))]
        if len(matches) == 1:
            return tuple(matches[0])
        if len(matches) > 1:
            return None
    return None


def combine_samples(samples: Iterable[NtpSample], *, now_monotonic: float,
                    generation: int = 0, rejected: Iterable[RejectedSource] = ()) -> SyncResult:
    all_samples = tuple(samples)
    failures = list(rejected)
    groups: dict[str, list[NtpSample]] = {}
    for sample in all_samples:
        provider = _provider(sample.provider, sample.host)
        values = (sample.offset_ms, sample.uncertainty_ms, sample.rtt_ms, sample.delay_ms,
                  sample.sampled_at, sample.sampled_monotonic)
        age = now_monotonic - sample.sampled_monotonic
        if (not all(math.isfinite(float(value)) for value in values) or not -0.01 <= age <= MAX_BATCH_AGE_SECONDS
                or not 0 <= sample.rtt_ms <= MAX_RTT_SECONDS * 1000
                or not 0 <= sample.delay_ms <= sample.rtt_ms + 2.0
                or not 0 <= sample.uncertainty_ms <= MAX_SAMPLE_UNCERTAINTY_MS
                or abs(sample.offset_ms) > MAX_OFFSET_SECONDS * 1000):
            failures.append(RejectedSource(provider, sample.host, "SAMPLE_QUALITY", "样本过旧或不确定度/往返时间异常。"))
            continue
        adjusted = replace(sample, provider=provider, uncertainty_ms=max(1.0, sample.uncertainty_ms))
        groups.setdefault(provider, []).append(adjusted)
    estimates: list[ProviderEstimate] = []
    for provider, candidates in groups.items():
        candidates = sorted(candidates, key=lambda item: (item.rtt_ms, item.uncertainty_ms))[:8]
        group = _cohort(candidates, 1)
        if group is None or len(group) <= len(candidates) / 2:
            failures.append(RejectedSource(provider, "", "PROVIDER_UNSTABLE", "同一提供商内部样本不一致，未采用该来源。"))
            continue
        for candidate in candidates:
            if candidate not in group:
                failures.append(RejectedSource(provider, candidate.host, "SAMPLE_OUTLIER", "该样本与提供商内多数样本不一致。"))
        best = min(group, key=lambda item: (item.rtt_ms, item.uncertainty_ms))
        estimates.append(ProviderEstimate(provider, best.host, best.offset_ms, best.uncertainty_ms,
                                          best.rtt_ms, len(group), best.sampled_at, best.sampled_monotonic, best.stratum))
    common = {"generation": generation, "samples": all_samples}
    if len(estimates) < 2:
        status = "degraded" if estimates else "unavailable"
        message = "有效来源不足两个独立提供商，不会自动应用单一来源时间。" if estimates else "没有取得可验证的 NTP 样本，请检查网络或 UDP 123 访问。"
        return SyncResult(status, message, providers=tuple(estimates), rejected=tuple(failures), **common)
    if len(estimates) > 8:
        return SyncResult("inconsistent", "独立来源数量超出本地共识范围，未应用。", rejected=tuple(failures), **common)
    accepted = _cohort(estimates, 2)
    if accepted is None or len(accepted) <= len(estimates) / 2:
        return SyncResult("inconsistent", "不同提供商的时间不能形成唯一多数共识，未自动应用。",
                          providers=tuple(estimates), rejected=tuple(failures), **common)
    for estimate in estimates:
        if estimate not in accepted:
            failures.append(RejectedSource(estimate.provider, estimate.host, "PROVIDER_OUTLIER", "该提供商与独立来源多数不一致，已剔除。"))
    offset = float(statistics.median(item.offset_ms for item in accepted))
    # 不用 sqrt(n) 假装精度提升：覆盖所有代表值的误差区间，保留不同来源分歧。
    uncertainty = max(item.uncertainty_ms + abs(item.offset_ms - offset) for item in accepted)
    oldest = min(accepted, key=lambda item: item.sampled_monotonic)
    status = "ready" if uncertainty <= MAX_RESULT_UNCERTAINTY_MS else "degraded"
    message = "已取得不同提供商的时间共识。" if status == "ready" else "来源大致一致，但时间不确定度仍过大，未自动应用。"
    return SyncResult(status, message, offset_ms=offset if status == "ready" else None,
                      uncertainty_ms=uncertainty, rtt_ms=max(item.rtt_ms for item in accepted),
                      # Individual samples record the immutable base timestamp;
                      # display the accepted consensus time in calibrated UTC.
                      sampled_at=oldest.sampled_at + offset / 1000.0,
                      sampled_monotonic=oldest.sampled_monotonic,
                      providers=tuple(accepted), rejected=tuple(failures), **common)


class TimeSyncService:
    """一轮最多两次有限采样；应用层决定何时启动下一轮和应用结果。"""

    def __init__(self, clock, *, sources: Sequence[NtpSource] = DEFAULT_SOURCES,
                 sample_fn: Callable[[NtpSource, threading.Event], NtpSample] | None = None,
                 max_rounds: int = 2, retry_delay: float = 2.0, timeout: float = 1.25):
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 3:
            raise ValueError("自动校时重试轮数必须在 1 到 3 之间")
        if not math.isfinite(float(retry_delay)) or not 0 <= retry_delay <= 10:
            raise ValueError("校时重试间隔必须在 0 到 10 秒之间")
        unique = tuple(dict.fromkeys(sources))
        if not unique or len(unique) > 16 or any(not isinstance(source, NtpSource) for source in unique):
            raise ValueError("请配置有限数量的明确 NTP 来源")
        counts = {source.provider: sum(item.provider == source.provider for item in unique) for source in unique}
        if any(count > 3 for count in counts.values()):
            raise ValueError("每个提供商最多配置三个主机")
        self.clock = clock
        self.sources = unique
        self.max_rounds = max_rounds
        self.retry_delay = float(retry_delay)
        self._sample = sample_fn or NtpClient(clock, timeout=timeout).sample
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._closed = False
        self._status = "idle"
        self._round = 0
        self._completed = 0
        self._latest: SyncResult | None = None

    @property
    def latest_result(self) -> SyncResult | None:
        with self._lock:
            return self._latest

    @property
    def running(self) -> bool:
        with self._lock:
            return self._status == "syncing" and not self._closed

    def snapshot(self) -> dict:
        with self._lock:
            result = None if self._latest is None else self._latest.to_dict(now_monotonic=self.clock.monotonic())
            return {"status": self._status, "state": self._status, "running": self.running,
                    "generation": self._generation, "round": self._round, "max_rounds": self.max_rounds,
                    "completed_samples": self._completed, "max_samples": len(self.sources) * self.max_rounds,
                    "result": result, "notice": NOTICE,
                    "configured_sources": [{"provider": source.provider, "host": source.host} for source in self.sources]}

    def start(self) -> dict:
        with self._lock:
            if self._closed or self._status == "syncing":
                return self.snapshot()
            self._generation += 1
            generation = self._generation
            self._cancel = threading.Event()
            self._status = "syncing"
            self._round = self._completed = 0
            self._worker = threading.Thread(target=self._run, args=(generation, self._cancel),
                                            name=f"ntp-sync-{generation}", daemon=True)
            self._worker.start()
            return self.snapshot()

    retry = start

    def close(self) -> None:
        """不等待 DNS/UDP；未开始的采样停止，在途结果也不会再发布。"""
        with self._lock:
            self._closed = True
            self._generation += 1
            self._status = "cancelled"
            self._cancel.set()

    cancel = close

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)

    def _run(self, generation: int, cancel_event: threading.Event) -> None:
        samples: list[NtpSample] = []
        rejected: list[RejectedSource] = []
        blocked_providers: set[str] = set()
        result = SyncResult("unavailable", "尚未取得有效 NTP 样本。", generation)
        try:
            for round_number in range(1, self.max_rounds + 1):
                with self._lock:
                    if self._closed or generation != self._generation:
                        return
                    self._round = round_number
                for source in self.sources:
                    if cancel_event.is_set():
                        return
                    if source.provider in blocked_providers:
                        continue
                    try:
                        sample = self._sample(source, cancel_event)
                        if cancel_event.is_set():
                            return
                        if not isinstance(sample, NtpSample):
                            raise NtpError("SAMPLE_TYPE", "采样没有返回可验证的 NTP 数据。")
                        if _provider(sample.provider, sample.host) != source.provider or sample.host != source.host:
                            raise NtpError("SOURCE_MISMATCH", "样本来源与本次请求不一致。")
                        samples.append(sample)
                    except SyncCancelled:
                        cancel_event.set()
                        return
                    except Exception as exc:
                        code = exc.code if isinstance(exc, NtpError) else "UNAVAILABLE"
                        message = exc.message if isinstance(exc, NtpError) else "该来源没有取得可验证样本。"
                        rejected.append(RejectedSource(source.provider, source.host, code, message))
                        if code in ("KOD_RATE", "KOD_DENY", "KOD_RSTR"):
                            blocked_providers.add(source.provider)
                    with self._lock:
                        if self._closed or generation != self._generation:
                            return
                        self._completed += 1
                result = combine_samples(samples, now_monotonic=self.clock.monotonic(),
                                         generation=generation, rejected=rejected)
                if result.status == "ready":
                    break
                if round_number < self.max_rounds and cancel_event.wait(self.retry_delay):
                    return
        except Exception:
            result = SyncResult("unavailable", "校时未完成，未应用任何新时间。", generation,
                                samples=tuple(samples), rejected=tuple(rejected))
        finally:
            with self._lock:
                if not self._closed and generation == self._generation:
                    if cancel_event.is_set():
                        self._status = "cancelled"
                    else:
                        self._latest = result
                        self._status = "ready" if result.status == "ready" else "failed"


__all__ = ["NtpSource", "NtpSample", "ProviderEstimate", "SyncResult", "NtpClient", "NtpError",
           "SyncCancelled", "TimeSyncService", "DEFAULT_SOURCES", "build_request", "parse_response",
           "encode_timestamp", "decode_timestamp", "combine_samples"]
