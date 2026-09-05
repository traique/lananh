import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import channel_command_service as service


@pytest.mark.asyncio
async def test_help_command():
    messages, provider = await service.maybe_handle_command(1, "/help")
    assert "/gia" in messages[0]
    assert "/tongket" in messages[0]
    assert provider is None


@pytest.mark.asyncio
async def test_non_command_is_not_handled():
    assert await service.maybe_handle_command(1, "xin chào") is None


@pytest.mark.asyncio
async def test_status_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/status", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_userouter9_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/userouter9", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_router9_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/router9 off", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_router9_toggle_on_off(monkeypatch):
    calls = []

    async def fake_set_enabled(enabled):
        calls.append(enabled)

    monkeypatch.setattr(service.orchestrator, "set_router9_enabled", fake_set_enabled)

    result, _ = await service.maybe_handle_command(1, "/router9 off")
    assert "Đã tắt 9Router" in result[0]
    result, _ = await service.maybe_handle_command(1, "/router9 on")
    assert "Đã bật 9Router" in result[0]
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_model_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/model auto", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_thongke_denied_for_non_admin():
    result, provider = await service.maybe_handle_command(1, "/thongke", is_admin=False)
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_thongke_admin_plain_text_no_html(monkeypatch):
    """Zalo/Zoom chỉ hiển thị plain text (channels/zalo_text.py) - kết quả
    /thongke qua kênh này KHÔNG được chứa thẻ HTML như <b>."""
    tc = service.telegram_commands

    async def fake_usage_by_user(since_hours):
        return [{
            "channel": "zalo", "telegram_user_id": -1, "calls": 3, "last_call_at": None,
        }]

    async def fake_usage_by_model(since_hours):
        return [{"provider": "groq", "model": "llama", "calls": 5, "last_call_at": None}]

    async def fake_list_users():
        return []

    monkeypatch.setattr(tc.db, "usage_by_user", fake_usage_by_user)
    monkeypatch.setattr(tc.db, "usage_by_model", fake_usage_by_model)
    monkeypatch.setattr(tc.zalo_users, "list_users", fake_list_users)

    result, provider = await service.maybe_handle_command(1, "/thongke", is_admin=True)
    text = result[0]
    assert "<b>" not in text
    assert "3" in text and "groq/llama" in text


@pytest.mark.asyncio
async def test_thongke_hours_parsing():
    assert service.telegram_commands._parse_thongke_hours("") == 24 * 7
    assert service.telegram_commands._parse_thongke_hours("3d") == 72
    assert service.telegram_commands._parse_thongke_hours("48") == 48
    assert service.telegram_commands._parse_thongke_hours("bậy bạ") == 24 * 7


@pytest.mark.asyncio
async def test_status_allowed_for_admin_by_default():
    result, provider = await service.maybe_handle_command(1, "/status")
    assert result != ["Lệnh này chỉ dành cho admin."]
    assert "Provider" in result[0]


@pytest.mark.asyncio
async def test_normal_commands_still_allowed_for_non_admin(monkeypatch):
    """Thành viên thường (is_admin=False) vẫn dùng được tính năng bình thường -
    chỉ 4 lệnh cấu hình toàn cục (/status, /userouter9, /router9, /model) mới bị chặn."""
    async def fake_clear(user_id):
        return None
    monkeypatch.setattr(service.orchestrator, "reset_chat", lambda: _noop())
    monkeypatch.setattr(service.db, "clear_chat", fake_clear)
    result, _ = await service.maybe_handle_command(1, "/reset", is_admin=False)
    assert "Đã xoá" in result[0]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_reset_clears_both_sessions(monkeypatch):
    called = []
    async def reset(): called.append("reset_chat")
    async def clear(user_id): called.append(("db", user_id))
    monkeypatch.setattr(service.orchestrator, "reset_chat", reset)
    monkeypatch.setattr(service.db, "clear_chat", clear)
    result, _ = await service.maybe_handle_command(7, "/reset")
    assert called == ["reset_chat", ("db", 7)]
    assert "Đã xoá" in result[0]


