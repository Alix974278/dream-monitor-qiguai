"""Local admin UI / safe deletion verification. No real platform/API/network access.
Only the local loopback HTTP test server is used.
"""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from users import Users
from autosign import Scheduler, _Job
import gui_server as gs


class AdminStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)
        self.users = Users(self.d)
        for uid, name, phone in [('90000001', '甲同学', '13812345678'),
                                 ('90000002', '乙同学', '13987654321'),
                                 ('90000003', '丙同学', '13700001234'),
                                 ('90000004', '丁同学', '13611112222')]:
            self.users.upsert(uid=uid, account=phone, password='FAKE_PASSWORD'+uid,
                              token='FAKE_TOKEN'+uid, name=name, school='演练大学',
                              profile={'phone': phone})
            (self.d / 'users').mkdir(exist_ok=True)
            (self.d / 'users' / (uid+'.db.screening-v1')).write_bytes(uid.encode())
            (self.d / 'users' / (uid+'_score.json')).write_bytes(b'{}')
        self.users.auto_add('90000002', '16887807', '模拟活动', 1900000000.0)
        self.sid = self.users.new_session('90000002')

    def test_admin_list_no_secrets_and_per_user_task_count(self):
        rows = self.users.admin_list()
        self.assertEqual(4, len(rows))
        byid = {x['uid']: x for x in rows}
        self.assertEqual('乙同学', byid['90000002']['name'])
        self.assertEqual('139****4321', byid['90000002']['account'])
        self.assertEqual(1, byid['90000002']['sessions'])
        self.assertEqual(1, byid['90000002']['task_active'])
        payload = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn('FAKE_PASSWORD',payload)
        self.assertNotIn('FAKE_TOKEN',payload)
        self.assertNotIn('13987654321',payload)
        self.assertNotIn(self.sid,payload)
        self.assertNotIn('pwd_enc',payload)

    def test_delete_single_user_is_permanent_and_preserves_three_others(self):
        key_before = (self.d/'secret.key').read_bytes()
        result = self.users.delete_user('90000002')
        self.assertEqual(3, self.users.count())
        self.assertIsNone(self.users.get('90000002'))
        self.assertIsNone(self.users.session_uid(self.sid))
        self.assertEqual([], self.users.auto_list('90000002'))
        self.assertEqual(1, result['tasks_removed'])
        self.assertEqual(1, result['sessions_removed'])
        self.assertEqual(2, result['cache_files_deleted'])
        self.assertEqual(0, result['cache_files_pending'])
        self.assertNotIn('backup', result)
        self.assertEqual(key_before, (self.d/'secret.key').read_bytes())
        for uid in ('90000001','90000003','90000004'):
            self.assertIsNotNone(self.users.get(uid))
            self.assertTrue((self.d/'users'/(uid+'.db.screening-v1')).exists())
        # 不留备份、不留暂存文件，被删账号的缓存文件不存在
        self.assertFalse((self.d/'admin_backups').exists())
        leftovers = [x.name for x in (self.d/'users').iterdir() if '90000002' in x.name]
        self.assertEqual([], leftovers)
        # secure_delete + VACUUM：被删账号的明文资料不残留在数据库文件里
        raw = (self.d/'users.db').read_bytes()
        self.assertNotIn('乙同学'.encode('utf-8'), raw)
        self.assertNotIn(b'FAKE_TOKEN90000002', raw)
        self.assertNotIn(b'13987654321', raw)
        self.assertIn('甲同学'.encode('utf-8'), raw)

    def test_deleted_user_can_log_in_again(self):
        self.users.delete_user('90000002')
        self.users.upsert(uid='90000002', account='13987654321', password='NEW',
                          token='NEW_TOKEN', name='乙同学', school='演练大学')
        self.assertIsNotNone(self.users.get('90000002'))
        self.assertEqual(4, self.users.count())

    def test_delete_ambiguous_uid_prefix_does_not_touch_neighbor(self):
        self.users.upsert(uid='900000020',account='13800009000',password='x',token='t')
        (self.d/'users'/'900000020.db').write_text('neighbor')
        self.users.delete_user('90000002')
        self.assertTrue((self.d/'users'/'900000020.db').exists())
        self.assertIsNotNone(self.users.get('900000020'))

    def test_unknown_user_keeps_files_untouched(self):
        with self.assertRaises(LookupError):
            self.users.delete_user('00000000')
        self.assertEqual(4,self.users.count())

    def test_invalid_uid_is_rejected(self):
        for value in ('../secret.key', '90000002_extra', '', '９０００'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.users.delete_user(value)
        self.assertEqual(4,self.users.count())

    def test_file_in_use_rolls_back_database_and_restores_files(self):
        path_cls=type(self.d)
        original=path_cls.replace
        def flaky(obj, destination):
            if obj.name == '90000002_score.json' and obj.parent.name == 'users':
                raise OSError('simulation: file in use')
            return original(obj,destination)
        with patch.object(path_cls,'replace',flaky):
            with self.assertRaises(OSError):
                self.users.delete_user('90000002')
        self.assertEqual(4,self.users.count())
        self.assertIsNotNone(self.users.session_uid(self.sid))
        self.assertEqual(1,len(self.users.auto_list('90000002')))
        self.assertTrue((self.d/'users'/'90000002.db.screening-v1').exists())
        self.assertTrue((self.d/'users'/'90000002_score.json').exists())
        self.assertEqual([], [x for x in (self.d/'users').iterdir() if x.name.startswith('.')])

    def test_locked_file_after_commit_is_purged_later(self):
        path_cls=type(self.d)
        original=path_cls.unlink
        def busy(obj, *a, **kw):
            if '.deleting-' in obj.name:
                raise PermissionError('simulation: file in use')
            return original(obj, *a, **kw)
        with patch.object(path_cls,'unlink',busy):
            result=self.users.delete_user('90000002')
        self.assertIsNone(self.users.get('90000002'))
        self.assertEqual(2, result['cache_files_pending'])
        self.assertEqual(2, self.users.purge_leftovers())
        self.assertEqual([], [x for x in (self.d/'users').iterdir() if '90000002' in x.name])


class AdminSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler=Scheduler(users=object(),hub_of=lambda uid:None)

    def test_delete_callback_and_blocked_user_are_scoped(self):
        self.scheduler._jobs[('A','x')] = SimpleNamespace(uid='A')
        self.scheduler._jobs[('B','x')] = SimpleNamespace(uid='B')
        self.scheduler._uncertain.add(('A','x'))
        self.scheduler._uncertain.add(('B','x'))
        result=self.scheduler.with_user_removed('A',lambda: {'ok':True})
        self.assertEqual({'ok':True},result)
        # 删除完成后必须解除屏蔽，否则同一人重新登录后的定时任务永远不执行
        self.assertNotIn('A',self.scheduler._blocked_users)
        self.assertNotIn(('A','x'),self.scheduler._jobs)
        self.assertIn(('B','x'),self.scheduler._jobs)
        self.assertEqual({('B','x')},self.scheduler._uncertain)

    def test_running_worker_blocks_deletion(self):
        job=_Job('A','x','demo',1.0,2.0,None,threading.Event(),'A:x:1:2')
        self.scheduler._active[job.key]=job
        called=[]
        with self.assertRaisesRegex(RuntimeError,'正在等待或执行'):
            self.scheduler.with_user_removed('A',lambda:called.append(True))
        self.assertEqual([],called)

    def test_failed_deletion_unblocks_user(self):
        def fail():
            raise OSError('storage')
        with self.assertRaises(OSError):
            self.scheduler.with_user_removed('A',fail)
        self.assertNotIn('A',self.scheduler._blocked_users)


class AdminRelinkTests(unittest.TestCase):
    """回归：删除账号后同一人重新登录，新建的定时任务必须被调度器接管。"""
    def test_relogin_after_delete_gets_scheduled(self):
        import time as _t
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        users=Users(Path(temp.name))
        users.upsert(uid='90000001',account='13800000000',password='x',token='t')
        asked=[]  # 被屏蔽的用户根本走不到取 Hub 这一步
        sched=Scheduler(users, lambda uid: asked.append(uid))
        self.addCleanup(sched.stop)
        sched.with_user_removed('90000001', lambda: users.delete_user('90000001'))
        self.assertNotIn('90000001', sched._blocked_users)
        users.upsert(uid='90000001',account='13800000000',password='x',token='t')
        users.auto_add('90000001','A1','演练活动',_t.time()+5)
        sched.tick()
        self.assertEqual(['90000001'], asked)


class AdminHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.users=Users(Path(self.temp.name))
        self.users.upsert(uid='90000001',account='13812345678',password='x',token='t',name='甲同学')
        self.users.upsert(uid='90000002',account='13987654321',password='x',token='t',name='乙同学')
        self.prev_users=gs.USERS
        self.prev_sched=gs.SCHED
        self.prev_hubs=dict(gs.HUBS)
        gs.USERS=self.users
        gs.SCHED=None
        gs.HUBS.clear()
        self.server=gs.Server(('127.0.0.1',0),gs.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.teardown_http)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def teardown_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        gs.USERS=self.prev_users
        gs.SCHED=self.prev_sched
        gs.HUBS.clear()
        gs.HUBS.update(self.prev_hubs)

    def fetch(self,path,method='GET',data=None,headers=None):
        content=json.dumps(data).encode() if data is not None else None
        hd={'Content-Type':'application/json',**(headers or {})}
        req=Request(self.url+path,method=method,data=content,headers=hd)
        try:
            with urlopen(req,timeout=3) as resp:
                return resp.status,json.load(resp)
        except HTTPError as e:
            return e.code,json.load(e)

    def _admin_headers(self):
        status, payload = self.fetch('/api/admin/users')
        self.assertEqual(200, status)
        self.assertIn('csrf_token', payload)
        return {'X-Dream-Admin-CSRF':payload['csrf_token'], 'Origin':self.url}

    def test_local_page_loads_users_without_password(self):
        status, result=self.fetch('/api/admin/users')
        self.assertEqual(200, status)
        self.assertEqual(2, len(result['users']))
        self.assertIn('csrf_token', result)
        self.assertNotIn('password', json.dumps(result))

    def test_post_requires_explicit_boolean_confirmation(self):
        hdr=self._admin_headers()
        for body in ({'uid':'90000002'}, {'uid':'90000002','confirmed':False},
                     {'uid':'90000002','confirmed':'true'}, {'uid':'','confirmed':True}):
            status,_=self.fetch('/api/admin/delete','POST',body,hdr)
            self.assertEqual(400,status,body)
            self.assertEqual(2,self.users.count())

    def test_post_deletes_only_selected_user(self):
        hdr=self._admin_headers()
        status,data=self.fetch('/api/admin/delete','POST',{'uid':'90000002','confirmed':True},hdr)
        self.assertEqual(200,status,data)
        self.assertEqual(1,data['users_remaining'])
        self.assertIsNone(self.users.get('90000002'))
        self.assertIsNotNone(self.users.get('90000001'))

    def test_current_profile_cannot_delete_itself(self):
        sid=self.users.new_session('90000002')
        headers={**self._admin_headers(),'Cookie':'dms='+sid}
        status,data=self.fetch('/api/admin/delete','POST',{'uid':'90000002','confirmed':True},headers)
        self.assertEqual(409,status,data)
        self.assertEqual(2,self.users.count())

    def test_wrong_origin_rejected(self):
        status,_=self.fetch('/api/admin/users',headers={'Origin':'https://another.example'})
        self.assertEqual(403,status)

    def test_cross_site_fetch_rejected(self):
        status,_=self.fetch('/api/admin/users',headers={'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(403,status)

    def test_delete_without_origin_rejected(self):
        h=self._admin_headers()
        h.pop('Origin')
        status,_=self.fetch('/api/admin/delete','POST',{'uid':'90000002','confirmed':True},h)
        self.assertEqual(403,status)
        self.assertEqual(2,self.users.count())

    def test_delete_without_auto_csrf_rejected(self):
        status,_=self.fetch('/api/admin/delete','POST',{'uid':'90000002','confirmed':True}, {'Origin':self.url})
        self.assertEqual(403,status)
        self.assertEqual(2,self.users.count())

    def test_delete_with_wrong_csrf_rejected(self):
        status,_=self.fetch('/api/admin/delete','POST',{'uid':'90000002','confirmed':True}, {'Origin':self.url,'X-Dream-Admin-CSRF':'invalid'})
        self.assertEqual(403,status)
        self.assertEqual(2,self.users.count())

    def test_admin_page_served_without_login(self):
        with urlopen(self.url+'/admin.html',timeout=3) as resp:
            body=resp.read().decode()
            self.assertIn('用户管理',body)
            self.assertNotIn('本次启动的管理员口令',body)

    def test_admin_list_reports_current_login(self):
        sid=self.users.new_session('90000002')
        status,data=self.fetch('/api/admin/users',headers={'Cookie':'dms='+sid})
        self.assertEqual(200,status)
        self.assertEqual('90000002',data['current_uid'])
        status,data=self.fetch('/api/admin/users')
        self.assertEqual('',data['current_uid'])

    def test_case_variant_admin_path_still_requires_local_host(self):
        for path in ('/ADMIN.HTML','/Admin.js','/admin.html.','/./admin.css'):
            with self.subTest(path=path):
                req=Request(self.url+path,headers={'Host':'other.example'})
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(req,timeout=3)
                self.assertEqual(403,ctx.exception.code)

    def test_rebinding_host_rejected(self):
        status,_=self.fetch('/api/admin/users',headers={'Host':'other.example'})
        self.assertEqual(403,status)

if __name__=='__main__': unittest.main()
