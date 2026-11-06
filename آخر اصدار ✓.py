"""
================================================================
بوت استضافة بوتات تيليجرام — نسخة الجوال (SQLite)
================================================================
- تستخدم SQLite (مدمجة في بايثون، لا تحتاج تثبيت إضافي).
- مناسبة للتشغيل على Pydroid / Termux / أي كمبيوتر.
- متطلبات: pip install pyTelegramBotAPI
  (لا تحتاج Flask — البوت يعمل بدون أي خادم ويب، مناسب للجوال 100%)
- التشغيل: python3 hosting_bot_sqlite_mobile.py
================================================================
"""

# ============================================================
# 1) الإعدادات — عدّل هذه القيم فقط
# ============================================================
import os

# استبدل السطرين القديمين بهذا التعديل:
BOT_TOKEN ='9059746440:AAEowTgSioThlGfamn5Su9OzOJ0QvNByWp4'
ADMIN_ID = 6561598105


DB_PATH = os.environ.get("DB_PATH", "hosting_bot.db")  # ملف قاعدة البيانات
KEEP_ALIVE_PORT = int(os.environ.get("PORT", "5000"))
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", "hosted_projects")
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "@OmarElba3ly")

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_FILES_PER_PROJECT = 50
ALLOWED_EXTENSIONS = {
    ".py", ".txt", ".json", ".csv", ".env", ".yaml", ".yml",
    ".ini", ".cfg", ".md", ".html", ".css", ".js", ".sql",
    ".xml", ".log", ".db",
}

# ============================================================
# 2) الاستيرادات
# ============================================================
import logging
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional

import telebot
from telebot import types

#from flask import Flask, jsonify

# ============================================================
# 3) فحوص أولية
# ============================================================
if not BOT_TOKEN or BOT_TOKEN == "ضع_توكن_بوتك_هنا":
    raise RuntimeError("BOT_TOKEN مفقود — ضعه في أعلى الملف")
if ADMIN_ID == 0:
    raise RuntimeError("ADMIN_ID مفقود أو غير صالح")

os.makedirs(PROJECTS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hosting-bot")

# ============================================================
# 4) قاعدة البيانات SQLite — اتصال واحد + قفل للكتابة
# ============================================================
_db_lock = threading.RLock()
_db_conn: Optional[sqlite3.Connection] = None


def db_init() -> None:
    global _db_conn
    if _db_conn is not None:
        return
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _db_conn.row_factory = sqlite3.Row
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    _db_conn.execute("PRAGMA foreign_keys=ON")
    _db_conn.execute("PRAGMA busy_timeout=5000")
    log.info("SQLite database opened at %s", DB_PATH)


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def db_fetchone(query: str, params: Iterable[Any] = ()) -> Optional[dict]:
    with _db_lock:
        cur = _db_conn.execute(query, tuple(params))
        row = cur.fetchone()
        cur.close()
    return _row_to_dict(row)


def db_fetchall(query: str, params: Iterable[Any] = ()) -> list:
    with _db_lock:
        cur = _db_conn.execute(query, tuple(params))
        rows = cur.fetchall()
        cur.close()
    return [dict(r) for r in rows]


def db_execute(query: str, params: Iterable[Any] = ()) -> None:
    with _db_lock:
        cur = _db_conn.execute(query, tuple(params))
        cur.close()


def db_execute_returning_id(query: str, params: Iterable[Any] = ()) -> int:
    """For INSERTs — returns the last inserted row id."""
    with _db_lock:
        cur = _db_conn.execute(query, tuple(params))
        rid = cur.lastrowid
        cur.close()
    return rid


# ============================================================
# 5) إنشاء الجداول تلقائياً عند أول تشغيل
# ============================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    first_name   TEXT,
    username     TEXT,
    approved     INTEGER NOT NULL DEFAULT 0,
    banned       INTEGER NOT NULL DEFAULT 0,
    max_bots     INTEGER NOT NULL DEFAULT 5,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    main_file       TEXT NOT NULL DEFAULT '',
    approved        INTEGER NOT NULL DEFAULT 0,
    is_running      INTEGER NOT NULL DEFAULT 0,
    last_started_at TIMESTAMP,
    last_stopped_at TIMESTAMP,
    last_error      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS project_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    content     BLOB NOT NULL,
    size_bytes  INTEGER NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, filename)
);

