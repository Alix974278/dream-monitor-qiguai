"""离线精确定时回归：虚拟时间、假 Hub、假网络和临时 SQLite。

不运行原项目自测账号，不访问任何官网、NTP、hook 或 probe。
"""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autosign import Scheduler
from precision.clock import Clock
from users import Users


class VirtualTime:
    def __init__(self):
        self.wall_value, self.mono_value = 1_800_000_000.0, 100.0
        self.cv = threading.Condition()
        self.waiters = 0

    def wall(self):
        with self.cv:
            return self.wall_value

    def mono(self):
        with self.cv:
            return self.mono_value

    def advance(self, seconds):
        with self.cv:
            self.wall_value += seconds
            self.mono_value += seconds
            self.cv.notify_all()

    def jump_wall(self, seconds):
        with self.cv:
            self.wall_value += seconds
            self.cv.notify_all()

    def event(self):
        owner = self

        class Event:
            flag = False

            def is_set(self):
                with owner.cv:
                    return self.flag

            def set(self):
                with owner.cv:
                    self.flag = True
                    owner.cv.notify_all()

            def wait(self, timeout=None):
                with owner.cv:
                    end = float('inf') if timeout is None else owner.mono_value + timeout
                    owner.waiters += 1
                    owner.cv.notify_all()
                    try:
                        while not self.flag and owner.mono_value < end:
                            owner.cv.wait()
                        return self.flag
                    finally:
                        owner.waiters -= 1
                        owner.cv.notify_all()

        return Event()

    def waiting(self, count=1):
        with self.cv:
            return self.cv.wait_for(lambda: self.waiters >= count, timeout=1)


class FakeAuthority:
    def __init__(self, clock):
        self.clock = clock
        self.frozen, self.released = [], []
        self.lock = threading.RLock()

    def freeze(self, key):
        with self.lock:
            self.frozen.append(key)
            return self.clock.freeze()

    def release(self, key):
        with self.lock:
            self.released.append(key)


class FakeUsers:
    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.version = 0
        self.lock = threading.RLock()
        self.local = threading.local()
        self.history = []
        self.recoveries = 0

    def _check(self):
        if getattr(self.local, 'in_send', False):
            raise AssertionError('on_send must perform zero database operations')

    def auto_add(self, uid, aid, name, join_start):
        self._check()
        with self.lock:
            self.version += 1
            self.rows[(uid, aid)] = dict(uid=uid, aid=aid, name=name, join_start=join_start,
                                         created=float(self.version), status='pending', note='', fired_at=None)

    def auto_get(self, uid, aid):
        self._check()
        with self.lock:
            return deepcopy(self.rows.get((uid, aid)))

    def auto_due(self, now, window=15):
        self._check()
        with self.lock:
            return sorted((deepcopy(row) for row in self.rows.values()
                           if row['status'] in ('pending', 'retry') and row['join_start'] - window <= now),
                          key=lambda row: row['join_start'])

    def auto_del(self, uid, aid):
        self._check()
        with self.lock:
            self.rows.pop((uid, aid), None)

    def auto_mark_if_current(self, uid, aid, created, start, status, note='', *, expected_statuses=None):
        self._check()
        with self.lock:
            row = self.rows.get((uid, aid))
            if not row or (row['created'], row['join_start']) != (created, start):
                return False
            if expected_statuses is not None and row['status'] not in expected_statuses:
                return False
            row.update(status=status, note=note, fired_at=self.clock.now())
            self.history.append((uid, aid, status, self.clock.monotonic(), created))
            return True

    def auto_claim(self, uid, aid, created, start):
        return self.auto_mark_if_current(uid, aid, created, start, 'claimed', expected_statuses=('pending', 'retry'))

    def auto_recover_claimed(self):
        self._check()
        with self.lock:
            self.recoveries += 1
            count = 0
            for row in self.rows.values():
                if row['status'] in ('claimed', 'submitting'):
                    row['status'] = 'unknown'
                    count += 1
            return count


