"""Pool kết nối Supabase Postgres + circuit breaker + tự nối lại (``with_reconnect``).

Tách từ core/database.py (module đó re-export các tên công khai: ``get_pool``,
``with_reconnect``, ``close_pool``, ``pool_stats``, ``DatabaseUnavailableError``...).
Test thay trạng thái nội bộ (``_pool``, ``_reset_pool``, ``_breaker_open_until``...)
thì thay ở module NÀY.
"""

import asyncio
import contextvars
import functools
import logging
import time
from typing import Optional
from urllib.parse import urlsplit

import asyncpg

from core import config

logger = logging.getLogger("core.database")

_pool: Optional[asyncpg.pool.Pool] = None
_pool_lock = asyncio.Lock()

_CONNECTION_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError)
# Lúc tắt app: chờ lệnh đang chạy trả kết nối tối đa chừng này giây rồi cắt hẳn
# (supervisord chỉ chờ cả tiến trình web 40 s).
_CLOSE_TIMEOUT_SEC = 5.0
# Supabase transaction pooler (Supavisor/PgBouncer) - không giữ prepared statement.
_TRANSACTION_POOLER_PORT = 6543
_DEFAULT_STATEMENT_CACHE = 100


class DatabaseUnavailableError(ConnectionError):
    """DB đang được coi là sập (circuit breaker mở): lệnh DB báo lỗi NGAY thay vì
    treo tới timeout - caller degrade trong vài ms (đọc trí nhớ/lịch sử trả
    rỗng, webhook trả 503 để Telegram tự gửi lại)."""


# ─── Circuit breaker ─────────────────────────────────────────────────────────
# CHỈ mở khi lần thử lại sau reconnect CŨNG mất kết nối, hoặc mở kết nối mới
# quá DB_CONNECT_TIMEOUT_SEC. Một connection bị Supavisor cắt lúc rảnh (chuyện
# thường ngày, max_inactive_connection_lifetime=60) chỉ là 1 lỗi đơn lẻ được
# _with_reconnect tự chữa, KHÔNG làm mở breaker. Hết DB_BREAKER_OPEN_SEC thì
# cho đúng 1 lệnh thăm dò đi qua: thành công -> đóng, lỗi kết nối -> mở tiếp.
_breaker_open_until = 0.0
_breaker_probe_inflight = False
_in_db_call: contextvars.ContextVar[bool] = contextvars.ContextVar("db_in_call", default=False)


def reset_circuit_breaker() -> None:
    global _breaker_open_until, _breaker_probe_inflight
    _breaker_open_until = 0.0
    _breaker_probe_inflight = False


def circuit_open() -> bool:
    return _breaker_open_until > 0 and time.monotonic() < _breaker_open_until


def _breaker_enter() -> bool:
    """Gọi trước mỗi lệnh DB. Trả True nếu lệnh này là lệnh thăm dò."""
    global _breaker_probe_inflight
    if _breaker_open_until <= 0:
        return False
    if time.monotonic() < _breaker_open_until or _breaker_probe_inflight:
        raise DatabaseUnavailableError("DB tạm thời không kết nối được (circuit breaker mở)")
    _breaker_probe_inflight = True
    return True


def _release_probe() -> None:
    global _breaker_probe_inflight
    _breaker_probe_inflight = False


def _breaker_trip(exc: BaseException) -> None:
    global _breaker_open_until, _breaker_probe_inflight
    open_sec = config.DB_BREAKER_OPEN_SEC
    _breaker_probe_inflight = False
    if open_sec <= 0:
        return
    if not circuit_open():
        logger.warning(
            "DB mất kết nối cả sau khi reconnect (%s: %s) - fail nhanh trong %ss.",
            type(exc).__name__,
            exc,
            open_sec,
        )
    _breaker_open_until = time.monotonic() + open_sec


def _breaker_ok() -> None:
    global _breaker_open_until, _breaker_probe_inflight
    if _breaker_open_until > 0:
        logger.info("DB kết nối lại được, đóng circuit breaker.")
    _breaker_open_until = 0.0
    _breaker_probe_inflight = False


def _is_command_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) and not isinstance(exc, DatabaseUnavailableError)


def _is_closing(pool) -> bool:
    is_closing = getattr(pool, "is_closing", None)
    return bool(is_closing()) if callable(is_closing) else False


