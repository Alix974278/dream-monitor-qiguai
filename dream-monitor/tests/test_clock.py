"""Offline clock tests: these tests never contact an HTTP server."""

from datetime import datetime, timezone
from email.utils import format_datetime
from dataclasses import FrozenInstanceError
import math
import unittest

from precision.clock import Clock, estimate_http_date, sample_https_date
from precision.time_sync import ProviderEstimate, SyncResult


class ClockTests(unittest.TestCase):
    def test_positive_offset_advances_deadline_and_wall_step_does_not_move_it(self):
        values = {"wall": 1_800_000_000.0, "mono": 100.0}
        clock = Clock(wall_time=lambda: values["wall"], monotonic=lambda: values["mono"])
        target = values["wall"] + 10.0
        self.assertEqual(clock.deadline_monotonic(target), 110.0)
        clock.set_manual_offset_ms(250.0)
        self.assertEqual(clock.deadline_monotonic(target), 109.75)
        self.assertEqual(clock.now(), values["wall"] + 0.25)
        # An OS clock correction is visible as drift, not a moved deadline.
        values["wall"] += 20.0
        self.assertEqual(clock.deadline_monotonic(target), 109.75)
        self.assertAlmostEqual(clock.status()["system_drift_ms"], 20_000.0)

    def test_wait_is_for_absolute_deadline_not_a_two_second_poll(self):
        values = {"wall": 1_800_000_000.0, "mono": 10.0}
        clock = Clock(wall_time=lambda: values["wall"], monotonic=lambda: values["mono"])

        class DeadlineEvent:
            waits = []

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits.append(timeout)
                values["mono"] += timeout
                values["wall"] += timeout
                return False

        event = DeadlineEvent()
        self.assertTrue(clock.wait_until(23.125, event))
        self.assertEqual(event.waits, [13.125])

    def test_cancelled_wait_returns_without_waiting(self):
        clock = Clock()

        class Cancelled:
            def is_set(self):
                return True

            def wait(self, timeout):
                raise AssertionError("a cancelled deadline must not wait")

        self.assertFalse(clock.wait_until(clock.monotonic() + 10, Cancelled()))

    def test_http_date_is_conservative_and_is_not_automatically_applied(self):
        epoch = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc).timestamp()
        date = format_datetime(datetime.fromtimestamp(epoch, timezone.utc), usegmt=True)
        clock = Clock(wall_time=lambda: epoch, monotonic=lambda: 10.0)
        sample = estimate_http_date(date, sent_wall=epoch - 0.2,
                                    sent_monotonic=10.0, received_monotonic=10.4,
                                    received_wall=epoch + 0.2)
        self.assertAlmostEqual(sample["offset_ms"], 500.0, delta=0.01)
        self.assertEqual(sample["uncertainty_ms"], 1200.0)
        self.assertEqual(sample["precision"], "seconds")
        self.assertFalse(sample["applied"])
        self.assertEqual(clock.manual_offset_ms, 0.0)
        self.assertIn("CDN", sample["notice"])

    def test_cached_date_and_clock_jump_are_rejected(self):
        date = "Wed, 04 Nov 2026 12:00:00 GMT"
        args = {"sent_wall": 1_800_000_000.0, "sent_monotonic": 10.0, "received_monotonic": 10.1}
        with self.assertRaises(ValueError):
            estimate_http_date(date, age_header="12", **args)
        with self.assertRaises(ValueError):
            estimate_http_date(date, received_wall=args["sent_wall"] + 3.0, **args)
        with self.assertRaises(ValueError):
            estimate_http_date("not a date", **args)

    def test_invalid_offsets_are_rejected(self):
        clock = Clock()
        for value in (math.nan, math.inf, -math.inf, "not a number", 400_000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clock.set_manual_offset_ms(value)

    def test_sampling_rejects_non_public_or_credential_urls_before_network(self):
        for url in (
            "http://www.5idream.net/", "https://localhost/", "https://example.test/",
            "https://user:secret@www.5idream.net/", "https://www.5idream.net/?token=secret",
            "https://www.5idream.net/activity/detail", "https://www.5idream.net:8443/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                sample_https_date(url)


class NtpClockTests(unittest.TestCase):
    def setUp(self):
        self.values = {"wall": 1_800_000_000.0, "mono": 100.0}
        self.clock = Clock(wall_time=lambda: self.values["wall"], monotonic=lambda: self.values["mono"])

    def result(self, *, offset=200.0, uncertainty=5.0, mono=None, generation=1, providers=("aliyun", "tencent"), status="ready"):
        mono = self.values["mono"] if mono is None else mono
        sampled_at = 1_800_000_000.0 + mono - 100.0
        estimates = tuple(ProviderEstimate(provider, "ntp." + provider + ".com", offset, uncertainty,
                                           10.0, 2, sampled_at, mono, 2) for provider in providers)
        return SyncResult(status, "离线测试结果", generation, offset, uncertainty, 10.0,
                          sampled_at, mono, estimates)

    def advance(self, seconds):
        self.values["mono"] += seconds
        self.values["wall"] += seconds

    def test_apply_uses_fixed_base_and_freeze_does_not_move_with_later_sync(self):
        target = self.values["wall"] + 10
        applied = self.clock.apply_sync(self.result(offset=200))
        self.assertTrue(applied["applied"])
        frozen = self.clock.freeze()
        deadline = frozen.deadline_monotonic(target)
        self.assertAlmostEqual(deadline, 109.8)
        self.assertEqual(frozen.source, "ntp")
        self.assertTrue(frozen.quality(10)["ready"])
        self.advance(1)
        self.clock.apply_sync(self.result(offset=400, generation=2))
        self.assertEqual(self.clock.offset_ms, 400)
        self.assertEqual(self.clock.manual_offset_ms, 0)
        self.assertAlmostEqual(self.clock.deadline_monotonic(target), 109.6)
        self.assertEqual(frozen.deadline_monotonic(target), deadline)
        self.assertAlmostEqual(frozen.epoch_from_monotonic(deadline), target)
        with self.assertRaises(FrozenInstanceError):
            frozen.offset_ms = 900

    def test_apply_new_service_generation_one_is_allowed_but_old_sample_is_not(self):
        first = self.result(offset=200, generation=99)
        self.clock.apply_sync(first)
        initial_clock_generation = self.clock.freeze().generation
        self.advance(2)
        self.assertTrue(self.clock.apply_sync(self.result(offset=300, generation=1))["applied"])
        self.assertGreater(self.clock.freeze().generation, initial_clock_generation)
        self.assertFalse(self.clock.apply_sync(first)["applied"])
        self.assertEqual(self.clock.offset_ms, 300)

    def test_clock_quality_expires_and_projects_drift_budget(self):
        self.clock.apply_sync(self.result(uncertainty=98.0))
        self.assertTrue(self.clock.quality()["ready"])
        self.assertTrue(self.clock.ready_for_seconds(30))
        self.assertFalse(self.clock.ready_for_seconds(60))
        self.assertAlmostEqual(self.clock.quality(60)["projected_uncertainty_ms"], 101.0)
        self.advance(901)
        quality = self.clock.quality()
        self.assertFalse(quality["ready"])
        self.assertIn("过期", quality["reason"])
        self.assertEqual(quality["age_seconds"], 901)
        self.assertEqual(quality["valid_for_seconds"], 0)

    def test_frozen_quality_ages_without_changing_its_mapping(self):
        self.clock.apply_sync(self.result())
        frozen = self.clock.freeze()
        deadline = frozen.deadline_monotonic(self.values["wall"] + 10)
        self.advance(850)
        self.assertTrue(frozen.quality(20)["ready"])
        self.assertFalse(frozen.quality(60)["ready"])
        self.assertEqual(frozen.deadline_monotonic(1_800_000_010.0), deadline)

    def test_system_wall_step_does_not_move_ntp_corrected_time(self):
        self.clock.apply_sync(self.result(offset=250))
        first = self.clock.now()
        self.advance(1)
        self.values["wall"] += 7200
        self.assertAlmostEqual(self.clock.now(), first + 1)
        self.assertTrue(self.clock.quality()["ready"])
        state = self.clock.status()
        self.assertAlmostEqual(state["offset_vs_system_ms"], -7_199_750.0, delta=0.001)
        self.assertEqual({item["provider"] for item in state["sources"]}, {"aliyun", "tencent"})

    def test_single_provider_uncertain_stale_and_future_results_cannot_apply(self):
        for result in (
            self.result(providers=("aliyun", "aliyun")), self.result(status="inconsistent"),
            self.result(uncertainty=101), self.result(mono=-801), self.result(mono=101),
            self.result(offset=math.nan), self.result(offset=90_000_000),
        ):
            with self.assertRaises(ValueError):
                self.clock.apply_sync(result)
            self.assertFalse(self.clock.quality()["ready"])
            self.assertEqual(self.clock.offset_ms, 0)

    def test_manual_time_is_not_mislabeled_as_verified_ntp(self):
        self.clock.set_manual_offset_ms(500)
        self.assertEqual(self.clock.status()["source"], "manual")
        self.assertFalse(self.clock.quality()["ready"])
        self.clock.apply_sync(self.result(offset=200))
        self.assertEqual(self.clock.offset_ms, 200)
        self.assertEqual(self.clock.manual_offset_ms, 0)
        self.assertTrue(self.clock.quality()["ready"])

    def test_invalid_quality_horizon_is_rejected(self):
        for value in (-1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                self.clock.quality(value)


if __name__ == "__main__":
    unittest.main()