CREATE TABLE IF NOT EXISTS pending_uploads (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename           TEXT NOT NULL,
    telegram_file_id   TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    event       TEXT NOT NULL,
    message     TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS gift_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT UNIQUE NOT NULL,
    points      INTEGER NOT NULL,
    max_uses    INTEGER NOT NULL,
    used_count  INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gift_link_uses (
    link_id  INTEGER NOT NULL REFERENCES gift_links(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    used_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (link_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_projects_user      ON projects (user_id);
CREATE INDEX IF NOT EXISTS idx_files_project      ON project_files (project_id);
CREATE INDEX IF NOT EXISTS idx_pending_project    ON pending_uploads (project_id);
CREATE INDEX IF NOT EXISTS idx_logs_project       ON process_logs (project_id);
CREATE INDEX IF NOT EXISTS idx_gift_uses_link     ON gift_link_uses (link_id);
"""


def db_create_schema() -> None:
    with _db_lock:
        _db_conn.executescript(SCHEMA_SQL)
    log.info("Database schema is ready")


def db_ensure_columns() -> None:
    """يضيف الأعمدة الجديدة (نقاط/إحالة/انتهاء صلاحية) لقواعد البيانات القديمة بدون فقد البيانات."""
    cols = [r["name"] for r in db_fetchall("PRAGMA table_info(users)")]
    if "points" not in cols:
        db_execute("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
        log.info("Added column users.points")
    if "referred_by" not in cols:
        db_execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        log.info("Added column users.referred_by")
    gcols = [r["name"] for r in db_fetchall("PRAGMA table_info(gift_links)")]
    if gcols and "expires_at" not in gcols:
        db_execute("ALTER TABLE gift_links ADD COLUMN expires_at TIMESTAMP")
        log.info("Added column gift_links.expires_at")


# ============================================================
# 5.1) إعدادات قابلة للتعديل من قبل الأدمن (سعر، عمولة، قناة)
# ============================================================
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    db_execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_upload_price() -> int:
    try:
        return max(0, int(get_setting("upload_price", "1") or "1"))
    except Exception:
        return 1


def get_transfer_fee() -> int:
    try:
        return max(0, int(get_setting("transfer_fee", "0") or "0"))
    except Exception:
        return 0


def get_force_channel() -> str:
    return (get_setting("force_channel", "") or "").strip()


def is_bot_disabled() -> bool:
    """هل البوت معطّل للمستخدمين العاديين؟ (الأدمن يستطيع الدخول دائماً)."""
    return (get_setting("bot_disabled", "0") or "0") == "1"


def set_bot_disabled(disabled: bool) -> None:
    set_setting("bot_disabled", "1" if disabled else "0")


def normalize_channel(text: str) -> str:
    """يحوّل رابط القناة أو الـ @username إلى @username موحّد."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("https://t.me/"):
        text = text[len("https://t.me/"):]
    if text.startswith("t.me/"):
        text = text[len("t.me/"):]
    text = text.split("/")[0].split("?")[0]
    if not text:
        return ""
    if not text.startswith("@"):
        text = "@" + text
    return text


# ============================================================
# 5.2) فحص الاشتراك الإجباري في القناة
# ============================================================
def is_user_subscribed(uid: int, channel: str) -> bool:
    """يعيد True إذا كان المستخدم مشتركاً في القناة (أو إذا تعذّر الفحص — لتفادي الحجب الخاطئ)."""
    if not channel:
        return True
    try:
        member = bot.get_chat_member(channel, uid)
        return getattr(member, "status", "left") not in ("left", "kicked")
    except Exception:
        log.exception("get_chat_member failed for %s in %s", uid, channel)
        # إذا لم يكن البوت مشرفاً في القناة لن نستطيع التحقق — نسمح بدلاً من الحجب الخاطئ
        return True


def force_sub_block(chat_id: int, uid: int) -> bool:
    """إن كان يجب على المستخدم الاشتراك في القناة، يرسل رسالة بزر الاشتراك ويعيد True."""
    if is_admin(uid):
        return False
    channel = get_force_channel()
    if not channel:
        return False
    if is_user_subscribed(uid, channel):
        return False
    join_url = f"https://t.me/{channel.lstrip('@')}"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📣 اشترك في القناة", url=join_url))
    kb.add(types.InlineKeyboardButton("✅ تحقّق من الاشتراك", callback_data="check_sub"))
    bot.send_message(
        chat_id,
        "⚠️ <b>للاستمرار في استخدام البوت يجب الاشتراك في القناة:</b>\n\n"
        f"📢 {channel}\n\n"
        "اضغط على زر الاشتراك ثم زر التحقق.",
        reply_markup=kb,
    )
    return True


# ============================================================
# 5.3) روابط هدايا النقاط
# ============================================================
def _new_gift_token() -> str:
    import secrets
    return secrets.token_urlsafe(8)


def process_gift_link(uid: int, token: str) -> None:
    link = db_fetchone("SELECT * FROM gift_links WHERE token=?", (token,))
    if not link:
        try:
            bot.send_message(uid, "❌ رابط الهدية غير صالح.")
        except Exception:
            pass
        return
    # تحقق من انتهاء الصلاحية
    expires_at = link["expires_at"] if "expires_at" in link.keys() else None
    if expires_at:
        try:
            exp = datetime.strptime(str(expires_at).split(".")[0], "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() > exp:
                try:
                    bot.send_message(uid, "❌ انتهت صلاحية رابط الهدية.")
                except Exception:
                    pass
                return
        except Exception:
            pass
    used = db_fetchone(
        "SELECT 1 AS x FROM gift_link_uses WHERE link_id=? AND user_id=?",
        (link["id"], uid),
    )
    if used:
        try:
            bot.send_message(uid, "❌ سبق وأن استخدمت هذا الرابط من قبل.")
        except Exception:
            pass
        return
    with _db_lock:
        link2 = db_fetchone(
            "SELECT used_count, max_uses, points FROM gift_links WHERE id=?",
            (link["id"],),
        )
        if not link2 or link2["used_count"] >= link2["max_uses"]:
            try:
                bot.send_message(uid, "❌ تم استنفاذ هذا الرابط (وصل للحد الأقصى).")
            except Exception:
                pass
            return
        db_execute(
            "UPDATE gift_links SET used_count = used_count + 1 WHERE id=? AND used_count < max_uses",
            (link["id"],),
        )
        db_execute(
            "INSERT OR IGNORE INTO gift_link_uses (link_id, user_id) VALUES (?, ?)",
            (link["id"], uid),
        )
        db_execute(
            "UPDATE users SET points = COALESCE(points,0) + ? WHERE id=?",
            (link2["points"], uid),
        )
    try:
        bot.send_message(
            uid,
            f"🎁 تهانينا! حصلت على <b>{link2['points']}</b> نقطة من رابط الهدية 🎉",
        )
    except Exception:
        pass


# ============================================================
# 6) مدير العمليات — كل بوت في عملية مستقلة + إعادة تشغيل تلقائي
# ============================================================
class HostedProcess:
    def __init__(self, project_id: int, name: str, main_file: str, work_dir: str):
        self.project_id = project_id
        self.name = name
        self.main_file = main_file
        self.work_dir = work_dir
        self.proc: Optional[subprocess.Popen] = None
        self.auto_restart = True
        self.restart_count = 0
        self.last_start = 0.0
        self.stopped_by_user = False
        self.lock = threading.Lock()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        with self.lock:
            if self.is_alive():
                return True
            log_path = os.path.join(self.work_dir, "_runtime.log")
            try:
                log_file = open(log_path, "ab", buffering=0)
                # على ويندوز: لا يدعم start_new_session بنفس الطريقة
                popen_kwargs = {
                    "cwd": self.work_dir,
                    "stdout": log_file,
                    "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL,
                    "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
                }
                if hasattr(os, "setsid"):
                    popen_kwargs["start_new_session"] = True
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", self.main_file],
                    **popen_kwargs,
                )
                self.last_start = time.time()
                self.stopped_by_user = False
                _record_event(self.project_id, "start", f"pid={self.proc.pid}")
                db_execute(
                    "UPDATE projects SET is_running=1, last_started_at=CURRENT_TIMESTAMP, last_error=NULL WHERE id=?",
                    (self.project_id,),
                )
                log.info("Started project %s (pid=%s)", self.project_id, self.proc.pid)
                return True
            except Exception as e:
                log.exception("Failed to start project %s", self.project_id)
                _record_event(self.project_id, "error", f"start failed: {e}")
                db_execute(
                    "UPDATE projects SET is_running=0, last_error=? WHERE id=?",
                    (str(e)[:500], self.project_id),
                )
                return False

    def stop(self, by_user: bool = True) -> bool:
        with self.lock:
            self.stopped_by_user = by_user
            if not self.is_alive():
                self._mark_stopped()
                return True
            assert self.proc is not None
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                else:
                    self.proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    else:
                        self.proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self._mark_stopped()
            _record_event(self.project_id, "stop", "by_user" if by_user else "by_system")
            return True

    def _mark_stopped(self):
        try:
            db_execute(
                "UPDATE projects SET is_running=0, last_stopped_at=CURRENT_TIMESTAMP WHERE id=?",
                (self.project_id,),
            )
        except Exception:
            log.exception("Failed to mark project %s stopped", self.project_id)


class ProcessManager:
    def __init__(self):
        self.processes: Dict[int, HostedProcess] = {}
        self.lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def _project_dir(self, project_id: int) -> str:
        return os.path.join(PROJECTS_DIR, str(project_id))

    def _materialize(self, project_id: int) -> str:
        work_dir = self._project_dir(project_id)
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)
        rows = db_fetchall(
            "SELECT filename, content FROM project_files WHERE project_id=?",
            (project_id,),
        )
        for row in rows:
            target = os.path.join(work_dir, row["filename"])
            os.makedirs(os.path.dirname(target) or work_dir, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(bytes(row["content"]))
        return work_dir

    def start_project(self, project_id: int):
        with self.lock:
            existing = self.processes.get(project_id)
            if existing and existing.is_alive():
                return True, "already running"

            row = db_fetchone(
                "SELECT id, name, main_file, approved FROM projects WHERE id=?",
                (project_id,),
            )
            if not row:
                return False, "project not found"
            if not row["approved"]:
                return False, "project not approved by admin yet"

            work_dir = self._materialize(project_id)
            main_path = os.path.join(work_dir, row["main_file"])
            if not os.path.exists(main_path):
                return False, f"main file '{row['main_file']}' missing"

            hp = HostedProcess(project_id, row["name"], row["main_file"], work_dir)
            hp.auto_restart = True
            self.processes[project_id] = hp

        ok = hp.start()
        return ok, "started" if ok else "failed to start"

    def stop_project(self, project_id: int):
        with self.lock:
            hp = self.processes.get(project_id)
        if not hp:
            db_execute("UPDATE projects SET is_running=0 WHERE id=?", (project_id,))
            return True, "not running"
        hp.stop(by_user=True)
        return True, "stopped"

    def restart_project(self, project_id: int):
        self.stop_project(project_id)
        time.sleep(0.5)
        return self.start_project(project_id)

    def is_running(self, project_id: int) -> bool:
        hp = self.processes.get(project_id)
        return bool(hp and hp.is_alive())

    def tail_log(self, project_id: int, n_bytes: int = 4000) -> str:
        path = os.path.join(self._project_dir(project_id), "_runtime.log")
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - n_bytes))
                data = fh.read()
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            return f"(failed to read log: {e})"

    def start_watchdog(self) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="hosting-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                with self.lock:
                    items = list(self.processes.items())
                for pid, hp in items:
                    if hp.stopped_by_user:
                        continue
                    if hp.proc is None or hp.is_alive():
                        continue
                    if not hp.auto_restart:
                        hp._mark_stopped()
                        continue
                    rc = hp.proc.poll()
                    elapsed = time.time() - hp.last_start
                    log.warning("Project %s exited rc=%s after %.1fs — restarting", pid, rc, elapsed)
                    _record_event(pid, "crash", f"exit={rc} uptime={elapsed:.0f}s")
                    backoff = min(30.0, 1.0 + hp.restart_count * 2)
                    time.sleep(backoff)
                    if hp.start():
                        hp.restart_count += 1
            except Exception:
                log.exception("watchdog loop error")
            self._stopping.wait(5)

    def shutdown(self) -> None:
        self._stopping.set()
        with self.lock:
            items = list(self.processes.values())
        for hp in items:
            try:
                hp.stop(by_user=False)
            except Exception:
                log.exception("error stopping %s during shutdown", hp.project_id)

    def restore_running(self) -> None:
        rows = db_fetchall("SELECT id FROM projects WHERE is_running=1 AND approved=1")
        for row in rows:
            ok, msg = self.start_project(row["id"])
            log.info("Auto-restore project %s: %s (%s)", row["id"], ok, msg)


def _record_event(project_id: int, event: str, message: str = "") -> None:
    try:
        db_execute(
            "INSERT INTO process_logs (project_id, event, message) VALUES (?,?,?)",
            (project_id, event, message[:1000] if message else None),
        )
    except Exception:
        log.exception("failed to record event")


manager = ProcessManager()

# ============================================================
# 7) خادم ويب صغير لإبقاء البوت حيّاً (Keep-alive)
# ============================================================




# ============================================================
# 8) واجهة التيليجرام — كل الأوامر والأزرار
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=8)


# ----- جعل كل النصوص في البوت على شكل اقتباس <blockquote> تلقائياً -----
def _wrap_blockquote(text):
    if text is None:
        return text
    s = str(text)
    if not s.strip():
        return s
    if "<blockquote" in s:
        return s
    return f"<blockquote>{s}</blockquote>"


_orig_send_message = bot.send_message
_orig_edit_message_text = bot.edit_message_text


def _send_message_quoted(chat_id, text, *args, **kwargs):
    parse_mode = kwargs.get("parse_mode")
    if parse_mode is None or str(parse_mode).upper() == "HTML":
        text = _wrap_blockquote(text)
    return _orig_send_message(chat_id, text, *args, **kwargs)


def _edit_message_text_quoted(text, *args, **kwargs):
    parse_mode = kwargs.get("parse_mode")
    if parse_mode is None or str(parse_mode).upper() == "HTML":
        text = _wrap_blockquote(text)
    return _orig_edit_message_text(text, *args, **kwargs)


bot.send_message = _send_message_quoted
bot.edit_message_text = _edit_message_text_quoted

_pending_actions: dict = {}
_pending_lock = threading.Lock()

# --- منع الازدواجية: نتأكد ألا نُعالج نفس الرسالة/زر مرتين ---
_seen_lock = threading.Lock()
_seen_keys: dict = {}  # key -> timestamp


def _seen_once(key) -> bool:
    """يرجع True إذا كان المفتاح قد عُولج سابقاً (تكرار)."""
    now = time.time()
    with _seen_lock:
        # تنظيف أي مفاتيح قديمة (>10 دقائق) لتجنب نمو الذاكرة
        if len(_seen_keys) > 2000:
            cutoff = now - 600
            for k in [k for k, t in _seen_keys.items() if t < cutoff]:
                _seen_keys.pop(k, None)
        if key in _seen_keys:
            return True
        _seen_keys[key] = now
    return False


def msg_seen(message) -> bool:
    return _seen_once(("m", message.chat.id, message.message_id))


def call_seen(call) -> bool:
    return _seen_once(("c", call.id))


# --- اسم البوت يُجلب مرة واحدة ويُخزّن لاستعمال رابط الإحالة ---
_bot_username_cache: Optional[str] = None


def get_bot_username() -> str:
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        _bot_username_cache = bot.get_me().username or "your_bot"
    except Exception:
        _bot_username_cache = "your_bot"
    return _bot_username_cache


def html_escape(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def safe_filename(name: str) -> str:
    name = os.path.basename(name)
    return "".join(c for c in name if c.isalnum() or c in "._-") or "file.bin"


def file_extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def is_allowed_extension(name: str) -> bool:
    return file_extension(name) in ALLOWED_EXTENSIONS


def set_pending(uid: int, action: dict) -> None:
    with _pending_lock:
        _pending_actions[uid] = action


def pop_pending(uid: int):
    with _pending_lock:
        return _pending_actions.pop(uid, None)


def get_pending(uid: int):
    with _pending_lock:
        return _pending_actions.get(uid)


def ensure_user(message_or_call) -> dict:
    user = (
        message_or_call.from_user
        if hasattr(message_or_call, "from_user")
        else message_or_call.message.from_user
    )
    db_execute(
        """
        INSERT INTO users (id, first_name, username, approved)
        VALUES (?, ?, ?, 1)
        ON CONFLICT (id) DO UPDATE
          SET first_name = excluded.first_name,
              username = excluded.username,
              approved = 1
        """,
        (user.id, user.first_name or "", user.username or ""),
    )
    row = db_fetchone(
        "SELECT id, approved, banned, max_bots FROM users WHERE id=?", (user.id,)
    )
    return row or {"approved": 1, "banned": 0, "max_bots": 5}


def ensure_user_by_id(uid: int) -> dict:
    row = db_fetchone(
        "SELECT id, first_name, username, approved, banned, max_bots FROM users WHERE id=?",
        (uid,),
    )
    return row or {
        "id": uid,
        "first_name": "",
        "username": "",
        "approved": 1,
        "banned": 0,
        "max_bots": 5,
    }


def main_menu_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚀 إضافة بوت جديد", callback_data="proj_new"),
        types.InlineKeyboardButton("📂 بوتاتي", callback_data="proj_list"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats"),
        types.InlineKeyboardButton("❓ المساعدة", callback_data="help"),
    )
    kb.add(
        types.InlineKeyboardButton("💎 تجميع نقاط", callback_data="points"),
        types.InlineKeyboardButton("💸 تحويل نقاط", callback_data="transfer_points"),
    )
    kb.add(types.InlineKeyboardButton("👨‍💻 المطور", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}"))
    return kb


def admin_menu_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("👥 طلبات التفعيل", callback_data="adm_pending_users"),
        types.InlineKeyboardButton("✅ طلبات الموافقة", callback_data="adm_pending_projects"),
    )
    kb.add(
        types.InlineKeyboardButton("👤 المستخدمون", callback_data="adm_users"),
        types.InlineKeyboardButton("🤖 كل البوتات", callback_data="adm_projects"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 إذاعة رسالة", callback_data="adm_broadcast"),
        types.InlineKeyboardButton("📊 إحصائيات النظام", callback_data="adm_stats"),
    )
    kb.add(
        types.InlineKeyboardButton("🎁 رابط هدية نقاط", callback_data="adm_gift_new"),
        types.InlineKeyboardButton("📣 قناة الاشتراك", callback_data="adm_force_channel"),
    )
    kb.add(
        types.InlineKeyboardButton("💰 سعر رفع البوت", callback_data="adm_set_upload_price"),
        types.InlineKeyboardButton("💸 عمولة التحويل", callback_data="adm_set_transfer_fee"),
    )
    kb.add(
        types.InlineKeyboardButton("🚫 حظر مستخدم", callback_data="adm_ban_by_id"),
        types.InlineKeyboardButton("✅ رفع حظر مستخدم", callback_data="adm_unban_by_id"),
    )
    kb.add(types.InlineKeyboardButton("🪪 معلومات مستخدم", callback_data="adm_user_info"))
    if is_bot_disabled():
        kb.add(types.InlineKeyboardButton("🟢 تفعيل البوت", callback_data="adm_toggle_bot"))
    else:
        kb.add(types.InlineKeyboardButton("⛔ تعطيل البوت", callback_data="adm_toggle_bot"))
    kb.add(types.InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="back_main"))
    return kb


def back_main_kb() -> types.InlineKeyboardMarkup:
    """زر رجوع إلى القائمة الرئيسية."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    return kb


def back_admin_kb() -> types.InlineKeyboardMarkup:
    """زر رجوع إلى لوحة الأدمن."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="adm_panel"))
    return kb


def show_main_menu(chat_id: int, uid: int, edit_message_id: Optional[int] = None) -> None:
    user = ensure_user_by_id(uid)
    proj_count = db_fetchone("SELECT COUNT(*) AS c FROM projects WHERE user_id=?", (uid,))["c"]
    running_count = 0
    for p in db_fetchall("SELECT id FROM projects WHERE user_id=?", (uid,)):
        hp = manager.processes.get(p["id"])
        if hp and hp.is_alive():
            running_count += 1
    stopped_count = max(proj_count - running_count, 0)
    points_row = db_fetchone("SELECT COALESCE(points,0) AS p FROM users WHERE id=?", (uid,))
    points = points_row["p"] if points_row else 0
    display_name = (user.get("first_name") or "").strip()
    if not display_name:
        uname = (user.get("username") or "").strip()
        display_name = f"@{uname}" if uname else "صديقي"
    bot_uname = get_bot_username() or "your_bot"
    text = (
        "✨ ━━━━━━━━━━━━━━━━ ✨\n\n"
        "اول حاجه صلي علي النبي 🤎\n\n"
        
        " أهلاً وسهلاً بيك يقلبي 😊\n"
        f"╰┈➤ <b>{html_escape(display_name)}</b> 👑\n\n"
        "🚀 في بوت استضافة البوتات المتقدم\n"
        f"╰┈➤ @{html_escape(bot_uname)} 💫\n\n"
        f"💎 نقاطك الحالية: <b>{points}</b> نقطة\n"
        f"👤 ايدي حسابك : <code>{uid}</code>\n"
        f"🤖 بوتاتك : <b>{proj_count}</b>\n"
        f"🔋 بوتات تعمل الان : <b>{running_count}</b>\n"
        f"🪫 بوتات متوقفة : <b>{stopped_count}</b>\n\n"
        "📊 اختر من القائمة أدناه:\n\n"
        
        "ومتنساش تصلي علي النبي ✨️\n\n"
        " 📍ملحوظه مهمه لو مش معاك نقط خالص كلمني وانا هضيفلك نقط مجاني يوزري @OmarElba3ly \n"
        "injoy يقلبي 😘\n"
        "✨ ━━━━━━━━━━━━━━━━ ✨"
    )
    kb = main_menu_markup()
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id, edit_message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


# -------------------- /start /admin /cancel --------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if msg_seen(message):
        return
    uid = message.from_user.id

    # استخراج البيانات الإضافية من /start <payload>
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) == 2 else ""
    ref_id = None
    gift_token = None
    if payload.startswith("gift_"):
        gift_token = payload[len("gift_"):]
    elif payload.isdigit():
        try:
            ref_id = int(payload)
            if ref_id == uid:
                ref_id = None
        except Exception:
            ref_id = None

    # هل المستخدم جديد؟ (نسجل الإحالة فقط للجديد)
    existing = db_fetchone("SELECT id FROM users WHERE id=?", (uid,))
    is_new_user = existing is None

    user = ensure_user(message)

    if user.get("banned"):
        bot.send_message(uid, "🚫 لقد تم حظرك من استخدام الاستضافة.")
        return

    # فحص تعطيل البوت (لا يطبَّق على الأدمن)
    if is_bot_disabled() and not is_admin(uid):
        bot.send_message(
            uid,
            "⛔ <b>البوت معطل حالياً</b>\n\n"
            "نعتذر، البوت متوقف مؤقتاً عن استقبال الطلبات الجديدة.\n"
            "بوتاتك المستضافة لا تزال تعمل بشكل طبيعي.\n\n"
            "يرجى المحاولة لاحقاً 🙏",
        )
        return

    # معالجة رابط الهدية (يجب أن يكون المستخدم مسجّلاً قبل إضافة النقاط)
    if gift_token:
        process_gift_link(uid, gift_token)

    # إن كان مستخدم جديد دخل عبر رابط إحالة صالح، نمنح صاحب الرابط نقطة
    if is_new_user and ref_id:
        ref_exists = db_fetchone("SELECT id FROM users WHERE id=?", (ref_id,))
        if ref_exists:
            db_execute(
                "UPDATE users SET referred_by=? WHERE id=? AND (referred_by IS NULL OR referred_by=0)",
                (ref_id, uid),
            )
            db_execute("UPDATE users SET points = COALESCE(points,0) + 1 WHERE id=?", (ref_id,))
            try:
                bot.send_message(
                    ref_id,
                    "🎉 شخص جديد دخل عبر رابط الإحالة الخاص بك!\n"
                    "⭐ تمت إضافة <b>1 نقطة</b> إلى رصيدك. تابع المشاركة!",
                )
            except Exception:
                pass

    # فحص الاشتراك الإجباري في القناة (لا يطبَّق على الأدمن)
    if force_sub_block(uid, uid):
        return

    # المطور والمستخدمون يرون نفس القائمة الرئيسية عند /start
    # لفتح لوحة المطور أرسل /admin
    show_main_menu(uid, uid)


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if msg_seen(message):
        return
    if not is_admin(message.from_user.id):
        return
    bot.send_message(message.chat.id, "👑 لوحة المطور:", reply_markup=admin_menu_markup())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if msg_seen(message):
        return
    pop_pending(message.from_user.id)
    bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية.")


# -------------------- إضافة بوت جديد --------------------
def start_new_project(chat_id: int, uid: int) -> None:
    proj_count = db_fetchone("SELECT COUNT(*) AS c FROM projects WHERE user_id=?", (uid,))["c"]
    is_first = proj_count == 0
    price = get_upload_price()

    # رفع البوت الأول مجاني، البقية تكلف السعر المحدد من الأدمن
    if not is_first and price > 0:
        pts_row = db_fetchone("SELECT COALESCE(points,0) AS p FROM users WHERE id=?", (uid,))
        pts = pts_row["p"] if pts_row else 0
        if pts < price:
            bot.send_message(
                chat_id,
                "❌ لا تملك نقاطاً كافية لرفع بوت إضافي.\n"
                f"💡 رفع البوت الإضافي يكلف <b>{price}</b> نقطة.\n"
                "🎁 يمكنك تجميع النقاط من قسم «تجميع نقاط» بمشاركة رابط الإحالة.",
                reply_markup=back_main_kb(),
            )
            return

    set_pending(uid, {"type": "new_project_name", "is_first": is_first, "price": price})
    bot.send_message(
        chat_id,
        "✨🔥 ارفع بوتك الاول مجانية على اقوى استضافة 🔥✨\n\n"
        "📛 أرسل اسماً قصيراً للبوت (حروف وأرقام فقط).\n"
        "أو /cancel للإلغاء.",
        reply_markup=back_main_kb(),
    )


@bot.message_handler(
    func=lambda m: get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "new_project_name",
    content_types=["text"],
)
def receive_project_name(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    pending = get_pending(uid) or {}
    is_first = pending.get("is_first", True)
    price = int(pending.get("price", get_upload_price()))
    name = message.text.strip()
    name = "".join(c for c in name if c.isalnum() or c in "_-")[:40]
    if not name:
        bot.send_message(uid, "❌ اسم غير صالح. حاول مرة أخرى أو /cancel.")
        return
    exists = db_fetchone("SELECT id FROM projects WHERE user_id=? AND name=?", (uid, name))
    if exists:
        bot.send_message(uid, "❌ يوجد بوت بنفس الاسم. اختر اسماً آخر.")
        return

    # خصم سعر الرفع لأي بوت بعد الأول، مع إعادة فحص الرصيد لمنع الالتفاف
    if not is_first and price > 0:
        with _db_lock:
            pts_row = db_fetchone("SELECT COALESCE(points,0) AS p FROM users WHERE id=?", (uid,))
            pts = pts_row["p"] if pts_row else 0
            if pts < price:
                pop_pending(uid)
                bot.send_message(
                    uid,
                    "❌ رصيد النقاط أصبح غير كافٍ. أُلغيت العملية.",
                )
                return
            db_execute(
                "UPDATE users SET points = points - ? WHERE id=? AND COALESCE(points,0) >= ?",
                (price, uid, price),
            )

    pid = db_execute_returning_id(
        "INSERT INTO projects (user_id, name, main_file, approved) VALUES (?, ?, '', 0)",
        (uid, name),
    )
    set_pending(uid, {"type": "new_project_main", "project_id": pid})
    bot.send_message(
        uid,
        f"✅ تم إنشاء البوت <b>{html_escape(name)}</b>.\n\n"
        "📤 الآن أرسل ملف البوت الرئيسي (.py).\n"
        "بعد القبول من المطور، يمكنك إضافة ملفات تخزين إضافية مثل .txt و .json.\n\n"
        "/cancel للإلغاء.",
    )


@bot.message_handler(content_types=["document"])
def receive_document(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    user = ensure_user_by_id(uid)
    if user["banned"]:
        bot.send_message(uid, "🚫 لقد تم حظرك من استخدام الاستضافة.")
        return

    pending = get_pending(uid)
    doc = message.document
    filename = safe_filename(doc.file_name or "file.bin")
    size = doc.file_size or 0

    if size > MAX_FILE_SIZE:
        bot.send_message(uid, "❌ الملف أكبر من الحد المسموح (50MB).")
        return
    if not is_allowed_extension(filename):
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        bot.send_message(uid, f"❌ صيغة غير مدعومة. المدعوم:\n<code>{allowed}</code>")
        return

    if pending and pending.get("type") == "new_project_main":
        if not filename.endswith(".py"):
            bot.send_message(uid, "❌ الملف الرئيسي يجب أن يكون .py")
            return
        project_id = pending["project_id"]
        db_execute(
            "INSERT INTO pending_uploads (user_id, project_id, filename, telegram_file_id, size_bytes) VALUES (?,?,?,?,?)",
            (uid, project_id, filename, doc.file_id, size),
        )
        db_execute("UPDATE projects SET main_file=? WHERE id=?", (filename, project_id))
        pop_pending(uid)
        proj = db_fetchone("SELECT name FROM projects WHERE id=?", (project_id,))
        bot.send_message(uid, "✅ تم استلام الملف الرئيسي.\n⏳ بانتظار موافقة المطور لتشغيل البوت.")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ موافقة وتشغيل", callback_data=f"adm_approve_proj_{project_id}"),
            types.InlineKeyboardButton("❌ رفض", callback_data=f"adm_reject_proj_{project_id}"),
        )
        bot.send_message(
            ADMIN_ID,
            f"📥 طلب موافقة على بوت\n\n"
            f"المستخدم: <code>{uid}</code>\n"
            f"البوت: <b>{html_escape(proj['name'])}</b>\n"
            f"الملف: <code>{html_escape(filename)}</code>\n"
            f"الحجم: {size//1024} KB",
            reply_markup=kb,
        )
        try:
            bot.forward_message(ADMIN_ID, uid, message.message_id)
        except Exception:
            pass
        return

    if pending and pending.get("type") == "add_file":
        project_id = pending["project_id"]
        proj = db_fetchone("SELECT id, approved FROM projects WHERE id=? AND user_id=?", (project_id, uid))
        if not proj:
            pop_pending(uid)
            bot.send_message(uid, "❌ البوت غير موجود.")
            return
        try:
            file_info = bot.get_file(doc.file_id)
            data = bot.download_file(file_info.file_path)
        except Exception as e:
            bot.send_message(uid, f"❌ فشل تحميل الملف: {e}")
            return
        db_execute(
            """
            INSERT INTO project_files (project_id, filename, content, size_bytes)
            VALUES (?,?,?,?)
            ON CONFLICT (project_id, filename) DO UPDATE
                SET content = excluded.content,
                    size_bytes = excluded.size_bytes,
                    updated_at = CURRENT_TIMESTAMP
            """,
            (project_id, filename, sqlite3.Binary(data), len(data)),
        )
        pop_pending(uid)
        bot.send_message(
            uid,
            f"✅ تم حفظ الملف <code>{html_escape(filename)}</code> ({len(data)//1024} KB).\n"
            "إذا كان البوت يعمل الآن، أعد تشغيله ليستخدم الملف الجديد.",
        )
        return

    bot.send_message(uid, "📎 لاستقبال هذا الملف اختر أولاً ما تريد فعله من القائمة (إنشاء بوت / إضافة ملف).")


# -------------------- إدارة البوتات --------------------
def list_user_projects(chat_id: int, uid: int, message_id: Optional[int] = None) -> None:
    rows = db_fetchall(
        "SELECT id, name, approved, is_running, main_file FROM projects WHERE user_id=? ORDER BY id",
        (uid,),
    )
    if not rows:
        text = "📭 لا توجد بوتات بعد. اضغط <b>إضافة بوت جديد</b> للبدء."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🚀 إضافة بوت جديد", callback_data="proj_new"))
        kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    else:
        text = "📂 <b>بوتاتك:</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for p in rows:
            running = manager.is_running(p["id"])
            status = "🟢" if running else ("🟡" if not p["approved"] else "🔴")
            kb.add(types.InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"proj_view_{p['id']}"))
        kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


# قائمة وحدات بايثون القياسية (للاستثناء من التثبيت)
_STDLIB_MODULES = {
    "os", "sys", "re", "json", "time", "math", "random", "string", "io", "logging",
    "threading", "subprocess", "signal", "shutil", "sqlite3", "pathlib", "typing",
    "datetime", "collections", "contextlib", "functools", "itertools", "asyncio",
    "socket", "http", "urllib", "html", "csv", "hashlib", "base64", "secrets",
    "tempfile", "glob", "argparse", "pickle", "struct", "uuid", "queue", "traceback",
    "platform", "warnings", "copy", "enum", "dataclasses", "abc", "inspect",
    "concurrent", "multiprocessing", "ssl", "email", "xml", "zipfile", "tarfile",
    "gzip", "bz2", "zlib", "ctypes", "operator", "weakref", "gc", "atexit",
}

# خرائط لأسماء الحزم المعروفة (اسم استيراد ↔ اسم تثبيت pip)
_PKG_MAP = {
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "discord": "discord.py",
    "magic": "python-magic",
    "Crypto": "pycryptodome",
    "sklearn": "scikit-learn",
}


def _detect_required_packages(work_dir: str) -> list:
    """يفحص ملفات .py ويستخرج أسماء المكتبات الخارجية المستوردة."""
    import re as _re
    found = set()
    if not os.path.isdir(work_dir):
        return []
    for root, _dirs, files in os.walk(work_dir):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    src = fh.read()
            except Exception:
                continue
            # import X / import X.Y / from X import Y
            for m in _re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z_][\w\.]*)", src, _re.MULTILINE):
                top = m.group(1).split(".")[0]
                if top and top not in _STDLIB_MODULES and not top.startswith("_"):
                    found.add(top)
    # تحويل لاسم pip
    pkgs = []
    for name in sorted(found):
        pkgs.append(_PKG_MAP.get(name, name))
    return pkgs


