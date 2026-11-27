"""Supabase Postgres - bền hơn SQLite vì ổ đĩa local trên Render free tier là ephemeral.

Mặt tiền (facade) của tầng DB: pool/breaker/``with_reconnect`` ở core/db/pool.py (re-export
dưới đây), SQL ở core/repositories/*. Mọi nơi gọi ``db.xxx``; test thay ``db.get_pool`` hoặc
hàm repository thì các hàm ở đây thấy ngay (tra tên lúc gọi).
"""

import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Concatenate, Optional, ParamSpec, TypeVar, overload

from core import config
from core.db.pool import DatabaseUnavailableError as DatabaseUnavailableError
from core.db.pool import _ssl_setting as _ssl_setting  # test/test_security_contracts.py
from core.db.pool import _with_reconnect
from core.db.pool import circuit_open as circuit_open
from core.db.pool import close_pool as close_pool
from core.db.pool import get_pool as get_pool
from core.db.pool import pool_stats as pool_stats
from core.db.pool import reset_circuit_breaker as reset_circuit_breaker
from core.db.pool import statement_cache_size as statement_cache_size
from core.db.pool import with_reconnect as with_reconnect
from core.repositories import app_devices as _app_devices
from core.repositories import avatar as _avatar
from core.repositories import brief as _brief
from core.repositories import chat_history as _chat_history
from core.repositories import expenses as _expenses
from core.repositories import feedback as _feedback
from core.repositories import meals as _meals
from core.repositories import jobs as _jobs
from core.repositories import memory as _memory
from core.repositories import notify as _notify
from core.repositories import observability as _observability
from core.repositories import ops as _ops
from core.repositories import portfolio as _portfolio
from core.repositories import prompts as _prompts
from core.repositories import quota as _quota
from core.repositories import reminders as _reminders
from core.repositories import settings as _settings

logger = logging.getLogger(__name__)

HISTORY_RETENTION_LIMIT = _prompts.HISTORY_RETENTION_LIMIT

_P = ParamSpec("_P")
_R = TypeVar("_R")
_T = TypeVar("_T")


@overload
def _bind(
    fn: Callable[Concatenate[Any, _P], Awaitable[_R]], *, idempotent: bool = True
) -> Callable[_P, Awaitable[_R]]: ...


@overload
def _bind(
    fn: Callable[Concatenate[Any, _P], Awaitable[Any]],
    *,
    idempotent: bool = True,
    post: Callable[[Any], _T],
) -> Callable[_P, Awaitable[_T]]: ...


def _bind(fn, *, idempotent=True, post=None):
    """``db.xxx = _bind(repo.fn)``: hàm gọi ``repo.fn(await get_pool(), *args, **kwargs)``
    có with_reconnect (``idempotent=False``: lệnh có thể đã commit thì không chạy lại).
    Hàm repository và ``get_pool`` được tra theo tên LÚC GỌI - test thay ``db.get_pool``
    hay hàm trong core/repositories vẫn có hiệu lực như với wrapper viết tay. ``post`` đổi
    kết quả (vd Record -> dict). Chữ ký (pyright) lấy từ hàm repository, bỏ tham số pool."""
    module, name = sys.modules[fn.__module__], fn.__name__

    async def bound(*args, **kwargs):
        result = await getattr(module, name)(await get_pool(), *args, **kwargs)
        return result if post is None else post(result)

    bound.__name__ = bound.__qualname__ = name
    bound.__doc__ = fn.__doc__
    return _with_reconnect(bound, idempotent=idempotent)


def _dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _dict_or_none(row) -> dict | None:
    return dict(row) if row else None


@_with_reconnect
async def ensure_migrations() -> None:
    """Apply all pending versioned schema migrations."""
    from core import migrations

    pool = await get_pool()
    if await migrations.run(pool):
        # Schema vừa đổi: thay mọi kết nối (lúc được mượn/trả lại) để không kết nối nào
        # còn giữ prepared statement soạn theo schema cũ.
        expire = getattr(pool, "expire_connections", None)
        if expire is not None:
            await expire()


