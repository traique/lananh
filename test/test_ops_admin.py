"""Admin API + lệnh Telegram của phần giám sát vận hành: /admin/api/watchdog
(cần đăng nhập) và /loi (chỉ chủ bot, plain text)."""

from types import SimpleNamespace

import pytest

import test_web_admin as _admin
import web_admin
from handlers import provider_commands

# Dùng lại fixture client/đăng nhập của test_web_admin.py.
client = _admin.client
settings_store = _admin.settings_store
sleeps = _admin.sleeps


async def test_watchdog_api_can_dang_nhap_va_tra_loi_va_restart(client, monkeypatch):
    async def restart_summary():
        return {"days": 7, "counts": {"deploy": 1}, "enabled": True}

    async def backup_status():
        raise RuntimeError("bảng backup_runs chưa có")

    monkeypatch.setattr(web_admin.watchdog, "restart_summary", restart_summary)
    monkeypatch.setattr(web_admin.backup_status, "status", backup_status)
    monkeypatch.setattr(web_admin.error_digest, "summary", lambda: {"today_total": 2, "today": []})
    assert (await client.get("/admin/api/watchdog")).status_code == 403
    await _admin._login(client)
    body = (await client.get("/admin/api/watchdog")).json()
    assert body["errors"]["today_total"] == 2
    assert body["restarts"]["counts"] == {"deploy": 1}
    assert body["backup"] == {"error": "RuntimeError"}  # 1 phần lỗi không hỏng cả trang


def test_trang_admin_co_the_giam_sat():
    html = (_admin.ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'id="watchdog-box"' in html and "/admin/api/watchdog" in html
    assert "['watchdog', loadWatchdog, 'watchdog-box']" in html


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


@pytest.mark.asyncio
async def test_lenh_loi_tra_plain_text_va_chi_chu_bot(monkeypatch):
    async def build_report():
        return "🐞 Lỗi hôm nay\n• 1× KeyError ở services/a_b.py:_f:3"

    monkeypatch.setattr(provider_commands.error_digest, "build_report", build_report)
    monkeypatch.setattr(provider_commands.common.config, "ALLOWED_USER_ID", 42)
    owner = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="owner"), message=_Message()
    )
    await provider_commands.loi_cmd(owner, None)
    text, kwargs = owner.message.replies[0]
    assert "services/a_b.py:_f:3" in text and "parse_mode" not in kwargs
    stranger = SimpleNamespace(
        effective_user=SimpleNamespace(id=7, username="x"), message=_Message()
    )
    await provider_commands.loi_cmd(stranger, None)
    assert stranger.message.replies == []
