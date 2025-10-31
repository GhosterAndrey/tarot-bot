#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import html
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
import secrets
import signal
import threading
from datetime import datetime

import requests
import sqlite3

# =========================
# Мини HTTP-сервер для Render Web Service (health check на $PORT)
# =========================
# Это нужно только для хостинга как Web Service. На VPS/Docker это не мешает.
try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        # убрать лишний шум в логах
        def log_message(self, format, *args):
            return

    def _run_health_server():
        try:
            port = int(os.environ.get("PORT", "10000"))
            httpd = HTTPServer(("0.0.0.0", port), _HealthHandler)
            httpd.serve_forever()
        except Exception:
            pass

    threading.Thread(target=_run_health_server, daemon=True).start()
except Exception:
    # если вдруг http.server недоступен — просто игнорируем
    pass


# =========================
# Логирование
# =========================

LOG_LEVEL = logging.INFO
logger = logging.getLogger("simple_tarot_bot")
logger.setLevel(LOG_LEVEL)

_console = logging.StreamHandler()
_console.setLevel(LOG_LEVEL)
_console.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_console)

_file = RotatingFileHandler("simple_tarot_bot.log", maxBytes=512_000, backupCount=3, encoding="utf-8")
_file.setLevel(LOG_LEVEL)
_file.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(_file)


# =========================
# Конфиг и константы
# =========================

def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(key)
    return v if v else default

# Токен — из ENV или впишите прямо сюда
BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Админы из ENV (опционально, можно добавлять через скрытую команду)
_admin_env = _env("ADMIN_IDS", "")
if _admin_env:
    try:
        ADMIN_IDS_ENV: List[int] = [int(x.strip()) for x in _admin_env.split(",") if x.strip()]
    except ValueError:
        ADMIN_IDS_ENV: List[int] = []
else:
    ADMIN_IDS_ENV: List[int] = []

# Постоянный секрет /iamadmin_<код>:
ADMIN_SECRET_ENV: Optional[str] = _env("ADMIN_SECRET", None)

DB_PATH = _env("DB_PATH", "simple_tarot.db")  # можно пробросить /data/simple_tarot.db на Render

# Быстрый выход
API_CONNECT_TIMEOUT = int(_env("API_CONNECT_TIMEOUT", "5"))
API_READ_TIMEOUT   = int(_env("API_READ_TIMEOUT", "10"))
POLL_TIMEOUT_SEC   = int(_env("POLL_TIMEOUT_SEC", "9"))
SLEEP_BETWEEN_POLLS = float(_env("SLEEP_BETWEEN_POLLS", "0.3"))
SLEEP_BETWEEN_BROADCAST = float(_env("SLEEP_BETWEEN_BROADCAST", "0.05"))

# Арканы Таро
TAROT_ARCANA: Dict[str, str] = {
    "fool": "🃏 Шут",
    "magician": "🧙‍♂️ Маг",
    "priestess": "🌙 Верховная Жрица",
    "empress": "👑 Императрица",
    "emperor": "🏛️ Император",
    "hierophant": "📜 Иерофант",
    "lovers": "💑 Влюбленные",
    "chariot": "⚔️ Колесница",
    "strength": "🦁 Сила",
    "hermit": "🕯️ Отшельник",
    "wheel": "🎡 Колесо Фортуны",
    "justice": "⚖️ Правосудие",
    "hanged": "🙏 Повешенный",
    "death": "💀 Смерть",
    "temperance": "🍶 Умеренность",
    "devil": "😈 Дьявол",
    "tower": "🏰 Башня",
    "star": "⭐ Звезда",
    "moon": "🌕 Луна",
    "sun": "☀️ Солнце",
    "judgement": "📯 Суд",
    "world": "🌍 Мир",
}
ARCANA_NAME_SET = set(TAROT_ARCANA.values())
ARCANA_BY_NAME = {v: k for k, v in TAROT_ARCANA.items()}

# Статусы
STATUSES: Dict[str, str] = {
    "new": "🆕 Новая",
    "accepted": "✅ Принятая",
    "completed": "🔒 Завершенная",
}