@_with_reconnect
async def init_db() -> None:
    await ensure_migrations()
    # Dọn dữ liệu là việc vận hành, không phải migration schema.
    await _prompts.prune_all(await get_pool())


# ─── Lịch sử lệnh /history: prompts + results (core/repositories/prompts.py) ─
save_prompt = _bind(_prompts.save, idempotent=False)
save_result = _bind(_prompts.save_result, idempotent=False)
get_history = _bind(_prompts.history)


# ─── Số liệu lượt gọi / provider / truy vết lượt (migrations/015, 021, 025) ──
# SQL ở core/repositories/{observability,quota,feedback}.py. INSERT số liệu không
# idempotent; ghi đánh giá 👍/👎 là UPSERT theo (người, prompt) nên thử lại an toàn.
usage_by_user = _bind(_observability.usage_by_user)
usage_by_model = _bind(_observability.usage_by_model)
provider_outcomes = _bind(_observability.provider_outcomes)
record_provider_call = _bind(_observability.insert_provider_call, idempotent=False)
provider_latency_summary = _bind(_observability.latency_by_provider)
record_turn_metrics = _bind(_observability.insert_turn_metrics, idempotent=False)
recent_turn_metrics = _bind(_observability.recent_turns)
turn_stage_summary = _bind(_observability.turn_stage_summary)
provider_calls_for_turn = _bind(_observability.provider_calls_for_turn)
provider_burn_since = _bind(_observability.burn_since)
user_calls_timeline = _bind(_observability.calls_timeline)

# Quota ngày thành viên Zalo (services/usage_quota.py, migrations/025).
user_calls_since = _bind(_quota.count_since)
user_calls_by_user_since = _bind(_quota.counts_by_user_since)
record_user_call = _bind(_quota.insert, idempotent=False)
# Không idempotent: UPDATE chọn lượt from_kind MỚI NHẤT, chạy lại sau khi đã commit sẽ
# đổi luôn lượt trước đó (lượt nặng +2) - chỉ retry khi lệnh chưa gửi đi.
relabel_last_user_call = _bind(_quota.relabel_latest, idempotent=False)

# Đánh giá câu trả lời (services/answer_feedback.py, migrations/021).
answer_feedback_upsert = _bind(_feedback.upsert)
answer_feedback_turn = _bind(_feedback.turn_snapshot)
answer_feedback_last_chat = _bind(_feedback.last_chat_snapshot)
answer_feedback_stats = _bind(_feedback.stats)
answer_feedback_recent_down = _bind(_feedback.recent_down)
answer_feedback_owner_down = _bind(_feedback.owner_down_for_evals)
answer_feedback_clear = _bind(_feedback.clear)


# ─── Lịch sử chat ngắn hạn: chat_messages (core/repositories/chat_history.py) ─
# Giữ tối đa N tin nhắn gần nhất / user (prompt chỉ cần CHAT_HISTORY_TURNS*2 dòng;
# phần dư là bộ đệm cho trích trí nhớ theo lô và tool search_history). Mặc định 1000,
# env CHAT_MESSAGES_RETENTION_LIMIT (kẹp 200-2000). Hàm ghi đọc hằng này LÚC GỌI.
CHAT_MESSAGES_RETENTION_LIMIT = config.CHAT_MESSAGES_RETENTION_LIMIT


@_with_reconnect(idempotent=False)
async def add_chat_message(telegram_user_id: int, role: str, content: str) -> None:
    await _chat_history.add_message(
        await get_pool(), telegram_user_id, role, content, retention=CHAT_MESSAGES_RETENTION_LIMIT
    )


@_with_reconnect(idempotent=False)
async def add_chat_turn(
    telegram_user_id: int, user_text: str, model_text: str, *, kind: str = "chat"
) -> list[int]:
    """Lưu CẢ lượt (tin người dùng + câu trả lời) trong 1 transaction - không
    bao giờ còn nửa lượt (chỉ có câu hỏi) khi DB lỗi giữa 2 lệnh INSERT.
    ``kind``: loại lượt (services/conversation_log.py) - trích nhớ chỉ đọc lượt
    chat/rag/agent, lượt "stock" giúp chặn câu chat sau mâu thuẫn policy. Trả
    id 2 dòng vừa lưu (🔄/✂️ xoá đúng cặp này sau khi làm lại)."""
    return await _chat_history.add_turn(
        await get_pool(),
        telegram_user_id,
        user_text,
        model_text,
        retention=CHAT_MESSAGES_RETENTION_LIMIT,
        kind=kind,
    )


