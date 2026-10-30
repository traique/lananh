"""Cách xưng hô riêng từng thành viên Zalo: core/address.py, persona, lệnh
/zalopair, router -> chat."""

import pytest

from core import address, config


def test_normalize_chi_nhan_danh_sach_dong():
    assert address.normalize("Chị") == "chị"
    assert address.normalize(" cô ") == "cô"
    assert address.normalize("sếp") is None
    assert address.normalize("") is None and address.normalize(None) is None
    assert address.self_word("cô") == "cháu" and address.self_word("em") == "chị"
    assert address.self_word("bạn") == "mình"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mẹ cô", ("Mẹ", "cô")),
        ("chị", ("", "chị")),
        ("Lan chị", ("Lan", "chị")),
        # Tên riêng viết hoa không bị cắt nhầm thành cách xưng hô.
        ("Tuấn Anh", ("Tuấn Anh", None)),
        ("Mai Chi", ("Mai Chi", None)),
        ("", ("", None)),
    ],
)
def test_tach_cach_xung_ho_cuoi_ten(text, expected):
    assert address.split_trailing_form(text) == expected


def _render(**kwargs) -> str:
    config._chat_skill_cache.clear()
    try:
        return config.load_chat_skill(**kwargs)
    finally:
        config._chat_skill_cache.clear()


def test_persona_thanh_vien_dung_cach_goi_rieng():
    member = _render(variant=config.PERSONA_MEMBER, address_form="cô")
    flat = " ".join(member.split())
    assert 'luôn xưng "cháu" và gọi người đang trò chuyện là "cô"' in flat
    # Ví dụ few-shot viết với "anh" chỉ để minh hoạ.
    assert 'luôn đổi thành "cô"/"cháu"' in flat
    admin = " ".join(_render(variant=config.PERSONA_ZALO_ADMIN, address_form="anh").split())
    assert 'gọi người đang trò chuyện là "anh"' in admin and "chỉ để minh hoạ" not in admin


def test_persona_mac_dinh_zalo_theo_tri_nho_neu_co_ghi_xung_ho():
    member = " ".join(_render(variant=config.PERSONA_MEMBER).split())
    assert "trí nhớ riêng của người này có ghi cách xưng hô" in member


def test_persona_chu_bot_khong_doi_theo_cach_goi_rieng():
    owner = _render()
    assert _render(address_form="chị") == owner
    assert "trí nhớ riêng của người này có ghi cách xưng hô" not in owner
    # Giá trị ngoài danh sách bị bỏ qua (không phình cache, không lọt vào prompt).
    assert _render(variant=config.PERSONA_MEMBER, address_form="sếp") == _render(
        variant=config.PERSONA_MEMBER
    )


def test_cache_persona_co_tran_theo_danh_sach_xung_ho():
    config._chat_skill_cache.clear()
    try:
        for variant in config.PERSONA_VARIANTS:
            for form in (*address.ADDRESS_FORMS, None, "lạ", "sếp"):
                for stock in (True, False):
                    config.load_chat_skill(variant=variant, address_form=form, stock=stock)
        parts = [key[0] for key in config._chat_skill_cache]
        assert parts.count(config._PART_CORE) <= 1 + 2 * (len(address.ADDRESS_FORMS) + 1)
        # Phần cổ phiếu không nhân theo biến thể hay cách xưng hô.
        assert parts.count(config._PART_STOCK) == 1
    finally:
        config._chat_skill_cache.clear()


# ─── /zalopair, /zaloadmin ──────────────────────────────────────────────────


class _Message:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _Update:
    def __init__(self, user_id=42):
        self.message = _Message()
        self.effective_message = self.message
        self.effective_user = type("U", (), {"id": user_id, "username": "owner"})()
        self.effective_chat = type("C", (), {"id": user_id})()


class _Context:
    def __init__(self, args):
        self.args = args


