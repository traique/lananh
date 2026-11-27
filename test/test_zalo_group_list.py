"""/nhomzalo (mọi nhóm bot đang ở, dùng được từ mọi kênh) và /themnhom, /fb_themnhom
chọn nhóm theo số thứ tự - channels/zalo_group_list.py."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import facebook_commands, group_commands, zalo_gateway_status, zalo_group_list
from channels.zalo_gateway_status import ZaloGroup
from services import channel_command_service

ROOT = Path(__file__).resolve().parent.parent
G1, G2, G3, G4 = (f"72123456789012345{i:02d}" for i in range(1, 5))


@pytest.fixture
def store(monkeypatch):
    data: dict[str, str] = {}

    async def get_setting(key):
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(zalo_group_list.db, "get_setting", get_setting)
    monkeypatch.setattr(zalo_group_list.db, "set_setting", set_setting)
    return data


@pytest.fixture
def gateway(monkeypatch, store):
    """Bot đang ở 4 nhóm; G2 đang theo dõi /tongket, G3 là nguồn Facebook."""
    groups = [
        ZaloGroup(G1, "Sale Team", 25),
        ZaloGroup(G2, "Gia Đình Nhỏ 🏠", 8),
        ZaloGroup(G3, "", None),
        ZaloGroup(G4, "Hội Chạy Bộ", 150),
    ]

    async def list_groups():
        return list(groups), len(groups)

    async def followed(account_id):
        return [(G2, "giadinh", True)]

    async def sources(account_id):
        return [(G3, "deal")]

    monkeypatch.setattr(zalo_gateway_status, "list_groups", list_groups)
    monkeypatch.setattr(zalo_group_list.zalo_repository, "list_groups_with_daily", followed)

    async def update_group_names(account_id, names):
        return len(names)

    monkeypatch.setattr(zalo_group_list.facebook_repository, "list_groups", sources)
    monkeypatch.setattr(
        zalo_group_list.facebook_repository, "update_group_names", update_group_names
    )
    return groups


@pytest.mark.asyncio
async def test_nhomzalo_liet_ke_ten_id_so_thanh_vien_va_danh_dau(gateway):
    result = await group_commands.maybe_handle_group_command("B", "/nhomzalo")
    text = result.messages[0]
    lines = text.splitlines()
    assert lines[0] == "👥 Nhóm Zalo bot đang tham gia (4):"
    # Sắp theo tên (không dấu), nhóm không rõ tên xuống cuối.
    assert lines[1] == f"1. Gia Đình Nhỏ 🏠 · 8 người — {G2} ✅ giadinh"
    assert lines[2] == f"2. Hội Chạy Bộ · 150 người — {G4}"
    assert lines[3] == f"3. Sale Team · 25 người — {G1}"
    assert lines[4] == f"4. (không rõ tên) — {G3} 📣 deal"
    assert "/themnhom <số> [tên-gợi-nhớ]" in text and "/fb_themnhom <số>" in text


@pytest.mark.asyncio
async def test_nhomzalo_loc_theo_ten_khong_dau(gateway):
    result = await group_commands.maybe_handle_group_command("B", "/nhomzalo gia dinh")
    lines = result.messages[0].splitlines()
    assert lines[0] == "👥 Nhóm Zalo có tên chứa “gia dinh” (1/4):"
    assert lines[1].startswith(f"1. Gia Đình Nhỏ 🏠 · 8 người — {G2}")
    # Lọc vẫn giữ SỐ GỐC trong toàn bộ danh sách (Hội Chạy Bộ là số 2).
    chay = await group_commands.maybe_handle_group_command("B", "/nhomzalo chay bo")
    lines = chay.messages[0].splitlines()
    assert lines[0] == "👥 Nhóm Zalo có tên chứa “chay bo” (1/4):"
    assert lines[1] == f"2. Hội Chạy Bộ · 150 người — {G4}"
    missing = await group_commands.maybe_handle_group_command("B", "/nhomzalo xyz")
    assert "Không có nhóm nào có tên chứa “xyz”" in missing.messages[0]


@pytest.mark.asyncio
async def test_so_thu_tu_khong_doi_theo_bo_loc(gateway, monkeypatch):
    """Admin A xem /nhomzalo, admin B lọc /nhomzalo chay: số của A vẫn đúng nhóm."""
    saved = []

    async def add_group(account_id, group_id, alias):
        saved.append((group_id, alias))

    monkeypatch.setattr(group_commands.zalo_repository, "add_group", add_group)
    await group_commands.maybe_handle_group_command("B", "/nhomzalo")
    await group_commands.maybe_handle_group_command("B", "/nhomzalo chay")
    await group_commands.maybe_handle_group_command("B", "/themnhom 3 sale")
    assert saved == [(G1, "sale")]  # số 3 = Sale Team trong danh sách đầy đủ


@pytest.mark.asyncio
async def test_themnhom_theo_so_thu_tu_tu_dat_ten_goi_nho(gateway, monkeypatch):
    saved = []

    async def add_group(account_id, group_id, alias):
        saved.append((account_id, group_id, alias))

    monkeypatch.setattr(group_commands.zalo_repository, "add_group", add_group)
    await group_commands.maybe_handle_group_command("B", "/nhomzalo")

    result = await group_commands.maybe_handle_group_command("B", "/themnhom 2")
    assert saved[-1] == ("B", G4, "hoi-chay-bo")
    assert result.messages[0] == (
        f"✅ Đã thêm nhóm hoi-chay-bo — Hội Chạy Bộ ({G4}). Tổng kết: /tongket hoi-chay-bo"
        " · Tín hiệu: /tinhieu on hoi-chay-bo"
    )
    await group_commands.maybe_handle_group_command("B", "/themnhom 3 Sale")
    assert saved[-1] == ("B", G1, "sale")
    # Nhóm đã theo dõi, không kèm tên: giữ tên gợi nhớ cũ, không ghi đè.
    again = await group_commands.maybe_handle_group_command("B", "/themnhom 1")
    assert "đã được theo dõi với tên gợi nhớ “giadinh”" in again.messages[0]
    assert len(saved) == 2
    # Số ngoài danh sách.
    out = await group_commands.maybe_handle_group_command("B", "/themnhom 9 x")
    assert "chỉ có 4 nhóm (số 1 đến 4)" in out.messages[0]


@pytest.mark.asyncio
async def test_so_thu_tu_can_danh_sach_con_han(store, monkeypatch):
    called = []

    async def add_group(*args):
        called.append(args)

    monkeypatch.setattr(group_commands.zalo_repository, "add_group", add_group)
    # Chưa gõ /nhomzalo: nhắc xem danh sách, KHÔNG thêm group ID "3" không tồn tại.
    result = await group_commands.maybe_handle_group_command("B", "/themnhom 3 abc")
    assert "Gõ /nhomzalo để xem" in result.messages[0] and called == []

    store["zalo:group_pick:B"] = f'{{"at": 1000, "groups": [["{G1}", "Sale Team"]]}}'
    monkeypatch.setattr(zalo_group_list.time, "time", lambda: 1000 + 61 * 60)
    expired = await group_commands.maybe_handle_group_command("B", "/themnhom 1 abc")
    assert "Gõ /nhomzalo để xem" in expired.messages[0] and called == []
    monkeypatch.setattr(zalo_group_list.time, "time", lambda: 1000 + 59 * 60)
    await group_commands.maybe_handle_group_command("B", "/themnhom 1 abc")
    assert called == [("B", G1, "abc")]


@pytest.mark.asyncio
async def test_fb_themnhom_theo_so_thu_tu(gateway, monkeypatch):
    saved = []

    async def add_group(account_id, group_id, alias, group_name=""):
        saved.append((account_id, group_id, alias, group_name))

    monkeypatch.setattr(facebook_commands.facebook_repository, "add_group", add_group)
    await group_commands.maybe_handle_group_command("B", "/nhomzalo")
    result = await facebook_commands.maybe_handle_facebook_command("B", "/fb_themnhom 1")
    # Lưu luôn tên thật của nhóm để bài/lệnh /fb_* hiện TÊN nhóm thay cho group ID.
    assert saved == [("B", G2, "gia-dinh-nho", "Gia Đình Nhỏ 🏠")]
    assert "“Gia Đình Nhỏ 🏠” (gợi nhớ: gia-dinh-nho)" in result.messages[0]
    assert G2 not in result.messages[0]
    # Đã là nguồn Facebook (G3, số 4), không kèm tên: giữ tên cũ.
    again = await facebook_commands.maybe_handle_facebook_command("B", "/fb_themnhom 4")
    assert "đã là nguồn Facebook với tên gợi nhớ “deal”" in again.messages[0]


def test_ten_goi_nho_mac_dinh():
    assert zalo_group_list.default_alias("Gia Đình Nhỏ 🏠", G1) == "gia-dinh-nho"
    assert zalo_group_list.default_alias("🎉🎉", G1) == G1
    assert zalo_group_list.default_alias("ON", G1) == "nhom-on"  # từ khoá /tongket on|off


@pytest.mark.asyncio
async def test_thanh_vien_thuong_khong_dung_duoc_nhomzalo():
    result, provider = await channel_command_service.maybe_handle_command(
        1, "/nhomzalo", is_admin=False
    )
    assert result == ["Lệnh này chỉ dành cho admin."]


@pytest.mark.asyncio
async def test_bao_loi_ro_khi_khong_lay_duoc_nhom(monkeypatch, store):
    async def offline():
        raise zalo_gateway_status.GatewayGroupsError("Zalo bot chưa đăng nhập ...")

    monkeypatch.setattr(zalo_gateway_status, "list_groups", offline)
    result = await group_commands.maybe_handle_group_command("B", "/nhomzalo")
    assert result.messages == ["Zalo bot chưa đăng nhập ..."]
    assert store == {}


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("không phải JSON")
        return self._payload


def _client(handler):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return handler(url)

    return _Client


@pytest.mark.asyncio
async def test_list_groups_doc_route_groups_cua_gateway(monkeypatch):
    urls = []
    payload = {
        "total": 250,
        "groups": [
            {"id": G1, "name": " Sale Team ", "members": 25},
            {"id": G2, "name": None, "members": True},  # bool không phải số thành viên
            {"id": "", "name": "thiếu id"},
            "rác",
        ],
    }

    def ok(url):
        urls.append(url)
        return _Response(200, payload)

    monkeypatch.setenv("ZALO_CONTROL_PORT", "9911")
    monkeypatch.delenv("ZALO_ENABLED", raising=False)
    with pytest.raises(zalo_gateway_status.GatewayGroupsError, match="ZALO_ENABLED"):
        await zalo_gateway_status.list_groups()

    monkeypatch.setenv("ZALO_ENABLED", "1")
    monkeypatch.setattr(zalo_gateway_status.http_client, "scoped", _client(ok))
    groups, total = await zalo_gateway_status.list_groups()
    assert urls == ["http://127.0.0.1:9911/groups"]
    assert groups == [ZaloGroup(G1, "Sale Team", 25), ZaloGroup(G2, "", None)]
    assert total == 250

    monkeypatch.setattr(
        zalo_gateway_status.http_client, "scoped", _client(lambda url: _Response(409, {}))
    )
    with pytest.raises(zalo_gateway_status.GatewayGroupsError, match="chưa đăng nhập"):
        await zalo_gateway_status.list_groups()

    def refused(url):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(zalo_gateway_status.http_client, "scoped", _client(refused))
    with pytest.raises(zalo_gateway_status.GatewayGroupsError, match="Không kết nối được"):
        await zalo_gateway_status.list_groups()


def test_route_groups_khop_gateway_va_gateway_khong_tu_xu_ly_nhomzalo():
    control = (ROOT / "zalo-gateway" / "src" / "control-server.ts").read_text(encoding="utf-8")
    assert f'"{zalo_gateway_status.GROUPS_PATH}"' in control
    index = (ROOT / "zalo-gateway" / "src" / "index.ts").read_text(encoding="utf-8")
    assert "controllerCommandAction" not in index  # /nhomzalo đi qua Python như mọi lệnh nhóm