async def _reset_pool(failed_pool: Optional[asyncpg.pool.Pool]) -> None:
    """Thay mọi kết nối của pool vừa lỗi mà KHÔNG chờ ai: kết nối đang rảnh được mở
    lại ở lần mượn kế, kết nối đang bị mượn đóng khi được trả về
    (``expire_connections``). Bản cũ ``await pool.close()`` chờ MỌI kết nối đang
    mượn trả về - 1 VACUUM FULL (tới 600 s) hay 1 migration làm mọi lượt dính lỗi
    kết nối treo theo ở ``_pool_lock``, các lượt khác nhận "pool is closing".

    Chỉ đụng tới pool nếu `_pool` hiện tại VẪN CÒN LÀ `failed_pool` - coroutine chạy
    sau (pool nó thấy bị lỗi đã cũ) không được phá pool MỚI mà coroutine chạy trước
    vừa tạo. Pool đã/đang đóng (lúc tắt app) thì bỏ hẳn để get_pool() tạo pool mới."""
    global _pool
    if failed_pool is None:
        return
    async with _pool_lock:
        if _pool is not failed_pool:
            return
        expire = getattr(failed_pool, "expire_connections", None)
        if _is_closing(failed_pool) or expire is None:
            _pool = None
            return
        await expire()


# Pool mà lượt gọi DB hiện tại THỰC SỰ dùng (get_pool() ghi lại). Nhờ vậy
# _with_reconnect chỉ reset đúng pool vừa lỗi, không đóng nhầm pool MỚI mà
# một coroutine khác vừa tạo (bản cũ đọc biến global `_pool` lúc bắt lỗi nên
# cơ chế chống race của _reset_pool bị vô hiệu).
_pool_in_use: contextvars.ContextVar[Optional[asyncpg.pool.Pool]] = contextvars.ContextVar(
    "_pool_in_use", default=None
)


def _safe_to_retry(exc: BaseException, idempotent: bool) -> bool:
    """Câu lệnh idempotent (SELECT/UPSERT/DELETE theo khoá) luôn retry được.

    Câu INSERT không idempotent chỉ retry khi chắc chắn lệnh CHƯA tới server:
    ``ConnectionDoesNotExistError`` ("connection was closed in the middle of
    operation") có thể xảy ra SAU khi server đã commit, retry sẽ tạo bản ghi
    trùng - lỗi đó được ném ra cho nơi gọi.
    """
    if idempotent:
        return True
    return not isinstance(exc, asyncpg.ConnectionDoesNotExistError)


def _with_reconnect(func=None, *, idempotent: bool = True):
    # Supabase free tier tự pause DB sau ~1 tuần không hoạt động, khiến pool
    # cũ giữ connection đã chết -> raise lỗi kết nối. Bắt lỗi, reset ĐÚNG pool
    # vừa lỗi rồi thử lại đúng 1 lần (nếu an toàn) để tự phục hồi.
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # Lệnh lồng trong 1 lệnh DB khác (vd enqueue -> ensure_migrations) đi
            # theo quyết định breaker của lệnh ngoài, không tự kiểm lại (nếu
            # không, lệnh thăm dò sẽ tự chặn chính lệnh con của nó).
            nested = _in_db_call.get()
            probe = False if nested else _breaker_enter()
            nested_token = _in_db_call.set(True)
            token = _pool_in_use.set(None)
            try:
                try:
                    result = await fn(*args, **kwargs)
                except asyncpg.InvalidCachedStatementError:
                    # Prepared statement cũ sau khi schema đổi (migration của instance
                    # khác lúc deploy). Ngoài transaction asyncpg đã tự chạy lại; trong
                    # transaction thì câu lỗi CHƯA chạy và cả transaction đã rollback,
                    # cache của mọi kết nối trong pool đã bị xoá -> chạy lại 1 lần an
                    # toàn kể cả với hàm không idempotent.
                    result = await fn(*args, **kwargs)
                except _CONNECTION_ERRORS as exc:
                    # TimeoutError là lớp con của OSError (Python 3.11+): câu lệnh
                    # chạy quá command_timeout KHÔNG phải mất kết nối - đóng cả pool
                    # rồi chạy lại chỉ nhân đôi thời gian chờ.
                    if isinstance(exc, DatabaseUnavailableError) or _is_command_timeout(exc):
                        raise
                    await _reset_pool(_pool_in_use.get())
                    if not _safe_to_retry(exc, idempotent):
                        raise
                    try:
                        result = await fn(*args, **kwargs)
                    except _CONNECTION_ERRORS as retry_exc:
                        if not _is_command_timeout(retry_exc):
                            _breaker_trip(retry_exc)
                        raise
            except _CONNECTION_ERRORS as exc:
                if probe and not _is_command_timeout(exc):
                    _breaker_trip(exc)
                elif probe:
                    _breaker_ok()
                raise
            except Exception:
                # Lỗi SQL/dữ liệu: DB vẫn trả lời được -> breaker đóng.
                if probe:
                    _breaker_ok()
                raise
            finally:
                _pool_in_use.reset(token)
                _in_db_call.reset(nested_token)
                if probe:
                    # Lệnh thăm dò bị huỷ (CancelledError) giữa chừng: nhả cờ để
                    # lệnh sau được thăm dò lại, không kẹt breaker mãi mãi.
                    _release_probe()
            if probe or (_breaker_open_until and not nested):
                _breaker_ok()
            return result

        return wrapper

    return decorate(func) if func is not None else decorate


