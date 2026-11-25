"""NTP 报文、共识和后台生命周期离线测试；无真实 DNS/UDP 请求。"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import math
import socket
import struct
import threading
import time
import unittest

from precision.clock import Clock
from precision.time_sync import (
    DEFAULT_SOURCES, NtpClient, NtpError, NtpSample, NtpSource, SyncCancelled,
    TimeSyncService, build_request, combine_samples, decode_timestamp,
    encode_timestamp, parse_response,
)


EPOCH = 1_800_000_000.0
ALI = NtpSource("aliyun", "ntp.aliyun.com")
TENCENT = NtpSource("tencent", "ntp.tencent.com")


def response(request, *, t2, t3, reference=None, originate=None, li=0, version=4,
             mode=4, stratum=2, precision=-20, root_delay=0.0, dispersion=0.001,
             reference_id=b"TEST"):
    return struct.pack(
        "!BBbbiI4s8s8s8s8s", (li << 6) | (version << 3) | mode,
        stratum, 6, precision, int(root_delay * 65536), int(dispersion * 65536), reference_id,
        encode_timestamp(t3 - 60) if reference is None else reference,
        request[40:48] if originate is None else originate,
        encode_timestamp(t2), encode_timestamp(t3),
    )


def sample(provider="aliyun", host="ntp.aliyun.com", *, offset=100.0, uncertainty=5.0,
           rtt=10.0, mono=100.0):
    return NtpSample(provider, host, "192.0.2.1", offset, uncertainty, rtt, rtt,
                     EPOCH + mono - 100.0, mono)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.request = build_request(EPOCH)

    def parse(self, packet, *, sent=EPOCH, received=EPOCH + 0.045, before=10.0, after=10.045, request=None):
        return parse_response(packet, self.request if request is None else request,
                              source=ALI, address="192.0.2.10", sent_unix=sent, received_unix=received,
                              sent_monotonic=before, received_monotonic=after)

    def test_standard_four_timestamps_remove_server_processing_time(self):
        packet = response(self.request, t2=EPOCH + 0.123 + 0.020, t3=EPOCH + 0.123 + 0.025)
        result = self.parse(packet)
        self.assertAlmostEqual(result.offset_ms, 123.0, delta=0.002)
        self.assertAlmostEqual(result.rtt_ms, 45.0, delta=0.002)
        self.assertAlmostEqual(result.delay_ms, 40.0, delta=0.002)
        self.assertGreaterEqual(result.uncertainty_ms, 20.0)
        self.assertLess(result.uncertainty_ms, 22.0)

    def test_asymmetry_is_not_claimed_as_exact_true_offset(self):
        packet = response(self.request, t2=EPOCH + 0.100 + 0.010, t3=EPOCH + 0.100 + 0.010)
        result = self.parse(packet, received=EPOCH + 0.060, after=10.060)
        self.assertAlmostEqual(result.offset_ms, 80.0, delta=0.002)
        self.assertLessEqual(abs(result.offset_ms - 100.0), result.uncertainty_ms)

    def test_protocol_header_and_originate_validation(self):
        cases = [({"mode": 3}, "MODE"), ({"version": 2}, "VERSION"),
                 ({"li": 3}, "UNSYNCHRONIZED"), ({"stratum": 16}, "STRATUM"),
                 ({"originate": bytes(8)}, "ORIGINATE"), ({"precision": 10}, "PRECISION"),
                 ({"root_delay": -1}, "ROOT_DISTANCE"), ({"dispersion": 11}, "ROOT_DISTANCE")]
        for change, expected in cases:
            packet = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025, **change)
            with self.subTest(change=change), self.assertRaises(NtpError) as caught:
                self.parse(packet)
            self.assertEqual(caught.exception.code, expected)

    def test_truncated_and_unnegotiated_extension_packets_are_rejected(self):
        good = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025)
        for packet in (good[:47], good + bytes(4), bytes(0)):
            with self.assertRaises(NtpError) as caught:
                self.parse(packet)
            self.assertEqual(caught.exception.code, "PACKET_LENGTH")

    def test_rate_kiss_requires_matching_request_and_is_not_a_time_sample(self):
        packet = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025,
                          stratum=0, reference_id=b"RATE")
        with self.assertRaises(NtpError) as caught:
            self.parse(packet)
        self.assertEqual(caught.exception.code, "KOD_RATE")
        unsynced = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025,
                            stratum=0, reference_id=b"RATE", li=3)
        with self.assertRaises(NtpError) as caught:
            self.parse(unsynced)
        self.assertEqual(caught.exception.code, "KOD_RATE")
        changed = bytearray(packet)
        changed[24:32] = bytes(8)
        with self.assertRaises(NtpError) as caught:
            self.parse(bytes(changed))
        self.assertEqual(caught.exception.code, "ORIGINATE")

    def test_zero_timestamps_server_order_and_reference_age_are_rejected(self):
        normal = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025)
        for offset in (16, 32, 40):
            broken = bytearray(normal)
            broken[offset:offset + 8] = bytes(8)
            with self.assertRaises(NtpError):
                self.parse(bytes(broken))
        for packet in (
            response(self.request, t2=EPOCH + 0.03, t3=EPOCH + 0.01),
            response(self.request, t2=EPOCH, t3=EPOCH + 1),
            response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025, reference=encode_timestamp(EPOCH + 1)),
            response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025, reference=encode_timestamp(EPOCH - 90_000)),
        ):
            with self.assertRaises(NtpError):
                self.parse(packet)

    def test_local_discontinuity_negative_rtt_and_huge_offset_are_rejected(self):
        packet = response(self.request, t2=EPOCH + 0.02, t3=EPOCH + 0.025)
        for args in ({"received": EPOCH + 2}, {"after": 9}, {"after": 15, "received": EPOCH + 5}):
            with self.assertRaises(NtpError):
                self.parse(packet, **args)
        packet = response(self.request, t2=EPOCH + 90_000, t3=EPOCH + 90_000.005)
        with self.assertRaises(NtpError) as caught:
            self.parse(packet)
        self.assertEqual(caught.exception.code, "OFFSET_RANGE")

    def test_2036_era_rollover_is_decoded_against_local_reference(self):
        epoch = datetime(2036, 2, 7, 6, 28, 15, tzinfo=timezone.utc).timestamp()
        request = build_request(epoch)
        packet = response(request, t2=epoch + 1.020, t3=epoch + 1.025)
        result = self.parse(packet, sent=epoch, received=epoch + 0.045, request=request)
        self.assertAlmostEqual(result.offset_ms, 1000, delta=0.002)
        for when in (epoch - 5, epoch, epoch + 5):
            self.assertAlmostEqual(decode_timestamp(encode_timestamp(when), reference_unix=when), when, delta=0.000001)


class ConsensusTests(unittest.TestCase):
    def test_successful_sync_time_is_calibrated_utc_even_when_pc_is_minutes_off(self):
        offset = 600_000.0
        result = combine_samples([
            sample(offset=offset),
            sample("tencent", "ntp.tencent.com", offset=offset),
        ], now_monotonic=100.0)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.sampled_at, EPOCH + 600.0)
        self.assertEqual(result.sampled_monotonic, 100.0)

    def test_lowest_rtt_is_selected_per_provider_not_per_host_votes(self):
        samples = [sample(host="ntp.aliyun.com", offset=100, uncertainty=40, rtt=80),
                   sample(host="ntp1.aliyun.com", offset=102, uncertainty=5, rtt=10),
                   sample("tencent", "ntp.tencent.com", offset=104, uncertainty=6, rtt=12)]
        result = combine_samples(samples, now_monotonic=100)
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.providers), 2)
        self.assertEqual(result.offset_ms, 103)
        self.assertEqual(result.providers[0].host, "ntp1.aliyun.com")
        self.assertIn({"provider": "aliyun", "host": "ntp1.aliyun.com"}, result.to_dict()["sources"])
        self.assertGreaterEqual(result.uncertainty_ms, 7)

    def test_multiple_ali_host_labels_cannot_form_independent_majority(self):
        values = [sample("first-label", "ntp.aliyun.com"), sample("second-label", "ntp1.aliyun.com")]
        result = combine_samples(values, now_monotonic=100)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(len(result.providers), 1)
        self.assertIsNone(result.offset_ms)
        self.assertEqual(result.to_dict()["sources"], [])
        self.assertEqual(NtpSource("pretend-third", "ntp2.aliyun.com").provider, "aliyun")

    def test_two_providers_disagreeing_are_not_arbitrarily_averaged(self):
        result = combine_samples([sample(offset=0), sample("tencent", "ntp.tencent.com", offset=500)], now_monotonic=100)
        self.assertEqual(result.status, "inconsistent")
        self.assertIsNone(result.offset_ms)

    def test_three_independent_providers_can_exclude_one_outlier(self):
        result = combine_samples([sample(offset=0), sample("tencent", "ntp.tencent.com", offset=2),
                                  sample("third", "ntp.third.example", offset=500)], now_monotonic=100)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.offset_ms, 1)
        self.assertEqual({provider.provider for provider in result.providers}, {"aliyun", "tencent"})
        self.assertTrue(any(item.code == "PROVIDER_OUTLIER" and item.provider == "third" for item in result.rejected))

    def test_equal_independent_cohorts_do_not_make_a_majority(self):
        samples = [sample(offset=0), sample("tencent", "ntp.tencent.com", offset=2),
                   sample("third", "ntp.third.example", offset=500),
                   sample("fourth", "ntp.fourth.example", offset=502)]
        self.assertEqual(combine_samples(samples, now_monotonic=100).status, "inconsistent")

    def test_provider_with_two_conflicting_hosts_is_excluded(self):
        samples = [sample(offset=0), sample(host="ntp1.aliyun.com", offset=500),
                   sample("tencent", "ntp.tencent.com", offset=2)]
        result = combine_samples(samples, now_monotonic=100)
        self.assertEqual(result.status, "degraded")
        self.assertTrue(any(item.code == "PROVIDER_UNSTABLE" for item in result.rejected))

    def test_stale_nan_and_high_uncertainty_samples_cannot_be_applied(self):
        for broken in (sample(mono=60), sample(offset=math.nan), sample(uncertainty=1000),
                       sample(rtt=-1), sample(mono=101)):
            result = combine_samples([broken, sample("tencent", "ntp.tencent.com")], now_monotonic=100)
            self.assertNotEqual(result.status, "ready")
            self.assertIsNone(result.offset_ms)

    def test_consensus_does_not_fake_precision_from_many_samples(self):
        result = combine_samples([sample(offset=0, uncertainty=80, rtt=160),
                                  sample("tencent", "ntp.tencent.com", offset=120, uncertainty=80, rtt=160)],
                                 now_monotonic=100)
        self.assertEqual(result.status, "degraded")
        self.assertGreater(result.uncertainty_ms, 100)
        self.assertIsNone(result.offset_ms)

    def test_published_results_are_immutable_and_lists_are_copies(self):
        result = combine_samples([sample(), sample("tencent", "ntp.tencent.com")], now_monotonic=100)
        with self.assertRaises(FrozenInstanceError):
            result.offset_ms = 999
        output = result.to_dict(now_monotonic=101)
        output["providers"][0]["offset_ms"] = 999
        self.assertEqual(result.providers[0].offset_ms, 100)
        self.assertEqual(output["age_seconds"], 1)


class TransportTests(unittest.TestCase):
    def test_fake_udp_uses_uncorrected_anchor_and_connected_source(self):
        values = {"wall": EPOCH, "mono": 10.0}
        clock = Clock(wall_time=lambda: values["wall"], monotonic=lambda: values["mono"])
        clock.set_manual_offset_ms(200)

        class FakeSocket:
            connected = None
            request = None
            closed = False

            def connect(self, address):
                self.connected = address

            def settimeout(self, value):
                self.timeout = value

            def send(self, request):
                self.request = request

            def recv(self, limit):
                values["mono"] += 0.04
                values["wall"] += 0.04
                return response(self.request, t2=EPOCH + 0.32, t3=EPOCH + 0.32)

            def close(self):
                self.closed = True

        channel = FakeSocket()
        resolver = lambda *args: [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("192.0.2.10", 123))]
        client = NtpClient(clock, resolver=resolver, socket_factory=lambda *args: channel)
        result = client.sample(ALI)
        self.assertAlmostEqual(result.offset_ms, 300, delta=0.002)
        self.assertAlmostEqual(decode_timestamp(channel.request[40:48], reference_unix=EPOCH), EPOCH, delta=0.000001)
        self.assertEqual(channel.connected, ("192.0.2.10", 123))
        self.assertTrue(channel.closed)

    def test_cancel_before_dns_does_not_create_a_socket(self):
        cancel = threading.Event()
        cancel.set()
        clock = Clock()

        def forbidden(*args):
            raise AssertionError("取消后不能开始解析或 UDP")

        client = NtpClient(clock, resolver=forbidden, socket_factory=forbidden)
        with self.assertRaises(SyncCancelled):
            client.sample(ALI, cancel)

    def test_cancel_during_dns_prevents_later_udp_send(self):
        cancel = threading.Event()

        def resolver(*args):
            cancel.set()
            return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("192.0.2.10", 123))]

        def forbidden(*args):
            raise AssertionError("DNS结束后已取消，不能创建UDP请求")

        with self.assertRaises(SyncCancelled):
            NtpClient(Clock(), resolver=resolver, socket_factory=forbidden).sample(ALI, cancel)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(wall_time=lambda: EPOCH, monotonic=lambda: 100.0)

    def service(self, sampler, **kwargs):
        service = TimeSyncService(self.clock, sample_fn=sampler, retry_delay=0, **kwargs)
        self.addCleanup(service.close)
        return service

    def test_construction_is_offline_and_success_is_measured_not_applied(self):
        calls = []

        def sampler(source, cancel):
            calls.append(source)
            return sample(source.provider, source.host)

        service = self.service(sampler)
        self.assertEqual(calls, [])
        self.assertEqual(service.snapshot()["status"], "idle")
        service.start()
        service.wait(2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(service.snapshot()["status"], "ready")
        self.assertEqual(self.clock.offset_ms, 0)
        self.assertFalse(self.clock.quality()["ready"])
        self.clock.apply_sync(service.latest_result)
        self.assertTrue(self.clock.quality()["ready"])
        self.assertEqual(self.clock.offset_ms, 100)

    def test_failed_round_has_finite_retries_and_no_background_polling(self):
        calls = []

        def sampler(source, cancel):
            calls.append(source)
            raise NtpError("TIMEOUT", "离线模拟 UDP 超时")

        service = self.service(sampler)
        service.start()
        service.wait(2)
        self.assertEqual(len(calls), 8)
        self.assertEqual(service.snapshot()["status"], "failed")
        self.assertEqual(service.latest_result.status, "unavailable")
        self.assertFalse(service.running)
        service.wait(0.01)
        self.assertEqual(len(calls), 8)

    def test_one_reachable_provider_does_not_become_automatic_time(self):
        def sampler(source, cancel):
            if source.provider == "tencent":
                raise NtpError("TIMEOUT", "离线超时")
            return sample(source.provider, source.host)

        service = self.service(sampler)
        service.start()
        service.wait(2)
        self.assertEqual(service.snapshot()["status"], "failed")
        self.assertEqual(service.latest_result.status, "degraded")
        self.assertEqual(self.clock.offset_ms, 0)

    def test_close_is_nonblocking_and_late_result_cannot_publish(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def sampler(source, cancel):
            calls.append(source)
            entered.set()
            release.wait(2)
            return sample(source.provider, source.host)

        service = self.service(sampler)
        service.start()
        self.assertTrue(entered.wait(1))
        generation = service.snapshot()["generation"]
        service.start()
        self.assertEqual(service.snapshot()["generation"], generation)
        before = time.monotonic()
        service.close()
        self.assertLess(time.monotonic() - before, 0.1)
        release.set()
        service.wait(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.snapshot()["status"], "cancelled")
        self.assertIsNone(service.latest_result)
        service.start()
        self.assertEqual(len(calls), 1)

    def test_rate_kiss_stops_more_hosts_of_the_same_provider_in_that_run(self):
        calls = []

        def sampler(source, cancel):
            calls.append(source)
            if source.provider == "aliyun":
                raise NtpError("KOD_RATE", "来源限流")
            return sample(source.provider, source.host)

        service = self.service(sampler)
        service.start()
        service.wait(2)
        self.assertEqual(sum(source.provider == "aliyun" for source in calls), 1)
        self.assertEqual(len(calls), 5)
        self.assertEqual(service.latest_result.status, "degraded")

    def test_misattributed_sample_cannot_create_a_second_provider(self):
        service = self.service(lambda source, cancel: sample())
        service.start()
        service.wait(2)
        self.assertNotEqual(service.latest_result.status, "ready")
        self.assertTrue(any(item.code == "SOURCE_MISMATCH" for item in service.latest_result.rejected))


if __name__ == "__main__":
    unittest.main()