@_with_reconnect
async def search_chat_history(
    telegram_user_id: int, terms: list[str], *, scan_limit: int = 300, max_chars: int = 600
) -> list[dict]:
    """Tìm trong lịch sử chat CỦA CHÍNH người này (tool search_history): dòng
    chứa ít nhất 1 từ trong ``terms`` (đã bỏ dấu, chữ thường; từ không phải
    a-z0-9 bị bỏ - không có ký tự đại diện LIKE), kèm câu cùng lượt; mới -> cũ."""
    patterns = [f"%{term}%" for term in terms if term.isascii() and term.isalnum()]
    if not patterns:
        return []
    rows = await _chat_history.search(
        await get_pool(), telegram_user_id, patterns, scan_limit=scan_limit, max_chars=max_chars
    )
    return [dict(row) for row in rows]


@_with_reconnect
async def get_session_messages(
    telegram_user_id: int, k: int, session_timeout_sec: int
) -> list[tuple[str, str]]:
    """Trả về tối đa `k` lượt gần nhất (role, content) THEO THỨ TỰ CŨ -> MỚI,
    chỉ trong phạm vi "phiên hiện tại": nếu khoảng nghỉ giữa 2 tin nhắn liên
    tiếp (hoặc giữa "bây giờ" và tin gần nhất) > session_timeout_sec, coi như
    phiên cũ đã kết thúc và KHÔNG lấy các tin trước điểm nghỉ đó."""
    if k <= 0:
        return []
    rows = await _chat_history.session_messages(
        await get_pool(), telegram_user_id, k, session_timeout_sec
    )
    return [(r["role"], r["content"]) for r in rows]


delete_chat_rows = _bind(_chat_history.delete_rows)
clear_chat = _bind(_chat_history.clear)
# Cửa sổ chưa trích nhớ (id > after_id, cũ -> mới) / lượt gần nhất (mới -> cũ).
get_chat_rows_after = _bind(_memory.chat_rows_after, post=_dicts)
get_recent_chat_rows = _bind(_memory.recent_chat_rows, post=_dicts)
get_last_model_row = _bind(_memory.last_model_row, post=_dict_or_none)
users_with_chat_since = _bind(_memory.users_with_chat_after)


# ─── Trí nhớ dài hạn: user_facts + highlights + ghi chú phiên ─────────────────
# (xem services/memory_service.py; SQL ở core/repositories/memory.py). Từ
# migrations/017: fact có ghim/độ quan trọng/nguồn, giá trị cũ được vô hiệu hoá
# (invalid_at) thay vì xoá - mọi hàm đọc "fact" dưới đây chỉ trả dòng hiện hành.

# Trần giá trị cũ giữ lại (cho câu hỏi "trước đây..."): theo từng key và tổng.
SUPERSEDED_FACTS_PER_KEY = 3
SUPERSEDED_FACTS_TOTAL = 60


async def upsert_fact(telegram_user_id: int, key: str, value: str) -> None:
    """Tương thích cũ: ghi fact tự động (nguồn 'auto', không ghim)."""
    await save_fact(telegram_user_id, key, value)


# Fact: giá trị đổi -> bản cũ vô hiệu hoá (giữ làm lịch sử); hàm đọc chỉ trả dòng hiện hành.
save_fact = _bind(_memory.save_fact)
get_facts = _bind(_memory.current_facts, post=lambda rows: [(r["key"], r["value"]) for r in rows])
get_fact_rows = _bind(_memory.current_facts, post=_dicts)
get_superseded_facts = _bind(_memory.superseded_facts, post=_dicts)
invalidate_fact = _bind(_memory.invalidate_fact)
# XOÁ HẲN mọi phiên bản của 1 key (người dùng chủ động bảo quên).
delete_fact = _bind(_memory.forget_key)
set_fact_pinned = _bind(_memory.set_pinned)
clear_facts = _bind(_memory.clear_facts)


