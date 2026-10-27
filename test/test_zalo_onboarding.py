"""Lời chào khi pair Zalo lần đầu (channels/zalo_onboarding.py) và /help theo vai trò."""

import pytest

from channels import zalo_onboarding, zalo_users


@pytest.fixture
def fake_store(monkeypatch):
    users: dict[str, zalo_users.ZaloUser] = {}
    sent: list[tuple[str, str, str]] = []
    state = {"account": "acc-1", "fail": False}

    async def resolve(external_id):
        return users.get(external_id)

    async def _save(external_id, display_name, role, address_form):
        old = users.get(external_id)
        users[external_id] = zalo_users.ZaloUser(
            external_id,
            old.internal_user_id if old else -(len(users) + 1),
            display_name or (old.display_name if old else ""),
            role,
            "active",
            address_form or (old.address_form if old else None),
        )
        return users[external_id]

    async def pair(external_id, display_name="", *, address_form=None):
        old = users.get(external_id)
        return await _save(external_id, display_name, old.role if old else "user", address_form)

    async def pair_as_admin(external_id, display_name="", *, address_form=None):
        return await _save(external_id, display_name, "admin", address_form)

    async def load_account_id():
        return state["account"]

    async def enqueue_outbox(account_id, recipient_id, content):
        if state["fail"]:
            raise RuntimeError("DB lỗi")
        sent.append((account_id, recipient_id, content))

    monkeypatch.setattr(zalo_onboarding.zalo_users, "resolve", resolve)
    monkeypatch.setattr(zalo_onboarding.zalo_users, "pair", pair)
    monkeypatch.setattr(zalo_onboarding.zalo_users, "pair_as_admin", pair_as_admin)
    monkeypatch.setattr(zalo_onboarding.zalo_session, "load_account_id", load_account_id)
    monkeypatch.setattr(zalo_onboarding.zalo_repository, "enqueue_outbox", enqueue_outbox)
    monkeypatch.delenv("ZALO_WELCOME_ON_PAIR", raising=False)
    return users, sent, state


@pytest.mark.asyncio
async def test_chi_chao_lan_pair_dau_tien(fake_store):
    _, sent, _ = fake_store
    first = await zalo_onboarding.pair("z1", "Mẹ", address_form="cô")
    again = await zalo_onboarding.pair("z1", "Mẹ yêu")

    assert first.created and first.welcome == zalo_onboarding.WELCOME_QUEUED
    assert not again.created and again.welcome == zalo_onboarding.WELCOME_NONE
    assert len(sent) == 1
    account, recipient, text = sent[0]
    assert (account, recipient) == ("acc-1", "z1")
    assert "Cháu chào cô ạ" in text and "nha cô." in text
    assert "/help" in text and "gửi lại câu đó" in text
    assert "quản trị" not in text  # thành viên thường không thấy dòng admin


@pytest.mark.asyncio
async def test_nang_quyen_chi_bao_mot_cau_ngan(fake_store):
    _, sent, _ = fake_store
    await zalo_onboarding.pair("z2", "Lan", address_form="chị")
    promoted = await zalo_onboarding.pair("z2", "", admin=True)
    still_admin = await zalo_onboarding.pair("z2", "", admin=True)

    assert promoted.promoted and promoted.welcome == zalo_onboarding.WELCOME_QUEUED
    assert still_admin.welcome == zalo_onboarding.WELCOME_NONE
    assert len(sent) == 2
    assert "Em báo chị nè" in sent[1][2] and "quản trị" in sent[1][2]


@pytest.mark.asyncio
async def test_admin_moi_thay_quyen_quan_tri_trong_loi_chao(fake_store):
    _, sent, _ = fake_store
    result = await zalo_onboarding.pair("z3", "", admin=True)
    assert result.created and "/fb_*" in sent[0][2] and "Tài khoản này có quyền" in sent[0][2]


@pytest.mark.asyncio
async def test_chua_dat_cach_goi_thi_khong_goi_tron_anh(fake_store):
    _, sent, _ = fake_store
    await zalo_onboarding.pair("z4", "Hoa")
    text = sent[0][2]
    assert text.startswith("🌸 Xin chào! Em là Lan Anh")
    assert " anh" not in text.replace("Lan Anh", "")
    # Không quảng cáo tính năng chưa có: ảnh Zalo chỉ được viết thành prompt.
    assert "ảnh" not in text.lower()


@pytest.mark.asyncio
async def test_khong_dang_nhap_zalo_hoac_loi_db_khong_hong_viec_cap_quyen(fake_store):
    users, sent, state = fake_store
    state["account"] = ""
    no_account = await zalo_onboarding.pair("z5", "")
    state["account"], state["fail"] = "acc-1", True
    failed = await zalo_onboarding.pair("z6", "")

    assert no_account.welcome == zalo_onboarding.WELCOME_NO_ACCOUNT
    assert failed.welcome == zalo_onboarding.WELCOME_FAILED
    assert sent == [] and {"z5", "z6"} <= set(users)


@pytest.mark.asyncio
async def test_tat_loi_chao_bang_env(fake_store, monkeypatch):
    _, sent, _ = fake_store
    monkeypatch.setenv("ZALO_WELCOME_ON_PAIR", "0")
    result = await zalo_onboarding.pair("z7", "")
    assert result.created and result.welcome == zalo_onboarding.WELCOME_DISABLED and sent == []


@pytest.mark.asyncio
async def test_zalopair_bao_chu_bot_trang_thai_loi_chao(fake_store, monkeypatch):
    from core import config
    from handlers import pairing_commands

    class _Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)

    class _Update:
        def __init__(self):
            self.message = _Message()
            self.effective_message = self.message
            self.effective_user = type("U", (), {"id": 42, "username": "owner"})()
            self.effective_chat = type("C", (), {"id": 42})()

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    update = _Update()
    await pairing_commands.zalopair_cmd(update, type("Ctx", (), {"args": ["z8", "Mẹ", "cô"]})())
    assert "💌 Đã xếp lời chào" in update.message.replies[0]
    update = _Update()
    await pairing_commands.zaloadmin_cmd(update, type("Ctx", (), {"args": ["z8"]})())
    assert "câu báo nâng quyền" in update.message.replies[0]


# ─── /help theo vai trò ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_thanh_vien_khong_thay_lenh_admin_va_fb():
    from services import channel_command_service as ccs

    member, _ = await ccs.maybe_handle_command(-5, "/help all", is_admin=False)
    admin, _ = await ccs.maybe_handle_command(-6, "/help all", is_admin=True)
    start, _ = await ccs.maybe_handle_command(-5, "/start", is_admin=False)
    short, _ = await ccs.maybe_handle_command(-5, "/help", is_admin=False)

    assert member == [ccs.HELP_MEMBER]
    assert admin == [ccs.HELP_ADMIN]
    # /help, /start: bản ngắn "cứ nhắn tự nhiên" - thành viên không thấy chủ đề admin.
    assert start == short and "nhắn tự nhiên" in short[0] and "/help all" in short[0]
    assert "admin" not in short[0] and "nhóm" not in short[0]
    for hidden in ("Chỉ admin", "/fb_", "/status", "/rag", "/tavily", "/anh on|off"):
        assert hidden not in member[0], hidden
        assert hidden in admin[0], hidden
    for shared in ("/lich", "/kymon", "/danhmuc", "/dich", "/memory", "/help"):
        assert shared in member[0] and shared in admin[0]
