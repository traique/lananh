"""Quota ngày của thành viên Zalo (services/usage_quota.py): trần lượt + lượt nặng,
câu báo giọng Lan Anh tối đa 1 lần/giờ, fail-open khi DB lỗi, cổng chặn trong
dispatch (sau cache, trước khoá lượt) và route ảnh/thoại/file, hết lượt nặng thì
chat không tra web, /admin sửa trần."""

import asyncio
from types import SimpleNamespace

import pytest

import test_channel_chat_service as _chat
import test_web_admin as _admin
import test_zalo_gateway_contract as _gateway
import web_admin
from core import database as db
from core import settings_cache
from services import channel_dispatch, usage_quota
from services.channel_result import ChannelResult

client = _admin.client
settings_store = _admin.settings_store
sleeps = _admin.sleeps
chat_fakes = _chat.chat_fakes
notice_bridge = _gateway.notice_bridge


@pytest.fixture(autouse=True)
def quota_db(monkeypatch):
    """Bảng user_calls + settings giả: counts[user_id] = [lượt, lượt nặng]."""
    usage_quota.reset()
    state = {"counts": {}, "queries": 0, "settings": {}, "relabel": [], "records": []}

    async def since(channel, user_id, start, heavy_kinds):
        assert channel == "zalo" and "chat_search" in heavy_kinds
        state["queries"] += 1
        turns, heavy = state["counts"].get(user_id, [0, 0])
        return turns, heavy

    async def get_setting(key):
        return state["settings"].get(key)

    async def set_setting(key, value):
        state["settings"][key] = value

    async def relabel(channel, user_id, from_kind, to_kind, start):
        state["relabel"].append((user_id, from_kind, to_kind))
        return True

    async def record(channel, user_id, kind):
        state["records"].append((channel, user_id, kind))

    monkeypatch.setattr(db, "user_calls_since", since)
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "relabel_last_user_call", relabel)
    monkeypatch.setattr(db, "record_user_call", record)
    monkeypatch.delenv("ZALO_MEMBER_DAILY_TURNS", raising=False)
    monkeypatch.delenv("ZALO_MEMBER_DAILY_HEAVY", raising=False)
    yield state
    usage_quota.reset()


def _member(**kw):
    return {"channel": "zalo", "is_admin": False, **kw}


# ─── Trần, câu báo, fail-open ────────────────────────────────────────────────


def test_chi_ap_cho_thanh_vien_zalo():
    assert usage_quota.applies("zalo", False)
    assert not usage_quota.applies("zalo", True)
    assert not usage_quota.applies("zoom", False)
    assert not usage_quota.applies("telegram", False)


async def test_chu_bot_admin_zoom_khong_dem(quota_db):
    for kwargs in ({"channel": "zalo", "is_admin": True}, {"channel": "zoom", "is_admin": False}):
        assert (await usage_quota.check(-1, "chào", **kwargs)).allowed
    assert quota_db["queries"] == 0


async def test_het_luot_thi_bao_mot_lan_moi_gio(quota_db):
    quota_db["counts"][-7] = [40, 0]
    first = await usage_quota.check(-7, "chào em", address_form="chị", **_member())
    assert not first.allowed and first.reason == "turns"
    assert "0h đêm nay" in first.notice and "chị" in first.notice
    second = await usage_quota.check(-7, "alo", address_form="chị", **_member())
    assert not second.allowed and second.notice is None  # im lặng trong giờ đó


async def test_duoi_tran_cho_qua_va_cong_don_trong_cache(quota_db):
    quota_db["counts"][-8] = [38, 0]
    assert (await usage_quota.check(-8, "1", **_member())).allowed
    assert (await usage_quota.check(-8, "2", **_member())).allowed
    # Cache 60 giây đã cộng dồn 2 lượt vừa cho qua -> lượt thứ 3 chạm trần 40.
    assert not (await usage_quota.check(-8, "3", **_member())).allowed
    assert quota_db["queries"] == 1