@_with_reconnect
async def trim_facts(
    telegram_user_id: int,
    keep_n: int,
    *,
    superseded_per_key: int = SUPERSEDED_FACTS_PER_KEY,
    superseded_total: int = SUPERSEDED_FACTS_TOTAL,
) -> None:
    """Giữ tối đa ``keep_n`` fact hiện hành theo thứ tự ưu tiên (fact ghim không
    bị cắt), kèm trần riêng cho giá trị cũ - chặn user_facts phình vô hạn."""
    await _memory.trim_facts(
        await get_pool(),
        telegram_user_id,
        keep_current=keep_n,
        keep_superseded_per_key=superseded_per_key,
        keep_superseded_total=superseded_total,
    )


# Ý quan trọng (highlights) + tóm tắt dài hạn.
add_highlight = _bind(_memory.add_highlight, idempotent=False)
get_highlights = _bind(_memory.highlights)
get_highlight_rows = _bind(_memory.highlight_rows, post=_dicts)
delete_highlights = _bind(_memory.delete_highlights)
clear_highlights = _bind(_memory.clear_highlights)
get_summary = _bind(_memory.summary)
set_summary = _bind(_memory.set_summary)
get_summary_row = _bind(_memory.summary_row, post=_dict_or_none)

# Ghi chú theo phiên: tối đa N phiên gần nhất / người (cắt khi ghi, đọc lúc gọi).
SESSION_NOTES_RETENTION = 100


@_with_reconnect
async def upsert_session_note(
    telegram_user_id: int,
    session_key: int,
    *,
    title: str = "",
    bullets: str = "",
    summary: str = "",
    started_at: datetime,
) -> None:
    await _memory.upsert_session_note(
        await get_pool(),
        telegram_user_id,
        session_key,
        title=title,
        bullets=bullets,
        summary=summary,
        started_at=started_at,
        keep_n=SESSION_NOTES_RETENTION,
    )


get_session_notes = _bind(_memory.session_notes, post=_dicts)
get_session_note = _bind(_memory.session_note, post=_dict_or_none)
# Ghi đè (kể cả rỗng) ghi chú 1 phiên; cả 3 trường rỗng thì xoá phiên đó.
rewrite_session_note = _bind(_memory.rewrite_session_note)
search_session_notes = _bind(_memory.search_session_notes)
clear_session_notes = _bind(_memory.clear_session_notes)

# Ghi chú (/note, tool add_note). INSERT có event_key thì chống trùng, không có thì không.
add_note = _bind(_memory.add_note, idempotent=False)
get_notes = _bind(_memory.notes)
get_recent_notes = _bind(_memory.recent_notes, post=_dicts)
delete_note = _bind(_memory.delete_note)
clear_notes = _bind(_memory.clear_notes)


# ─── Sự kiện hỏi thăm chủ động (migrations/018, services/proactive_service.py) ──
USER_EVENTS_PER_USER = 50


@_with_reconnect
async def add_user_event(telegram_user_id: int, title: str, when_text: str, event_date) -> bool:
    """Thêm sự kiện (trùng người + ngày + tiêu đề thì bỏ qua - chạy lại an toàn)."""
    return await _memory.add_event(
        await get_pool(),
        telegram_user_id,
        title,
        when_text,
        event_date,
        keep_n=USER_EVENTS_PER_USER,
    )


expire_user_events = _bind(_memory.expire_events)
users_with_due_events = _bind(_memory.users_with_due_events)
# Claim 1 sự kiện tới hạn (pending -> asked). Không retry sau khi mất kết nối giữa
# chừng: lần 2 sẽ claim THÊM 1 sự kiện khác.
claim_due_user_event = _bind(_memory.claim_due_event, idempotent=False, post=_dict_or_none)
release_user_event = _bind(_memory.release_event)
mark_user_event_asked = _bind(_memory.mark_event_asked)
finish_user_events = _bind(_memory.finish_events)
finish_user_event_ids = _bind(_memory.finish_event_ids)
list_user_events = _bind(_memory.list_events, post=_dicts)
user_events_on = _bind(_memory.events_on, post=_dicts)
clear_user_events = _bind(_memory.clear_events)