def install_project_requirements(chat_id: int, uid: int, project_id: int, msg_id: Optional[int] = None) -> None:
    """يثبّت مكتبات البوت — يقرأ requirements.txt إن وُجد، وإلا يكتشف الاستيرادات."""
    p = db_fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        bot.send_message(chat_id, "❌ البوت غير موجود.")
        return
    if p["user_id"] != uid and not is_admin(uid):
        bot.send_message(chat_id, "❌ لا تملك صلاحية على هذا البوت.")
        return
    work_dir = os.path.join(PROJECTS_DIR, str(project_id))
    req_path = os.path.join(work_dir, "requirements.txt")

    bot.send_message(
        chat_id,
        "📦 <b>جارِ تحميل المكتبات…</b>\nقد يستغرق هذا من 10 إلى 60 ثانية حسب عدد المكتبات.",
    )

    # بناء أمر التثبيت
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    source = ""
    if os.path.isfile(req_path):
        cmd += ["-r", req_path]
        source = "requirements.txt"
    else:
        pkgs = _detect_required_packages(work_dir)
        if not pkgs:
            bot.send_message(
                chat_id,
                "ℹ️ لم يتم اكتشاف أي مكتبات خارجية لهذا البوت (يستخدم وحدات بايثون القياسية فقط).",
            )
            return
        cmd += pkgs
        source = "اكتشاف تلقائي من الاستيرادات"

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        out = out.strip() or "(لا يوجد إخراج)"
        # نقصّ المخرجات لتجنّب تجاوز حد الرسالة
        snippet = out[-3000:]
        if result.returncode == 0:
            bot.send_message(
                chat_id,
                f"✅ <b>تم تحميل المكتبات بنجاح</b>\n"
                f"📋 المصدر: <b>{html_escape(source)}</b>\n\n"
                f"<pre>{html_escape(snippet)}</pre>",
            )
        else:
            bot.send_message(
                chat_id,
                f"⚠️ <b>فشل تحميل بعض المكتبات</b> (كود الخطأ: {result.returncode})\n"
                f"📋 المصدر: <b>{html_escape(source)}</b>\n\n"
                f"<pre>{html_escape(snippet)}</pre>",
            )
    except subprocess.TimeoutExpired:
        bot.send_message(chat_id, "❌ انتهت المهلة (5 دقائق). جرّب لاحقاً.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ خطأ أثناء التثبيت: <code>{html_escape(str(e))}</code>")


