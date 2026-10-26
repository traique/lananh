"""Danh sách theo dõi + ngưỡng giá người dùng tự đặt (parse tất định)."""

import pytest

from services import portfolio_service, watchlist_service as ws
from stock import providers


@pytest.fixture
def env(monkeypatch):
    state = {"alerts": [], "watch": [], "full": False, "price": 26_000.0, "deliver": True}

    async def validate(symbol):
        return symbol.upper() if symbol.upper() in {"HPG", "FPT", "MWG"} else None

    async def quote(symbol):
        if state["price"] is None:
            return None
        return providers.Quote(symbol, state["price"], state["price"], 0.0, 0.0, "2026-10-26", True)

    async def add_alert(user_id, symbol, direction, level, *, state: str, max_per_user):
        if env_state["full"]:
            return None, True
        key = (user_id, symbol, direction, level)
        if any(a[:4] == key for a in env_state["alerts"]):
            return None, False
        env_state["alerts"].append((*key, state))
        return {"id": len(env_state["alerts"])}, False

    async def add_watch(user_id, symbols, max_symbols):
        added = [s for s in symbols if s not in env_state["watch"]][
            : max_symbols - len(env_state["watch"])
        ]
        env_state["watch"].extend(added)
        return added

    env_state = state
    monkeypatch.setattr(ws.portfolio_service, "_validate_symbol", validate)
    monkeypatch.setattr(ws.providers, "fetch_quote", quote)
    monkeypatch.setattr(ws.db, "portfolio_add_price_alert", add_alert)
    monkeypatch.setattr(ws.db, "portfolio_add_watch", add_watch)
    monkeypatch.setattr(ws.portfolio_push, "can_deliver", lambda uid: state["deliver"])
    return state


@pytest.mark.asyncio
async def test_cau_bao_anh_khi_dat_nguong(env):
    reply = await portfolio_service.maybe_handle_natural_language(42, "báo anh khi HPG về 25 nhé")
    assert env["alerts"] == [(42, "HPG", "BELOW", 25_000, "armed")]
    assert "Em sẽ báo anh khi HPG về 25.000đ" in reply and "không phải khuyến nghị" in reply
    assert env["watch"] == ["HPG"]  # tự thêm vào danh sách theo dõi
    await portfolio_service.maybe_handle_natural_language(42, "nhắc em khi FPT vượt 130k")
    assert env["alerts"][-1][:4] == (42, "FPT", "ABOVE", 130_000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["báo anh khi HPG về 25 được không?", "có nên mua HPG khi về 25 không", "HPG về 25 chưa"],
)
async def test_cau_hoi_khong_dat_nguong(env, text):
    await portfolio_service.maybe_handle_natural_language(42, text)
    assert env["alerts"] == []


@pytest.mark.asyncio
async def test_khong_ghi_chieu_thi_suy_theo_gia_hien_tai(env):
    await ws.add_alert(42, "HPG", 30_000)
    await ws.add_alert(42, "HPG", 24_000, "chạm")
    assert [a[2] for a in env["alerts"]] == ["ABOVE", "BELOW"]
    env["price"] = None
    reply = await ws.add_alert(42, "HPG", 22_000)
    assert "chưa lấy được giá HPG" in reply and "/canhbaogia HPG về" in reply


@pytest.mark.asyncio
async def test_gia_da_o_phia_nguong_thi_cho_cham_lai(env):
    reply = await ws.add_alert(42, "HPG", 27_000, "về")  # giá 26.000 đã dưới 27.000
    assert env["alerts"][-1][4] == "crossed"
    assert "quay lại rồi chạm lại" in reply


@pytest.mark.asyncio
async def test_tran_nguong_va_kenh_tat_tin_chu_dong(env):
    env["full"] = True
    assert "đã đặt đủ" in await ws.add_alert(42, "HPG", 25_000, "về")
    env["full"] = False
    env["deliver"] = False
    assert "tắt tin nhắn chủ động" in await ws.add_alert(-5, "HPG", 25_000, "về")
    assert env["alerts"] == []


@pytest.mark.asyncio
async def test_lenh_theodoi_canhbaogia_xoacanhbao(env, monkeypatch):
    reply = await portfolio_service.handle_command(42, "/theodoi FPT, mwg XYZ")
    assert "FPT, MWG" in reply and "XYZ" in reply
    await portfolio_service.handle_command(42, "/canhbaogia HPG lên 28.5")
    assert env["alerts"][-1][:4] == (42, "HPG", "ABOVE", 28_500)
    assert "Cú pháp" in await portfolio_service.handle_command(42, "/canhbaogia HPG ngang 28")

    deleted = {}

    async def delete(user_id, symbol, level):
        deleted["args"] = (user_id, symbol, level)
        return 1

    async def remove(user_id, symbol):
        return True, 2

    monkeypatch.setattr(ws.db, "portfolio_delete_price_alerts", delete)
    monkeypatch.setattr(ws.db, "portfolio_remove_watch", remove)
    assert "Đã xoá 1" in await portfolio_service.handle_command(42, "/xoacanhbao hpg 28.5")
    assert deleted["args"] == (42, "HPG", 28_500)
    assert "và 2 ngưỡng giá" in await portfolio_service.handle_command(42, "/botheodoi HPG")
