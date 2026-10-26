"""Gom lỗi theo chữ ký, báo chủ bot khi có lỗi MỚI và gửi bản tổng hợp hằng ngày.

Trước đây exception không bắt được chỉ nằm trong log stdout của Render: người
dùng thấy câu báo lỗi chung chung, chủ bot không hay biết gì trừ khi sự kiện
webhook lỗi đủ 8 lần thành DEAD.

- ``ErrorDigestHandler`` (logging.Handler, logging_setup gắn vào root logger)
  nhận record ERROR trở lên và WARNING có exc_info. Chữ ký = (logger, tên loại
  exception, frame cuối cùng nằm trong repo ``đường/dẫn.py:hàm``). CHỈ lưu tên
  loại và vị trí code, KHÔNG BAO GIỜ lưu message hay str(exception) - chúng có
  thể chứa tin nhắn người dùng hoặc secret.
- ERROR có chữ ký mới trong ngày -> báo ngay (tối đa ERROR_ALERT_MAX_PER_HOUR
  tin/giờ). WARNING có exc_info (job nền lỗi rồi tự thử lại) chỉ vào bản tổng
  hợp. Record gắn ``extra={"alerted": True}`` (đã có cảnh báo riêng: toàn bộ
  provider chết, webhook DEAD, lượt đăng Page chưa rõ...) không báo lần 2.
- ``emit()`` có thể chạy trên thread của asyncio.to_thread: đếm dưới
  threading.Lock, việc gửi cảnh báo chuyển về event loop bằng
  ``loop.call_soon_threadsafe``.
- Bỏ qua record từ chính đường gửi cảnh báo (core.alerts, services.owner_alerts,
  httpx/httpcore/telegram): Telegram sập không được biến thành vòng lặp cảnh báo.
- Bảng chữ ký có trần ``MAX_SIGNATURES`` mỗi ngày (đầy thì bỏ chữ ký lâu không
  gặp nhất), chỉ giữ hôm nay + hôm qua; lưu vào settings mỗi chu kỳ monitor khi
  có thay đổi nên sống qua restart. Từ ERROR_DIGEST_HOUR_VN (7h) gửi bản tổng
  hợp của hôm qua nếu có lỗi. /loi (Telegram) và /admin đọc từ đây.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core import alerts
from core import database as db
from core.envutil import env_int, env_on

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_SIGNATURES = 200
_DAYS_KEPT = 2
SETTINGS_KEY = "errors:digest"
_SENT_KEY = "errors:digest:last_sent"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKIP_LOGGERS = (
    "core.alerts",
    "services.owner_alerts",
    "services.error_digest",
    "httpx",
    "httpcore",
    "telegram",
)
_ALERT_COOLDOWN_SEC = 6 * 3600
_REPORT_LIMIT = 15


def immediate_enabled() -> bool:
    return env_on("ERROR_ALERT_IMMEDIATE")


def max_alerts_per_hour() -> int:
    return env_int("ERROR_ALERT_MAX_PER_HOUR", 4, 0, 60)


@dataclass
class Entry:
    logger: str
    exc_type: str
    where: str
    line: int
    level: str  # "ERROR" | "WARNING"
    count: int
    first: float
    last: float

    def merge(self, other: Entry) -> None:
        self.count += other.count
        self.first = min(self.first, other.first)
        if other.last >= self.last:
            self.last, self.line = other.last, other.line
        if other.level == "ERROR":
            self.level = "ERROR"


def signature(entry: Entry) -> str:
    return f"{entry.logger}|{entry.exc_type}|{entry.where}"


class DigestStore:
    """Số lần gặp từng chữ ký lỗi theo ngày (giờ VN); an toàn đa luồng."""

    def __init__(self, max_signatures: int = MAX_SIGNATURES) -> None:
        self.max_signatures = max_signatures
        self.dirty = False
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, Entry]] = {}

    def _bucket(self, day: str) -> dict[str, Entry]:
        entries = self._days.setdefault(day, {})
        for old in sorted(self._days)[:-_DAYS_KEPT]:
            del self._days[old]
        return entries

    def _insert(self, entries: dict[str, Entry], entry: Entry) -> bool:
        key = signature(entry)
        current = entries.get(key)
        if current is not None:
            current.merge(entry)
            return False
        if len(entries) >= self.max_signatures:
            del entries[min(entries, key=lambda k: entries[k].last)]
        entries[key] = entry
        return True

    def add(self, day: str, entry: Entry) -> bool:
        """Cộng 1 lần gặp; True nếu là chữ ký mới trong ngày."""
        with self._lock:
            self.dirty = True
            return self._insert(self._bucket(day), entry)

    def entries(self, day: str) -> list[Entry]:
        with self._lock:
            items = [Entry(**asdict(e)) for e in self._days.get(day, {}).values()]
        return sorted(items, key=lambda e: (-e.count, -e.last))

    def to_json(self) -> str:
        with self._lock:
            return json.dumps(
                {
                    "v": 1,
                    "days": {
                        day: [asdict(e) for e in entries.values()]
                        for day, entries in self._days.items()
                    },
                },
                ensure_ascii=False,
            )

    def merge_json(self, raw: str | None, *, today: str) -> None:
        """Gộp số liệu đã lưu (trước restart) vào bộ đếm trong RAM."""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return
        days = data.get("days") if isinstance(data, dict) else None
        if not isinstance(days, dict):
            return
        keep = {today, (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()}
        with self._lock:
            for day, items in days.items():
                if day not in keep or not isinstance(items, list):
                    continue
                entries = self._bucket(day)
                for item in items[: self.max_signatures]:
                    try:
                        self._insert(entries, Entry(**item))
                    except TypeError:
                        continue


store = DigestStore()
_guard = threading.local()
_loop: asyncio.AbstractEventLoop | None = None
_rate_lock = threading.Lock()
_alert_times: deque[float] = deque(maxlen=64)
_loaded = False


@functools.lru_cache(maxsize=256)
def _repo_relative(path: str) -> str | None:
    """Đường dẫn tương đối trong repo của 1 file mã nguồn; None nếu là thư viện ngoài."""
    try:
        rel = Path(path).resolve().relative_to(_REPO_ROOT)
    except (ValueError, OSError):
        return None
    if rel.parts and (rel.parts[0] in (".venv", "venv") or "site-packages" in rel.parts):
        return None
    return rel.as_posix()


def _location(record: logging.LogRecord) -> tuple[str, int]:
    """Frame cuối cùng nằm trong repo (theo traceback nếu có), dạng ``file.py:hàm``."""
    if record.exc_info and record.exc_info[2] is not None:
        found = None
        tb = record.exc_info[2]
        while tb is not None:
            code = tb.tb_frame.f_code
            rel = _repo_relative(code.co_filename)
            if rel:
                found = (f"{rel}:{code.co_name}", tb.tb_lineno)
            tb = tb.tb_next
        if found:
            return found
    rel = _repo_relative(record.pathname)
    if rel:
        return f"{rel}:{record.funcName}", record.lineno
    return record.name, record.lineno


def _skipped(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _SKIP_LOGGERS)


class ErrorDigestHandler(logging.Handler):
    def __init__(self, digest_store: DigestStore | None = None) -> None:
        super().__init__(level=logging.WARNING)
        self.store = digest_store or store

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "active", False):
            return
        try:
            if record.levelno < logging.ERROR and not record.exc_info:
                return
            if _skipped(record.name):
                return
            exc = record.exc_info[0] if record.exc_info else None
            where, line = _location(record)
            level = "ERROR" if record.levelno >= logging.ERROR else "WARNING"
            entry = Entry(
                record.name,
                exc.__name__ if exc else "-",
                where,
                line,
                level,
                1,
                record.created,
                record.created,
            )
            day = datetime.fromtimestamp(record.created, VN_TZ).date().isoformat()
            new = self.store.add(day, entry)
            if new and level == "ERROR" and not getattr(record, "alerted", False):
                _schedule_alert(entry)
        except Exception:
            # Không bao giờ được làm hỏng logging (handleError in traceback ra stderr).
            pass


_handler: ErrorDigestHandler | None = None


def handler() -> ErrorDigestHandler:
    """Handler dùng chung (gắn 1 lần dù configure_logging bị gọi lại)."""
    global _handler
    if _handler is None:
        _handler = ErrorDigestHandler(store)
    return _handler


def bind_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Đăng ký event loop nhận cảnh báo lỗi mới (gọi từ monitor_service.start)."""
    global _loop
    _loop = loop or asyncio.get_running_loop()