def view_project(chat_id: int, uid: int, project_id: int, message_id: Optional[int] = None) -> None:
    p = db_fetchone("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid))
    if not p and not is_admin(uid):
        return
    if not p and is_admin(uid):
        p = db_fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        return
    files = db_fetchall(
        "SELECT filename, size_bytes FROM project_files WHERE project_id=? ORDER BY filename",
        (project_id,),
    )
    running = manager.is_running(project_id)
    status_lines = [
        f"📛 <b>{html_escape(p['name'])}</b>",
        f"🆔 <code>{p['id']}</code>",
        f"👤 المالك: <code>{p['user_id']}</code>",
        f"⚙️ الحالة: {'🟢 يعمل' if running else '🔴 متوقف'}",
        f"📄 الملف الرئيسي: <code>{html_escape(p['main_file'] or '-')}</code>",
        f"📁 عدد الملفات: {len(files)}",
        f"✅ موافق عليه: {'نعم' if p['approved'] else 'لا'}",
    ]
    if p["last_error"]:
        status_lines.append(f"⚠️ آخر خطأ: <code>{html_escape(p['last_error'][:200])}</code>")
    text = "\n".join(status_lines)
    if files:
        text += "\n\n<b>الملفات:</b>\n"
        for f in files[:20]:
            text += f"• <code>{html_escape(f['filename'])}</code> ({f['size_bytes']//1024} KB)\n"

    kb = types.InlineKeyboardMarkup(row_width=2)
    if p["approved"]:
        if running:
            kb.add(types.InlineKeyboardButton("🛑 إيقاف", callback_data=f"proj_stop_{project_id}"))
            kb.add(types.InlineKeyboardButton("🔄 إعادة تشغيل", callback_data=f"proj_restart_{project_id}"))
        else:
            kb.add(types.InlineKeyboardButton("▶️ تشغيل", callback_data=f"proj_start_{project_id}"))
    kb.add(
        types.InlineKeyboardButton("➕ إضافة ملف", callback_data=f"proj_addfile_{project_id}"),
        types.InlineKeyboardButton("📜 السجل", callback_data=f"proj_log_{project_id}"),
    )
    kb.add(types.InlineKeyboardButton("📦 تحميل المكتبات", callback_data=f"proj_install_{project_id}"))
    kb.add(types.InlineKeyboardButton("🗑️ حذف البوت", callback_data=f"proj_del_{project_id}"))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="proj_list"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


# -------------------- موجّه الأزرار --------------------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    if call_seen(call):
        return
    uid = call.from_user.id
    user = ensure_user_by_id(uid)
    if user.get("banned"):
        bot.answer_callback_query(call.id, "🚫 محظور", show_alert=True)
        return
    if is_bot_disabled() and not is_admin(uid):
        bot.answer_callback_query(call.id, "⛔ البوت معطل حالياً", show_alert=True)
        return
    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # زر التحقق من الاشتراك (لا يخضع لفحص الاشتراك نفسه)
    if data == "check_sub":
        channel = get_force_channel()
        if not channel or is_user_subscribed(uid, channel):
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "✅ تم التحقق")
            if is_admin(uid):
                bot.send_message(chat_id, "👑 أهلاً بالمطور.", reply_markup=admin_menu_markup())
            else:
                show_main_menu(chat_id, uid)
        else:
            bot.answer_callback_query(call.id, "❌ لم تشترك بعد", show_alert=True)
        return

    # فحص الاشتراك الإجباري قبل أي إجراء آخر
    if not is_admin(uid):
        channel = get_force_channel()
        if channel and not is_user_subscribed(uid, channel):
            bot.answer_callback_query(call.id, "⚠️ يجب الاشتراك في القناة أولاً", show_alert=True)
            force_sub_block(chat_id, uid)
            return

    try:
        if data == "back_main":
            pop_pending(uid)
            show_main_menu(chat_id, uid, msg_id)
        elif data == "adm_panel":
            if not is_admin(uid):
                bot.answer_callback_query(call.id, "❌ للمطور فقط", show_alert=True)
                return
            pop_pending(uid)
            try:
                bot.edit_message_text("👑 لوحة المطور:", chat_id, msg_id, reply_markup=admin_menu_markup())
            except Exception:
                bot.send_message(chat_id, "👑 لوحة المطور:", reply_markup=admin_menu_markup())
        elif data == "proj_new":
            start_new_project(chat_id, uid)
        elif data == "proj_list":
            list_user_projects(chat_id, uid, msg_id)
        elif data == "my_stats":
            show_user_stats(chat_id, uid, msg_id)
        elif data == "help":
            show_help(chat_id, msg_id)
        elif data == "points":
            show_points(chat_id, uid, msg_id)
        elif data == "transfer_points":
            start_transfer_points(chat_id, uid, msg_id)

        elif data.startswith("proj_view_"):
            view_project(chat_id, uid, int(data.split("_")[-1]), msg_id)
        elif data.startswith("proj_start_"):
            pid = int(data.split("_")[-1])
            ok, msg = manager.start_project(pid)
            bot.answer_callback_query(call.id, ("✅ " if ok else "❌ ") + msg)
            view_project(chat_id, uid, pid, msg_id)
        elif data.startswith("proj_stop_"):
            pid = int(data.split("_")[-1])
            ok, msg = manager.stop_project(pid)
            bot.answer_callback_query(call.id, ("✅ " if ok else "❌ ") + msg)
            view_project(chat_id, uid, pid, msg_id)
        elif data.startswith("proj_restart_"):
            pid = int(data.split("_")[-1])
            ok, msg = manager.restart_project(pid)
            bot.answer_callback_query(call.id, ("✅ " if ok else "❌ ") + msg)
            view_project(chat_id, uid, pid, msg_id)
        elif data.startswith("proj_addfile_"):
            pid = int(data.split("_")[-1])
            set_pending(uid, {"type": "add_file", "project_id": pid})
            bot.answer_callback_query(call.id, "أرسل الآن الملف 📎")
            bot.send_message(chat_id, "📎 أرسل الملف الذي تريد إضافته (txt, json, csv, ...)\n/cancel للإلغاء.")
        elif data.startswith("proj_log_"):
            pid = int(data.split("_")[-1])
            log_text = manager.tail_log(pid, 3500) or "(لا يوجد سجل بعد)"
            bot.send_message(chat_id, f"📜 آخر مخرجات البوت:\n<pre>{html_escape(log_text)[-3500:]}</pre>")
        elif data.startswith("proj_install_"):
            pid = int(data.split("_")[-1])
            bot.answer_callback_query(call.id, "⏳ جارِ التحميل…")
            install_project_requirements(chat_id, uid, pid, msg_id)
        elif data.startswith("proj_del_"):
            pid = int(data.split("_")[-1])
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton("✅ نعم احذف", callback_data=f"proj_delconfirm_{pid}"),
                types.InlineKeyboardButton("❌ تراجع", callback_data=f"proj_view_{pid}"),
            )
            bot.edit_message_text("⚠️ هل أنت متأكد من حذف البوت وكل ملفاته؟", chat_id, msg_id, reply_markup=kb)
        elif data.startswith("proj_delconfirm_"):
            pid = int(data.split("_")[-1])
            manager.stop_project(pid)
            db_execute(
                "DELETE FROM projects WHERE id=? AND (user_id=? OR ?=?)",
                (pid, uid, uid, ADMIN_ID),
            )
            bot.answer_callback_query(call.id, "🗑️ تم الحذف")
            list_user_projects(chat_id, uid, msg_id)

        # ---- أوامر الأدمن ----
        elif data.startswith("adm_") and not is_admin(uid):
            bot.answer_callback_query(call.id, "❌ للمطور فقط", show_alert=True)
        elif data == "adm_pending_users":
            show_pending_users(chat_id, msg_id)
        elif data == "adm_pending_projects":
            show_pending_projects(chat_id, msg_id)
        elif data == "adm_users":
            show_all_users(chat_id, msg_id)
        elif data == "adm_projects":
            show_all_projects(chat_id, msg_id)
        elif data == "adm_stats":
            show_admin_stats(chat_id, msg_id)
        elif data == "adm_broadcast":
            set_pending(uid, {"type": "broadcast"})
            bot.send_message(chat_id, "📢 أرسل نص الإذاعة الآن، أو /cancel.", reply_markup=back_admin_kb())
        elif data == "adm_gift_new":
            start_create_gift_link(chat_id, uid)
        elif data == "adm_force_channel":
            start_set_force_channel(chat_id, uid)
        elif data == "adm_set_upload_price":
            start_set_upload_price(chat_id, uid)
        elif data == "adm_set_transfer_fee":
            start_set_transfer_fee(chat_id, uid)
        elif data == "adm_ban_by_id":
            start_ban_by_id(chat_id, uid)
        elif data == "adm_unban_by_id":
            start_unban_by_id(chat_id, uid)
        elif data == "adm_user_info":
            start_user_info(chat_id, uid)
        elif data == "adm_toggle_bot":
            new_state = not is_bot_disabled()
            set_bot_disabled(new_state)
            bot.answer_callback_query(
                call.id,
                "⛔ تم تعطيل البوت" if new_state else "✅ تم تفعيل البوت",
                show_alert=True,
            )
            try:
                bot.edit_message_text("👑 لوحة المطور:", chat_id, msg_id, reply_markup=admin_menu_markup())
            except Exception:
                bot.send_message(chat_id, "👑 لوحة المطور:", reply_markup=admin_menu_markup())
        elif data.startswith("adm_approve_user_"):
            target = int(data.split("_")[-1])
            db_execute("UPDATE users SET approved=1, banned=0 WHERE id=?", (target,))
            bot.answer_callback_query(call.id, "✅ تم التفعيل")
            try:
                bot.send_message(target, "🎉 تم تفعيل حسابك. أرسل /start للبدء.")
            except Exception:
                pass
        elif data.startswith("adm_ban_user_"):
            target = int(data.split("_")[-1])
            db_execute("UPDATE users SET banned=1 WHERE id=?", (target,))
            for p in db_fetchall("SELECT id FROM projects WHERE user_id=?", (target,)):
                manager.stop_project(p["id"])
            bot.answer_callback_query(call.id, "🚫 تم الحظر")
        elif data.startswith("adm_unban_user_"):
            target = int(data.split("_")[-1])
            db_execute("UPDATE users SET banned=0 WHERE id=?", (target,))
            bot.answer_callback_query(call.id, "✅ تم رفع الحظر")
        elif data.startswith("adm_approve_proj_"):
            pid = int(data.split("_")[-1])
            approve_project(pid)
            bot.answer_callback_query(call.id, "✅ تمت الموافقة وبدء التشغيل", show_alert=True)
            try:
                bot.edit_message_text(f"✅ تمت الموافقة على البوت #{pid}", chat_id, msg_id)
            except Exception:
                pass
        elif data.startswith("adm_reject_proj_"):
            pid = int(data.split("_")[-1])
            row = db_fetchone("SELECT user_id, name FROM projects WHERE id=?", (pid,))
            if row:
                try:
                    bot.send_message(row["user_id"], f"❌ تم رفض البوت <b>{html_escape(row['name'])}</b> من المطور.")
                except Exception:
                    pass
            db_execute("DELETE FROM projects WHERE id=?", (pid,))
            bot.answer_callback_query(call.id, "❌ تم الرفض")
            try:
                bot.edit_message_text(f"❌ تم رفض البوت #{pid}", chat_id, msg_id)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id)
    except Exception as e:
        log.exception("callback error: %s", data)
        try:
            bot.answer_callback_query(call.id, f"خطأ: {str(e)[:80]}", show_alert=True)
        except Exception:
            pass