async def test_het_luot_nang_chan_lenh_nang_chat_van_duoc_nhung_khong_tra_web(quota_db):
    quota_db["counts"][-9] = [10, 5]
    blocked = await usage_quota.check(-9, "/anh con mèo", **_member())
    assert not blocked.allowed and blocked.reason == "heavy" and "tạo ảnh" in blocked.notice
    tuvi = await usage_quota.check(-9, "/tuvi_chitiet 1/1/1990 9h nam", **_member())
    assert not tuvi.allowed and tuvi.notice is None  # câu báo "heavy" đã gửi trong giờ
    chat = await usage_quota.check(-9, "giá vàng hôm nay?", **_member())
    assert chat.allowed and not chat.search_allowed
    # /anh on|off hoặc /anh trống không phải lượt tạo ảnh.
    assert (await usage_quota.check(-9, "/anh", **_member())).allowed


def test_phan_loai_lenh_nang():
    assert usage_quota.heavy_kind("/anh một con mèo") == "image"
    assert usage_quota.heavy_kind("/ANH@lananh_bot phố cổ") == "image"
    assert usage_quota.heavy_kind("/anh off") is None
    assert usage_quota.heavy_kind("/kymon_chitiet đi xa") == "kymon_chitiet"
    assert usage_quota.heavy_kind("/tsh_chitiet 1/1/1990") == "tsh_chitiet"
    assert usage_quota.heavy_kind("/tuvi 1/1/1990") is None
    assert usage_quota.heavy_kind("vẽ giúp em con mèo") is None