# ─── Nhắc việc (services/reminder_service.py, scheduler.py, migrations/019) ──


# Nhắc 1 lần: INSERT có thể đã commit -> idempotent=False (event_key chống trùng webhook).
add_reminder = _bind(_reminders.insert_once, idempotent=False)
mark_reminder_sent = _bind(_reminders.mark_sent)
reminder_count_pending_once = _bind(_reminders.count_pending_once)
reminder_count_series = _bind(_reminders.count_series)
# Tạo chuỗi INSERT nhiều dòng trong 1 transaction -> idempotent=False; chèn lần kế tiếp
# dùng ON CONFLICT nên thử lại an toàn.
reminder_create_series = _bind(_reminders.create_series, idempotent=False)
reminder_get_series = _bind(_reminders.get_series)
reminder_add_occurrence = _bind(_reminders.add_occurrence)
reminder_list_pending_once = _bind(_reminders.list_pending_once)
reminder_list_series = _bind(_reminders.list_series)
reminder_delete = _bind(_reminders.delete_reminder)
reminder_delete_series = _bind(_reminders.delete_series)
reminder_reschedule = _bind(_reminders.reschedule)
reminder_last_fired = _bind(_reminders.last_fired)
reminder_get = _bind(_reminders.get_owned)
reminder_mark_done = _bind(_reminders.mark_done)
reminder_mark_fired = _bind(_reminders.mark_fired)
# Nhắc việc chưa gửi đến hạn trong [start, end) - mục nhắc việc của bản tin sáng.
list_reminders_between = _bind(_brief.list_reminders_between)
list_settings_with_prefix = _bind(_brief.list_settings_with_prefix)

# ─── Chi tiêu, nhật ký ăn uống (core/repositories/expenses.py, meals.py) ─────
# Ngày truyền dạng text YYYY-MM-DD. Khoản không có event_key (lệnh ngoài webhook)
# INSERT không idempotent -> không retry.
expense_add = _bind(_expenses.insert, idempotent=False)
expense_count_since = _bind(_expenses.count_since)
expense_delete = _bind(_expenses.delete)
expense_last = _bind(_expenses.last_created)
expense_totals = _bind(_expenses.totals)
expense_recent = _bind(_expenses.recent)
meal_add = _bind(_meals.insert, idempotent=False)
meal_count_since = _bind(_meals.count_since)
meal_day = _bind(_meals.day_rows)
meal_day_totals = _bind(_meals.day_totals)
meal_delete = _bind(_meals.delete)
meal_recent = _bind(_meals.recent)
meal_clear = _bind(_meals.clear)

# ─── Ảnh avatar (/admin/avatar, core/repositories/avatar.py) ─────────────────
# UPSERT/DELETE theo khoá kind nên chạy lại an toàn.
avatar_photo_meta = _bind(_avatar.meta)
avatar_photo_get = _bind(_avatar.photo)
avatar_photo_put = _bind(_avatar.put)
avatar_photo_delete = _bind(_avatar.delete)


# ─── Settings (bảng key-value) ───────────────────────────────────────────────
# Mọi lần ghi qua set_setting/compare_and_set_setting báo cho các bộ đệm đã đăng ký
# (core/settings_cache.py) - khoá đọc qua cache không bao giờ cũ hơn những gì chính
# process này đã ghi. Ghi lỗi (không rõ đã commit chưa) thì báo value=None = bỏ khỏi cache.
_setting_listeners: list[Callable[[str, Optional[str]], None]] = []


def on_setting_write(listener: Callable[[str, Optional[str]], None]) -> None:
    if listener not in _setting_listeners:
        _setting_listeners.append(listener)


def _setting_written(key: str, value: Optional[str]) -> None:
    for listener in tuple(_setting_listeners):
        listener(key, value)


get_setting = _bind(_settings.get)


@_with_reconnect
async def set_setting(key: str, value: str) -> None:
    try:
        await _settings.set(await get_pool(), key, value)
    except BaseException:
        _setting_written(key, None)
        raise
    _setting_written(key, value)