# -------------------- الموافقة على البوت + تنزيل ملفه --------------------
def approve_project(project_id: int) -> None:
    p = db_fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        return
    pending = db_fetchone(
        "SELECT * FROM pending_uploads WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,),
    )
    if pending:
        try:
            file_info = bot.get_file(pending["telegram_file_id"])
            data = bot.download_file(file_info.file_path)
            db_execute(
                """
                INSERT INTO project_files (project_id, filename, content, size_bytes)
                VALUES (?,?,?,?)
                ON CONFLICT (project_id, filename) DO UPDATE
                    SET content = excluded.content,
                        size_bytes = excluded.size_bytes,
                        updated_at = CURRENT_TIMESTAMP
                """,
                (project_id, pending["filename"], sqlite3.Binary(data), len(data)),
            )
            db_execute("DELETE FROM pending_uploads WHERE id=?", (pending["id"],))
        except Exception as e:
            log.exception("download for project %s failed", project_id)
            db_execute("UPDATE projects SET last_error=? WHERE id=?", (f"download failed: {e}", project_id))
            try:
                bot.send_message(p["user_id"], f"❌ فشل تحميل ملف البوت: {e}")
            except Exception:
                pass
            return
    db_execute("UPDATE projects SET approved=1 WHERE id=?", (project_id,))
    try:
        bot.send_message(p["user_id"], f"🎉 تمت الموافقة على بوت <b>{html_escape(p['name'])}</b> وبدء تشغيله.")
    except Exception:
        pass
    ok, msg = manager.start_project(project_id)
    log.info("Approved project %s — start=%s (%s)", project_id, ok, msg)


