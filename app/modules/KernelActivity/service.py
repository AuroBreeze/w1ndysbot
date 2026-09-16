import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from . import DATA_DIR


DATABASE_PATH = os.path.join(DATA_DIR, "kernel_activity.db")


class KernelActivityService:
    _lock = threading.RLock()

    def __init__(self):
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with self._lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activity_records (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    last_active_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT PRIMARY KEY,
                    default_group_id TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_code TEXT NOT NULL UNIQUE,
                    group_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    deadline_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    publisher_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    closed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS task_participations (
                    task_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT,
                    accepted_at INTEGER,
                    completed_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (task_id, user_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS task_views (
                    task_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    first_viewed_at INTEGER NOT NULL,
                    last_viewed_at INTEGER NOT NULL,
                    view_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (task_id, user_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS task_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    editor_id TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_group_status
                    ON tasks(group_id, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_activity_group_user
                    ON activity_records(group_id, user_id);
                """
            )

    def mark_active(self, group_id, user_id, source, now=None):
        now = int(now or time.time())
        group_id, user_id = str(group_id), str(user_id)
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO activity_records(group_id, user_id, last_active_at, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    last_active_at=excluded.last_active_at,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (group_id, user_id, now, source, now),
            )
            previous = conn.execute(
                """SELECT created_at FROM activity_logs
                   WHERE group_id=? AND user_id=? AND source=?
                   ORDER BY id DESC LIMIT 1""",
                (group_id, user_id, source),
            ).fetchone()
            if not previous or now - previous[0] >= 60:
                conn.execute(
                    "INSERT INTO activity_logs(group_id,user_id,source,created_at) VALUES(?,?,?,?)",
                    (group_id, user_id, source, now),
                )
        return now

    def get_activity(self, group_id, user_id):
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM activity_records WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            ).fetchone()
            return dict(row) if row else None

    def get_effective_activity(self, group_id, user_id, napcat_last_sent=0):
        record = self.get_activity(group_id, user_id)
        module_time = int(record["last_active_at"]) if record else 0
        return max(int(napcat_last_sent or 0), module_time)

    def _new_task_code(self, conn, now):
        prefix = datetime.fromtimestamp(now).strftime("K-%Y%m%d-")
        row = conn.execute(
            "SELECT task_code FROM tasks WHERE task_code LIKE ? ORDER BY task_code DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        sequence = int(row[0].rsplit("-", 1)[1]) + 1 if row else 1
        return f"{prefix}{sequence:03d}"

    def create_task(self, group_id, title, content, deadline_days, publisher_id):
        now = int(time.time())
        deadline_at = now + deadline_days * 86400 if deadline_days > 0 else None
        with self._lock, self._connection() as conn:
            code = self._new_task_code(conn, now)
            conn.execute(
                """INSERT INTO tasks(task_code,group_id,title,content,deadline_at,status,
                   publisher_id,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?,?)""",
                (code, str(group_id), title, content, deadline_at, str(publisher_id), now, now),
            )
        self.mark_active(group_id, publisher_id, "task_publish", now)
        return self.get_task(code)

    def get_task(self, task_code):
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_code=?", (task_code,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, group_id, history=False, page=1, page_size=10):
        page = max(1, int(page))
        now = int(time.time())
        if history:
            # “历史任务”表示发布历史，因此包含进行中、已截止和已关闭任务。
            where = "group_id=?"
            params = (str(group_id),)
        else:
            where = "group_id=? AND status='active' AND (deadline_at IS NULL OR deadline_at>?)"
            params = (str(group_id), now)
        with self._lock, self._connection() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(row) for row in rows], total

    def update_task(self, task_code, editor_id, title=None, content=None, deadline_days=None):
        task = self.get_task(task_code)
        if not task:
            return None, []
        now = int(time.time())
        changes, updates, params = [], [], []
        if title is not None and title != task["title"]:
            changes.append(f"标题：{task['title']} → {title}")
            updates.append("title=?"); params.append(title)
        if content is not None and content != task["content"]:
            changes.append("内容已更新")
            updates.append("content=?"); params.append(content)
        if deadline_days is not None:
            new_deadline = now + deadline_days * 86400 if deadline_days > 0 else None
            changes.append(f"截止时间：{self.format_deadline(task['deadline_at'])} → {self.format_deadline(new_deadline)}")
            updates.append("deadline_at=?"); params.append(new_deadline)
        if not updates:
            return task, []
        new_version = int(task["version"]) + 1
        updates.extend(["version=?", "updated_at=?"]); params.extend([new_version, now, task_code])
        with self._lock, self._connection() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE task_code=?", params)
            conn.execute(
                "INSERT INTO task_revisions(task_id,version,editor_id,changes,created_at) VALUES(?,?,?,?,?)",
                (task["id"], new_version, str(editor_id), "\n".join(changes), now),
            )
        self.mark_active(task["group_id"], editor_id, "task_edit", now)
        return self.get_task(task_code), changes

    def set_task_status(self, task_code, status, actor_id):
        task = self.get_task(task_code)
        if not task:
            return None
        now = int(time.time())
        closed_at = now if status == "closed" else None
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE tasks SET status=?,closed_at=?,updated_at=? WHERE task_code=?",
                (status, closed_at, now, task_code),
            )
        self.mark_active(task["group_id"], actor_id, f"task_{status}", now)
        return self.get_task(task_code)

    def record_view(self, task, user_id):
        now = int(time.time())
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO task_views(task_id,group_id,user_id,first_viewed_at,last_viewed_at,view_count)
                   VALUES(?,?,?,?,?,1) ON CONFLICT(task_id,user_id) DO UPDATE SET
                   last_viewed_at=excluded.last_viewed_at,view_count=view_count+1""",
                (task["id"], task["group_id"], str(user_id), now, now),
            )
        self.mark_active(task["group_id"], user_id, "task_detail_view", now)

    def set_participation(self, task_code, user_id, action, note=""):
        task = self.get_task(task_code)
        if not task:
            return None, "任务不存在"
        now = int(time.time())
        if action in {"accept", "complete"} and task["status"] != "active":
            return None, "任务已关闭"
        status = {"accept": "accepted", "complete": "completed", "cancel": "accepted"}[action]
        accepted_at = now if action == "accept" else None
        completed_at = now if action == "complete" else None
        with self._lock, self._connection() as conn:
            existing = conn.execute(
                "SELECT * FROM task_participations WHERE task_id=? AND user_id=?",
                (task["id"], str(user_id)),
            ).fetchone()
            if action == "complete" and existing and existing["status"] == "completed":
                self.mark_active(task["group_id"], user_id, "task_complete_view", now)
                return task, "already_completed"
            if action == "cancel" and not existing:
                return task, "not_completed"
            conn.execute(
                """INSERT INTO task_participations(task_id,group_id,user_id,status,note,accepted_at,completed_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id,user_id) DO UPDATE SET
                   status=excluded.status,note=excluded.note,
                   accepted_at=COALESCE(task_participations.accepted_at,excluded.accepted_at),
                   completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
                (task["id"], task["group_id"], str(user_id), status, note or None,
                 accepted_at, completed_at, now),
            )
        self.mark_active(task["group_id"], user_id, f"task_{action}", now)
        return task, status

    def task_stats(self, task_id):
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT v.user_id) views,
                   (SELECT COUNT(*) FROM task_participations WHERE task_id=?) accepted,
                   (SELECT COUNT(*) FROM task_participations WHERE task_id=? AND status='accepted') in_progress,
                   (SELECT COUNT(*) FROM task_participations WHERE task_id=? AND status='completed') completed
                   FROM task_views v WHERE v.task_id=?""",
                (task_id, task_id, task_id, task_id),
            ).fetchone()
            return dict(row)

    def task_members(self, task_id):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT user_id,status,completed_at FROM task_participations WHERE task_id=? ORDER BY updated_at DESC",
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def my_tasks(self, group_id, user_id):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """SELECT t.task_code,t.title,p.status,p.completed_at FROM task_participations p
                   JOIN tasks t ON t.id=p.task_id WHERE p.group_id=? AND p.user_id=?
                   ORDER BY p.updated_at DESC LIMIT 20""",
                (str(group_id), str(user_id)),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_subscription(self, group_id, user_id, enabled):
        now = int(time.time())
        group_id, user_id = str(group_id), str(user_id)
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO subscriptions(group_id,user_id,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
                   enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (group_id, user_id, int(enabled), now, now),
            )
            preference = conn.execute(
                "SELECT default_group_id FROM user_preferences WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if enabled and (not preference or not preference[0]):
                conn.execute(
                    """INSERT INTO user_preferences(user_id,default_group_id,updated_at)
                       VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                       default_group_id=excluded.default_group_id,updated_at=excluded.updated_at""",
                    (user_id, group_id, now),
                )
            elif not enabled and preference and str(preference[0]) == group_id:
                replacement = conn.execute(
                    """SELECT group_id FROM subscriptions
                       WHERE user_id=? AND enabled=1 AND group_id!=?
                       ORDER BY created_at ASC LIMIT 1""",
                    (user_id, group_id),
                ).fetchone()
                conn.execute(
                    "UPDATE user_preferences SET default_group_id=?,updated_at=? WHERE user_id=?",
                    (str(replacement[0]) if replacement else None, now, user_id),
                )
        self.mark_active(group_id, user_id, "subscription_change", now)

    def set_default_group(self, user_id, group_id):
        now = int(time.time())
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO user_preferences(user_id,default_group_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                   default_group_id=excluded.default_group_id,updated_at=excluded.updated_at""",
                (str(user_id), str(group_id), now),
            )

    def get_default_group(self, user_id):
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT default_group_id FROM user_preferences WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def subscribers(self, group_id):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM subscriptions WHERE group_id=? AND enabled=1",
                (str(group_id),),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def subscriptions(self, user_id):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT group_id FROM subscriptions WHERE user_id=? AND enabled=1 ORDER BY updated_at DESC",
                (str(user_id),),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def revisions(self, task_id):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_revisions WHERE task_id=? ORDER BY version",
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def format_time(timestamp):
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M") if timestamp else "无"

    @staticmethod
    def format_deadline(deadline_at, now=None):
        if not deadline_at:
            return "长期任务"
        now = int(now or time.time())
        seconds = int(deadline_at) - now
        future = seconds > 0
        seconds = abs(seconds)
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = max(1, math.ceil(remainder / 60))
        if days:
            value = f"{days}天" + (f"{hours}小时" if hours else "")
        elif hours:
            value = f"{hours}小时"
        else:
            value = f"{minutes}分钟"
        return f"还剩{value}" if future else f"已逾期{value}"


service = KernelActivityService()


def get_effective_activity(group_id, user_id, napcat_last_sent=0):
    return service.get_effective_activity(group_id, user_id, napcat_last_sent)
