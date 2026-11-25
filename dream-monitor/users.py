"""多用户存储：账号、加密密码、会话、每人独立的数据目录。

设计取舍（2026-10-21 与用户确认）：
    ① 密码加密存盘 —— 允许 token 失效后自动/手动续期，代价是服务器上有密文
    ② 每人一个独立库 —— data/users/<到梦空间uid>.db，互不干扰
    ③ 开放注册 —— 谁打开页面都能用自己的到梦空间账号登录

密码用 AES-256-CBC 加密，密钥放在 data/secret.key（首次启动自动生成，
权限 0600）。密钥与库分开存放：只拿到 users.db 无法解出密码。
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path

import dmcrypto

SESSION_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid        TEXT PRIMARY KEY,
    account    TEXT UNIQUE NOT NULL,
    name       TEXT DEFAULT '',
    school     TEXT DEFAULT '',
    college    TEXT DEFAULT '',
    student_id TEXT DEFAULT '',
    phone      TEXT DEFAULT '',
    avatar     TEXT DEFAULT '',
    pwd_enc    TEXT NOT NULL,
    token      TEXT DEFAULT '',
    created    REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    sid     TEXT PRIMARY KEY,
    uid     TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
CREATE TABLE IF NOT EXISTS auto_signup (
    uid        TEXT NOT NULL,
    aid        TEXT NOT NULL,
    name       TEXT DEFAULT '',
    join_start REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    note       TEXT DEFAULT '',
    created    REAL NOT NULL,
    fired_at   REAL,
    PRIMARY KEY (uid, aid)
);
CREATE INDEX IF NOT EXISTS idx_auto_start ON auto_signup(status, join_start);
"""