@pytest.mark.asyncio
async def test_dich_command_no_argument_returns_usage():
    result, provider = await service.maybe_handle_command(1, "/dich")
    assert "Cú pháp" in result[0]
    assert provider is None


@pytest.mark.asyncio
async def test_dich_command_translates_via_translate_service(monkeypatch):
    async def fake_translate(text, direction=None):
        class FakeResponse:
            used_fallback = False
        return "hello", "ja_vi", FakeResponse()

    # telemetry.start -> db.save_prompt cần Postgres thật; test này chỉ kiểm
    # tra luồng translate nên mock telemetry để chạy được ở máy không có DB.
    async def fake_telemetry_start(user_id, action_type, prompt_text, channel="telegram"):
        return 1

    async def fake_telemetry_success(prompt_id, action_type, content_text):
        return None

    async def fake_telemetry_failure(prompt_id, action_type, error):
        return None

    monkeypatch.setattr(service.telemetry, "start", fake_telemetry_start)
    monkeypatch.setattr(service.telemetry, "success", fake_telemetry_success)
    monkeypatch.setattr(service.telemetry, "failure", fake_telemetry_failure)
    monkeypatch.setattr(service.translate_service, "translate", fake_translate)
    result, provider = await service.maybe_handle_command(1, "/dich こんにちは")
    assert "hello" in result[0]
    assert "Tiếng Nhật" in result[0]
    assert provider is None


@pytest.mark.asyncio
async def test_dich_tieng_anh_khong_doan_chieu_hoi_lai():
    # Text Latin thuần không dấu tiếng Việt -> KHÔNG dịch mò "Việt→Nhật",
    # trả lời hỏi lại chiều (trước đây bị dịch ngược từ nguyên văn tiếng Anh).
    result, provider = await service.maybe_handle_command(1, "/dich Could you check the report tomorrow?")
    assert any("ja>vi" in r for r in result)
    assert provider is None


@pytest.mark.asyncio
async def test_dich_tieng_anh_co_chi_dinh_chieu_van_dich(monkeypatch):
    async def fake_translate(text, direction=None):
        class FakeResponse:
            used_fallback = False
        return "report", direction, FakeResponse()

    async def fake_telemetry_start(user_id, action_type, prompt_text, channel="telegram"):
        return 1

    monkeypatch.setattr(service.telemetry, "start", fake_telemetry_start)
    monkeypatch.setattr(service.translate_service, "translate", fake_translate)
    result, provider = await service.maybe_handle_command(1, "/dich ja>vi Could you check the report?")
    assert any("report" in r for r in result)
    assert provider is None


# ─── /anh (tạo ảnh, Agnes AI) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anh_command_khong_co_mo_ta_tra_ve_trang_thai(monkeypatch):
    async def fake_get_enabled():
        return True

    monkeypatch.setattr(service.agnes_client, "get_enabled", fake_get_enabled)

    result, provider = await service.maybe_handle_command(1, "/anh")

    assert "BẬT" in result[0]
    assert provider is None
    assert service.take_pending_image() is None


@pytest.mark.asyncio
async def test_anh_command_on_off_chi_admin(monkeypatch):
    called = []

    async def fake_set_enabled(v):
        called.append(v)

    monkeypatch.setattr(service.agnes_client, "set_enabled", fake_set_enabled)

    result, provider = await service.maybe_handle_command(1, "/anh off", is_admin=False)
    assert "chỉ dành cho admin" in result[0]
    assert called == []

    result, provider = await service.maybe_handle_command(1, "/anh on", is_admin=True)
    assert called == [True]
    assert "bật" in result[0].lower()