# Прайс одной услуги
PRICE_SERVICE = {
    "name": "🎴 Активация по аркану Таро",
    "price": "1000 руб.",
    "description": "Подробный анализ выбранного аркана, его значения в вашей жизни и практические рекомендации",
}


# =========================
# Вспомогательные структуры
# =========================

@dataclass
class AdminState:
    action: str
    app_id: Optional[int] = None
    user_id: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)


# =========================
# Утилиты
# =========================

def escape(s: Any) -> str:
    return html.escape(str(s), quote=True)

def dt_short(ts: str) -> str:
    try:
        return ts[:16]
    except Exception:
        return ts

def chunked(seq: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]

def reply_keyboard(rows: List[List[str]], resize: bool = True, one_time: bool = False) -> Dict[str, Any]:
    return {
        "keyboard": [[{"text": text} for text in row] for row in rows],
        "resize_keyboard": resize,
        "one_time_keyboard": one_time,
    }


# =========================
# БД слой (SQLite)
# =========================

class Storage:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            # заявки
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    arcana TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    answer TEXT,
                    admin_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_app_user ON applications(user_id)")

            # админы
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP
                )
                """
            )

            # настройки
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # пользователи (все, кто писал боту)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        logger.info("✅ База данных инициализирована")

    # ---------- Settings ----------
    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    # ---------- Users ----------
    def upsert_user(self, user_id: int, username: str, first_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_seen)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_seen=CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name),
            )

    def list_known_users(self) -> List[Tuple[int, Optional[str], Optional[str]]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT user_id, username, first_name FROM users WHERE is_blocked=0")
            return [(int(r["user_id"]), r["username"], r["first_name"]) for r in cur.fetchall()]

    def mark_user_blocked(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))

    # ---------- Applications ----------
    def create_application(self, user_id: int, username: str, first_name: str, arcana: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO applications (user_id, username, first_name, arcana) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, arcana),
            )
            return int(cur.lastrowid)

    def get_application(self, app_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
            return cur.fetchone()

    def update_application(
        self,
        app_id: int,
        *,
        status: Optional[str] = None,
        answer: Optional[str] = None,
        admin_id: Optional[int] = None,
    ) -> None:
        updates: List[str] = []
        params: List[Any] = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if answer is not None:
            updates.append("answer = ?")
            params.append(answer)
        if admin_id is not None:
            updates.append("admin_id = ?")
            params.append(admin_id)

        if not updates:
            return

        params.append(app_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE applications SET {', '.join(updates)} WHERE id = ?", params)

    def list_user_applications(self, user_id: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM applications WHERE user_id = ? ORDER BY id DESC", (user_id,))
            return cur.fetchall()

    def list_all_applications(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM applications
                ORDER BY
                    CASE status
                        WHEN 'new' THEN 1
                        WHEN 'accepted' THEN 2
                        ELSE 3
                    END,
                    id DESC
                """
            )
            return cur.fetchall()

    # ---------- Admins ----------
    def upsert_admin(self, user_id: int, username: str, first_name: str, is_active: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admins (user_id, username, first_name, is_active, last_seen)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    is_active=excluded.is_active,
                    last_seen=CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name, 1 if is_active else 0),
            )

    def list_admin_ids(self, active_only: bool = True) -> List[int]:
        with self._connect() as conn:
            if active_only:
                cur = conn.execute("SELECT user_id FROM admins WHERE is_active=1")
            else:
                cur = conn.execute("SELECT user_id FROM admins")
            return [int(r["user_id"]) for r in cur.fetchall()]

    def set_admin_active(self, user_id: int, active: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE admins SET is_active=?, last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
                (1 if active else 0, user_id),
            )

    def admin_count(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) AS c FROM admins WHERE is_active=1")
            return int(cur.fetchone()["c"])

    def touch_if_admin(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE admins SET last_seen=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))


# =========================
# Бот
# =========================

class SimpleTarotBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.last_update_id = 0
        self.admin_states: Dict[int, AdminState] = {}
        self.storage = Storage(DB_PATH)
        self.session = requests.Session()
        self.stop_event = threading.Event()

        # Постоянный секрет админа
        if ADMIN_SECRET_ENV:
            self.admin_secret = ADMIN_SECRET_ENV
            self.storage.set_setting("admin_secret", self.admin_secret)
        else:
            saved = self.storage.get_setting("admin_secret")
            if saved:
                self.admin_secret = saved
            else:
                self.admin_secret = str(secrets.randbelow(900000) + 100000)
                self.storage.set_setting("admin_secret", self.admin_secret)

        # Инициализация режима перерыва (если не было)
        if self.storage.get_setting("maintenance_enabled") is None:
            self.storage.set_setting("maintenance_enabled", "0")
        if self.storage.get_setting("maintenance_text") is None:
            self.storage.set_setting("maintenance_text",
                "🛠 Временно проводим технические работы. Возможны задержки или недоступность. Благодарим за понимание!"
            )
        if self.storage.get_setting("maintenance_since") is None:
            self.storage.set_setting("maintenance_since", "")

    # -------------- Сигналы (быстрое выключение) --------------

    def _handle_signal(self, signum, frame):
        logger.info(f"Получен сигнал {signum}, останавливаемся...")
        self.stop_event.set()
        try:
            self.session.close()
        except Exception:
            pass

    # -------------- Telegram API helpers --------------

    def delete_webhook(self) -> None:
        url = f"{self.base_url}deleteWebhook"
        try:
            resp = self.session.get(url, timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT))
            resp.raise_for_status()
            j = resp.json()
            if not j.get("ok", False):
                logger.warning(f"deleteWebhook ok=false: {resp.text}")
            else:
                logger.info("🔧 deleteWebhook: ok")
        except Exception as e:
            logger.warning(f"deleteWebhook error (можно игнорировать): {e}")

    def get_updates(self) -> Dict[str, Any]:
        if self.stop_event.is_set():
            return {"ok": True, "result": []}

        url = f"{self.base_url}getUpdates"
        server_timeout = max(1, min(POLL_TIMEOUT_SEC, max(1, API_READ_TIMEOUT - 1)))
        params = {
            "offset": self.last_update_id + 1,
            "timeout": server_timeout,
            "allowed_updates": json.dumps(["message", "callback_query"])
        }
        try:
            resp = self.session.get(
                url, params=params, timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT)
            )
            try:
                data = resp.json()
            except Exception:
                data = {"ok": False, "result": []}
            resp.raise_for_status()
            return data
        except requests.exceptions.ReadTimeout:
            return {"ok": True, "result": []}
        except requests.HTTPError as e:
            body = ""
            try:
                body = resp.text
            except Exception:
                pass
            logger.error(f"Ошибка getUpdates: {e} | details={body}")
            return {"ok": False, "result": []}
        except Exception as e:
            if not self.stop_event.is_set():
                logger.error(f"Ошибка getUpdates: {e}")
            return {"ok": False, "result": []}

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
    ) -> bool:
        if self.stop_event.is_set():
            return False

        url = f"{self.base_url}sendMessage"
        data: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        try:
            resp = self.session.post(url, data=data, timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT))
            resp_json = None
            try:
                resp_json = resp.json()
            except Exception:
                pass

            resp.raise_for_status()
            ok = bool((resp_json or {}).get("ok", False))
            if not ok:
                logger.error(f"sendMessage ok=false: {resp.text}")
            return ok
        except requests.HTTPError as e:
            body = ""
            try:
                body = resp.text
            except Exception:
                pass
            # 403 — пользователь заблокировал бота
            if "403" in str(e):
                try:
                    self.storage.mark_user_blocked(int(chat_id))
                except Exception:
                    pass
            safe = {k: v for k, v in data.items() if k not in ("text", "reply_markup")}
            logger.error(f"Ошибка sendMessage: {e} | details={body} | params={safe}")
            return False
        except Exception as e:
            if not self.stop_event.is_set():
                logger.error(f"Ошибка sendMessage: {e}")
            return False

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        url = f"{self.base_url}answerCallbackQuery"
        try:
            self.session.post(
                url,
                data={"callback_query_id": callback_query_id, "text": text, "show_alert": False},
                timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
            )
        except Exception as e:
            if not self.stop_event.is_set():
                logger.error(f"Ошибка answerCallbackQuery: {e}")

    # -------------- Admin identities --------------

    def combined_admin_ids(self) -> List[int]:
        ids = set(ADMIN_IDS_ENV or [])
        ids.update(self.storage.list_admin_ids(active_only=True))
        return list(sorted(ids))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.combined_admin_ids()

    # -------------- UI helpers --------------

    def admin_controls_keyboard(self, app_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Принять", "callback_data": f"accept:{app_id}"},
                {"text": "💬 Ответ", "callback_data": f"answer:{app_id}"},
            ]]
        }

    def maintenance_status_keyboard(self, enabled: bool) -> dict:
        if enabled:
            return {"inline_keyboard": [[{"text": "❌ Выключить технический режим", "callback_data": "maint_off"}]]}
        else:
            return {"inline_keyboard": [[{"text": "🟢 Включить (используйте /maintenance_on)", "callback_data": "noop"}]]}

    def text_welcome(self) -> str:
        return (
            "🎴 <b>Добро пожаловать в Таро-бот!</b>\n\n"
            "Я предлагаю активации по арканам Таро.\n\n"
            "<b>Команды:</b>\n"
            "/apply — Выбрать аркан для активации\n"
            "/my_applications — Мои заявки\n"
            "/price — Узнать стоимость"
        )

    def text_price(self) -> str:
        s = PRICE_SERVICE
        return (
            "💰 <b>СТОИМОСТЬ УСЛУГИ</б>\n\n"
            f"<b>{escape(s['name'])}</b>\n"
            f"💵 Стоимость: {escape(s['price'])}\n"
            f"📝 {escape(s['description'])}\n\n"
            "💎 <b>Как заказать активацию:</b>\n"
            "1) Используйте команду /apply\n"
            "2) Выберите аркан, по которому хотите получить активацию\n"
            "3) Мы свяжемся с вами для уточнения деталей и оплаты\n\n"
            "🃏 <i>Активация включает подробный анализ аркана и практические рекомендации</i>"
        )

    def text_arcana_prompt(self) -> str:
        return (
            "🎴 <b>Выберите аркан Таро для активации:</b>\n\n"
            "После выбора аркана создастся заявка на активацию, "
            "и мы свяжемся с вами для уточнения деталей."
        )

    # -------------- Maintenance helpers --------------

    def maintenance_enabled(self) -> bool:
        return self.storage.get_setting("maintenance_enabled") == "1"

    def maintenance_text(self) -> str:
        return self.storage.get_setting("maintenance_text") or ""

    def maintenance_since(self) -> str:
        return self.storage.get_setting("maintenance_since") or ""

    def maintenance_enable(self, text: str, admin_id: int) -> None:
        self.storage.set_setting("maintenance_enabled", "1")
        self.storage.set_setting("maintenance_text", text)
        self.storage.set_setting("maintenance_since", datetime.now().strftime("%Y-%m-%d %H:%M"))

        # Сразу рассылаем всем известным пользователям
        users = self.storage.list_known_users()
        total, ok, fail = len(users), 0, 0
        msg = f"⚙️ <b>Технический перерыв</b>\n\n{escape(text)}"
        for uid, _u, _f in users:
            if self.stop_event.is_set():
                break
            try:
                if self.send_message(uid, msg):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                logger.error(f"Ошибка рассылки пользователю {uid}: {e}")
                fail += 1
            self.stop_event.wait(SLEEP_BETWEEN_BROADCAST)

        info = (
            "🛠 Технический режим включён.\n"
            f"Рассылка отправлена: ok={ok}, fail={fail}, всего пользователей={total}.\n"
            "Новые /start тоже получат это сообщение, пока режим включён."
        )
        for aid in self.combined_admin_ids():
            self.send_message(aid, info)

    def maintenance_disable(self, admin_id: int) -> None:
        self.storage.set_setting("maintenance_enabled", "0")
        for aid in self.combined_admin_ids():
            self.send_message(aid, "🟢 Технический режим выключен.")

    # -------------- Флоу пользователя --------------

    def handle_start(self, chat_id: int) -> None:
        self.send_message(chat_id, self.text_welcome())
        if self.maintenance_enabled():
            self.send_message(chat_id, f"⚙️ <b>Технический перерыв</b>\n\n{escape(self.maintenance_text())}")

    def show_price_list(self, chat_id: int) -> None:
        self.send_message(chat_id, self.text_price())

    def show_arcana_selection(self, chat_id: int) -> None:
        arcana_names = list(TAROT_ARCANA.values())
        rows = [row for row in chunked(arcana_names, 3)]
        rows.append(["💰 Стоимость"])
        kb = reply_keyboard(rows, resize=True, one_time=False)
        self.send_message(chat_id, self.text_arcana_prompt(), reply_markup=kb)

    def handle_arcana_selection(self, chat_id: int, user: Dict[str, Any], arcana_text: str) -> None:
        arcana_key = ARCANA_BY_NAME.get(arcana_text, "fool")

        app_id = self.storage.create_application(
            user_id=int(user["id"]),
            username=user.get("username", "") or "",
            first_name=user.get("first_name", "") or "",
            arcana=arcana_key,
        )

        self.send_message(
            chat_id,
            (
                f"✅ <b>Заявка #{app_id} на активацию создана!</b>\n\n"
                f"🎴 Аркан: {escape(arcana_text)}\n"
                f"💰 Стоимость: {escape(PRICE_SERVICE['price'])}\n"
                f"📊 Статус: {STATUSES['new']}\n\n"
                "Мы свяжемся с вами для уточнения деталей активация.\n\n"
                "💎 <i>Для оплаты и уточнений используйте команду /price</i>"
            ),
            reply_markup={"remove_keyboard": True},
        )

        self.notify_admins(app_id, user, arcana_text)

    def show_user_applications(self, chat_id: int, user: Dict[str, Any]) -> None:
        apps = self.storage.list_user_applications(int(user["id"]))
        if not apps:
            self.send_message(
                chat_id,
                "📭 У вас пока нет заявок на активацию.\n\nИспользуйте /apply чтобы создать заявку.",
            )
            return

        self.send_message(chat_id, "📋 <b>Ваши заявки на активацию:</b>")
        for r in apps:
            arcana_name = TAROT_ARCANA.get(r["arcana"], "🃏 Шут")
            status_info = STATUSES.get(r["status"], r["status"])
            msg = (
                f"📋 <b>Заявка #{r['id']}</b>\n"
                f"🎴 {escape(arcana_name)}\n"
                f"📅 {escape(dt_short(r['created_at']))}\n"
                f"📊 {escape(status_info)}\n"
                f"💰 {escape(PRICE_SERVICE['price'])}"
            )
            if r["answer"]:
                msg += f"\n💌 <b>Ответ:</b> {escape(r['answer'])}"

            self.send_message(chat_id, msg)

    # -------------- Админ-функции (заявки) --------------

    def show_all_applications(self, chat_id: int) -> None:
        apps = self.storage.list_all_applications()
        if not apps:
            self.send_message(chat_id, "📭 Заявок на активацию нет.")
            return

        new_count = sum(1 for r in apps if r["status"] == "new")
        total = len(apps)
        self.send_message(chat_id, f"📋 <b>Все заявки на активацию:</b> {total} всего, {new_count} новых")

        for r in apps[:10]:
            arcana_name = TAROT_ARCANA.get(r["arcana"], "🃏 Шут")
            status_info = STATUSES.get(r["status"], r["status"])
            username_display = f"@{r['username']}" if r["username"] else "без username"
            msg = (
                f"📋 <b>#{r['id']}</b>\n"
                f"👤 {escape(r['first_name'] or '')} ({escape(username_display)})\n"
                f"🎴 {escape(arcana_name)}\n"
                f"📊 {escape(status_info)}\n"
                f"💰 {escape(PRICE_SERVICE['price'])}\n"
                f"📅 {escape(dt_short(r['created_at']))}"
            )
            kb = self.admin_controls_keyboard(int(r["id"]))
            self.send_message(chat_id, msg, reply_markup=kb)

    def start_answer_process(self, chat_id: int, user: Dict[str, Any], command_text: str) -> None:
        try:
            app_id = int(command_text.split("_")[1])
        except Exception:
            self.send_message(chat_id, "❌ Неверный формат команды.")
            return

        r = self.storage.get_application(app_id)
        if not r:
            self.send_message(chat_id, "❌ Заявка не найдена!")
            return

        arcana_name = TAROT_ARCANA.get(r["arcana"], "🃏 Шут")
        self.send_message(
            chat_id,
            (
                f"📝 <b>Ответ на заявку #{r['id']}</b>\n"
                f"🎴 Аркан: {escape(arcana_name)}\n"
                f"👤 От: {escape(r['first_name'] or '')} (@{escape(r['username'] or 'нет')})\n"
                f"💰 Стоимость: {escape(PRICE_SERVICE['price'])}\n\n"
                "💬 Введите ваш ответ:"
            ),
        )
        self.admin_states[int(user["id"])] = AdminState(action="answer", app_id=int(r["id"]), user_id=int(r["user_id"]))

    def accept_application(self, chat_id: int, user: Dict[str, Any], command_text: str) -> None:
        try:
            app_id = int(command_text.split("_")[1])
        except Exception:
            self.send_message(chat_id, "❌ Неверный формат команды.")
            return

        r = self.storage.get_application(app_id)
        if not r:
            self.send_message(chat_id, "❌ Заявка не найдена!")
            return

        self.storage.update_application(app_id, status="accepted", admin_id=int(user["id"]))
        self.send_message(chat_id, f"✅ Заявка #{app_id} принята!")

        arcana_name = TAROT_ARCANA.get(r["arcana"], "🃏 Шут")
        self.send_message(
            int(r["user_id"]),
            (
                f"📋 <b>Ваша заявка #{app_id} принята</b>\n\n"
                f"🎴 Аркан: {escape(arcana_name)}\n"
                f"💰 Стоимость: {escape(PRICE_SERVICE['price'])}\n"
                f"📊 {STATUSES['accepted']}\n\n"
                "Скоро вы получите активацию!"
            ),
        )

    def handle_admin_answer(self, chat_id: int, admin_id: int, answer_text: str) -> None:
        state = self.admin_states.get(admin_id)
        if not state or state.action != "answer" or not state.app_id or not state.user_id:
            return

        del self.admin_states[admin_id]
        self.storage.update_application(state.app_id, status="completed", answer=answer_text, admin_id=admin_id)

        r = self.storage.get_application(state.app_id)
        arcana_name = TAROT_ARCANA.get(r["arcana"], "🃏 Шут") if r else "🃏 Шут"

        self.send_message(
            state.user_id,
            (
                f"💌 <b>Активация по аркану #{state.app_id}</b>\n\n"
                f"🎴 Аркан: {escape(arcana_name)}\n"
                f"💰 Стоимость: {escape(PRICE_SERVICE['price'])}\n\n"
                f"{escape(answer_text)}\n\n"
                "🙏 <i>Благодарим за обращение!</i>"
            ),
        )
        self.send_message(chat_id, "✅ Активация отправлена пользователю!")

    # -------------- Админ: технический режим (без планирования) --------------

    def maintenance_on_start(self, chat_id: int, admin_id: int) -> None:
        self.admin_states[admin_id] = AdminState(action="maint_text")
        self.send_message(
            chat_id,
            "🛠 <b>Включить технический режим</b>\n\n"
            "Отправьте <b>текст объявления</b> для пользователей.\n"
            "Или отправьте <code>-</code>, чтобы использовать стандартный текст.",
        )

    def maintenance_on_receive_text(self, chat_id: int, admin_id: int, text: str) -> None:
        if text.strip() == "-":
            text = self.storage.get_setting("maintenance_text") or \
                   "🛠 Временно проводим технические работы. Возможны задержки или недоступность. Благодарим за понимание!"
        self.maintenance_enable(text.strip(), admin_id)
        self.send_message(chat_id, "✅ Технический режим включён и сообщение разослано всем известным пользователям.")

    def maintenance_off(self, chat_id: int, admin_id: int) -> None:
        if not self.maintenance_enabled():
            self.send_message(chat_id, "ℹ️ Технический режим уже выключен.")
            return
        self.maintenance_disable(admin_id)
        self.send_message(chat_id, "🟢 Технический режим выключен.")

    def maintenance_status(self, chat_id: int) -> None:
        enabled = self.maintenance_enabled()
        since = self.maintenance_since()
        text = self.maintenance_text()
        msg = (
            f"⚙️ <b>Статус технического режима:</b> {'<b>ВКЛЮЧЕН</b>' if enabled else 'выключен'}\n"
            f"{'⏱ С включения: ' + escape(since) if enabled and since else ''}\n\n"
            f"📝 Текст объявления:\n{escape(text)}"
        )
        self.send_message(chat_id, msg, reply_markup=self.maintenance_status_keyboard(enabled))

    # -------------- Callback Query --------------

    def handle_callback_query(self, cq: dict) -> None:
        data = (cq.get("data") or "").strip()
        from_user = cq.get("from", {})
        admin_id = int(from_user.get("id", 0))
        message = cq.get("message") or {}
        chat_id = int(message.get("chat", {}).get("id", 0))
        cq_id = cq.get("id")

        if not self.is_admin(admin_id):
            self.answer_callback(cq_id, "Недостаточно прав")
            return

        if data.startswith("accept:"):
            app_id = int(data.split(":")[1])
            self.accept_application(chat_id, from_user, f"/accept_{app_id}")
            self.answer_callback(cq_id, "Принято")
            return

        if data.startswith("answer:"):
            app_id = int(data.split(":")[1])
            self.start_answer_process(chat_id, from_user, f"/answer_{app_id}")
            self.answer_callback(cq_id, "Введите ответ сообщением")
            return

        if data == "maint_off":
            self.maintenance_off(chat_id, admin_id)
            self.answer_callback(cq_id, "Выключено")
            return

        if data == "noop":
            self.answer_callback(cq_id, "Используйте /maintenance_on")
            return

    # -------------- Служебные и команды сервиса --------------

    def notify_admins(self, app_id: int, user: Dict[str, Any], arcana_name: str) -> None:
        admins = self.combined_admin_ids()
        if not admins:
            logger.warning("Нет активных админов: заявка создана, но уведомить некого.")
            return

        text = (
            f"🎴 <b>НОВАЯ ЗАЯВКА</b> #{app_id}\n\n"
            f"🃏 Аркан: {escape(arcana_name)}\n"
            f"👤 От: {escape(user.get('first_name', 'Unknown'))}\n"
            f"📛 Username: @{escape(user.get('username', 'нет'))}\n"
            f"🆔 ID: {escape(user['id'])}"
        )
        kb = self.admin_controls_keyboard(app_id)
        for admin_id in admins:
            self.send_message(admin_id, text, reply_markup=kb)

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        user = message["from"]
        user_id = int(user["id"])
        text = (message.get("text") or "").strip()
        if not text:
            return

        # Учет пользователя
        try:
            self.storage.upsert_user(
                user_id,
                user.get("username", "") or "",
                user.get("first_name", "") or "",
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить users: {e}")

        # Лог (не палим секрет)
        log_text = text if not text.startswith("/iamadmin_") else "/iamadmin_******"
        logger.info(f"Сообщение от {user.get('first_name', 'Unknown')} [{user_id}]: {log_text}")

        if self.is_admin(user_id):
            self.storage.touch_if_admin(user_id)

        # Скрытая активация админа
        if text.startswith("/iamadmin_"):
            provided = text.split("_", 1)[1].strip()
            if provided == self.admin_secret:
                self.storage.upsert_admin(user_id, user.get("username", "") or "", user.get("first_name", "") or "", True)
                self.send_message(chat_id, "✅ Вы назначены администратором этого бота.")
                logger.info(f"Добавлен админ user_id={user_id}")
            else:
                self.send_message(chat_id, "🤖 Команда не распознана.")
            return

        # Пользовательские команды
        if text == "/start":
            self.handle_start(chat_id)
            return

        if text == "/apply":
            self.show_arcana_selection(chat_id)
            return

        if text == "💰 Стоимость" or text == "/price":
            self.show_price_list(chat_id)
            return

        if text == "/my_applications":
            self.show_user_applications(chat_id, user)
            return

        # Админ-команды
        if text == "/list" and self.is_admin(user_id):
            self.show_all_applications(chat_id)
            return

        if text.startswith("/answer_") and self.is_admin(user_id):
            self.start_answer_process(chat_id, user, text)
            return

        if text.startswith("/accept_") and self.is_admin(user_id):
            self.accept_application(chat_id, user, text)
            return

        if text == "/admin_list" and self.is_admin(user_id):
            ids = self.combined_admin_ids()
            self.send_message(chat_id, "👮 Активные админы: " + ", ".join(map(str, ids)) if ids else "👮 Нет активных админов.")
            return

        # Технический режим
        if text == "/maintenance_on" and self.is_admin(user_id):
            self.maintenance_on_start(chat_id, user_id)
            return

        if text == "/maintenance_off" and self.is_admin(user_id):
            self.maintenance_off(chat_id, user_id)
            return

        if text == "/maintenance_status" and self.is_admin(user_id):
            self.maintenance_status(chat_id)
            return

        # Состояния админа
        state = self.admin_states.get(user_id)
        if state:
            if state.action == "answer":
                self.handle_admin_answer(chat_id, user_id, text)
                return
            if state.action == "maint_text":
                del self.admin_states[user_id]
                self.maintenance_on_receive_text(chat_id, user_id, text)
                return

        # Выбор аркана
        if text in ARCANA_NAME_SET:
            self.handle_arcana_selection(chat_id, user, text)
            return

        # Нераспознанное
        if text.startswith("/"):
            self.send_message(
                chat_id,
                "🤖 Команда не распознана.\nДоступно: /apply, /my_applications, /price",
            )

    # -------------- Запуск --------------

    def run(self) -> None:
        print("=" * 60)
        print("🎴  Т А Р О - Б О Т   —   К О Н С У Л Ь Т А Ц И И  🎴")
        print("=" * 60)
        if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            print("❌ ЗАМЕНИТЕ TELEGRAM_BOT_TOKEN НА ВАШ РЕАЛЬНЫЙ ТОКЕН!")
            return

        # Сигналы для быстрого выхода
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
        except Exception:
            pass

        self.delete_webhook()

        print("✅ Бот запущен...")
        print("⏹️  Нажмите Ctrl+C для остановки")
        print(f"\n💰 Услуга: {PRICE_SERVICE['name']} — {PRICE_SERVICE['price']}")
        print(f"🤫 Секрет активации (постоянный): /iamadmin_{self.admin_secret}")

        try:
            while not self.stop_event.is_set():
                updates = self.get_updates()
                if updates.get("ok"):
                    for upd in updates.get("result", []):
                        if self.stop_event.is_set():
                            break
                        self.last_update_id = int(upd["update_id"])

                        if "callback_query" in upd:
                            self.handle_callback_query(upd["callback_query"])
                            continue

                        msg = upd.get("message")
                        if msg:
                            self.handle_message(msg)

                self.stop_event.wait(SLEEP_BETWEEN_POLLS)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — выходим...")
        except Exception as e:
            logger.exception(f"Критическая ошибка: {e}")
        finally:
            try:
                self.session.close()
            except Exception:
                pass
            print("\n🛑 Бот остановлен быстро и корректно")


# =========================
# Точка входа
# =========================

if __name__ == "__main__":
    bot = SimpleTarotBot(BOT_TOKEN)
    bot.run()