class Users:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "users.db"
        self.key = self._load_key()
        with self._db() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """老库补列（v1 -> v2 加了学院/学号/手机号/头像）。"""
        have = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for col in ("college", "student_id", "phone", "avatar"):
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")

    # ------------------------------------------------------------------ 基础
    def _load_key(self) -> bytes:
        p = self.dir / "secret.key"
        if p.exists():
            raw = p.read_bytes()
            if len(raw) == 32:
                return raw
        raw = os.urandom(32)
        p.write_bytes(raw)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return raw

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def _db(self):
        """算上提交与关闭。

        `with sqlite3.connect(...)` 只管事务、不关连接，每次调用都会漏一个文件描述符；
        长时间运行的进程会被 RLIMIT_NOFILE（默认 1024）耗尽，报
        `OperationalError: unable to open database file`，HTTP 也随之卡死。
        """
        c = self._conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -------------------------------------------------------------- 密码加解密
    def encrypt(self, plain: str) -> str:
        iv = os.urandom(16)
        ct = dmcrypto.aes_cbc_encrypt(self.key, iv, plain.encode("utf-8"))
        return f"{base64.b64encode(iv).decode()}:{base64.b64encode(ct).decode()}"

    def decrypt(self, blob: str) -> str:
        iv_b64, ct_b64 = blob.split(":", 1)
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
        return dmcrypto.aes_cbc_decrypt(self.key, iv, ct).decode("utf-8")

    # ------------------------------------------------------------------ 用户
    def upsert(self, *, uid: str, account: str, password: str,
               token: str, name: str = "", school: str = "",
               profile: dict | None = None) -> dict:
        now = time.time()
        p = dict(profile or {})
        college = str(p.get("college", ""))
        student_id = str(p.get("student_id", ""))
        phone = str(p.get("phone", ""))
        avatar = str(p.get("avatar", ""))
        with self._db() as c:
            c.execute(
                "INSERT INTO users(uid,account,name,school,college,student_id,phone,avatar,"
                " pwd_enc,token,created,last_seen)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET account=excluded.account,"
                " name=excluded.name, school=excluded.school,"
                " college=CASE WHEN excluded.college<>'' THEN excluded.college ELSE users.college END,"
                " student_id=CASE WHEN excluded.student_id<>'' THEN excluded.student_id ELSE users.student_id END,"
                " phone=CASE WHEN excluded.phone<>'' THEN excluded.phone ELSE users.phone END,"
                " avatar=CASE WHEN excluded.avatar<>'' THEN excluded.avatar ELSE users.avatar END,"
                " pwd_enc=excluded.pwd_enc, token=excluded.token, last_seen=excluded.last_seen",
                (uid, account, name, school, college, student_id, phone, avatar,
                 self.encrypt(password), token, now, now),
            )
        return self.get(uid) or {}

    def get(self, uid: str) -> dict | None:
        with self._db() as c:
            row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None

    def by_account(self, account: str) -> dict | None:
        with self._db() as c:
            row = c.execute("SELECT * FROM users WHERE account=?", (account,)).fetchone()
        return dict(row) if row else None

    def check_password(self, user: dict, password: str) -> bool:
        try:
            return hmac.compare_digest(self.decrypt(user["pwd_enc"]), password)
        except Exception:  # noqa: BLE001
            return False

    def password_of(self, uid: str) -> str:
        u = self.get(uid) or {}
        return self.decrypt(u["pwd_enc"]) if u else ""

    def set_token(self, uid: str, token: str) -> None:
        with self._db() as c:
            c.execute("UPDATE users SET token=?, last_seen=? WHERE uid=?",
                      (token, time.time(), uid))

    def touch(self, uid: str) -> None:
        with self._db() as c:
            c.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))

    def count(self) -> int:
        with self._db() as c:
            return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # ------------------------------------------------------------------ 本地管理员：只返回脱敏的资料
    @staticmethod
    def _mask(value: str) -> str:
        value = str(value or "")
        if not value:
            return "未记录"
        if "@" in value:
            local, _, domain = value.partition("@")
            return (local[:2] + "***@" + domain) if local else "***@" + domain
        if len(value) <= 4:
            return "*" * len(value)
        if len(value) < 8:
            return value[:1] + "*" * (len(value) - 3) + value[-2:]
        return value[:3] + "****" + value[-4:]

    def admin_list(self) -> list[dict]:
        """管理员列表：绝不返回密码密文、token、会话 ID 或完整手机号。"""
        with self._db() as c:
            rows = c.execute("""
                SELECT u.uid, u.name, u.school, u.phone, u.account,
                       u.created, u.last_seen,
                       (SELECT COUNT(*) FROM sessions s
                        WHERE s.uid=u.uid AND s.expires>?) AS sessions,
                       (SELECT COUNT(*) FROM auto_signup a
                        WHERE a.uid=u.uid) AS task_total,
                       (SELECT COUNT(*) FROM auto_signup a
                        WHERE a.uid=u.uid AND a.status IN
                        ('pending','retry','claimed','submitting')) AS task_active
                FROM users u ORDER BY u.last_seen DESC
            """, (time.time(),)).fetchall()
        return [dict(uid=str(row["uid"]), name=str(row["name"] or "未记录姓名"),
                     school=str(row["school"] or "未记录学校"),
                     account=self._mask(row["account"]), phone=self._mask(row["phone"]),
                     created=row["created"], last_seen=row["last_seen"],
                     sessions=row["sessions"], task_total=row["task_total"],
                     task_active=row["task_active"]) for row in rows]

    _STAGED_MARK = '.deleting-'

    def _account_files(self, uid: str) -> list[Path]:
        """该账号在 data/users 下的缓存文件；按确切 uid 边界匹配，不碰相邻 ID。"""
        user_dir = self.dir / 'users'
        if not user_dir.exists():
            return []
        out = []
        for child in user_dir.iterdir():
            if not (child.name == uid or child.name.startswith(uid + '.')
                    or child.name.startswith(uid + '_')):
                continue
            if child.is_symlink() or not child.is_file():
                raise RuntimeError("发现非普通账号缓存文件，已阻止删除，请人工检查")
            out.append(child)
        return out

    def purge_leftovers(self) -> int:
        """清理上次删除时因文件占用没能删掉的残留缓存（启动时和每次删除后调用）。"""
        user_dir = self.dir / 'users'
        if not user_dir.exists():
            return 0
        removed = 0
        for child in user_dir.iterdir():
            if (child.name.startswith('.') and self._STAGED_MARK in child.name
                    and child.is_file() and not child.is_symlink()):
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def delete_user(self, uid: str) -> dict:
        """彻底删除一个本地账号：资料、加密密码、Token、会话、定时任务和缓存文件。

        不做备份。数据库行在同一事务内删除；缓存文件先改名暂存，
        若改名失败（如 Windows 文件占用）则整体撤销，账号保持原样。
        提交后立即删除暂存文件，并开启 secure_delete + VACUUM，
        让被删内容不残留在 users.db 的空闲页里。
        """
        uid = str(uid)
        if not uid or not uid.isascii() or not uid.isdigit():
            raise ValueError("用户 ID 格式无效")
        if self.get(uid) is None:
            raise LookupError("此用户已不存在，请刷新列表")
        staged: list[tuple[Path, Path]] = []
        c = self._conn()
        try:
            c.execute('PRAGMA secure_delete=ON')
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM users WHERE uid=?', (uid,)).fetchone() is None:
                raise LookupError("此用户已被移除，请刷新列表")
            for child in self._account_files(uid):
                tmp = child.with_name('.' + child.name + self._STAGED_MARK
                                      + secrets.token_hex(4))
                child.replace(tmp)
                staged.append((child, tmp))
            task_count = c.execute('DELETE FROM auto_signup WHERE uid=?', (uid,)).rowcount
            session_count = c.execute('DELETE FROM sessions WHERE uid=?', (uid,)).rowcount
            removed = c.execute('DELETE FROM users WHERE uid=?', (uid,)).rowcount
            if removed != 1:
                raise RuntimeError("账号删除未完成，已撤销")
            c.commit()
        except BaseException:
            c.rollback()
            for original, tmp in reversed(staged):
                if tmp.exists():
                    tmp.replace(original)
            c.close()
            raise
        try:
            c.execute('VACUUM')
        except sqlite3.Error:
            pass  # 其他连接正忙时跳过；secure_delete 已覆盖被删行
        finally:
            c.close()
        left = 0
        for _original, tmp in staged:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                left += 1  # 文件被占用：下次启动或下次删除时自动清理
        self.purge_leftovers()
        return {'uid': uid, 'tasks_removed': task_count,
                'sessions_removed': session_count,
                'cache_files_deleted': len(staged) - left,
                'cache_files_pending': left}

    def db_for(self, uid: str) -> str:
        """每人一个活动库：data/users/<uid>.db，删用户就删这个文件。"""
        d = self.dir / "users"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{uid}.db")

    # ------------------------------------------------------------------ 会话
    # ------------------------------------------------------------- 定时报名
    def auto_add(self, uid: str, aid: str, name: str, join_start: float) -> None:
        """新增/覆盖一条定时报名（同一活动只保留一条）。"""
        now = time.time()
        with self._db() as c:
            c.execute(
                "INSERT INTO auto_signup (uid, aid, name, join_start, status, note, created) "
                "VALUES (?,?,?,?,'pending','',?) "
                "ON CONFLICT(uid, aid) DO UPDATE SET "
                "  name=excluded.name, join_start=excluded.join_start, "
                "  status='pending', note='', created=excluded.created, fired_at=NULL",
                (str(uid), str(aid), name or "", float(join_start), now),
            )

    def auto_list(self, uid: str) -> list[dict]:
        with self._db() as c:
            rows = c.execute(
                "SELECT * FROM auto_signup WHERE uid=? ORDER BY join_start ASC", (str(uid),)
            ).fetchall()
        return [dict(r) for r in rows]

    def auto_map(self, uid: str) -> dict:
        """aid -> {status, join_start} 供列表页标记用。"""
        return {r["aid"]: {"status": r["status"], "join_start": r["join_start"]}
                for r in self.auto_list(uid)}

    def auto_del(self, uid: str, aid: str) -> None:
        with self._db() as c:
            c.execute("DELETE FROM auto_signup WHERE uid=? AND aid=?", (str(uid), str(aid)))

    def auto_due(self, now: float, window: float = 15.0) -> list[dict]:
        """到点（或即将到点）的待办：join_start - window <= now。"""
        with self._db() as c:
            rows = c.execute(
                "SELECT * FROM auto_signup WHERE status IN ('pending','retry') AND join_start-? <= ? "
                "ORDER BY join_start ASC", (float(window), float(now)),
            ).fetchall()
        return [dict(r) for r in rows]

    def auto_mark(self, uid: str, aid: str, status: str, note: str = "") -> None:
        with self._db() as c:
            c.execute(
                "UPDATE auto_signup SET status=?, note=?, fired_at=? WHERE uid=? AND aid=?",
                (status, note or "", time.time(), str(uid), str(aid)),
            )

    def auto_get(self, uid: str, aid: str) -> dict | None:
        """读取一条计划；不读取或改动账号、密码及活动筛选。"""
        with self._db() as c:
            row = c.execute("SELECT * FROM auto_signup WHERE uid=? AND aid=?",
                            (str(uid), str(aid))).fetchone()
        return dict(row) if row else None

    def auto_mark_if_current(self, uid: str, aid: str, expected_created: float,
                             expected_join_start: float, status: str, note: str = "", *,
                             expected_statuses=None) -> bool:
        """版本匹配才更新，已删除或重建的计划不能被旧网络响应覆盖。

        fired_at 沿用旧版状态更新时间。真实 POST 发起时间由调度器
        on_send 独立记录，不能用 fired_at 代替。
        """
        sql = ("UPDATE auto_signup SET status=?, note=?, fired_at=? "
               "WHERE uid=? AND aid=? AND created=? AND join_start=?")
        params = [str(status), note or "", time.time(), str(uid), str(aid),
                  float(expected_created), float(expected_join_start)]
        if expected_statuses is not None:
            states = tuple(str(value) for value in expected_statuses)
            if not states:
                return False
            sql += " AND status IN (" + ",".join("?" for _ in states) + ")"
            params.extend(states)
        with self._db() as c:
            changed = c.execute(sql, params).rowcount
        return changed == 1

    def auto_claim(self, uid: str, aid: str, expected_created: float,
                   expected_join_start: float) -> bool:
        """原子接管；崩溃后不把可能发出的请求再次发送。"""
        return self.auto_mark_if_current(
            uid, aid, expected_created, expected_join_start, "claimed",
            "定时任务已接管；若程序中断，请核验后再重新启用。",
            expected_statuses=("pending", "retry"),
        )

    def auto_recover_claimed(self) -> int:
        """首次发现前运行：旧在途状态不自动恢复提交。"""
        with self._db() as c:
            changed = c.execute(
                "UPDATE auto_signup SET status='unknown', note=?, fired_at=? "
                "WHERE status IN ('claimed','submitting')",
                ("上次运行曾接管此任务，无法确定是否已提交；请先人工核验，不会自动重发。", time.time()),
            ).rowcount
        return changed

    def new_session(self, uid: str) -> str:
        sid = secrets.token_hex(24)
        now = time.time()
        with self._db() as c:
            c.execute("INSERT INTO sessions(sid,uid,created,expires) VALUES(?,?,?,?)",
                      (sid, uid, now, now + SESSION_DAYS * 86400))
            c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        return sid

    def session_uid(self, sid: str) -> str | None:
        if not sid:
            return None
        with self._db() as c:
            row = c.execute("SELECT uid,expires FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not row or row["expires"] < time.time():
            return None
        return str(row["uid"])

    def drop_session(self, sid: str) -> None:
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def _selftest() -> None:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="dmusers_"))
    u = Users(d)
    rec = u.upsert(uid="62153844", account="18281107989", password="96287248Ly.",
                   token="abc", name="刘烨", school="四川职业技术学院")
    assert rec["uid"] == "62153844", rec
    assert u.get("62153844")["token"] == "abc"
    assert u.by_account("18281107989")["name"] == "刘烨"
    assert u.check_password(rec, "96287248Ly.") is True
    assert u.check_password(rec, "96287248ly.") is False
    assert u.password_of("62153844") == "96287248Ly."
    # 密文里不该出现明文
    raw = (d / "users.db").read_bytes()
    assert b"96287248" not in raw, "密码明文落盘了"
    sid = u.new_session("62153844")
    assert u.session_uid(sid) == "62153844"
    assert u.session_uid("bogus") is None
    u.drop_session(sid)
    assert u.session_uid(sid) is None
    print("users selftest: OK")


if __name__ == "__main__":
    _selftest()