@pytest.mark.asyncio
async def test_zalopair_luu_cach_goi_va_bao_lai_chu_bot(monkeypatch):
    from channels import zalo_users
    from handlers import pairing_commands

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 42)
    seen = {}

    async def fake_pair(external_id, display_name="", *, admin=False, address_form=None):
        from channels import zalo_onboarding

        seen.update(id=external_id, name=display_name, form=address_form)
        user = zalo_users.ZaloUser(external_id, -7, display_name, "user", "active", address_form)
        return zalo_onboarding.PairResult(user, False, False, zalo_onboarding.WELCOME_NONE)

    monkeypatch.setattr(pairing_commands.zalo_onboarding, "pair", fake_pair)
    update = _Update()
    await pairing_commands.zalopair_cmd(update, _Context(["123", "Mẹ", "cô"]))

    assert seen == {"id": "123", "name": "Mẹ", "form": "cô"}
    assert "THÀNH VIÊN" in update.message.replies[0]
    assert "gọi người này là “cô”, xưng “cháu”" in update.message.replies[0]

    update = _Update()
    await pairing_commands.zalopair_cmd(update, _Context(["124", "Tuấn", "Anh"]))
    assert seen == {"id": "124", "name": "Tuấn Anh", "form": None}
    assert "gọi người này" not in update.message.replies[0]


# ─── Router Zalo -> chat: cách gọi riêng đi theo lượt ───────────────────────


@pytest.mark.asyncio
async def test_router_gan_cach_goi_rieng_cho_luot_chat(monkeypatch):
    from channels import router as zalo_router
    from channels import zalo_users
    from channels.contracts import ZaloMessageRequest
    from services import reply_audience
    from services.channel_result import ChannelResult

    class _NoLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def no_cache(*args, **kwargs):
        return None

    async def fake_resolve(external_id):
        return zalo_users.ZaloUser(external_id, -9, "Mẹ", "user", "active", "cô")

    seen = []

    async def fake_chat(user_id, text, is_admin=True):
        seen.append(reply_audience.current().address_form)
        return ChannelResult(["dạ"])

    monkeypatch.setattr(zalo_router, "_secret", lambda: "s3cr3t")
    monkeypatch.setattr(zalo_router, "assistant_turn", lambda *_a, **_k: _NoLock())
    monkeypatch.setattr(zalo_router.idempotency, "get_zalo_response", no_cache)
    monkeypatch.setattr(zalo_router.idempotency, "save_zalo_response", no_cache)
    monkeypatch.setattr(zalo_router.zalo_users, "resolve", fake_resolve)
    monkeypatch.setattr(zalo_router, "handle_channel_text", fake_chat)

    payload = ZaloMessageRequest(
        account_id="acc", sender_id="z", conversation_id="z", message_id="m", text="chào"
    )
    response = await zalo_router.receive(payload, "s3cr3t")

    assert response.messages == ["dạ"] and seen == ["cô"]
    assert reply_audience.current().address_form is None  # không rò sang lượt khác


@pytest.mark.asyncio
async def test_handle_channel_text_truyen_cach_goi_xuong_orchestrator(monkeypatch):
    from services import channel_chat_service as ccs
    from services import reply_audience

    seen = []

    async def fake_turn(user_id, text, **kwargs):
        seen.append(kwargs["address_form"])
        return ccs.ChatTurn("dạ")

    async def nothing(*args, **kwargs):
        return None

    async def no_stock(user_id, text, is_admin=True):
        return None, ""

    monkeypatch.setattr(ccs.portfolio_service, "handle_command", nothing)
    monkeypatch.setattr(ccs.portfolio_service, "maybe_handle_natural_language", nothing)
    monkeypatch.setattr(ccs, "_handle_stock", no_stock)
    monkeypatch.setattr(ccs, "run_chat_turn", fake_turn)

    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        await ccs.handle_channel_text(-5, "chào em", is_admin=False)
    await ccs.handle_channel_text(-5, "chào em", is_admin=False)
    assert seen == ["chị", None]


def test_check_cua_migration_khop_danh_sach_trong_code():
    import re
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1] / "migrations" / "024_zalo_address_form.sql"
    ).read_text(encoding="utf-8")
    allowed = re.search(r"address_form IN \(([^)]*)\)", sql).group(1)
    assert set(re.findall(r"'([^']+)'", allowed)) == set(address.ADDRESS_FORMS)