def new_error_text(entry: Entry) -> str:
    return (
        f"🐞 Lỗi mới: {entry.exc_type} ở {entry.where}:{entry.line} (logger {entry.logger}). "
        "Nội dung lỗi chỉ có trong log Render; gõ /loi để xem tổng hợp hôm nay."
    )


def _schedule_alert(entry: Entry) -> None:
    loop = _loop
    if loop is None or loop.is_closed() or not immediate_enabled():
        return
    now = time.monotonic()
    with _rate_lock:
        recent = sum(1 for t in _alert_times if now - t < 3600)
        if recent >= max_alerts_per_hour():
            return
        _alert_times.append(now)
    try:
        loop.call_soon_threadsafe(_send_alert, new_error_text(entry), f"err:{signature(entry)}")
    except RuntimeError:
        pass  # loop đã đóng (đang tắt)


def _send_alert(text: str, key: str) -> None:
    _guard.active = True
    try:
        alerts.send(text, key=key, cooldown=_ALERT_COOLDOWN_SEC)
    finally:
        _guard.active = False


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(VN_TZ)).date().isoformat()


async def load() -> None:
    """Gộp số liệu đã lưu trước restart (gọi 1 lần lúc khởi động)."""
    global _loaded
    store.merge_json(await db.get_setting(SETTINGS_KEY), today=_today())
    _loaded = True