# -------------------- صفحات إحصائية وأدمن --------------------
def show_user_stats(chat_id: int, uid: int, message_id: int) -> None:
    rows = db_fetchall("SELECT id, is_running FROM projects WHERE user_id=?", (uid,))
    total = len(rows)
    running = sum(1 for r in rows if manager.is_running(r["id"]))
    size_row = db_fetchone(
        """SELECT COALESCE(SUM(pf.size_bytes),0) AS s
           FROM project_files pf
           JOIN projects p ON p.id = pf.project_id
           WHERE p.user_id=?""",
        (uid,),
    )
    text = (
        "📊 <b>إحصائياتك</b>\n\n"
        f"📦 عدد البوتات: <b>{total}</b>\n"
        f"⚡ التي تعمل الآن: <b>{running}</b>\n"
        f"💾 حجم ملفاتك: <b>{(size_row['s'] or 0)//1024} KB</b>\n"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_points(chat_id: int, uid: int, message_id: int) -> None:
    row = db_fetchone(
        "SELECT COALESCE(points,0) AS points FROM users WHERE id=?",
        (uid,),
    )
    points = row["points"] if row else 0
    refs_count_row = db_fetchone(
        "SELECT COUNT(*) AS c FROM users WHERE referred_by=?",
        (uid,),
    )
    refs_count = refs_count_row["c"] if refs_count_row else 0

    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start={uid}"
    share_url = (
        f"https://t.me/share/url?url={ref_link}"
        "&text=" + "انضم%20إلى%20بوت%20استضافة%20البوتات%20عبر%20رابطي"
    )

    text = (
        "🎁 <b>قسم تجميع النقاط</b>\n\n"
        "مرحباً بك في قسم تجميع النقاط 🎉\n"
        "قم بمشاركة رابط الإحالة الخاص بك وستحصل على "
        "<b>1 نقطة</b> لكل شخص يدخل عبر الرابط 👇\n\n"
        f"🔗 <code>{ref_link}</code>\n\n"
        f"⭐ نقاطك: <b>{points}</b>\n"
        f"👥 عدد من دخل عبر رابطك: <b>{refs_count}</b>"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📤 مشاركة الرابط", url=share_url))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=kb)


def start_transfer_points(chat_id: int, uid: int, message_id: int) -> None:
    fee = get_transfer_fee()
    set_pending(uid, {"type": "transfer_recipient"})
    text = (
        "💸 <b>قسم تحويل النقاط:</b>\n\n"
        "يمكن الآن تحويل النقاط بكل سهولة، فقط قم بإرسال "
        "<b>id</b> المستلم 🎁\n\n"
        "⚠️ أقل عدد يمكن تحويله: <b>5</b>\n"
        f"⚠️ عمولة التحويل: <b>{fee}</b> نقطة\n\n"
        "أو /cancel للإلغاء."
    )
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=back_main_kb())
    except Exception:
        bot.send_message(chat_id, text, reply_markup=back_main_kb())


@bot.message_handler(
    func=lambda m: get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "transfer_recipient",
    content_types=["text"],
)
def receive_transfer_recipient(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        bot.send_message(uid, "❌ يرجى إرسال <b>id</b> رقمي صالح. أو /cancel للإلغاء.")
        return
    recipient_id = int(text)
    if recipient_id == uid:
        bot.send_message(uid, "❌ لا يمكنك التحويل لنفسك. أرسل <b>id</b> آخر أو /cancel.")
        return
    recipient = db_fetchone("SELECT id, banned FROM users WHERE id=?", (recipient_id,))
    if not recipient:
        bot.send_message(
            uid,
            "❌ المستخدم غير موجود في البوت.\n"
            "تأكد من أن المستلم قد بدأ البوت من قبل، ثم أعد المحاولة أو /cancel.",
        )
        return
    if recipient.get("banned"):
        bot.send_message(uid, "❌ لا يمكن التحويل إلى مستخدم محظور. أرسل id آخر أو /cancel.")
        return
    set_pending(uid, {"type": "transfer_amount", "recipient_id": recipient_id})
    bot.send_message(uid, "❄️ ارسل عدد النقاط المراد تحويلها:")


@bot.message_handler(
    func=lambda m: get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "transfer_amount",
    content_types=["text"],
)
def receive_transfer_amount(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    pending = get_pending(uid) or {}
    recipient_id = pending.get("recipient_id")
    if not recipient_id:
        pop_pending(uid)
        bot.send_message(uid, "❌ انتهت جلسة التحويل. ابدأ من جديد.")
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(uid, "❌ يرجى إرسال عدد صحيح موجب. أو /cancel.")
        return
    amount = int(text)
    if amount < 5:
        bot.send_message(
            uid,
            "❌ أقل عدد يمكن تحويله هو <b>5</b> نقاط. أرسل عدداً صحيحاً أو /cancel.",
        )
        return

    fee = get_transfer_fee()
    total_deduct = amount + fee

    # تنفيذ التحويل ذرّياً تحت قفل قاعدة البيانات لمنع السباق
    with _db_lock:
        sender_row = db_fetchone("SELECT COALESCE(points,0) AS p FROM users WHERE id=?", (uid,))
        sender_pts = sender_row["p"] if sender_row else 0
        if sender_pts < total_deduct:
            pop_pending(uid)
            bot.send_message(
                uid,
                f"❌ رصيدك ({sender_pts}) غير كافٍ.\n"
                f"المطلوب: {amount} + عمولة {fee} = <b>{total_deduct}</b> نقطة. أُلغيت العملية.",
            )
            return
        db_execute(
            "UPDATE users SET points = points - ? WHERE id=? AND COALESCE(points,0) >= ?",
            (total_deduct, uid, total_deduct),
        )
        db_execute(
            "UPDATE users SET points = COALESCE(points,0) + ? WHERE id=?",
            (amount, recipient_id),
        )
    pop_pending(uid)

    sender_msg = f"🎁 تم إرسال <b>{amount}</b> نقطة إلى المستخدم <code>{recipient_id}</code> بنجاح 🎁"
    if fee > 0:
        sender_msg += f"\n💸 (تم خصم {fee} نقطة عمولة، الإجمالي {total_deduct})"
    bot.send_message(uid, sender_msg, reply_markup=back_main_kb())
    try:
        bot.send_message(
            recipient_id,
            f"🎉 تهانينا، تم استلام مبلغ <b>{amount}</b> نقطة من المستخدم <code>{uid}</code> 🎁",
        )
    except Exception:
        log.exception("Failed to notify recipient %s", recipient_id)


def show_help(chat_id: int, message_id: int) -> None:
    text = (
        "🆘 <b>دليل الاستخدام</b>\n\n"
        "1️⃣ اضغط <b>إضافة بوت جديد</b> ثم أعطه اسماً.\n"
        "2️⃣ أرسل ملف بايثون الرئيسي (.py).\n"
        "3️⃣ بعد موافقة المطور سيبدأ البوت تلقائياً.\n"
        "4️⃣ من قائمة البوتات يمكنك تشغيل/إيقاف/إعادة تشغيل/إضافة ملفات.\n\n"
        "📦 <b>ملفات التخزين المدعومة:</b>\n"
        ".py .txt .json .csv .env .yaml .ini .md .sql .db .html .css .js\n\n"
        "🛡️ كل بوت يعمل في عملية مستقلة، إذا توقف يُعاد تشغيله تلقائياً.\n"
        f"📞 الدعم: {html_escape(ADMIN_CONTACT)}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_pending_users(chat_id: int, message_id: int) -> None:
    rows = db_fetchall(
        "SELECT id, first_name, username FROM users WHERE approved=0 AND banned=0 ORDER BY created_at DESC LIMIT 30"
    )
    if not rows:
        text = "✨ لا توجد طلبات تفعيل."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        return
    text = "⏳ <b>طلبات تفعيل بانتظار:</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        label = f"{r['first_name'] or '-'} (@{r['username'] or '-'}) [{r['id']}]"
        kb.add(types.InlineKeyboardButton(f"✅ تفعيل {label}", callback_data=f"adm_approve_user_{r['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_pending_projects(chat_id: int, message_id: int) -> None:
    rows = db_fetchall(
        """SELECT p.id, p.name, p.user_id, p.main_file
           FROM projects p
           WHERE p.approved=0
           ORDER BY p.created_at DESC LIMIT 30"""
    )
    if not rows:
        text = "✨ لا توجد بوتات بانتظار الموافقة."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        return
    text = "⏳ <b>بوتات بانتظار الموافقة:</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=2)
    for r in rows:
        text += f"• #{r['id']} <b>{html_escape(r['name'])}</b> — مالك <code>{r['user_id']}</code>\n"
        kb.add(
            types.InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"adm_approve_proj_{r['id']}"),
            types.InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"adm_reject_proj_{r['id']}"),
        )
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_all_users(chat_id: int, message_id: int) -> None:
    rows = db_fetchall(
        "SELECT id, first_name, username, approved, banned FROM users ORDER BY created_at DESC LIMIT 50"
    )
    text = "👥 <b>المستخدمون (آخر 50):</b>\n\n"
    for r in rows:
        s = "🟢" if r["approved"] and not r["banned"] else ("🚫" if r["banned"] else "🟡")
        text += f"{s} <code>{r['id']}</code> {html_escape(r['first_name'] or '')} (@{r['username'] or '-'})\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_all_projects(chat_id: int, message_id: int) -> None:
    rows = db_fetchall(
        "SELECT id, user_id, name, approved, is_running FROM projects ORDER BY id DESC LIMIT 50"
    )
    text = "🤖 <b>كل البوتات (آخر 50):</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        running = manager.is_running(r["id"])
        s = "🟢" if running else ("🟡" if not r["approved"] else "🔴")
        kb.add(types.InlineKeyboardButton(f"{s} #{r['id']} {r['name']} — {r['user_id']}", callback_data=f"proj_view_{r['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


def show_admin_stats(chat_id: int, message_id: int) -> None:
    counts = db_fetchone(
        """
        SELECT
          (SELECT COUNT(*) FROM users) AS users_total,
          (SELECT COUNT(*) FROM users WHERE approved=1) AS users_approved,
          (SELECT COUNT(*) FROM users WHERE banned=1) AS users_banned,
          (SELECT COUNT(*) FROM projects) AS projects_total,
          (SELECT COUNT(*) FROM projects WHERE approved=1) AS projects_approved,
          (SELECT COALESCE(SUM(size_bytes),0) FROM project_files) AS total_bytes
        """
    )
    running = sum(1 for hp in manager.processes.values() if hp.is_alive())
    text = (
        "📈 <b>إحصائيات النظام</b>\n\n"
        f"👥 المستخدمون: {counts['users_total']} (مفعّل {counts['users_approved']}، محظور {counts['users_banned']})\n"
        f"🤖 البوتات: {counts['projects_total']} (موافق عليها {counts['projects_approved']})\n"
        f"⚡ تعمل الآن: <b>{running}</b>\n"
        f"💾 إجمالي ملفات التخزين: {counts['total_bytes']//1024} KB\n"
        f"⏰ الوقت: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main"))
    bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)


# -------------------- الإذاعة --------------------
@bot.message_handler(
    func=lambda m: get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "broadcast",
    content_types=["text"],
)
def receive_broadcast(message):
    if msg_seen(message):
        return
    if not is_admin(message.from_user.id):
        return
    pop_pending(message.from_user.id)
    text = message.text
    rows = db_fetchall("SELECT id FROM users WHERE banned=0")
    ok = 0
    fail = 0
    bot.send_message(message.chat.id, f"📤 إرسال إلى {len(rows)} مستخدم…")
    for r in rows:
        try:
            bot.send_message(r["id"], f"📢 <b>إعلان من المطور:</b>\n\n{html_escape(text)}")
            ok += 1
            time.sleep(0.05)
        except Exception:
            fail += 1
    bot.send_message(
        message.chat.id,
        f"✅ تم: نجح {ok} / فشل {fail}",
        reply_markup=back_admin_kb(),
    )


# -------------------- إعدادات الأدمن (سعر/عمولة/قناة/حظر/هدية) --------------------
def _admin_only(uid: int) -> bool:
    return is_admin(uid)


def start_set_upload_price(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    cur = get_upload_price()
    set_pending(uid, {"type": "set_upload_price"})
    bot.send_message(
        chat_id,
        "💰 <b>تعديل سعر رفع البوت</b>\n\n"
        f"السعر الحالي: <b>{cur}</b> نقطة\n\n"
        "أرسل العدد الجديد (0 = مجاني للجميع).\n"
        "أو /cancel للإلغاء.",
        reply_markup=back_admin_kb(),
    )


def start_set_transfer_fee(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    cur = get_transfer_fee()
    set_pending(uid, {"type": "set_transfer_fee"})
    bot.send_message(
        chat_id,
        "💸 <b>تعديل عمولة التحويل</b>\n\n"
        f"العمولة الحالية: <b>{cur}</b> نقطة\n\n"
        "أرسل العدد الجديد (0 = بدون عمولة).\n"
        "أو /cancel للإلغاء.",
        reply_markup=back_admin_kb(),
    )


def start_set_force_channel(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    cur = get_force_channel() or "(لا يوجد)"
    set_pending(uid, {"type": "set_force_channel"})
    bot.send_message(
        chat_id,
        "📢 <b>قناة الاشتراك الإجباري</b>\n\n"
        f"الحالية: <code>{html_escape(cur)}</code>\n\n"
        "أرسل اسم القناة بصيغة <code>@channel</code> أو رابط <code>https://t.me/channel</code>.\n"
        "للإلغاء (تعطيل الاشتراك الإجباري) أرسل كلمة: <code>off</code>\n\n"
        "⚠️ يجب أن يكون البوت <b>مشرفاً</b> في القناة لكي يستطيع التحقق من الاشتراك.\n"
        "أو /cancel للإلغاء.",
        reply_markup=back_admin_kb(),
    )


def start_ban_by_id(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    set_pending(uid, {"type": "admin_ban_user"})
    bot.send_message(
        chat_id,
        "🚫 <b>حظر مستخدم</b>\n\nأرسل <b>id</b> المستخدم المراد حظره.\nأو /cancel.",
        reply_markup=back_admin_kb(),
    )


def start_unban_by_id(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    set_pending(uid, {"type": "admin_unban_user"})
    bot.send_message(
        chat_id,
        "✅ <b>رفع حظر مستخدم</b>\n\nأرسل <b>id</b> المستخدم المراد فكّ حظره.\nأو /cancel.",
        reply_markup=back_admin_kb(),
    )


def start_user_info(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    set_pending(uid, {"type": "admin_user_info"})
    bot.send_message(
        chat_id,
        "🪪 <b>معلومات مستخدم</b>\n\nأرسل <b>id</b> المستخدم لعرض كامل معلوماته.\nأو /cancel.",
        reply_markup=back_admin_kb(),
    )


def start_create_gift_link(chat_id: int, uid: int) -> None:
    if not _admin_only(uid):
        return
    set_pending(uid, {"type": "gift_points"})
    bot.send_message(
        chat_id,
        "🎁 <b>إنشاء رابط هدية نقاط</b>\n\n"
        "🔢 <b>الخطوة 1/3:</b> أرسل عدد النقاط لكل مستخدم.\nأو /cancel للإلغاء.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "set_upload_price",
    content_types=["text"],
)
def receive_set_upload_price(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(uid, "❌ أرسل رقماً صحيحاً ≥ 0. أو /cancel.", reply_markup=back_admin_kb())
        return
    val = int(text)
    set_setting("upload_price", val)
    pop_pending(uid)
    bot.send_message(
        uid,
        f"✅ تم تحديث سعر رفع البوت إلى <b>{val}</b> نقطة.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "set_transfer_fee",
    content_types=["text"],
)
def receive_set_transfer_fee(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(uid, "❌ أرسل رقماً صحيحاً ≥ 0. أو /cancel.", reply_markup=back_admin_kb())
        return
    val = int(text)
    set_setting("transfer_fee", val)
    pop_pending(uid)
    bot.send_message(
        uid,
        f"✅ تم تحديث عمولة التحويل إلى <b>{val}</b> نقطة.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "set_force_channel",
    content_types=["text"],
)
def receive_set_force_channel(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if text.lower() in ("off", "none", "disable", "تعطيل", "ايقاف"):
        set_setting("force_channel", "")
        pop_pending(uid)
        bot.send_message(uid, "✅ تم تعطيل الاشتراك الإجباري.", reply_markup=back_admin_kb())
        return
    channel = normalize_channel(text)
    if not channel or len(channel) < 3:
        bot.send_message(
            uid,
            "❌ صيغة غير صالحة. مثال: <code>@my_channel</code>\nأو /cancel.",
            reply_markup=back_admin_kb(),
        )
        return
    set_setting("force_channel", channel)
    pop_pending(uid)
    bot.send_message(
        uid,
        f"✅ تم تعيين قناة الاشتراك الإجباري إلى: <b>{html_escape(channel)}</b>\n\n"
        "⚠️ تأكد من أن البوت <b>مشرف</b> في القناة لتفعيل التحقق.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "admin_ban_user",
    content_types=["text"],
)
def receive_admin_ban_user(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        bot.send_message(uid, "❌ أرسل id رقمي صالح. أو /cancel.", reply_markup=back_admin_kb())
        return
    target = int(text)
    if target == uid:
        bot.send_message(uid, "❌ لا يمكنك حظر نفسك.", reply_markup=back_admin_kb())
        return
    db_execute(
        "INSERT INTO users (id, banned) VALUES (?, 1) "
        "ON CONFLICT(id) DO UPDATE SET banned=1",
        (target,),
    )
    for p in db_fetchall("SELECT id FROM projects WHERE user_id=?", (target,)):
        manager.stop_project(p["id"])
    pop_pending(uid)
    bot.send_message(uid, f"🚫 تم حظر المستخدم <code>{target}</code>.", reply_markup=back_admin_kb())
    try:
        bot.send_message(target, "🚫 لقد تم حظرك من استخدام البوت.")
    except Exception:
        pass


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "admin_user_info",
    content_types=["text"],
)
def receive_admin_user_info(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip().lstrip("@")
    if not text.lstrip("-").isdigit():
        bot.send_message(uid, "❌ أرسل id رقمي صالح. أو /cancel.", reply_markup=back_admin_kb())
        return
    target = int(text)
    row = db_fetchone(
        "SELECT id, first_name, username, approved, banned, max_bots, "
        "COALESCE(points,0) AS points, created_at, referred_by "
        "FROM users WHERE id=?",
        (target,),
    )
    if not row:
        pop_pending(uid)
        bot.send_message(
            uid,
            f"❌ لا يوجد مستخدم بهذا الـ id: <code>{target}</code>",
            reply_markup=back_admin_kb(),
        )
        return

    proj_total = db_fetchone(
        "SELECT COUNT(*) AS c FROM projects WHERE user_id=?", (target,)
    )["c"]
    proj_approved = db_fetchone(
        "SELECT COUNT(*) AS c FROM projects WHERE user_id=? AND approved=1", (target,)
    )["c"]
    running_count = 0
    project_lines = []
    for p in db_fetchall(
        "SELECT id, name, approved FROM projects WHERE user_id=? ORDER BY id ASC",
        (target,),
    ):
        hp = manager.processes.get(p["id"])
        is_run = bool(hp and hp.is_alive())
        if is_run:
            running_count += 1
        status = "🟢 يعمل" if is_run else ("🟡 معتمد" if p["approved"] else "🔴 بانتظار الموافقة")
        project_lines.append(
            f"   ▫️ <code>#{p['id']}</code> — <b>{html_escape(p['name'])}</b> — {status}"
        )

    referrals_count = db_fetchone(
        "SELECT COUNT(*) AS c FROM users WHERE referred_by=?", (target,)
    )["c"]

    fname = (row.get("first_name") or "").strip() or "—"
    uname = (row.get("username") or "").strip()
    uname_disp = f"@{uname}" if uname else "—"
    joined = row.get("created_at") or "—"
    points = row.get("points") or 0
    max_bots = row.get("max_bots") or 0
    referred_by = row.get("referred_by")
    ref_disp = f"<code>{referred_by}</code>" if referred_by else "لا يوجد"

    status_parts = []
    if row.get("banned"):
        status_parts.append("🚫 محظور")
    if row.get("approved"):
        status_parts.append("✅ مفعّل")
    else:
        status_parts.append("⏳ بانتظار التفعيل")
    status_disp = " | ".join(status_parts)

    info = (
        "🪪 <b>معلومات المستخدم الكاملة</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>الاسم:</b> {html_escape(fname)}\n"
        f"🔖 <b>اليوزر:</b> {html_escape(uname_disp)}\n"
        f"🆔 <b>الايدي:</b> <code>{target}</code>\n"
        f"📅 <b>تاريخ الدخول للبوت:</b> <code>{html_escape(str(joined))}</code>\n"
        f"⚙️ <b>الحالة:</b> {status_disp}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>عدد النقاط:</b> <b>{points}</b>\n"
        f"📦 <b>الحد الأقصى للبوتات:</b> <b>{max_bots}</b>\n"
        f"🤖 <b>عدد البوتات الكلي:</b> <b>{proj_total}</b>\n"
        f"✅ <b>البوتات المعتمدة:</b> <b>{proj_approved}</b>\n"
        f"🔋 <b>البوتات النشطة الآن:</b> <b>{running_count}</b>\n"
        f"🪫 <b>البوتات المتوقفة:</b> <b>{max(proj_total - running_count, 0)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>عدد الإحالات (المشاركات):</b> <b>{referrals_count}</b>\n"
        f"👥 <b>دخل عبر إحالة:</b> {ref_disp}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    if project_lines:
        info += "\n📋 <b>قائمة بوتاته:</b>\n" + "\n".join(project_lines)
    else:
        info += "\n📋 <b>قائمة بوتاته:</b> لا يوجد"

    pop_pending(uid)
    bot.send_message(uid, info, reply_markup=back_admin_kb())


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "admin_unban_user",
    content_types=["text"],
)
def receive_admin_unban_user(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        bot.send_message(uid, "❌ أرسل id رقمي صالح. أو /cancel.", reply_markup=back_admin_kb())
        return
    target = int(text)
    db_execute("UPDATE users SET banned=0 WHERE id=?", (target,))
    pop_pending(uid)
    bot.send_message(uid, f"✅ تم رفع الحظر عن <code>{target}</code>.", reply_markup=back_admin_kb())
    try:
        bot.send_message(target, "🎉 تم رفع الحظر عنك. أرسل /start للبدء.")
    except Exception:
        pass


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "gift_points",
    content_types=["text"],
)
def receive_gift_points(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        bot.send_message(
            uid,
            "❌ أرسل عدد نقاط صحيح موجب (> 0). أو /cancel.",
            reply_markup=back_admin_kb(),
        )
        return
    points = int(text)
    set_pending(uid, {"type": "gift_uses", "points": points})
    bot.send_message(
        uid,
        f"✅ النقاط: <b>{points}</b>\n\n"
        "🔢 <b>الخطوة 2/3:</b> أرسل عدد المستخدمين الذين يمكنهم استخدام الرابط.\n"
        "أو /cancel.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "gift_uses",
    content_types=["text"],
)
def receive_gift_uses(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    pending = get_pending(uid) or {}
    points = int(pending.get("points", 0))
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        bot.send_message(
            uid,
            "❌ أرسل عدد مستخدمين صحيح موجب (> 0). أو /cancel.",
            reply_markup=back_admin_kb(),
        )
        return
    max_uses = int(text)
    set_pending(uid, {"type": "gift_days", "points": points, "max_uses": max_uses})
    bot.send_message(
        uid,
        f"✅ عدد المستخدمين: <b>{max_uses}</b>\n\n"
        "⏳ <b>الخطوة 3/3:</b> أرسل مدة صلاحية الرابط <b>بالأيام</b>.\n"
        "• مثال: <code>7</code> يعني الرابط يبقى صالحاً 7 أيام\n"
        "• أرسل <code>0</code> لجعله بدون انتهاء (دائم)\n"
        "أو /cancel.",
        reply_markup=back_admin_kb(),
    )


@bot.message_handler(
    func=lambda m: is_admin(m.from_user.id)
    and get_pending(m.from_user.id) is not None
    and get_pending(m.from_user.id).get("type") == "gift_days",
    content_types=["text"],
)
def receive_gift_days(message):
    if msg_seen(message):
        return
    uid = message.from_user.id
    pending = get_pending(uid) or {}
    points = int(pending.get("points", 0))
    max_uses = int(pending.get("max_uses", 0))
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(
            uid,
            "❌ أرسل عدد أيام صحيح ≥ 0. (0 = بدون انتهاء). أو /cancel.",
            reply_markup=back_admin_kb(),
        )
        return
    days = int(text)
    expires_at = None
    if days > 0:
        expires_at = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    token = _new_gift_token()
    while db_fetchone("SELECT 1 AS x FROM gift_links WHERE token=?", (token,)):
        token = _new_gift_token()
    db_execute(
        "INSERT INTO gift_links (token, points, max_uses, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, points, max_uses, uid, expires_at),
    )
    pop_pending(uid)

    # بناء رابط البدء
    try:
        me = bot.get_me()
        bot_username = me.username
    except Exception:
        bot_username = ""
    if bot_username:
        link = f"https://t.me/{bot_username}?start=gift_{token}"
    else:
        link = f"start=gift_{token}"

    expiry_line = (
        f"⏳ ينتهي في: <b>{html_escape(expires_at)}</b> (UTC)\n"
        if expires_at
        else "⏳ المدة: <b>دائم</b> (بدون انتهاء)\n"
    )

    bot.send_message(
        uid,
        "✅ <b>تم إنشاء رابط الهدية</b>\n\n"
        f"🎁 النقاط لكل مستخدم: <b>{points}</b>\n"
        f"👥 الحد الأقصى للاستخدامات: <b>{max_uses}</b>\n"
        f"{expiry_line}\n"
        f"🔗 <code>{link}</code>\n\n"
        "شارك الرابط مع المستخدمين، وكل مستخدم يضغط عليه يحصل على النقاط مرة واحدة فقط.",
        reply_markup=back_admin_kb(),
    )


# -------------------- أوامر سلاش للأدمن --------------------
@bot.message_handler(commands=["add"])
def cmd_add(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "الاستخدام: /add <user_id>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ معرّف غير صالح")
        return
    db_execute(
        "INSERT INTO users (id, approved) VALUES (?, 1) ON CONFLICT (id) DO UPDATE SET approved=1, banned=0",
        (target,),
    )
    bot.send_message(message.chat.id, f"✅ تم تفعيل {target}")
    try:
        bot.send_message(target, "🎉 تم تفعيل حسابك. أرسل /start.")
    except Exception:
        pass


@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "الاستخدام: /ban <user_id>")
        return
    target = int(parts[1])
    db_execute("UPDATE users SET banned=1 WHERE id=?", (target,))
    for p in db_fetchall("SELECT id FROM projects WHERE user_id=?", (target,)):
        manager.stop_project(p["id"])
    bot.send_message(message.chat.id, f"🚫 تم حظر {target}")


@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        return
    target = int(parts[1])
    db_execute("UPDATE users SET banned=0 WHERE id=?", (target,))
    bot.send_message(message.chat.id, f"✅ تم رفع الحظر عن {target}")


@bot.message_handler(commands=["limit"])
def cmd_limit(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "الاستخدام: /limit <user_id> <max_bots>")
        return
    target = int(parts[1])
    n = int(parts[2])
    db_execute("UPDATE users SET max_bots=? WHERE id=?", (n, target))
    bot.send_message(message.chat.id, f"✅ تم ضبط الحد {n} للمستخدم {target}")


@bot.message_handler(
    func=lambda m: not (m.text or "").startswith("/"),
    content_types=["text"],
)
def fallback(message):
    if msg_seen(message):
        return
    if get_pending(message.from_user.id):
        return
    show_main_menu(message.chat.id, message.from_user.id)


# ============================================================
# 9) نقطة التشغيل
# ============================================================
def graceful_shutdown(signum, frame):
    log.warning("Received signal %s, shutting down…", signum)
    try:
        manager.shutdown()
    finally:
        sys.exit(0)


def run_polling():
    # نتأكد من إزالة أي webhook قديم لمنع وصول التحديثات مرتين (webhook + polling).
    try:
        bot.remove_webhook()
    except Exception:
        log.exception("remove_webhook failed (يمكن تجاهلها)")
    while True:
        try:
            log.info("Starting Telegram polling…")
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
                logger_level=logging.WARNING,
            )
        except Exception:
            log.exception("Polling crashed; restarting in 5s")
            time.sleep(5)


def main():
    log.info("Opening SQLite database…")
    db_init()
    db_create_schema()
    db_ensure_columns()

    db_execute("UPDATE projects SET is_running=0")

    log.info("Starting keep-alive web server…")

    log.info("Starting watchdog…")
    manager.start_watchdog()

    log.info("Restoring previously-running projects…")
    try:
        manager.restore_running()
    except Exception:
        log.exception("restore_running failed")

    try:
        signal.signal(signal.SIGTERM, graceful_shutdown)
        signal.signal(signal.SIGINT, graceful_shutdown)
    except Exception:
        pass  # بعض البيئات على الجوال لا تسمح بضبط الإشارات

    log.info("Hosting bot is up. Admin id=%s", ADMIN_ID)
    run_polling()


if __name__ == "__main__":
    main()