# Các dòng settings đã mã hoá (xoay khoá - services/key_rotation.py).
list_encrypted_settings = _bind(_settings.list_encrypted)


@_with_reconnect
async def compare_and_set_setting(key: str, expected: str, value: str) -> bool:
    try:
        swapped = await _settings.compare_and_set(await get_pool(), key, expected, value)
    except BaseException:
        _setting_written(key, None)
        raise
    if swapped:
        _setting_written(key, value)
    return swapped


# ─── Danh mục cổ phiếu: vị thế, sổ giao dịch, theo dõi/cảnh báo giá ─────────
# SQL ở core/repositories/portfolio.py. Hàm ghi vị thế INSERT dòng sổ giao dịch trong
# cùng transaction -> idempotent=False (không retry khi lệnh có thể đã commit). Ngày
# truyền dạng text YYYY-MM-DD.
portfolio_list_holdings = _bind(_portfolio.list_holdings)
portfolio_get_holding = _bind(_portfolio.get_holding)
portfolio_list_all_holdings = _bind(_portfolio.list_all_holdings)
portfolio_replace_holding = _bind(_portfolio.replace_holding, idempotent=False)
portfolio_record_purchase = _bind(_portfolio.record_purchase, idempotent=False)
portfolio_record_sale = _bind(_portfolio.record_sale, idempotent=False)
portfolio_update_alerts = _bind(_portfolio.update_alerts, idempotent=False)
portfolio_rebase_holding = _bind(_portfolio.rebase_holding, idempotent=False)
portfolio_delete_holding = _bind(_portfolio.delete_holding, idempotent=False)
portfolio_was_closed = _bind(_portfolio.was_closed)
portfolio_list_transactions = _bind(_portfolio.list_transactions)
portfolio_transactions_between = _bind(_portfolio.transactions_between)
# Không idempotent: chạy lại sau khi đã commit sẽ điền giá cho lệnh bán CŨ HƠN.
portfolio_fill_sale_price = _bind(_portfolio.fill_sale_price, idempotent=False)
portfolio_watchlist = _bind(_portfolio.watchlist)
portfolio_add_watch = _bind(_portfolio.add_watch)
portfolio_remove_watch = _bind(_portfolio.remove_watch)
portfolio_list_price_alerts = _bind(_portfolio.list_price_alerts)
portfolio_add_price_alert = _bind(_portfolio.add_price_alert)
portfolio_delete_price_alerts = _bind(_portfolio.delete_price_alerts)
portfolio_active_price_alerts = _bind(_portfolio.active_price_alerts)
portfolio_set_price_alert_state = _bind(_portfolio.set_price_alert_state)
portfolio_alert_logged = _bind(_portfolio.alert_logged)
portfolio_log_alert = _bind(_portfolio.log_alert)
portfolio_count_alerts_on = _bind(_portfolio.count_alerts_on)
portfolio_prune_alert_log = _bind(_portfolio.prune_alert_log)
# (kind, ref_key, ngày) nhật ký thông báo danh mục từ ngày ``since``.
portfolio_alert_log_since = _bind(_portfolio.alert_log_since)

# ─── Hàng đợi thông báo gom (migrations/026, services/notify_hub.py) ────────
# Xếp tin không retry sau khi mất kết nối: tin không có dedup_key có thể đã được xếp.
notify_enqueue = _bind(_notify.enqueue, idempotent=False)
notify_pending_recipients = _bind(_notify.pending_recipients)
notify_claim = _bind(_notify.claim, idempotent=False)
notify_mark_sent = _bind(_notify.mark_sent)
notify_release = _bind(_notify.release)
notify_take_dropped = _bind(_notify.take_dropped, idempotent=False)
notify_flush_zalo = _bind(_notify.flush_zalo, idempotent=False)
notify_drop_pending = _bind(_notify.drop_pending)
notify_count_pending = _bind(_notify.count_pending)
notify_clear_user = _bind(_notify.clear_user)