async def persist() -> None:
    """Lưu bộ đếm vào settings nếu có thay đổi (mỗi chu kỳ monitor + lúc tắt)."""
    if not _loaded:
        await load()  # chưa gộp số liệu cũ thì không được ghi đè lên nó
    if not store.dirty:
        return
    store.dirty = False
    try:
        await db.set_setting(SETTINGS_KEY, store.to_json())
    except BaseException:
        store.dirty = True
        raise


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, VN_TZ).strftime("%H:%M")


def format_entries(entries: list[Entry], limit: int = _REPORT_LIMIT) -> list[str]:
    lines = [
        f"• {e.count}× {e.exc_type} ở {e.where}:{e.line} - {e.logger}"
        f"{' (cảnh báo, tự thử lại)' if e.level == 'WARNING' else ''}, lần cuối {_fmt_time(e.last)}"
        for e in entries[:limit]
    ]
    if len(entries) > limit:
        lines.append(f"… và {len(entries) - limit} loại lỗi khác.")
    return lines


def digest_text(day: str, entries: list[Entry]) -> str:
    total = sum(e.count for e in entries)
    errors = sum(e.count for e in entries if e.level == "ERROR")
    label = datetime.fromisoformat(day).strftime("%d/%m")
    return "\n".join(
        [
            f"🧾 Tổng hợp lỗi ngày {label}: {total} lần ({errors} lỗi, "
            f"{total - errors} cảnh báo), {len(entries)} loại.",
            *format_entries(entries, 10),
            "Chi tiết từng lần xem log Render; /loi để xem hôm nay.",
        ]
    )


async def send_daily_digest_if_due(now: datetime | None = None) -> str | None:
    """Từ ERROR_DIGEST_HOUR_VN, 1 lần/ngày gửi tổng hợp lỗi của hôm qua (nếu có)."""
    now = now or datetime.now(VN_TZ)
    hour = env_int("ERROR_DIGEST_HOUR_VN", 7, -1, 23)
    if hour < 0 or now.hour < hour:
        return None
    today = _today(now)
    if await db.get_setting(_SENT_KEY) == today:
        return None
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    entries = store.entries(yesterday)
    await db.set_setting(_SENT_KEY, today)
    if not entries:
        return None
    text = digest_text(yesterday, entries)
    alerts.send(text)
    return text


def summary(now: datetime | None = None) -> dict:
    """Số liệu cho /admin/api/watchdog."""
    now = now or datetime.now(VN_TZ)
    today = store.entries(_today(now))
    yesterday = store.entries((now.date() - timedelta(days=1)).isoformat())
    return {
        "today_total": sum(e.count for e in today),
        "yesterday_total": sum(e.count for e in yesterday),
        "today": [
            {**asdict(e), "last_at": _fmt_time(e.last), "first_at": _fmt_time(e.first)}
            for e in today[:_REPORT_LIMIT]
        ],
        "signatures_today": len(today),
    }


async def build_report(now: datetime | None = None) -> str:
    """Text cho lệnh /loi: lỗi hôm nay, tổng hôm qua, số lần khởi động lại 7 ngày."""
    from services import watchdog

    now = now or datetime.now(VN_TZ)
    today = store.entries(_today(now))
    yesterday = store.entries((now.date() - timedelta(days=1)).isoformat())
    lines = ["🐞 Lỗi hôm nay"]
    if today:
        lines.append(f"{sum(e.count for e in today)} lần, {len(today)} loại:")
        lines += format_entries(today)
    else:
        lines.append("Chưa ghi nhận lỗi nào, anh yên tâm nha.")
    lines.append(
        f"Hôm qua: {sum(e.count for e in yesterday)} lần, {len(yesterday)} loại."
        if yesterday
        else "Hôm qua: không có lỗi."
    )
    try:
        restarts = await watchdog.restart_summary()
    except Exception:
        logger.warning("Không đọc được thống kê khởi động lại.", exc_info=True)
    else:
        counts = restarts["counts"]
        if counts:
            parts = ", ".join(
                f"{watchdog.KIND_LABELS.get(kind, kind)} {n}" for kind, n in sorted(counts.items())
            )
            lines.append(f"Khởi động lại 7 ngày qua: {parts}.")
    lines.append("Chỉ lưu loại lỗi + vị trí code; nội dung chi tiết xem log Render.")
    return "\n".join(lines)