# Tên công khai cho các repository ngoài module này (idempotency, webhook inbox,
# zalo_users...) dùng chung đúng cơ chế retry an toàn ở trên.
with_reconnect = _with_reconnect


def _ssl_setting():
    """Giá trị ``ssl=`` cho asyncpg theo DATABASE_SSL_MODE.

    "require": như libpq - mã hoá đường truyền, không xác thực chứng chỉ (dễ
    cấu hình, chống nghe lén thụ động nhưng không chống MITM có chứng chỉ giả).
    "verify-full": SSLContext xác thực chứng chỉ server + hostname, dùng CA hệ
    thống hoặc DATABASE_SSL_CA. "disable": không SSL (chỉ cho DB local/test)."""
    mode = config.DATABASE_SSL_MODE
    if mode == "disable":
        return False
    if mode in ("verify-full", "verify-ca"):
        import ssl

        ctx = ssl.create_default_context(cafile=config.DATABASE_SSL_CA)
        ctx.check_hostname = mode == "verify-full"
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    if mode != "require":
        logger.warning("DATABASE_SSL_MODE=%r không hợp lệ, dùng 'require'.", mode)
    return "require"


def _dsn_port(dsn: str) -> Optional[int]:
    """Cổng ghi trong DATABASE_URL; None nếu không ghi/không đọc được (DSN lạ thì đặt
    DB_STATEMENT_CACHE_SIZE tường minh)."""
    try:
        return urlsplit(dsn).port
    except ValueError:
        return None


def statement_cache_size() -> int:
    """``statement_cache_size`` của asyncpg: DB_STATEMENT_CACHE_SIZE nếu đặt, không thì 0
    cho cổng 6543 (transaction pooler - prepared statement không sống qua lượt sau) và
    mặc định asyncpg cho cổng khác. README bắt buộc Session Pooler (5432, migration cần
    advisory lock theo session) nên mặc định bật cache: mỗi câu lệnh 1 vòng thay vì 2."""
    if config.DB_STATEMENT_CACHE_SIZE is not None:
        return config.DB_STATEMENT_CACHE_SIZE
    if _dsn_port(config.DATABASE_URL) == _TRANSACTION_POOLER_PORT:
        return 0
    return _DEFAULT_STATEMENT_CACHE


# ─── Đo thời gian chờ mượn kết nối (pool_stats, /status) ────────────────────
# Chỉ giữ vài con số (không giữ danh sách) nên không phình RAM dù chạy lâu.
_SLOW_ACQUIRE_SEC = 1.0
_SLOW_ACQUIRE_LOG_EVERY_SEC = 60.0


class _AcquireStats:
    __slots__ = (
        "acquires",
        "exhausted",
        "slow",
        "max_wait",
        "last_slow_at",
        "last_log",
        "unlogged",
    )

    def __init__(self) -> None:
        self.acquires = 0  # số lần mượn kết nối
        self.exhausted = 0  # số lần mượn lúc mọi kết nối đều đang bận (phải chờ người khác trả)
        self.slow = 0  # số lần chờ > _SLOW_ACQUIRE_SEC (gồm cả mở kết nối mới)
        self.max_wait = 0.0
        self.last_slow_at: Optional[float] = None  # time.time() lần chờ chậm gần nhất
        self.last_log = float("-inf")
        self.unlogged = 0


_acquire_stats = _AcquireStats()


def _record_acquire(pool, waited: float, exhausted: bool) -> None:
    stats = _acquire_stats
    stats.acquires += 1
    stats.exhausted += exhausted
    stats.max_wait = max(stats.max_wait, waited)
    if waited <= _SLOW_ACQUIRE_SEC:
        return
    stats.slow += 1
    stats.last_slow_at = time.time()
    now = time.monotonic()
    if now - stats.last_log < _SLOW_ACQUIRE_LOG_EVERY_SEC:
        stats.unlogged += 1  # tối đa 1 dòng log/phút khi pool nghẽn
        return
    logger.warning(
        "Chờ mượn kết nối DB %.1f s (%s, pool %s/%s kết nối, %s rảnh; +%s lần chờ chậm chưa log).",
        waited,
        "mọi kết nối đang bận" if exhausted else "mở kết nối mới",
        pool.get_size(),
        pool.get_max_size(),
        pool.get_idle_size(),
        stats.unlogged,
    )
    stats.last_log, stats.unlogged = now, 0