# ─── Việc nền + tác vụ định kỳ (migrations/027, services/background_jobs.py,
# services/routines.py). SQL ở core/repositories/jobs.py.
bg_job_insert = _bind(_jobs.insert_job, idempotent=False)
bg_job_claim = _bind(_jobs.claim_next, idempotent=False)
bg_job_renew = _bind(_jobs.renew_lease)
bg_job_finish = _bind(_jobs.finish)
bg_job_recover = _bind(_jobs.recover_stale, idempotent=False)
bg_job_cancel = _bind(_jobs.cancel)
bg_job_get = _bind(_jobs.get_job)
bg_job_list = _bind(_jobs.list_jobs)
bg_job_active = _bind(_jobs.active_jobs)
bg_job_position = _bind(_jobs.queue_position)
bg_job_recent_all = _bind(_jobs.recent_all)
bg_job_clear_user = _bind(_jobs.clear_user)
routine_count = _bind(_jobs.count_routines)
routine_insert = _bind(_jobs.insert_routine, idempotent=False)
routine_list = _bind(_jobs.list_routines)
routine_all = _bind(_jobs.all_routines)
routine_get = _bind(_jobs.get_routine)
routine_delete = _bind(_jobs.delete_routine)
routine_set_status = _bind(_jobs.set_routine_status)
routine_due = _bind(_jobs.due_routines)
# Idempotent nhờ điều kiện next_run_at = giá trị cũ + dedup_key của bg_jobs.
routine_fire = _bind(_jobs.fire_routine)
routine_runs_since = _bind(_jobs.routine_runs_since)
routine_jobs_since = _bind(_jobs.routine_jobs_since)

# ─── Giám sát vận hành / sao lưu (services/watchdog.py, backup_status.py) ───
database_bytes = _bind(_ops.database_bytes)
webhook_backlog = _bind(_ops.webhook_backlog)
latest_backup_run = _bind(_ops.latest_backup_run)

# ─── Thiết bị app màn hình thông tin (/app, core/repositories/app_devices.py) ─
# Chỉ nhận HASH của mã ghép/token. Không idempotent: tạo chờ ghép lần 2 thêm 1 dòng
# thứ hai; ghép lần 2 thấy mã đã bị xoá -> báo sai "mã hết hạn".
app_device_create_pending = _bind(_app_devices.create_pending, idempotent=False)
app_device_pair = _bind(_app_devices.pair, idempotent=False)
app_device_by_token = _bind(_app_devices.by_token)
app_device_list = _bind(_app_devices.list_for_user)
app_device_revoke = _bind(_app_devices.revoke)


# ─── Zoom Team Chat pairing (bot 1 chủ, dùng chung ALLOWED_USER_ID) ─────────
# Zoom không có khái niệm "user Telegram" - chỉ có ĐÚNG 1 jid Zoom được phép
# nói chuyện với bot (như ALLOWED_USER_ID của Telegram). Lưu qua bảng settings
# key-value sẵn có thay vì tạo bảng users/roles riêng như vietassist, vì kiến
# trúc Gemini là bot 1 chủ (ALLOWED_USER_ID), không phải multi-user/multi-role.
_SETTING_ZOOM_JID = "zoom_paired_jid"
_SETTING_ZOOM_NAME = "zoom_paired_name"


async def zoom_get_pairing() -> Optional[tuple[str, str]]:
    """Trả về (jid, display_name) đã pair, hoặc None nếu chưa pair ai. Đọc qua
    core.settings_cache: mỗi webhook Zoom/thông báo chủ động không còn 2 lượt đọc DB."""
    from core import settings_cache

    jid = await settings_cache.get(_SETTING_ZOOM_JID)
    if not jid:
        return None
    name = await settings_cache.get(_SETTING_ZOOM_NAME) or ""
    return jid, name


async def zoom_set_pairing(jid: str, display_name: str = "") -> None:
    from core import settings_cache

    await settings_cache.set(_SETTING_ZOOM_JID, jid)
    await settings_cache.set(_SETTING_ZOOM_NAME, display_name)


async def zoom_clear_pairing() -> None:
    from core import settings_cache

    await settings_cache.set(_SETTING_ZOOM_JID, "")
    await settings_cache.set(_SETTING_ZOOM_NAME, "")
