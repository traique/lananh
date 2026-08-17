"""Unit test cho handlers/common.py: authorization (check_access/restricted)
và services/telemetry.py (TelemetryService.failure - helper nhạy cảm về bảo
mật/riêng tư). Dùng fake Update/context nhẹ (chỉ có thuộc tính thực sự đọc
tới) thay vì mock toàn bộ python-telegram-bot.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core import database as db  # noqa: E402
from handlers import common  # noqa: E402
from services.telemetry import telemetry  # noqa: E402


class _FakeUser:
    def __init__(self, user_id: int, username: str = "someone"):
        self.id = user_id
        self.username = username


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, user_id: int | None):
        self.effective_user = _FakeUser(user_id) if user_id is not None else None
        self.effective_message = _FakeMessage()
        self.message = self.effective_message


# ─── check_access ─────────────────────────────────────────────────────────

def test_check_access_dung_user_duoc_phep(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    assert common.check_access(_FakeUpdate(42)) is True


def test_check_access_sai_user_bi_tu_choi(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    assert common.check_access(_FakeUpdate(999)) is False


def test_check_access_khong_co_effective_user(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    assert common.check_access(_FakeUpdate(None)) is False


# ─── restricted decorator ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restricted_chan_user_la_khong_goi_handler_that(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    handler_calls = []

    @common.restricted
    async def fake_handler(update, context):
        handler_calls.append(1)

    update = _FakeUpdate(999)  # user lạ
    await fake_handler(update, context=None)

    assert handler_calls == []  # handler thật KHÔNG được gọi
    assert update.effective_message.replies == []  # im lặng, không tiết lộ bot tồn tại cho người lạ


@pytest.mark.asyncio
async def test_restricted_cho_qua_user_dung(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    handler_calls = []

    @common.restricted
    async def fake_handler(update, context):
        handler_calls.append(1)

    update = _FakeUpdate(42)
    await fake_handler(update, context=None)

    assert handler_calls == [1]  # handler thật ĐƯỢC gọi
    assert update.effective_message.replies == []  # không có tin nhắn từ chối nào


# ─── extract_arg ──────────────────────────────────────────────────────────

class _FakeContext:
    def __init__(self, args):
        self.args = args


def test_extract_arg_noi_cac_tu():
    assert common.extract_arg(_FakeContext(["FPT", "phan", "tich"])) == "FPT phan tich"


def test_extract_arg_khong_co_args_tra_rong():
    assert common.extract_arg(_FakeContext([])) == ""
    assert common.extract_arg(_FakeContext(None)) == ""


# ─── TelemetryService.failure: chỉ lưu tên loại lỗi, KHÔNG lưu nội dung ────

@pytest.mark.asyncio
async def test_telemetry_failure_chi_luu_ten_loai_loi_khong_luu_noi_dung(monkeypatch):
    saved = {}

    async def fake_save_result(prompt_id, result_type, content_text=None, file_path=None):
        saved["prompt_id"] = prompt_id
        saved["result_type"] = result_type
        saved["content_text"] = content_text

    monkeypatch.setattr(db, "save_result", fake_save_result)

    secret_detail = "DATABASE_URL=postgresql://user:hunter2@host/db"
    await telemetry.failure(7, "chat", ValueError(secret_detail))

    assert saved["prompt_id"] == 7
    assert saved["result_type"] == "chat"
    assert saved["content_text"] == "error: ValueError"
    assert secret_detail not in saved["content_text"]


@pytest.mark.asyncio
async def test_telemetry_failure_khong_raise_khi_db_loi(monkeypatch):
    async def fake_save_result_raises(*args, **kwargs):
        raise ConnectionError("DB tạm thời không kết nối được")

    monkeypatch.setattr(db, "save_result", fake_save_result_raises)

    # Không được raise ra ngoài - đây là hàm xử lý lỗi, bản thân nó lỗi thì
    # không được phép làm crash luồng xử lý chính.
    await telemetry.failure(7, "chat", ValueError("x"))