@pytest.mark.asyncio
async def test_anh_command_tao_anh_thanh_cong_dua_qua_pending_image(monkeypatch):
    async def fake_generate_image(prompt):
        assert prompt == "mèo phi hành gia"
        return service.agnes_client.GeneratedImage(
            data=b"\x89PNG\r\n\x1a\nfakebytes", url="https://cdn.agnes-ai.com/x.png"
        )

    monkeypatch.setattr(service.agnes_client, "generate_image", fake_generate_image)

    result, provider = await service.maybe_handle_command(1, "/anh mèo phi hành gia")

    # Tạo ảnh thành công KHÔNG kèm caption text - chỉ gửi ảnh qua
    # pending_image, danh sách tin nhắn rỗng (đúng hành vi hiện tại của
    # _generate_image()/handle_channel_text(), xem services/channel_command_service.py).
    assert result == []
    assert provider is None

    image_b64 = service.take_pending_image()
    assert image_b64 is not None
    import base64
    assert base64.b64decode(image_b64) == b"\x89PNG\r\n\x1a\nfakebytes"
    # Lấy 1 lần là hết - lần 2 phải None (tránh gửi lặp ảnh cũ cho request khác).
    assert service.take_pending_image() is None
    # Zalo dùng base64, KHÔNG được đụng tới pending_image_url (dành cho Zoom).
    assert service.take_pending_image_url() is None


@pytest.mark.asyncio
async def test_anh_command_tren_zoom_dung_url_khong_dung_base64(monkeypatch):
    async def fake_generate_image(prompt):
        return service.agnes_client.GeneratedImage(
            data=b"\x89PNG\r\n\x1a\nfakebytes", url="https://cdn.agnes-ai.com/x.png"
        )

    monkeypatch.setattr(service.agnes_client, "generate_image", fake_generate_image)

    result, provider = await service.maybe_handle_command(1, "/anh mèo", channel="zoom")

    # Cùng quy tắc: chỉ gửi ảnh, không kèm caption text.
    assert result == []
    assert service.take_pending_image_url() == "https://cdn.agnes-ai.com/x.png"
    # Zoom dùng URL, KHÔNG được đụng tới pending_image (base64, dành cho Zalo).
    assert service.take_pending_image() is None


@pytest.mark.asyncio
async def test_anh_command_loi_agnes_khong_dat_pending_image(monkeypatch):
    async def fake_generate_image_raise(prompt):
        raise service.agnes_client.AgnesError("Chưa cấu hình AGNES_API_KEY")

    monkeypatch.setattr(service.agnes_client, "generate_image", fake_generate_image_raise)

    result, provider = await service.maybe_handle_command(1, "/anh mèo")

    assert "Không tạo được ảnh" in result[0]
    assert service.take_pending_image() is None


@pytest.mark.asyncio
async def test_channel_chat_service_gan_image_b64_vao_channel_result(monkeypatch):
    """Kiểm tra dây chuyền đầy đủ: maybe_handle_command() đặt pending image ->
    channel_chat_service.handle_channel_text() phải lấy và gắn vào
    ChannelResult.image_b64 - đây là field mà channels/router.py forward sang
    ZaloMessageResponse cho gateway."""
    from services import channel_chat_service

    async def fake_generate_image(prompt):
        return service.agnes_client.GeneratedImage(data=b"\x89PNG\r\n\x1a\nfakebytes", url=None)

    monkeypatch.setattr(service.agnes_client, "generate_image", fake_generate_image)

    result = await channel_chat_service.handle_channel_text(1, "/anh test", channel="zalo")

    assert result.image_b64 is not None
    import base64
    assert base64.b64decode(result.image_b64) == b"\x89PNG\r\n\x1a\nfakebytes"
    assert result.image_url is None


@pytest.mark.asyncio
async def test_channel_chat_service_gan_image_url_vao_channel_result_cho_zoom(monkeypatch):
    from services import channel_chat_service

    async def fake_generate_image(prompt):
        return service.agnes_client.GeneratedImage(
            data=b"\x89PNG\r\n\x1a\nfakebytes", url="https://cdn.agnes-ai.com/y.png"
        )

    monkeypatch.setattr(service.agnes_client, "generate_image", fake_generate_image)

    result = await channel_chat_service.handle_channel_text(1, "/anh test", channel="zoom")

    assert result.image_url == "https://cdn.agnes-ai.com/y.png"
    assert result.image_b64 is None
