"""Hợp đồng Python <-> zalo-gateway (Node): danh sách lệnh làn nhanh, loại câu
báo, style zca-js, trần tin trích dẫn phải khớp hai bên; endpoint POST
/internal/zalo/notice dựng câu báo theo cách gọi riêng và im lặng với người lạ."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import httpx
import pytest
from fastapi import FastAPI

from channels import contracts
from services import media_text, turn_control

GATEWAY_SRC = Path(__file__).resolve().parent.parent / "zalo-gateway" / "src"
SECRET = "bridge-secret"


def _ts_string_array(file: str, name: str) -> set[str]:
    """Các chuỗi trong `export const NAME = [...]` của 1 file TypeScript."""
    source = (GATEWAY_SRC / file).read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert match, f"không thấy {name} trong {file}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_lenh_lam_nhanh_cua_gateway_khop_turn_control():
    assert _ts_string_array("commands.ts", "CANCEL_COMMANDS") == set(turn_control.CANCEL_COMMANDS)
    assert _ts_string_array("commands.ts", "READONLY_ANY_COMMANDS") == set(
        turn_control._READONLY_ANY
    )
    assert _ts_string_array("commands.ts", "READONLY_BARE_COMMANDS") == set(
        turn_control._READONLY_BARE
    )


def test_loai_cau_bao_khop_ca_hai_ben():
    source = (GATEWAY_SRC / "bridge.ts").read_text(encoding="utf-8")
    union = re.search(r"export type NoticeKind\s*=(.*?);", source, re.DOTALL)
    assert union
    gateway_kinds = set(re.findall(r'"([^"]+)"', union.group(1)))
    local = (GATEWAY_SRC / "notices.ts").read_text(encoding="utf-8")
    block = re.search(r"export const LOCAL_NOTICES[^=]*=\s*\{(.*?)\n\};", local, re.DOTALL)
    assert block
    local_kinds = set(re.findall(r"^  (\w+):", block.group(1), re.MULTILINE))
    assert gateway_kinds == set(get_args(contracts.ZaloNoticeKind))
    assert gateway_kinds == media_text.ZALO_NOTICE_KINDS
    assert local_kinds == gateway_kinds


def test_style_zca_js_va_tran_trich_dan_khop():
    assert _ts_string_array("types.ts", "ZALO_TEXT_STYLES") == set(
        get_args(contracts.ZaloTextStyleName)
    )
    source = (GATEWAY_SRC / "msgtype.ts").read_text(encoding="utf-8")
    match = re.search(r"export const QUOTE_MAX_CHARS = (\d+);", source)
    assert match and int(match.group(1)) == contracts.QUOTE_TEXT_MAX_CHARS


def test_gateway_gui_quote_text_dung_ten_truong():
    source = (GATEWAY_SRC / "bridge.ts").read_text(encoding="utf-8")
    assert "quote_text: m.quoteText" in source
    request = contracts.ZaloMessageRequest(
        account_id="a",
        sender_id="s",
        conversation_id="s",
        message_id="m",
        text="giải thích ý 2",
        quote_text="x" * 700,
    )
    assert request.quote_text == "x" * contracts.QUOTE_TEXT_MAX_CHARS


def test_so_luot_song_song_cua_gateway_khop_so_cho_thanh_vien(monkeypatch):
    """Gateway xếp tin riêng song song ZALO_DIRECT_CONCURRENCY = số chỗ thành viên
    bên Python (MAX_CONCURRENT_TURNS - OWNER_RESERVED_SLOTS, services/concurrency.py)
    để tin chờ ở gateway, không chờ trong Python quá BRIDGE_TIMEOUT_MS."""
    from services import concurrency

    monkeypatch.delenv("MAX_CONCURRENT_TURNS", raising=False)
    monkeypatch.delenv("OWNER_RESERVED_SLOTS", raising=False)
    max_turns = concurrency._max_concurrent_turns()
    member_slots = max_turns - concurrency._owner_reserved_slots(max_turns)
    config = (GATEWAY_SRC / "config.ts").read_text(encoding="utf-8")
    match = re.search(r'number\("ZALO_DIRECT_CONCURRENCY", (\d+),', config)
    assert match and int(match.group(1)) == member_slots
    render = (GATEWAY_SRC.parent.parent / "render.yaml").read_text(encoding="utf-8")
    env = dict(re.findall(r'- key: (\w+)\s+value: "([^"]*)"', render))
    rendered_slots = int(env["MAX_CONCURRENT_TURNS"]) - int(env["OWNER_RESERVED_SLOTS"])
    assert int(env["ZALO_DIRECT_CONCURRENCY"]) == rendered_slots


@pytest.mark.parametrize("kind", sorted(media_text.ZALO_NOTICE_KINDS))
def test_cau_bao_zalo_theo_cach_goi(kind):
    member = media_text.zalo_notice_text(kind, address_form="chị", mb=5)
    assert "chị" in member and " anh" not in member
    neutral = media_text.zalo_notice_text(kind, mb=5)
    assert " anh" not in neutral and " chị" not in neutral
    assert media_text.zalo_notice_text(kind, owner=True).count("anh") >= 1
    elder = media_text.zalo_notice_text(kind, address_form="cô")
    assert "cháu" in elder.lower()


class _User:
    def __init__(self, *, active=True, address_form=None):
        self.is_active = active
        self.is_admin = False
        self.internal_user_id = -7
        self.address_form = address_form


@pytest.fixture
def notice_bridge(monkeypatch):
    from channels import router as zalo_router
    from channels import zalo_media

    users = {
        "chi-lan": _User(address_form="chị"),
        "owner-zalo": _User(),
        "locked": _User(active=False),
    }
    notified = []

    async def resolve(external_id):
        return users.get(external_id)

    async def controller():
        return "owner-zalo"

    monkeypatch.setattr(zalo_router, "_secret", lambda: SECRET)
    monkeypatch.setattr(zalo_router, "_controller", controller)
    monkeypatch.delenv("ZALO_BRIDGE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(zalo_media.zalo_users, "resolve", resolve)
    monkeypatch.setattr(
        zalo_media.zalo_users, "notify_unpaired", lambda *a, **k: notified.append(a)
    )
    return notified


async def _post_notice(body: dict, secret: str = SECRET):
    from channels import zalo_media

    app = FastAPI()
    app.include_router(zalo_media.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(
            "/internal/zalo/notice", json=body, headers={"x-zalo-bridge-secret": secret}
        )


@pytest.mark.asyncio
async def test_notice_thanh_vien_theo_cach_goi_rieng(notice_bridge):
    response = await _post_notice({"sender_id": "chi-lan", "kind": "video"})
    assert response.status_code == 200
    text = response.json()["text"]
    assert text.startswith("🎬") and "chị" in text and " anh" not in text
    assert response.json()["paired"] is True


@pytest.mark.asyncio
async def test_notice_chu_bot_goi_anh_va_bao_dung_tran_file(notice_bridge, monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", str(3 * 1024 * 1024))
    response = await _post_notice({"sender_id": "owner-zalo", "kind": "file_too_large"})
    text = response.json()["text"]
    assert "3 MB" in text and "anh" in text


@pytest.mark.asyncio
async def test_notice_nguoi_la_va_bi_khoa_im_lang_khong_bao_chu_bot(notice_bridge):
    for sender, paired in (("stranger", False), ("locked", True)):
        response = await _post_notice({"sender_id": sender, "kind": "image"})
        assert response.status_code == 200
        # paired=False: gateway quên người này (người lạ / đã bị /zaloxoa).
        assert response.json() == {"text": None, "paired": paired}
    # Câu báo không thay cho lượt thật: không báo chủ bot (lượt ảnh/thoại tự báo).
    assert notice_bridge == []


@pytest.mark.asyncio
async def test_notice_sai_secret_va_loai_la(notice_bridge):
    assert (await _post_notice({"sender_id": "chi-lan", "kind": "image"}, "sai")).status_code == 403
    bad = await _post_notice({"sender_id": "chi-lan", "kind": "sticker"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_admin_doc_bo_dem_gateway_bo_ma_qr(monkeypatch):
    from channels import zalo_gateway_status

    monkeypatch.delenv("ZALO_ENABLED", raising=False)
    assert await zalo_gateway_status.snapshot() == {"enabled": False}

    monkeypatch.setenv("ZALO_ENABLED", "true")

    async def down():
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(zalo_gateway_status, "_fetch_status", down)
    assert await zalo_gateway_status.snapshot() == {"enabled": True, "reachable": False}

    async def status():
        return {
            "state": "connected",
            "connected": True,
            "qr": "iVBORw0KGgo...",
            "accountId": "123",
            "stats": {"direct_messages": 5, "merged_messages": 2},
            "queue": {"pending": 0, "running": 1},
        }

    monkeypatch.setattr(zalo_gateway_status, "_fetch_status", status)
    snap = await zalo_gateway_status.snapshot()
    assert snap["reachable"] is True and snap["stats"]["merged_messages"] == 2
    assert "qr" not in snap and "accountId" not in snap


def test_cong_dieu_khien_doc_tu_env(monkeypatch):
    from channels import zalo_gateway_status

    monkeypatch.setenv("ZALO_CONTROL_PORT", "9911")
    assert zalo_gateway_status._control_url() == "http://127.0.0.1:9911/status"
    monkeypatch.setenv("ZALO_CONTROL_PORT", "abc")
    assert zalo_gateway_status._control_url() == "http://127.0.0.1:9901/status"


def test_trang_admin_hien_bo_dem_gateway():
    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "d.zalo_gateway" in html and "<th>Gateway Zalo</th>" in html


def test_gateway_doc_truong_paired_va_co_route_quen_nguoi_gui():
    """Python báo người lạ bằng paired=False (ZaloMessageResponse/ZaloNoticeResponse)
    và báo gateway quên người bị /zaloxoa qua POST /known/forget - tên trường/route
    phải khớp hai bên."""
    from channels import zalo_gateway_status

    assert "paired" in contracts.ZaloMessageResponse.model_fields
    assert "paired" in contracts.ZaloNoticeResponse.model_fields
    bridge = (GATEWAY_SRC / "bridge.ts").read_text(encoding="utf-8")
    assert re.search(r"paired\?: boolean \| null", bridge)
    control = (GATEWAY_SRC / "control-server.ts").read_text(encoding="utf-8")
    assert f'"{zalo_gateway_status.FORGET_PATH}"' in control


@pytest.mark.asyncio
async def test_bao_gateway_quen_nguoi_gui(monkeypatch):
    from channels import zalo_gateway_status

    posted = []

    class _Response:
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            posted.append((url, json))
            return _Response()

    monkeypatch.setattr(zalo_gateway_status.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ZALO_CONTROL_PORT", "9911")
    monkeypatch.delenv("ZALO_ENABLED", raising=False)
    assert await zalo_gateway_status.forget_sender("z-member") is False
    assert posted == []

    monkeypatch.setenv("ZALO_ENABLED", "1")
    assert await zalo_gateway_status.forget_sender("z-member") is True
    assert posted == [("http://127.0.0.1:9911/known/forget", {"id": "z-member"})]

    async def refused(self, url, json):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(_Client, "post", refused)
    # Gateway tắt/không trả lời: chỉ log, không làm hỏng /zaloxoa.
    assert await zalo_gateway_status.forget_sender("z-member") is False
