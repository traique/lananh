"""Watchdog theo luật cố định (KHÔNG nhờ AI) cho các sự cố âm thầm - phần mở
rộng của services/monitor_service.py, chạy trong cùng vòng MONITOR_INTERVAL_SEC.

Các cảnh báo sự kiện đã có sẵn ở nơi khác và KHÔNG lặp lại ở đây: router9/toàn
bộ provider chết, webhook DEAD + reaper, tin Zalo outbox hết lượt thử
(channels/router.py), gateway Zalo mất kết nối, lượt đăng Page chưa rõ kết quả,
migration bị sửa. Module này lo những thứ hỏng từ từ mà không ai thấy:

1. RAM container: working set (memory.current - inactive_file) vượt
   WATCHDOG_MEMORY_PCT (85%) 2 chu kỳ liền; bộ đếm cgroup ``memory.events``
   ``oom_kill`` tăng (kernel kill tiến trình) hoặc ``max``/``high`` tăng khi RAM đã
   cao (chạm trần).
2. Dung lượng DB so với DB_SIZE_LIMIT_MB ở mốc 80% và 90%, 1 lần/mốc/ngày.
3. webhook_inbox: sự kiện PENDING đã tới hạn quá WATCHDOG_INBOX_STALE_MIN phút
   mà worker chưa nhận.
4. Job hằng ngày quá hạn chưa chạy xong: digest danh mục (ngày giao dịch, trừ khi
   DIGEST_ON_NON_TRADING_DAYS), bản tin sáng, cảnh báo danh mục cuối phiên và job
   danh mục buổi sáng (ngày giao dịch), từng slot Page chứng khoán (đã hết khung
   chạy bù). Hạn tính từ cấu hình (giờ chạy + khung bù) và cờ bật; chỉ xét job
   đang chạy trong process này và đã từng chạy ít nhất 1 lần (chưa cấu hình thì
   không báo giả); 1 lần/job/ngày (dedup lưu settings, sống qua restart).
5. Nhịp tim (settings ``runtime:heartbeat``) mỗi chu kỳ + lúc tắt. Khi khởi động
   so với nhịp tim trước để phân loại: deploy (commit đổi) / restart thường /
   OOM hoặc crash trong cùng container (supervisord chạy lại uvicorn - PID 1 khởi
   động trước process này) / ngủ-gián đoạn lâu. Chỉ báo ngay OOM, crash, gián
   đoạn > WATCHDOG_RESTART_GAP_MIN; restart thường chỉ báo khi dồn dập
   (>= WATCHDOG_RESTART_BURST lần/24h - dấu hiệu Render kill instance vì hết RAM).
   Restart mà instance trước tắt êm (sửa env, Manual Deploy, Restart service: Render
   gửi SIGTERM) không tính; vì instance cũ chỉ tắt SAU khi instance mới healthy, phần
   đếm dồn dập chạy ở chu kỳ sau (_BURST_SETTLE_SEC), báo tối đa 1 lần/24 giờ.

Watchdog trong process không báo được khi chính process đã chết: cần thêm 1
monitor UptimeRobot trỏ /healthz có liên hệ cảnh báo và bật email thông báo của
Render (xem README). Mọi trạng thái trong RAM đều có trần.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from core import alerts, config
from core import database as db
from core.envutil import env_int, env_on
from services import memory_stats

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MIB = 1024 * 1024
HEARTBEAT_KEY = "runtime:heartbeat"
RESTARTS_KEY = "runtime:restarts"
# Instance đã tắt êm (SIGTERM -> monitor_service.stop): restart sau nó là có chủ ý.
CLEAN_EXITS_KEY = "runtime:clean_exits"
MAX_CLEAN_EXITS = 20
_BURST_KEY = "watchdog:restart_burst"
# Đếm restart dồn dập sau ngần này giây kể từ khi process khởi động: Render chạy chồng
# instance cũ tới khi instance mới healthy rồi mới SIGTERM (chờ tắt tối đa 300 s).
_BURST_SETTLE_SEC = 600
_MISSED_KEY = "watchdog:missed"
_DB_LEVEL_KEY = "watchdog:db_level"
MAX_RESTART_LOG = 50
_RESTART_LOG_SEC = 30 * 86400
# PID 1 (supervisord) khởi động trước process web quá ngần này => cùng container,
# tức uvicorn vừa chết và được supervisord chạy lại (container mới thì ~0-2s).
_SAME_CONTAINER_SEC = 60
_HOUR = 3600.0
# Phân biệt 2 instance chạy chồng lúc deploy (instance cũ tắt sau instance mới).
_INSTANCE = secrets.token_hex(4)
_PROCESS_STARTED = time.time()
PROC_ROOT = Path("/proc")

KIND_LABELS = {
    "first": "lần đầu ghi nhịp tim",
    "deploy": "deploy",
    "restart": "khởi động lại",
    "sleep": "gián đoạn lâu",
    "crash": "tiến trình chết",
    "oom": "hết RAM (OOM)",
}


def enabled() -> bool:
    return env_on("WATCHDOG_ENABLED")


def _on_render() -> bool:
    """Nhịp tim/phát hiện restart chỉ chạy trên Render: chạy local (main.py) trỏ
    cùng DB production không được ghi đè nhịp tim của bản đang chạy thật."""
    return os.getenv("RENDER", "").strip().lower() == "true"


def _commit() -> str:
    return os.getenv("RENDER_GIT_COMMIT", "").strip()[:12]


def _load_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ─── 1. RAM container ───────────────────────────────────────────────────────

_high_cycles = 0
_last_events: dict[str, int] | None = None


def memory_threshold_pct() -> int:
    return env_int("WATCHDOG_MEMORY_PCT", 85, 50, 99)


async def check_memory() -> None:
    global _high_cycles, _last_events
    working = memory_stats.working_set()
    percent = working["percent"]
    threshold = memory_threshold_pct()
    if percent is not None and percent >= threshold:
        _high_cycles += 1
        if _high_cycles >= 2:
            alerts.send(
                f"⚠️ RAM container đang {working['working_set_mib']:.0f}/"
                f"{working['limit_mib']:.0f} MB ({percent:.0f}%, không tính page cache) "
                f"liên tục {_high_cycles} lần đo - sắp chạm trần 512 MB của Render free "
                "(vượt là bị kill cả instance). Xem /admin → Hệ thống để biết tiến "
                "trình/thư viện nào đang nặng.",
                key="watchdog:memory",
                cooldown=6 * _HOUR,
            )
    else:
        _high_cycles = 0

    events = memory_stats.memory_events()
    if events and _last_events is not None:
        oom = events.get("oom_kill", 0) - _last_events.get("oom_kill", 0)
        hits = (events.get("max", 0) - _last_events.get("max", 0)) + (
            events.get("high", 0) - _last_events.get("high", 0)
        )
        if oom > 0:
            alerts.send(
                f"🚨 Kernel vừa kill {oom} tiến trình trong container vì hết RAM "
                "(cgroup oom_kill). supervisord sẽ tự chạy lại tiến trình đó; nếu là "
                "gateway Zalo thì vài phút đầu có thể lỡ tin.",
                key="watchdog:oom_kill",
                cooldown=0.5 * _HOUR,
            )
        # Page cache cũng làm bộ đếm max tăng khi RAM rảnh: chỉ báo khi RAM thật đã cao.
        elif hits > 0 and percent is not None and percent >= threshold - 10:
            alerts.send(
                f"⚠️ RAM container chạm trần cgroup {hits} lần trong chu kỳ vừa rồi "
                f"(đang {percent:.0f}%) - kernel phải thu hồi gấp, dễ dẫn tới OOM.",
                key="watchdog:memory_max",
                cooldown=6 * _HOUR,
            )
    if events:
        _last_events = events


# ─── 2. Dung lượng DB ───────────────────────────────────────────────────────


async def check_db_size(now: datetime) -> None:
    limit_mb = env_int("DB_SIZE_LIMIT_MB", 500, 50, 100_000)
    size = await db.database_bytes()
    percent = size * 100 / (limit_mb * MIB)
    level = 90 if percent >= 90 else 80 if percent >= 80 else 0
    if not level:
        return
    today = now.date().isoformat()
    day, _, stored = (await db.get_setting(_DB_LEVEL_KEY) or "").partition(":")
    if day == today and stored.isdigit() and int(stored) >= level:
        return
    await db.set_setting(_DB_LEVEL_KEY, f"{today}:{level}")
    alerts.send(
        f"💽 Dung lượng DB đang {size / MIB:.0f}/{limit_mb} MB ({percent:.0f}%). "
        "Quota 500 MB của Supabase free tính theo dung lượng TRUNG BÌNH mỗi ngày trong "
        "chu kỳ thanh toán, cộng chung mọi project của tổ chức; vượt sẽ bị hạn chế "
        "(lỗi 402). Xem /admin → Hệ thống; dọn ảnh Facebook hoặc giảm RETENTION_*_DAYS."
    )


# ─── 3. Webhook inbox ứ đọng ────────────────────────────────────────────────


async def check_inbox() -> None:
    minutes = env_int("WATCHDOG_INBOX_STALE_MIN", 10, 1, 1440)
    stale, oldest_sec = await db.webhook_backlog(timedelta(minutes=minutes))
    if stale <= 0:
        return
    oldest = f", cũ nhất {oldest_sec / 60:.0f} phút" if oldest_sec else ""
    alerts.send(
        f"⚠️ Có {stale} tin Telegram/Zoom đã tới hạn xử lý quá {minutes} phút mà worker "
        f"webhook inbox chưa nhận{oldest}. Worker có thể bị kẹt - xem log Render, "
        "restart service nếu không tự hết.",
        key="watchdog:inbox",
        cooldown=_HOUR,
    )


# ─── 4. Job hằng ngày quá hạn ───────────────────────────────────────────────


@dataclass(frozen=True)
class JobCheck:
    name: str  # id chống trùng trong ngày
    label: str
    deadline: datetime
    setting_key: str  # key job tự đánh dấu khi chạy xong
    # Giá trị coi là "xong hôm nay"; None = có giá trị bất kỳ là xong (slot Page).
    done_value: str | None
    # Key chứng tỏ job từng chạy (None = chính setting_key có giá trị).
    ever_key: str | None = None


def _task_alive(module, attr: str) -> bool:
    task = getattr(module, attr, None)
    return isinstance(task, asyncio.Task) and not task.done()


def daily_job_checks(now: datetime) -> list[JobCheck]:
    """Các job hằng ngày đang được lập lịch trong process này, kèm hạn chót hôm
    nay (giờ chạy + khung chạy bù, đọc từ chính module của job)."""
    import scheduler
    from services import market_page, morning_news, portfolio_alerts, portfolio_events
    from stock import calendar as trading_calendar

    today = now.date()
    iso = today.isoformat()

    def at(hour: int, minute: int = 0) -> datetime:
        return datetime.combine(today, dtime(hour, minute), VN_TZ)

    checks: list[JobCheck] = []
    trading_day = trading_calendar.is_trading_day(today)
    # Digest bỏ qua ngày không có phiên (scheduler.digest_due) trừ khi bật
    # DIGEST_ON_NON_TRADING_DAYS: cuối tuần/lễ không coi là quá hạn.
    digest_today = trading_day or config.DIGEST_ON_NON_TRADING_DAYS
    if config.ENABLE_DAILY_DIGEST and digest_today and _task_alive(scheduler, "_digest_task"):
        checks.append(
            JobCheck(
                "digest",
                "Digest danh mục buổi sáng",
                at(config.DAILY_DIGEST_HOUR_VN) + timedelta(hours=scheduler.DIGEST_CATCHUP_HOURS),
                scheduler._DIGEST_LAST_SENT_KEY,
                iso,
            )
        )
    if config.MORNING_NEWS_ENABLED and _task_alive(morning_news, "_task"):
        checks.append(
            JobCheck(
                "morning_news",
                "Bản tin sáng Zalo/Zoom (chưa pair kênh nào hoặc mọi nguồn RSS lỗi cũng tính)",
                at(config.MORNING_NEWS_HOUR_VN) + timedelta(hours=morning_news.CATCHUP_HOURS),
                morning_news._LAST_SENT_KEY,
                iso,
            )
        )
    if config.ENABLE_PORTFOLIO_ALERTS and trading_day and _task_alive(portfolio_alerts, "_task"):
        checks.append(
            JobCheck(
                "portfolio_alerts",
                "Quét cảnh báo danh mục cuối phiên",
                at(portfolio_alerts.ALERT_HOUR_VN, portfolio_alerts.ALERT_MINUTE_VN)
                + timedelta(hours=portfolio_alerts.CATCHUP_HOURS),
                portfolio_alerts._LAST_RUN_KEY,
                iso,
            )
        )
    if trading_day and _task_alive(portfolio_events, "_task"):
        checks.append(
            JobCheck(
                "portfolio_events",
                "Job danh mục buổi sáng (quy đổi sự kiện quyền, báo lịch quyền)",
                at(portfolio_events.run_hour()) + timedelta(hours=portfolio_events.CATCHUP_HOURS),
                portfolio_events._LAST_RUN_KEY,
                iso,
            )
        )
    if _task_alive(market_page, "_task"):
        for job, at_time, weekdays in market_page._schedule():
            if today.weekday() not in weekdays or (job == "stock" and not trading_day):
                continue
            when = datetime.combine(today, at_time, VN_TZ)
            kind = "báo cáo chứng khoán" if job == "stock" else "tin CafeF"
            checks.append(
                JobCheck(
                    f"market:{job}:{at_time:%H%M}",
                    f"Page chứng khoán - {kind} {at_time:%H:%M} (đã hết lượt thử)",
                    when + market_page._catchup(),
                    market_page._slot_key(job, when),
                    None,
                    market_page._LAST_RUN_KEY.format(job=job),
                )
            )
    return checks


# Job ghi ngày chạy (digest, bản tin, cảnh báo danh mục) mà lần được gần nhất đã
# quá ngần này ngày thì coi như cố ý bỏ (vd không còn pair Zalo/Zoom): chỉ nhắc
# trong 1 tuần đầu rồi thôi, không báo mỗi ngày mãi.
_ABANDONED_DAYS = 7


def _days_since(value: str, now: datetime) -> int:
    try:
        return (now.date() - datetime.fromisoformat(value).date()).days
    except ValueError:
        return 0


# (ngày, tên job) đã xác nhận xong/đã báo hôm nay - đỡ đọc settings mỗi chu kỳ.
# Chỉ giữ ngày hiện tại nên luôn nhỏ (vài phần tử).
_settled: set[tuple[str, str]] = set()


async def check_missed_jobs(now: datetime) -> list[str]:
    """Báo (1 tin gộp) các job đã quá hạn hôm nay mà chưa chạy xong; trả tên job đã báo."""
    today = now.date().isoformat()
    for item in [item for item in _settled if item[0] != today]:
        _settled.discard(item)
    due = [
        check
        for check in daily_job_checks(now)
        if check.deadline.date() == now.date()
        and now >= check.deadline
        and (today, check.name) not in _settled
    ]
    if not due:
        return []
    state = _load_json(await db.get_setting(_MISSED_KEY)) or {}
    alerted = set(state.get("jobs") or []) if state.get("date") == today else set()
    missed: list[JobCheck] = []
    for check in due:
        if check.name in alerted:
            _settled.add((today, check.name))
            continue
        value = await db.get_setting(check.setting_key)
        done = bool(value) if check.done_value is None else value == check.done_value
        if done:
            _settled.add((today, check.name))
            continue
        ever = value if check.ever_key is None else await db.get_setting(check.ever_key)
        if not ever:
            continue  # job chưa từng chạy (chưa cấu hình/chưa pair): không báo giả
        if check.done_value is not None and _days_since(ever, now) > _ABANDONED_DAYS:
            continue  # lần chạy được gần nhất đã lâu (vd đã bỏ pair kênh): thôi nhắc
        missed.append(check)
    if not missed:
        return []
    alerted.update(check.name for check in missed)
    await db.set_setting(_MISSED_KEY, json.dumps({"date": today, "jobs": sorted(alerted)}))
    _settled.update((today, check.name) for check in missed)
    lines = "\n".join(f"• {check.label} - hạn {check.deadline:%H:%M}" for check in missed)
    alerts.send(
        "⏰ Job hằng ngày đã quá hạn mà hôm nay chưa chạy xong:\n"
        f"{lines}\n"
        "Thường do bot ngủ/khởi động lại đúng giờ chạy, hết lượt thử hoặc job lỗi - xem "
        "log Render hoặc /admin."
    )
    return [check.name for check in missed]


# ─── 5. Nhịp tim + phát hiện khởi động lại ──────────────────────────────────


def _proc_start_ticks(pid: str) -> int | None:
    """Trường starttime (thứ 22) trong /proc/<pid>/stat - đơn vị clock tick."""
    try:
        text = (PROC_ROOT / pid / "stat").read_text()
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def container_start_offset() -> float | None:
    """Số giây PID 1 (supervisord) khởi động trước process này; None khi PID 1
    không phải supervisord (chạy local) hoặc không đọc được /proc."""
    try:
        cmdline = (PROC_ROOT / "1" / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    if "supervisord" not in cmdline:
        return None
    pid1, current = _proc_start_ticks("1"), _proc_start_ticks("self")
    if pid1 is None or current is None:
        return None
    try:
        hertz = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError):
        hertz = 100
    return (current - pid1) / hertz


def _heartbeat_payload(*, clean: bool) -> dict:
    events = memory_stats.memory_events()
    return {
        "ts": round(time.time()),
        "inst": _INSTANCE,
        "commit": _commit(),
        "oom_kill": events.get("oom_kill"),
        "ws_pct": memory_stats.working_set()["percent"],
        "clean": clean,
    }


def _clean_exits(raw: str | None) -> list[dict]:
    items = _load_json(raw)
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("inst"), str)]


async def _record_clean_exit() -> None:
    exits = [
        i for i in _clean_exits(await db.get_setting(CLEAN_EXITS_KEY)) if i["inst"] != _INSTANCE
    ]
    exits.append({"inst": _INSTANCE, "ts": round(time.time())})
    await db.set_setting(CLEAN_EXITS_KEY, json.dumps(exits[-MAX_CLEAN_EXITS:]))


async def write_heartbeat(*, clean: bool = False) -> None:
    """Ghi nhịp tim (mỗi chu kỳ; ``clean=True`` lúc tắt êm)."""
    if not enabled() or not _on_render():
        return
    if clean:
        # Ghi nhận tắt êm TRƯỚC (kể cả khi nhịp tim đã thuộc instance mới): restart
        # sau instance này là có chủ ý, không tính vào cảnh báo dồn dập.
        await _record_clean_exit()
        # Deploy: instance mới đã ghi nhịp tim trước khi instance cũ tắt - không ghi đè.
        current = _load_json(await db.get_setting(HEARTBEAT_KEY)) or {}
        if current.get("inst") not in (None, _INSTANCE):
            return
    await db.set_setting(HEARTBEAT_KEY, json.dumps(_heartbeat_payload(clean=clean)))


@dataclass(frozen=True)
class RestartInfo:
    kind: str  # first | deploy | restart | sleep | crash | oom
    gap_min: float | None
    alert: bool


def classify_restart(
    prev: dict | None,
    *,
    now: float,
    commit: str,
    oom_kill: int | None,
    pid1_offset: float | None,
    gap_alert_min: int,
) -> RestartInfo:
    """Phân loại lần khởi động này so với nhịp tim của process trước."""
    if not isinstance(prev, dict) or not isinstance(prev.get("ts"), (int, float)):
        return RestartInfo("first", None, False)
    gap_min = max(0.0, (now - float(prev["ts"])) / 60)
    same_container = pid1_offset is not None and pid1_offset > _SAME_CONTAINER_SEC
    prev_oom = prev.get("oom_kill")
    if same_container and isinstance(oom_kill, int) and isinstance(prev_oom, int):
        if oom_kill > prev_oom:
            return RestartInfo("oom", gap_min, True)
    if same_container:
        return RestartInfo("crash", gap_min, True)
    if commit and prev.get("commit") and commit != prev.get("commit"):
        return RestartInfo("deploy", gap_min, False)
    if gap_min > gap_alert_min:
        return RestartInfo("sleep", gap_min, True)
    return RestartInfo("restart", gap_min, False)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, VN_TZ).strftime("%H:%M %d/%m")


def restart_alert_text(info: RestartInfo, prev: dict) -> str:
    last = _fmt_ts(float(prev["ts"]))
    ram = prev.get("ws_pct")
    ram_note = f" Lần đo RAM cuối trước đó: {ram:.0f}%." if isinstance(ram, (int, float)) else ""
    if info.kind == "oom":
        return (
            "🚨 Tiến trình web vừa bị kernel kill vì hết RAM (OOM) và supervisord đã chạy "
            f"lại (nhịp tim cuối {last}).{ram_note} Xem /admin → Hệ thống."
        )
    if info.kind == "crash":
        return (
            "🚨 Tiến trình web vừa chết và được supervisord chạy lại - không phải deploy "
            f"(nhịp tim cuối {last}).{ram_note} Xem log Render quanh thời điểm đó."
        )
    return (
        f"⚠️ Bot vừa khởi động lại sau {info.gap_min or 0:.0f} phút gián đoạn (nhịp tim cuối "
        f"{last}).{ram_note} Có thể UptimeRobot ngừng ping nên Render cho ngủ, Render "
        "restart/sự cố nền tảng, hoặc hết RAM - xem Events trên dashboard Render."
    )


def _restart_log(raw: str | None, now: float) -> list[dict]:
    items = _load_json(raw)
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("ts"), (int, float))
        and now - item["ts"] < _RESTART_LOG_SEC
    ][-MAX_RESTART_LOG:]


_burst_pending = False


async def detect_restart(now: float | None = None) -> RestartInfo | None:
    """Gọi 1 lần lúc khởi động: phân loại, ghi nhật ký restart (có trần), báo
    chủ bot nếu đáng báo, rồi ghi nhịp tim mới. Restart thường thì chỉ đánh dấu
    để check_restart_burst xét sau (instance cũ có thể chưa tắt)."""
    global _burst_pending
    if not enabled() or not _on_render():
        return None
    now = now or time.time()
    prev = _load_json(await db.get_setting(HEARTBEAT_KEY))
    info = classify_restart(
        prev,
        now=now,
        commit=_commit(),
        oom_kill=memory_stats.memory_events().get("oom_kill"),
        pid1_offset=container_start_offset(),
        gap_alert_min=env_int("WATCHDOG_RESTART_GAP_MIN", 20, 5, 1440),
    )
    log = _restart_log(await db.get_setting(RESTARTS_KEY), now)
    if info.kind != "first":
        entry: dict = {"ts": round(now), "kind": info.kind, "gap_min": round(info.gap_min or 0, 1)}
        prev_inst = prev.get("inst") if isinstance(prev, dict) else None
        if isinstance(prev_inst, str):
            entry["prev"] = prev_inst
        log.append(entry)
        log = log[-MAX_RESTART_LOG:]
        await db.set_setting(RESTARTS_KEY, json.dumps(log))
    await write_heartbeat()
    logger.info("watchdog: khởi động (%s, gián đoạn %s phút).", info.kind, info.gap_min)
    if info.alert and isinstance(prev, dict):
        alerts.send(
            restart_alert_text(info, prev), key=f"watchdog:restart:{info.kind}", cooldown=600
        )
    elif info.kind == "restart":
        _burst_pending = True
    return info


def _unplanned(item: dict, clean_ids: set[str]) -> bool:
    """Restart không chủ ý: crash/OOM, hoặc restart mà instance trước KHÔNG tắt êm
    (bản ghi cũ chưa có ``prev`` coi như không tắt êm)."""
    kind = item.get("kind")
    if kind in ("crash", "oom"):
        return True
    return kind == "restart" and item.get("prev") not in clean_ids


async def check_restart_burst(now: float | None = None) -> None:
    """Chạy trong vòng watchdog: 1 lần cho mỗi lần khởi động kiểu "restart", sau
    _BURST_SETTLE_SEC (instance cũ đã tắt hẳn, kịp ghi bản ghi tắt êm). Báo khi số
    restart không chủ ý trong 24 giờ >= WATCHDOG_RESTART_BURST, tối đa 1 lần/24 giờ
    (dedup lưu settings - cooldown của alerts mất theo mỗi restart)."""
    global _burst_pending
    if not _burst_pending or not enabled() or not _on_render():
        return
    now = now or time.time()
    if now - _PROCESS_STARTED < _BURST_SETTLE_SEC:
        return
    log = _restart_log(await db.get_setting(RESTARTS_KEY), now)
    clean_ids = {i["inst"] for i in _clean_exits(await db.get_setting(CLEAN_EXITS_KEY))}
    burst = sum(1 for item in log if now - item["ts"] < 86400 and _unplanned(item, clean_ids))
    if burst < env_int("WATCHDOG_RESTART_BURST", 3, 2, 50):
        _burst_pending = False
        return
    last = _load_json(await db.get_setting(_BURST_KEY))
    _burst_pending = False
    if isinstance(last, (int, float)) and now - last < 86400:
        return
    text = (
        f"⚠️ Bot đã khởi động lại {burst} lần trong 24 giờ qua mà không có deploy mới và "
        "không phải tắt êm (sửa env/Restart service không tính) - thường do hết RAM "
        "(Render kill cả instance) hoặc nền tảng chập chờn. Xem Events trên dashboard "
        "Render và RAM ở /admin."
    )
    if alerts.send(text, key="watchdog:restart:burst", cooldown=600):
        await db.set_setting(_BURST_KEY, json.dumps(round(now)))


async def restart_summary(days: int = 7) -> dict:
    """Thống kê khởi động lại cho /admin và /loi."""
    now = time.time()
    log = _restart_log(await db.get_setting(RESTARTS_KEY), now)
    recent = [item for item in log if now - item["ts"] < days * 86400]
    heartbeat = _load_json(await db.get_setting(HEARTBEAT_KEY)) or {}
    beat = heartbeat.get("ts")
    return {
        "days": days,
        "counts": dict(Counter(str(item.get("kind")) for item in recent)),
        "recent": [
            {**item, "at": datetime.fromtimestamp(item["ts"], VN_TZ).isoformat(timespec="minutes")}
            for item in recent[-10:]
        ],
        "heartbeat_at": (
            datetime.fromtimestamp(beat, VN_TZ).isoformat(timespec="minutes")
            if isinstance(beat, (int, float))
            else None
        ),
        "uptime_min": round((now - _PROCESS_STARTED) / 60, 1),
        "commit": _commit() or None,
        "enabled": enabled() and _on_render(),
    }


# ─── Đang có gì bất thường (lệnh /chuy) ────────────────────────────────────


async def active_issues(now: datetime | None = None) -> list[str]:
    """Các luật watchdog ĐANG đúng lúc này / đã báo hôm nay (đọc trạng thái có
    sẵn, không gửi gì, không gọi AI). Mỗi mục lỗi riêng thì bỏ mục đó."""
    now = now or datetime.now(VN_TZ)
    today = now.date().isoformat()
    out: list[str] = []
    working = memory_stats.working_set()
    percent = working.get("percent")
    if percent is not None and percent >= memory_threshold_pct():
        out.append(
            f"RAM container {working['working_set_mib']:.0f}/{working['limit_mib']:.0f} MB "
            f"({percent:.0f}%, ngưỡng {memory_threshold_pct()}%)"
        )
    try:
        day, _, level = (await db.get_setting(_DB_LEVEL_KEY) or "").partition(":")
        if day == today and level.isdigit():
            out.append(f"dung lượng DB đã qua mốc {level}% (DB_SIZE_LIMIT_MB)")
        state = _load_json(await db.get_setting(_MISSED_KEY)) or {}
        if state.get("date") == today and state.get("jobs"):
            labels = {check.name: check.label for check in daily_job_checks(now)}
            names = [labels.get(name, name) for name in state["jobs"]][:5]
            out.append("job hằng ngày quá hạn: " + "; ".join(names))
        minutes = env_int("WATCHDOG_INBOX_STALE_MIN", 10, 1, 1440)
        stale, _ = await db.webhook_backlog(timedelta(minutes=minutes))
        if stale > 0:
            out.append(f"{stale} tin Telegram/Zoom ứ trong inbox quá {minutes} phút")
        last_burst = _load_json(await db.get_setting(_BURST_KEY))
        if isinstance(last_burst, (int, float)) and time.time() - last_burst < 86400:
            out.append("bot khởi động lại dồn dập trong 24 giờ qua")
        log = _restart_log(await db.get_setting(RESTARTS_KEY), time.time())
        crashes = [
            i for i in log if i.get("kind") in ("crash", "oom") and time.time() - i["ts"] < 86400
        ]
        if crashes:
            out.append(f"{len(crashes)} lần tiến trình chết/hết RAM trong 24 giờ qua")
    except Exception:
        logger.warning("watchdog: không đọc được trạng thái cho /chuy.", exc_info=True)
    return out


# ─── Điều phối ──────────────────────────────────────────────────────────────


async def run_checks(now: datetime | None = None) -> None:
    """1 lượt kiểm tra (gọi từ vòng monitor_service). Mỗi luật lỗi riêng không
    chặn luật khác; lỗi log WARNING (vào bản tổng hợp lỗi, không báo ngay)."""
    if not enabled():
        return
    now = now or datetime.now(VN_TZ)
    steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
        ("RAM", check_memory),
        ("dung lượng DB", lambda: check_db_size(now)),
        ("webhook inbox", check_inbox),
        ("job hằng ngày", lambda: check_missed_jobs(now)),
        ("restart dồn dập", check_restart_burst),
        ("nhịp tim", write_heartbeat),
    )
    for label, step in steps:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("watchdog: kiểm tra %s lỗi.", label, exc_info=True)