class FakeHub:
    def __init__(self, clock, users):
        self.clock, self.users = clock, users
        self.prepared, self.invoked, self.posts, self.confirmed, self.closed = [], [], [], [], []
        self.scripts = defaultdict(lambda: ['pending_confirmation'])
        self.confirmations = defaultdict(lambda: {'status': 'confirmed', 'signup_id': '700'})
        self.prefetched = defaultdict(lambda: True)
        self.prepare_errors = defaultdict(int)
        self.fallback_reads = []
        self.prepare_gate, self.before_gate, self.after_gate = {}, {}, {}
        self.prepare_entered = defaultdict(threading.Event)
        self.submit_entered = defaultdict(threading.Event)
        self.sent = defaultdict(threading.Event)

    def prepare_signup(self, aid):
        self.prepared.append((aid, self.clock.monotonic()))
        self.prepare_entered[aid].set()
        gate = self.prepare_gate.get(aid)
        if gate is not None and not gate.wait(2):
            raise TimeoutError('offline prepare gate')
        if self.prepare_errors[aid]:
            self.prepare_errors[aid] -= 1
            raise TimeoutError('offline prefetch unavailable')
        return SimpleNamespace(aid=aid, prefetched=self.prefetched[aid],
                               prepared_at=self.clock.now() if self.prefetched[aid] else None,
                               note='原流程到点再读' if not self.prefetched[aid] else '')

    def submit_prepared(self, context, on_send):
        aid = context.aid
        self.invoked.append(aid)
        self.submit_entered[aid].set()
        gate = self.before_gate.get(aid)
        if gate is not None and not gate.wait(2):
            raise TimeoutError('offline before-send gate')
        if not context.prefetched:
            self.fallback_reads.append(aid)
            context.prefetched = True
            context.prepared_at = self.clock.now()
            context.note = '到点按原流程取得表单'
        self.users.local.in_send = True
        try:
            on_send({'wall': self.clock.system_now(), 'monotonic': self.clock.monotonic(),
                     'timestamp': int(self.clock.now() * 1000), 'activity_id': aid})
        finally:
            self.users.local.in_send = False
        self.posts.append((aid, self.clock.monotonic()))
        self.sent[aid].set()
        gate = self.after_gate.get(aid)
        if gate is not None and not gate.wait(2):
            raise TimeoutError('offline after-send gate')
        script = self.scripts[aid]
        value = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(value, Exception):
            raise value
        return {'status': value, 'msg': '离线结果', 'code': '2000166' if value == 'full' else '100'}

    def confirm_signup(self, context):
        self.confirmed.append(context.aid)
        return self.confirmations[context.aid]

    def close_prepared(self, context):
        self.closed.append(context.aid)


class PreciseSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.virtual = VirtualTime()
        self.clock = Clock(wall_time=self.virtual.wall, monotonic=self.virtual.mono)
        self.authority = FakeAuthority(self.clock)
        self.users = FakeUsers(self.clock)
        self.hub = FakeHub(self.clock, self.users)
        self.scheduler = Scheduler(self.users, lambda uid: self.hub, authority=self.authority,
                                   event_factory=self.virtual.event)

    def tearDown(self):
        for gates in (self.hub.prepare_gate, self.hub.before_gate, self.hub.after_gate):
            for gate in gates.values():
                gate.set()
        self.scheduler.stop(join_timeout=1)

    def eventually(self, predicate, label='condition'):
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.001)
        self.fail('timed out waiting for ' + label)

    def timing(self, aid='a'):
        return self.scheduler.snapshot('u')[aid]['timing']

    def state(self, state, aid='a'):
        self.eventually(lambda: self.timing(aid)['phase'] == state, aid + ' phase ' + state)
        return self.timing(aid)

    def add(self, aid='a', delay=5):
        start = self.clock.now() + delay
        self.users.auto_add('u', aid, '离线活动 ' + aid, start)
        self.scheduler.tick()
        return start

    def ready(self, count=1):
        self.eventually(lambda: len([item for item in self.users.history if item[2] == 'submitting']) >= count,
                        'durable marker before final wait')
        self.assertTrue(self.virtual.waiting(count))

    def test_no_ntp_consensus_does_not_gate_original_local_scheduling(self):
        self.assertFalse(self.clock.quality()['ready'])
        planned = self.add(delay=10)
        self.ready()
        self.virtual.advance(9.5)
        self.assertEqual(self.hub.posts, [])
        self.virtual.advance(.5)
        timing = self.state('success')
        self.assertEqual(timing['actual_send_at'], planned)
        self.assertEqual(timing['actual_send_monotonic'], 110)
        self.assertEqual(timing['dispatch_lateness_ms'], 0)
        self.assertEqual(timing['clock_source'], 'system')
        self.assertEqual(timing['sent_count'], 1)
        self.assertEqual(self.hub.confirmed, ['a'])
        self.assertLess(self.users.history[1][3], timing['actual_send_monotonic'])

    def test_discovery_freeze_preparation_and_send_have_separate_deadlines(self):
        planned = self.add(delay=150)
        self.assertEqual(self.scheduler.snapshot('u'), {})
        self.virtual.advance(30)
        self.scheduler.tick()
        self.assertTrue(self.virtual.waiting())
        self.assertEqual(self.authority.frozen, [])
        self.virtual.advance(60)
        self.eventually(lambda: len(self.authority.frozen) == 1)
        self.assertEqual(self.hub.prepared, [])
        self.assertTrue(self.virtual.waiting())
        self.virtual.advance(30)
        self.ready()
        self.assertEqual(self.hub.prepared, [('a', 220)])
        self.assertEqual(self.hub.posts, [])
        self.virtual.advance(30)
        self.assertEqual(self.state('success')['actual_send_at'], planned)
        self.eventually(lambda: self.authority.released == self.authority.frozen)

    def test_slow_submission_does_not_delay_other_activity(self):
        gate = self.hub.after_gate['slow'] = threading.Event()
        self.add('slow')
        self.add('fast')
        self.ready(2)
        self.virtual.advance(5)
        self.assertTrue(self.hub.sent['slow'].wait(1))
        self.state('success', 'fast')
        self.assertTrue(self.timing('slow')['in_flight'])
        self.assertEqual(dict(self.hub.posts), {'slow': 105, 'fast': 105})
        gate.set()
        self.state('success', 'slow')

    def test_prefetch_failure_uses_original_flow_at_deadline(self):
        self.hub.prefetched['a'] = False
        planned = self.add(delay=10)
        self.ready()
        self.assertFalse(self.timing()['prefetched'])
        self.assertIsNone(self.timing()['prepared_at'])
        self.assertEqual(self.hub.fallback_reads, [])
        self.virtual.advance(10)
        timing = self.state('success')
        self.assertEqual(timing['actual_send_at'], planned)
        self.assertEqual(timing['prepared_at'], planned)
        self.assertTrue(timing['context_ready'])
        self.assertFalse(timing['prefetched'])
        self.assertEqual(timing['prepare_note'], '到点按原流程取得表单')
        self.assertEqual(self.hub.fallback_reads, ['a'])
        self.assertEqual(len(self.hub.prepared), 1)

    def test_unexpected_prepare_exception_is_retried_once_at_deadline(self):
        self.hub.prepare_errors['a'] = 1
        self.add()
        self.ready()
        self.assertEqual(len(self.hub.prepared), 1)
        self.virtual.advance(5)
        self.state('success')
        self.assertEqual(len(self.hub.prepared), 2)
        self.assertEqual(len(self.hub.posts), 1)

    def test_cancel_waiting_never_sends_and_releases_frozen_authority(self):
        self.add()
        self.ready()
        self.scheduler.cancel('u', 'a')
        self.users.auto_del('u', 'a')
        self.virtual.advance(50)
        self.eventually(lambda: len(self.authority.released) == 1)
        self.assertEqual(self.hub.posts, [])
        self.assertEqual(self.hub.confirmed, [])
        self.assertEqual(self.timing()['phase'], 'cancelled')
        self.assertEqual(self.hub.closed, ['a'])

    def test_cancel_during_preparation_does_not_fall_back_to_submission(self):
        gate = self.hub.prepare_gate['a'] = threading.Event()
        self.add()
        self.assertTrue(self.hub.prepare_entered['a'].wait(1))
        self.scheduler.cancel('u', 'a')
        self.users.auto_del('u', 'a')
        gate.set()
        self.virtual.advance(5)
        self.eventually(lambda: self.hub.closed == ['a'])
        self.assertEqual(self.hub.posts, [])
        self.assertEqual(self.hub.invoked, [])

    def test_cancel_just_before_post_is_checked_without_database_io(self):
        gate = self.hub.before_gate['a'] = threading.Event()
        self.add()
        self.ready()
        self.virtual.advance(5)
        self.assertTrue(self.hub.submit_entered['a'].wait(1))
        self.scheduler.cancel('u', 'a')
        self.users.auto_del('u', 'a')
        gate.set()
        self.eventually(lambda: not self.timing()['in_flight'])
        self.assertEqual(self.hub.posts, [])
        self.assertEqual(self.timing()['sent_count'], 0)

    def test_sent_unknown_tombstone_blocks_recreated_plan_and_late_response(self):
        gate = self.hub.after_gate['a'] = threading.Event()
        self.add()
        self.ready()
        self.virtual.advance(5)
        self.assertTrue(self.hub.sent['a'].wait(1))
        old_created = self.users.auto_get('u', 'a')['created']
        self.scheduler.cancel('u', 'a')
        self.users.auto_del('u', 'a')
        self.users.auto_add('u', 'a', '重新创建', self.clock.now() + 1)
        self.scheduler.tick()
        self.assertEqual(self.users.auto_get('u', 'a')['status'], 'unknown')
        self.assertNotEqual(self.users.auto_get('u', 'a')['created'], old_created)
        self.assertTrue(self.timing()['needs_confirmation'])
        gate.set()
        self.virtual.advance(100)
        self.eventually(lambda: not self.scheduler._active)
        self.assertEqual(self.users.auto_get('u', 'a')['status'], 'unknown')
        self.assertEqual(len(self.hub.posts), 1)
        self.assertEqual(self.hub.confirmed, [])

    def test_cancelled_unsent_plan_can_be_recreated_without_old_response_overwrite(self):
        gate = self.hub.before_gate['a'] = threading.Event()
        self.add()
        self.ready()
        self.virtual.advance(5)
        self.assertTrue(self.hub.submit_entered['a'].wait(1))
        self.scheduler.cancel('u', 'a')
        self.users.auto_del('u', 'a')
        del self.hub.before_gate['a']
        planned = self.add(delay=5)
        self.eventually(lambda: len([row for row in self.users.history if row[2] == 'submitting']) == 2)
        self.assertTrue(self.virtual.waiting())
        gate.set()
        self.virtual.advance(5)
        timing = self.state('success')
        self.assertEqual(timing['actual_send_at'], planned)
        self.assertEqual(len(self.hub.posts), 1)
        self.eventually(lambda: not self.scheduler._active)
        self.assertEqual(self.users.auto_get('u', 'a')['status'], 'success')

    def test_only_not_open_gets_two_short_bounded_retries(self):
        self.hub.scripts['a'] = ['not_open', 'not_open', 'pending_confirmation']
        self.add()
        self.ready()
        self.virtual.advance(5)
        self.state('retrying')
        self.assertTrue(self.virtual.waiting())
        self.virtual.advance(.25)
        self.eventually(lambda: len(self.hub.posts) == 2)
        self.assertTrue(self.virtual.waiting())
        self.virtual.advance(.5)
        self.state('success')
        self.assertEqual([when for aid, when in self.hub.posts], [105, 105.25, 105.75])
        self.virtual.advance(600)
        self.scheduler.tick()
        self.assertEqual(len(self.hub.posts), 3)

    def test_definite_full_auth_rate_limit_and_rejections_do_not_retry(self):
        for status in ('full', 'auth_required', 'rate_limited', 'manual_required', 'failed', 'cancelled'):
            with self.subTest(status=status):
                aid = status
                self.hub.scripts[aid] = [status]
                before = len(self.users.history)
                self.add(aid, delay=1)
                self.eventually(lambda: any(row[1] == aid and row[2] == 'submitting'
                                           for row in self.users.history[before:]))
                self.virtual.advance(1)
                timing = self.state('failed', aid)
                self.assertEqual(timing['result_status'], status)
                self.assertEqual(sum(key == aid for key, _ in self.hub.posts), 1)
                self.assertNotIn(aid, self.hub.confirmed)

    def test_unknown_or_timeout_only_confirms_and_never_blindly_repeats(self):
        for index, outcome in enumerate(('unknown', TimeoutError('offline response lost'))):
            aid = 'unknown-' + str(index)
            self.hub.scripts[aid] = [outcome]
            self.hub.confirmations[aid] = {'status': 'unknown', 'signup_id': ''}
            self.add(aid, delay=1)
            self.eventually(lambda: self.timing(aid)['prefetched'])
            self.virtual.advance(1)
            self.state('unknown', aid)
            self.virtual.advance(10)
            self.scheduler.tick()
            self.assertEqual(sum(key == aid for key, _ in self.hub.posts), 1)
            self.assertEqual(self.hub.confirmed.count(aid), 1)

    def test_acceptance_without_positive_personal_id_is_not_success(self):
        self.hub.confirmations['a'] = {'status': 'confirmed', 'signup_id': '0'}
        self.add()
        self.ready()
        self.virtual.advance(5)
        self.state('unknown')
        self.assertEqual(self.users.auto_get('u', 'a')['status'], 'unknown')

    def test_wall_step_and_later_clock_correction_do_not_move_frozen_deadline(self):
        planned = self.add(delay=10)
        self.ready()
        self.virtual.advance(1)
        self.virtual.jump_wall(3600)
        self.clock.set_manual_offset_ms(2000)
        self.virtual.advance(8.5)
        self.assertEqual(self.hub.posts, [])
        self.virtual.advance(.5)
        timing = self.state('success')
        self.assertEqual(timing['actual_send_at'], planned)
        self.assertEqual(timing['clock_offset_ms'], 0)
        self.assertEqual(timing['actual_send_monotonic'], 110)

    def test_far_wait_uses_new_internal_mapping_before_sixty_second_freeze(self):
        planned = self.add(delay=110)
        self.assertTrue(self.virtual.waiting())
        with self.authority.lock:
            self.clock.set_manual_offset_ms(1000)
            self.virtual.advance(49)
        self.eventually(lambda: len(self.authority.frozen) == 1)
        self.assertEqual(self.timing()['clock_offset_ms'], 1000)
        self.assertEqual(self.timing()['planned_at'], planned)
        self.assertTrue(self.virtual.waiting())
        self.virtual.advance(30)
        self.ready()
        self.virtual.advance(30)
        self.assertEqual(self.state('success')['actual_send_at'], planned)

    def test_startup_claimed_tasks_become_unknown_and_future_pending_survives(self):
        for aid, status in (('old-claimed', 'claimed'), ('old-sending', 'submitting'), ('future', 'pending')):
            self.users.auto_add('u', aid, aid, self.clock.now() + (1000 if aid == 'future' else 5))
            self.users.rows[('u', aid)]['status'] = status
        self.scheduler.tick()
        self.assertEqual(self.users.auto_get('u', 'old-claimed')['status'], 'unknown')
        self.assertEqual(self.users.auto_get('u', 'old-sending')['status'], 'unknown')
        self.assertEqual(self.users.auto_get('u', 'future')['status'], 'pending')
        self.scheduler.tick()
        self.assertEqual(self.users.recoveries, 1)
        self.assertEqual(self.hub.prepared, [])

    def test_per_account_limit_preserves_overflow_until_a_slot_is_free(self):
        for i in range(5):
            self.users.auto_add('u', str(i), str(i), self.clock.now() + 20)
        started = self.scheduler.tick()
        self.assertEqual(len(started), 4)
        self.ready(4)
        self.assertEqual(self.users.auto_get('u', '4')['status'], 'pending')
        self.scheduler.cancel('u', '0')
        self.users.auto_del('u', '0')
        self.eventually(lambda: len(self.scheduler._active) == 3)
        self.scheduler.tick()
        self.eventually(lambda: any(aid == '4' for aid, _ in self.hub.prepared))
        self.assertEqual(len(self.scheduler._active), 4)
        self.assertEqual(self.hub.posts, [])

    def test_missing_hub_keeps_original_pending_behavior(self):
        self.scheduler.hub_of = lambda uid: None
        self.add()
        self.assertEqual(self.users.auto_get('u', 'a')['status'], 'pending')
        self.assertEqual(self.scheduler.snapshot('u'), {})


class ConditionalUsersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.users = Users(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def add(self, now=1000, start=2000):
        with patch('users.time.time', return_value=now):
            self.users.auto_add('offline', 'a', '离线活动', start)
        return self.users.auto_get('offline', 'a')

    def test_claim_and_updates_require_both_version_fields_and_expected_status(self):
        row = self.add()
        args = ('offline', 'a', row['created'], row['join_start'])
        self.assertFalse(self.users.auto_claim('offline', 'a', row['created'] + 1, row['join_start']))
        self.assertFalse(self.users.auto_claim('offline', 'a', row['created'], row['join_start'] + 1))
        self.assertTrue(self.users.auto_claim(*args))
        self.assertFalse(self.users.auto_claim(*args))
        self.assertFalse(self.users.auto_mark_if_current(*args, 'success', expected_statuses=('pending',)))
        self.assertTrue(self.users.auto_mark_if_current(*args, 'submitting', expected_statuses=('claimed',)))
        self.assertEqual(self.users.auto_get('offline', 'a')['status'], 'submitting')

    def test_deleted_and_recreated_plan_cannot_be_overwritten_by_old_response(self):
        old = self.add()
        self.users.auto_claim('offline', 'a', old['created'], old['join_start'])
        self.users.auto_del('offline', 'a')
        current = self.add(now=1001)
        self.assertFalse(self.users.auto_mark_if_current('offline', 'a', old['created'], old['join_start'], 'success'))
        self.assertEqual(self.users.auto_get('offline', 'a')['created'], current['created'])
        self.assertEqual(self.users.auto_get('offline', 'a')['status'], 'pending')

    def test_recovery_excludes_pending_retry_and_completed_rows(self):
        for index, status in enumerate(('claimed', 'submitting', 'pending', 'retry', 'success')):
            aid = str(index)
            self.users.auto_add('offline', aid, aid, 2000)
            self.users.auto_mark('offline', aid, status)
        self.assertEqual(self.users.auto_recover_claimed(), 2)
        self.assertEqual([row['status'] for row in self.users.auto_list('offline')],
                         ['unknown', 'unknown', 'pending', 'retry', 'success'])
        self.assertEqual(self.users.auto_recover_claimed(), 0)


if __name__ == '__main__':
    unittest.main()