class _MeteredPool(asyncpg.pool.Pool):
    """Pool asyncpg + đo thời gian mượn kết nối. ``Pool.fetch/execute/acquire``... đều
    đi qua ``_acquire(timeout)`` (asyncpg ghim ==0.29.0; test/test_db.py khẳng định hàm
    này còn đúng chữ ký - nâng asyncpg mà đổi thì test báo ngay)."""

    __slots__ = ()

    async def _acquire(self, acquire_timeout):  # asyncpg gọi theo vị trí: _acquire(timeout)
        exhausted = self.get_size() - self.get_idle_size() >= self.get_max_size()
        started = time.monotonic()
        try:
            return await super()._acquire(acquire_timeout)
        finally:
            _record_acquire(self, time.monotonic() - started, exhausted)


def pool_stats() -> dict:
    """Số liệu pool kết nối DB (không chạm DB, không tạo pool) - /status hiển thị.

    - ``ready``: đã có pool chưa; ``size``/``idle``/``in_use``: số kết nối đang mở /
      đang rảnh / đang bị mượn (None khi chưa có pool); ``min``/``max``: cấu hình;
      ``statement_cache``: số prepared statement cache mỗi kết nối.
    - Từ lúc process chạy: ``acquires`` số lần mượn kết nối, ``exhausted`` số lần mượn
      lúc mọi kết nối đều bận, ``slow_waits`` số lần chờ > 1 s (kể cả mở kết nối mới),
      ``max_wait_ms`` lần chờ lâu nhất, ``last_slow_wait_at`` epoch giây (UTC) lần chờ
      > 1 s gần nhất (None nếu chưa có).
    """
    pool = _pool
    sized = pool is not None and hasattr(pool, "get_size") and not _is_closing(pool)
    size = pool.get_size() if sized else None
    idle = pool.get_idle_size() if sized else None
    stats = _acquire_stats
    return {
        "ready": pool is not None,
        "size": size,
        "idle": idle,
        "in_use": size - idle if size is not None and idle is not None else None,
        "min": config.DB_POOL_MIN_SIZE,
        "max": config.DB_POOL_MAX_SIZE,
        "statement_cache": statement_cache_size(),
        "acquires": stats.acquires,
        "exhausted": stats.exhausted,
        "slow_waits": stats.slow,
        "max_wait_ms": round(stats.max_wait * 1000),
        "last_slow_wait_at": stats.last_slow_at,
    }


async def get_pool() -> asyncpg.pool.Pool:
    pool = await _get_or_create_pool()
    _pool_in_use.set(pool)
    return pool


async def _get_or_create_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            try:
                _pool = await _create_pool()
            except TimeoutError as exc:
                # Mở kết nối quá DB_CONNECT_TIMEOUT_SEC: DB treo/mất mạng ->
                # mở breaker ngay (không đợi thêm 1 lần timeout nữa).
                _breaker_trip(exc)
                raise DatabaseUnavailableError(
                    f"Không mở được kết nối DB trong {config.DB_CONNECT_TIMEOUT_SEC}s"
                ) from exc
    return _pool


async def _create_pool() -> asyncpg.pool.Pool:
    pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN_SIZE,
        max_size=config.DB_POOL_MAX_SIZE,
        # Trần thời gian mở 1 kết nối (asyncpg mặc định 60s).
        timeout=config.DB_CONNECT_TIMEOUT_SEC,
        # Đóng connection rảnh sau 60s: Supavisor/Supabase hay cắt
        # connection idle, giữ lại chỉ tạo lỗi "connection is closed" ở
        # lượt kế tiếp (và tốn RAM vô ích).
        max_inactive_connection_lifetime=60,
        command_timeout=30,
        ssl=_ssl_setting(),
        statement_cache_size=statement_cache_size(),
    )
    if type(pool) is asyncpg.pool.Pool:
        # Cùng bố cục bộ nhớ (__slots__ rỗng) nên đổi lớp tại chỗ được: giữ nguyên
        # asyncpg.create_pool (test thay được) mà vẫn đo được thời gian mượn kết nối.
        pool.__class__ = _MeteredPool
    return pool


async def close_pool(grace_sec: float = _CLOSE_TIMEOUT_SEC) -> None:
    """Đóng pool lúc tắt app. ``Pool.close()`` chờ MỌI kết nối đang mượn trả về
    (VACUUM FULL tới 600 s, migration...) trong khi supervisord chỉ chờ 40 s: quá
    ``grace_sec`` giây thì cắt hẳn các kết nối (terminate) để tắt kịp, sạch."""
    global _pool
    pool = _pool
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), grace_sec)
    except Exception as exc:
        logger.warning(
            "Đóng pool DB không xong trong %ss (%s) - cắt hẳn các kết nối.",
            grace_sec,
            type(exc).__name__,
        )
        terminate = getattr(pool, "terminate", None)
        if callable(terminate):
            terminate()
    finally:
        if _pool is pool:
            _pool = None