async def test_db_loi_thi_cho_qua(quota_db, monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("db sập")

    monkeypatch.setattr(db, "user_calls_since", boom)
    decision = await usage_quota.check(-3, "chào", **_member())
    assert decision.allowed and decision.search_allowed


async def test_tran_0_la_khong_gioi_han(quota_db, monkeypatch):
    monkeypatch.setenv("ZALO_MEMBER_DAILY_TURNS", "0")
    monkeypatch.setenv("ZALO_MEMBER_DAILY_HEAVY", "0")
    quota_db["counts"][-4] = [999, 999]
    assert (await usage_quota.check(-4, "/anh mèo", **_member())).allowed
    assert quota_db["queries"] == 0


async def test_tran_tren_admin_ghi_de_env_va_ap_dung_ngay(quota_db, monkeypatch):
    monkeypatch.setenv("ZALO_MEMBER_DAILY_TURNS", "40")
    quota_db["counts"][-5] = [10, 0]
    assert (await usage_quota.check(-5, "a", **_member())).allowed
    limits = await usage_quota.set_caps(turns=5, heavy=0)
    assert (limits.turns, limits.heavy) == (5, 0)
    assert quota_db["settings"][usage_quota.TURNS_KEY] == "5"
    assert not (await usage_quota.check(-5, "b", **_member())).allowed


async def test_doc_settings_loi_thi_theo_env(monkeypatch):
    async def boom(key):
        raise ConnectionError("db")

    settings_cache.clear()
    monkeypatch.setattr(db, "get_setting", boom)
    monkeypatch.setenv("ZALO_MEMBER_DAILY_TURNS", "12")
    assert (await usage_quota.caps()).turns == 12


async def test_cache_va_nhat_ky_cau_bao_co_tran(quota_db):
    for i in range(usage_quota.MAX_CACHE + 20):
        await usage_quota.check(-1000 - i, "x", **_member())
    assert len(usage_quota._cache) == usage_quota.MAX_CACHE
    for i in range(usage_quota.MAX_NOTICES + 5):
        usage_quota._notice(-i, "turns", None, 1)
    assert len(usage_quota._notices) == usage_quota.MAX_NOTICES


async def test_tra_web_chi_danh_dau_trong_pham_vi_cua_dung_nguoi(quota_db):
    await usage_quota.note_web_search(-1)  # ngoài phạm vi quota: bỏ qua
    with usage_quota.scope(usage_quota.ALLOW, -1):  # chủ bot/admin: không đặt phạm vi
        await usage_quota.note_web_search(-1)
    assert quota_db["relabel"] == []
    with usage_quota.scope(usage_quota.Decision(True, applied=True), -1):
        await usage_quota.note_web_search(-2)  # người khác: bỏ qua
        await usage_quota.note_web_search(-1)
    assert quota_db["relabel"] == [(-1, "chat", "chat_search")]


# ─── Cổng chặn trong dispatch ────────────────────────────────────────────────


def _handlers(calls, cached=None):
    async def chat(user_id, text, is_admin):
        calls.append(("chat", text))
        return ChannelResult(["dạ"])

    async def get_cached(*args):
        return cached

    async def save_cached(account_id, event_id, kind, payload):
        calls.append(("save", payload["messages"]))

    def turn(key):
        calls.append(("turn", key))
        return _Noop()

    return channel_dispatch.DispatchHandlers(
        chat=chat, get_cached=get_cached, save_cached=save_cached, turn=turn
    )


class _Noop:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _request(text="chào em", is_admin=False):
    return channel_dispatch.DispatchRequest(
        account_id="acc",
        event_id="m1",
        cache_kind="text",
        user_id=-7,
        is_admin=is_admin,
        channel="zalo",
        text=text,
        turn_key="zalo:-7",
    )


async def test_dispatch_chan_thanh_vien_het_luot_truoc_khoa_luot(quota_db):
    quota_db["counts"][-7] = [40, 0]
    calls = []
    payload = await channel_dispatch.dispatch(_request(), _handlers(calls))
    assert len(payload["messages"]) == 1 and "0h đêm nay" in payload["messages"][0]
    # Không lấy khoá lượt, không gọi chat; câu báo được cache.
    assert [c[0] for c in calls] == ["save"]
    calls.clear()
    usage_quota._cache.clear()
    payload = await channel_dispatch.dispatch(_request(), _handlers(calls))
    assert payload["messages"] == []  # trong 1 giờ: im lặng (vẫn cache)


async def test_dispatch_tin_gui_lai_lay_cache_khong_dem(quota_db):
    calls = []
    cached = {"messages": ["câu cũ"], "provider": None, "image_b64": None, "image_url": None}
    assert await channel_dispatch.dispatch(_request(), _handlers(calls, cached)) == cached
    assert quota_db["queries"] == 0


async def test_dispatch_admin_va_lenh_chi_doc_khong_bi_chan(quota_db):
    quota_db["counts"][-7] = [999, 999]
    calls = []
    await channel_dispatch.dispatch(_request(is_admin=True), _handlers(calls))
    await channel_dispatch.dispatch(_request(text="/help"), _handlers(calls))
    assert [c for c in calls if c[0] == "chat"] == [("chat", "chào em"), ("chat", "/help")]
    assert quota_db["queries"] == 0


async def test_dispatch_dat_pham_vi_quota_cho_luot_chat(quota_db):
    quota_db["counts"][-7] = [3, 5]
    seen = {}

    async def chat(user_id, text, is_admin):
        seen["blocked"] = usage_quota.search_blocked()
        return ChannelResult(["dạ"])

    handlers = _handlers([])
    handlers = channel_dispatch.DispatchHandlers(
        chat=chat,
        get_cached=handlers.get_cached,
        save_cached=handlers.save_cached,
        turn=handlers.turn,
    )
    await channel_dispatch.dispatch(_request(), handlers)
    assert seen["blocked"] is True


# ─── run_chat_turn: hết lượt nặng thì không tra web ──────────────────────────


async def test_het_luot_nang_chat_khong_tra_web_va_kem_ghi_chu(chat_fakes, monkeypatch, quota_db):
    from services import channel_chat_service as ccs

    async def plan_forbidden(text, user_id=None):
        raise AssertionError("đã hết lượt tra web")

    seen = {}

    async def fake_chat(user_id, text, *, grounding, require_real_search, search_mode, **kw):
        seen.update(grounding=grounding, forced=require_real_search, mode=search_mode)
        return _chat._FakeResponse("dạ")

    async def daily_needs_search(text):
        # Chưa có số liệu vàng bằng code -> bình thường sẽ ép tra web thật.
        return ccs.vn_daily_chat.DailyGrounding(force_real_search=True)

    monkeypatch.setattr(ccs.web_search, "plan_search", plan_forbidden)
    monkeypatch.setattr(ccs.orchestrator, "chat", fake_chat)
    monkeypatch.setattr(ccs.vn_daily_chat, "grounding_for", daily_needs_search)
    with usage_quota.scope(usage_quota.Decision(True, search_allowed=False, applied=True), -9):
        await ccs.run_chat_turn(-9, "tin tức mới nhất hôm nay về giá vàng", channel="zalo")
    await ccs.stop_background_tasks()
    assert "hết lượt tra web" in seen["grounding"]
    assert seen["forced"] is False and seen["mode"] == "off"


async def test_luot_tra_web_cua_thanh_vien_duoc_danh_dau(chat_fakes, monkeypatch, quota_db):
    from services import channel_chat_service as ccs
    from services.web_search import SearchPlan

    async def tavily_plan(text, user_id=None):
        return SearchPlan("<<<web>>>", "off", True)

    monkeypatch.setattr(ccs.web_search, "plan_search", tavily_plan)
    with usage_quota.scope(usage_quota.Decision(True, applied=True), -9):
        await ccs.run_chat_turn(-9, "tin mới về bão", channel="zalo")
    await ccs.stop_background_tasks()
    assert quota_db["relabel"] == [(-9, "chat", "chat_search")]


# ─── Route ảnh/thoại/file Zalo, /anh, cổ phiếu ───────────────────────────────


async def test_route_media_chan_thanh_vien_het_luot(quota_db, monkeypatch):
    from channels import router

    saved = []

    async def no_cache(*args):
        return None

    async def save(account_id, message_id, kind, payload):
        saved.append((kind, payload["messages"]))

    monkeypatch.setattr(router.idempotency, "get_zalo_response", no_cache)
    monkeypatch.setattr(router.idempotency, "save_zalo_response", save)
    quota_db["counts"][-7] = [40, 0]
    member = SimpleNamespace(internal_user_id=-7, is_admin=False, address_form="cô")
    blocked, decision = await router.media_quota_gate(member, "acc", "m5", "voice")
    assert blocked is not None and "cháu" in blocked.messages[0]
    assert saved[0][0] == "voice" and not decision.allowed
    admin = SimpleNamespace(internal_user_id=-7, is_admin=True, address_form=None)
    assert await router.media_quota_gate(admin, "acc", "m6", "file") == (
        None,
        usage_quota.ALLOW,
    )
    quota_db["counts"][-8] = [1, 5]
    other = SimpleNamespace(internal_user_id=-8, is_admin=False, address_form=None)
    allowed, decision = await router.media_quota_gate(other, "acc", "m7", "file")
    assert allowed is None and decision.applied and not decision.search_allowed


async def test_xem_het_luot_khong_cong_don_va_fail_open(quota_db, monkeypatch):
    quota_db["counts"][-7] = [39, 0]
    assert not await usage_quota.turns_exhausted(-7, **_member())
    assert not await usage_quota.turns_exhausted(-7, **_member())
    # Xem 2 lần không cộng dồn: lượt thật thứ 40 vẫn được cho qua, rồi mới hết.
    assert (await usage_quota.check(-7, "ảnh", **_member())).allowed
    assert await usage_quota.turns_exhausted(-7, **_member())
    assert not await usage_quota.turns_exhausted(-7, channel="zalo", is_admin=True)
    assert usage_quota._notices == {}  # chỉ xem, không "tiêu" câu báo hết lượt

    async def boom(*args, **kwargs):
        raise ConnectionError("db sập")

    usage_quota.reset()
    monkeypatch.setattr(db, "user_calls_since", boom)
    assert not await usage_quota.turns_exhausted(-7, **_member())


async def test_cau_bao_anh_cua_gateway_im_lang_khi_thanh_vien_het_luot(
    quota_db, notice_bridge, monkeypatch
):
    """Gateway gửi "em nhận được ảnh rồi, đợi em xem xíu" TRƯỚC lượt ảnh (POST
    /internal/zalo/notice) rồi mới gọi route ảnh - route này bị quota chặn. Thành
    viên đã hết lượt thì /notice trả null để không hứa rồi im lặng; câu báo hết
    lượt vẫn do route ảnh trả (1 lần/giờ). Loại báo khác không đổi."""
    from channels import router

    async def no_cache(*args):
        return None

    async def save(*args):
        return None

    monkeypatch.setattr(router.idempotency, "get_zalo_response", no_cache)
    monkeypatch.setattr(router.idempotency, "save_zalo_response", save)
    quota_db["counts"][-7] = [39, 0]
    first = (await _gateway._post_notice({"sender_id": "chi-lan", "kind": "image"})).json()
    assert first["text"] and "chị" in first["text"]
    member = SimpleNamespace(internal_user_id=-7, is_admin=False, address_form="chị")
    allowed, decision = await router.media_quota_gate(member, "acc", "img-1", "image")
    assert allowed is None and decision.applied  # lượt thứ 40 chạy

    silent = await _gateway._post_notice({"sender_id": "chi-lan", "kind": "image"})
    assert silent.json() == {"text": None}
    blocked, decision = await router.media_quota_gate(member, "acc", "img-2", "image")
    assert blocked is not None and "chị" in blocked.messages[0] and not decision.allowed
    video = (await _gateway._post_notice({"sender_id": "chi-lan", "kind": "video"})).json()
    assert video["text"] and video["text"].startswith("🎬")
    assert notice_bridge == []


async def test_anh_ghi_luot_image(quota_db, monkeypatch):
    from services import channel_command_service as svc

    async def generate(prompt):
        return SimpleNamespace(data=b"png", url="https://img/1.png")

    monkeypatch.setattr(svc.agnes_client, "generate_image", generate)
    await svc.maybe_handle_command(-7, "/anh con mèo", is_admin=False, channel="zalo")
    svc.take_pending_image()
    assert quota_db["records"] == [("zalo", -7, "image")]


async def test_luot_co_phieu_ghi_user_calls(quota_db, monkeypatch):
    from services import channel_chat_service as ccs

    async def stock(user_id, text, is_admin=True):
        return ChannelResult(["FPT 120"]), ""

    async def none(*args, **kwargs):
        return None

    async def record(*args, **kwargs):
        return None

    monkeypatch.setattr(ccs, "_side_effect_paths", none)
    monkeypatch.setattr(ccs.article_reader, "maybe_handle", none)
    monkeypatch.setattr(ccs.vn_daily_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.weather_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs.lunar_chat, "maybe_handle", none)
    monkeypatch.setattr(ccs, "_handle_stock", stock)
    monkeypatch.setattr(ccs.conversation_log, "record_exchange", record)
    result = await ccs.handle_channel_text(-7, "giá FPT", is_admin=False, channel="zalo")
    assert result.messages == ["FPT 120"]
    assert quota_db["records"] == [("zalo", -7, "stock")]


# ─── /admin ──────────────────────────────────────────────────────────────────


async def test_admin_api_quota(client, monkeypatch, quota_db):
    async def overview():
        return {"turns": 40, "heavy": 5, "members": []}

    saved = {}

    async def set_caps(turns=None, heavy=None):
        saved.update(turns=turns, heavy=heavy)
        return usage_quota.Caps(turns if turns is not None else 40, heavy or 0)

    monkeypatch.setattr(web_admin.usage_quota, "today_overview", overview)
    monkeypatch.setattr(web_admin.usage_quota, "set_caps", set_caps)
    assert (await client.get("/admin/api/quota")).status_code == 403
    await _admin._login(client)
    assert (await client.get("/admin/api/quota")).json()["turns"] == 40
    ok = await client.post("/admin/api/quota", json={"turns": 20, "heavy": 0})
    assert ok.json() == {"turns": 20, "heavy": 0} and saved == {"turns": 20, "heavy": 0}
    for bad in ({}, {"turns": -1}, {"heavy": "nhiều"}, {"turns": 10**6}):
        assert (await client.post("/admin/api/quota", json=bad)).status_code == 400


async def test_tong_quan_hom_nay_chi_liet_ke_thanh_vien(monkeypatch):
    from channels import zalo_users

    async def users():
        return [
            SimpleNamespace(
                internal_user_id=-1, is_admin=True, display_name="Admin", external_id="a"
            ),
            SimpleNamespace(internal_user_id=-2, is_admin=False, display_name="", external_id="b"),
        ]

    async def by_user(channel, since, heavy_kinds, limit=100):
        return [{"telegram_user_id": -2, "turns": 7, "heavy": 1}]

    monkeypatch.setattr(zalo_users, "list_users", users)
    monkeypatch.setattr(db, "user_calls_by_user_since", by_user)
    overview = await usage_quota.today_overview()
    assert overview["members"] == [{"user_id": -2, "label": "b", "turns": 7, "heavy": 1}]
    assert overview["turns"] == 40 and overview["heavy"] == 5


def test_help_thanh_vien_neu_gioi_han_va_trang_admin_co_o_sua():
    from services import channel_command_service as svc

    assert "giới hạn" in svc.HELP_MEMBER
    html = (_admin.ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'id="quota-box"' in html and "/admin/api/quota" in html


async def test_check_chay_song_song_khong_loi(quota_db):
    results = await asyncio.gather(*(usage_quota.check(-20, str(i), **_member()) for i in range(5)))
    assert all(r.allowed for r in results)
