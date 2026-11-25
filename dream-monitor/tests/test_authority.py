"""Time-service lifecycle without any DNS, NTP or business requests."""
from types import SimpleNamespace
import unittest

from precision.authority import TimeAuthority
from precision.clock import Clock


class FakeService:
    def __init__(self, clock):
        self.latest_result = None
        self.status = "idle"
        self.closed = False
        self.starts = 0

    def start(self):
        self.starts += 1
        self.status = "syncing"

    def snapshot(self):
        return {"status": self.status}

    def close(self):
        self.closed = True


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.t = [100.0]
        self.clock = Clock(wall_time=lambda: 1800000000.0 + self.t[0], monotonic=lambda: self.t[0])
        self.services = []
        def factory(clock):
            service = FakeService(clock)
            self.services.append(service)
            return service
        self.authority = TimeAuthority(clock=self.clock, service_factory=factory)
        self.addCleanup(self.authority.close)

    def complete(self, offset=250.0):
        result = SimpleNamespace(
            status="ready", message="offline consensus", generation=1,
            offset_ms=offset, uncertainty_ms=5.0, rtt_ms=10.0,
            sampled_monotonic=self.t[0], sampled_at=self.clock.base_now() + offset / 1000,
            providers=tuple({"provider":p,"host":h} for p,h in (
                ("aliyun","ntp.aliyun.com"),("tencent","ntp.tencent.com"))),
        )
        self.services[-1].latest_result = result
        self.services[-1].status = "ready"
        self.authority.maintain()
        return result

    def test_no_network_on_construction_and_unsynced_task_can_keep_original_timing(self):
        self.assertEqual(self.services, [])
        state = self.authority.state()
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"], "degraded")
        frozen = self.authority.freeze("task")
        self.assertEqual(frozen.now(), self.clock.now())
        self.assertEqual(self.services, [])
        self.assertTrue(self.authority.state()["locked"])
        self.authority.release("task")
        self.authority.maintain()
        self.assertEqual(self.services[0].starts, 1)

    def test_valid_consensus_applies_and_refresh_failure_retains_real_success_time(self):
        self.authority.maintain()
        result = self.complete()
        state = self.authority.state()
        self.assertTrue(state["ready"])
        self.assertEqual(state["last_sync_at"], result.sampled_at)
        self.assertEqual(len(state["sources"]), 2)
        self.t[0] += 301
        self.authority.maintain()
        self.services[-1].latest_result = SimpleNamespace(
            status="unavailable", message="offline failure", generation=2,
            sampled_monotonic=self.t[0])
        self.services[-1].status = "failed"
        self.authority.maintain()
        state = self.authority.state()
        self.assertTrue(state["ready"])
        self.assertEqual(state["last_sync_at"], result.sampled_at)
        self.assertIn("本轮校时未完成", state["message"])
        self.assertEqual(self.clock.offset_ms, 250)

    def test_freeze_discards_pending_sample_and_all_leases_must_finish(self):
        self.authority.maintain()
        self.complete()
        old_service = self.services[0]
        frozen = self.authority.freeze("first")
        self.authority.freeze("second")
        self.assertTrue(old_service.closed)
        before = frozen.deadline_monotonic(frozen.now() + 8)
        self.authority.release("first")
        self.authority.maintain()
        self.assertEqual(len(self.services), 1)
        self.assertTrue(self.authority.state()["locked"])
        self.authority.release("second")
        self.t[0] += 1
        self.authority.maintain()
        self.assertEqual(len(self.services), 2)
        self.complete(offset=1250)
        self.assertEqual(before, frozen.deadline_monotonic(frozen.now() + 7))
        self.assertEqual(self.clock.offset_ms, 1250)

    def test_stale_result_is_explicitly_degraded_and_does_not_invent_fresh_success(self):
        self.authority.maintain()
        result = self.complete()
        self.t[0] += 1000
        state = self.authority.state()
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["last_sync_at"], result.sampled_at)
        self.assertIn("精度尚未重新确认", state["message"])

    def test_failed_service_is_not_continuously_restarted(self):
        self.authority.maintain()
        service = self.services[0]
        service.status = "failed"
        service.latest_result = SimpleNamespace(
            status="unavailable", message="offline failure", generation=1,
            sampled_monotonic=self.t[0])
        for _ in range(10):
            self.t[0] += 1
            self.authority.maintain()
        self.assertEqual(service.starts, 1)
        self.t[0] += 300
        self.authority.maintain()
        self.assertEqual(service.starts, 2)

