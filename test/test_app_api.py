"""App Android màn hình thông tin: ghép thiết bị (services/app_devices.py), dữ liệu
dashboard (services/app_dashboard.py) và API /api/v1/app/* (web_app_api.py).

DB và nguồn dữ liệu đều giả lập (không mạng, không AI)."""

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

import web_app_api
from core import database as db
from services import app_dashboard, app_devices, morning_news, price_lookup, weather, weather_home
from stock import providers

VN = ZoneInfo("Asia/Ho_Chi_Minh")
OWNER = 777


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeDevices:
    """Bảng app_devices trong RAM, cùng hành vi với core/repositories/app_devices.py."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.lookups = 0
        self.fail_lookup = False

    async def create_pending(self, user_id, name, code_hash, ttl_sec, max_devices):
        active = [r for r in self.rows.values() if r["user"] == user_id and not r["revoked"]]
        if len(active) >= max_devices:
            return None
        device_id = self.next_id
        self.next_id += 1
        self.rows[device_id] = {
            "id": device_id,
            "user": user_id,
            "name": name,
            "code": code_hash,
            "token": None,
            "revoked": False,
            "paired_at": None,
            "last_seen_at": None,
            "pair_expires_at": dt.datetime(2026, 10, 31, 9, 10, tzinfo=VN),
            "created_at": dt.datetime(2026, 10, 31, 9, 0, tzinfo=VN),
        }
        return device_id

    async def pair(self, code_hash, token_hash):
        for r in self.rows.values():
            if r["code"] == code_hash and r["token"] is None and not r["revoked"]:
                r.update(code=None, token=token_hash, paired_at=dt.datetime.now(VN))
                return {"id": r["id"], "telegram_user_id": r["user"], "name": r["name"]}
        return None

    async def by_token(self, token_hash):
        self.lookups += 1
        if self.fail_lookup:
            raise db.DatabaseUnavailableError("DB sập")
        for r in self.rows.values():
            if r["token"] == token_hash and not r["revoked"]:
                return {"id": r["id"], "telegram_user_id": r["user"], "name": r["name"]}
        return None

    async def list_for_user(self, user_id):
        return [
            {k: r[k] for k in ("id", "name", "paired_at", "last_seen_at", "pair_expires_at")}
            for r in self.rows.values()
            if r["user"] == user_id and not r["revoked"]
        ]

    async def revoke(self, user_id, device_id):
        r = self.rows.get(device_id)
        if r is None or r["user"] != user_id or r["revoked"]:
            return False
        r.update(revoked=True, token=None, code=None)
        return True


@pytest.fixture
def devices(monkeypatch):
    fake = FakeDevices()
    monkeypatch.setattr(db, "app_device_create_pending", fake.create_pending)
    monkeypatch.setattr(db, "app_device_pair", fake.pair)
    monkeypatch.setattr(db, "app_device_by_token", fake.by_token)
    monkeypatch.setattr(db, "app_device_list", fake.list_for_user)
    monkeypatch.setattr(db, "app_device_revoke", fake.revoke)
    app_devices.clear_cache()
    yield fake
    app_devices.clear_cache()


async def _new_code(fake: FakeDevices, monkeypatch, code="K7QF3MXA", name="Phòng khách") -> str:
    monkeypatch.setattr(app_devices, "new_code", lambda: code)
    text = await app_devices.handle_command(OWNER, ["moi", *name.split()])
    assert "K7QF-3MXA" in text and name in text
    return code


# ─── Ghép, xác thực, thu hồi ─────────────────────────────────────────────────


def test_ma_ghep_dung_bang_chu_khong_nham():
    for _ in range(50):
        code = app_devices.new_code()
        assert len(code) == 8 and not set(code) & set("01OIL")
    assert app_devices.normalize_code(" k7qf-3mxa ") == "K7QF3MXA"
    assert app_devices.display_code("K7QF3MXA") == "K7QF-3MXA"


async def test_ghep_xong_token_dung_duoc_ma_khong_dung_lai(devices, monkeypatch):
    code = await _new_code(devices, monkeypatch)
    stored = next(iter(devices.rows.values()))
    assert stored["code"] == _h(code)  # DB chỉ giữ hash

    token, device = await app_devices.pair("k7qf-3mxa")
    assert token.startswith("la_") and len(token) > 40
    assert device.user_id == OWNER and device.name == "Phòng khách"
    assert stored["token"] == _h(token) and stored["code"] is None

    with pytest.raises(app_devices.PairError) as err:
        await app_devices.pair(code)
    assert err.value.code == "CODE_NOT_FOUND"

    got = await app_devices.authenticate(f"Bearer {token}")
    assert got == device
    for bad in ("", token, f"Basic {token}", "Bearer la_khong-co", "Bearer abc"):
        assert await app_devices.authenticate(bad) is None


async def test_xac_thuc_dung_cache_va_chiu_duoc_db_chap_chon(devices, monkeypatch):
    await _new_code(devices, monkeypatch)
    token, device = await app_devices.pair("K7QF3MXA")
    assert await app_devices.authenticate(f"Bearer {token}") == device
    assert await app_devices.authenticate(f"Bearer {token}") == device
    assert devices.lookups == 1  # lần 2 trong TTL: không chạm DB

    # Hết TTL mà DB lỗi: vẫn dùng bản đã xác thực (trong _AUTH_STALE_SEC).
    monkeypatch.setattr(app_devices, "_AUTH_TTL_SEC", 0)
    devices.fail_lookup = True
    assert await app_devices.authenticate(f"Bearer {token}") == device
    # Token chưa từng xác thực mà DB lỗi: báo lỗi lên (API trả 503), không cho qua.
    with pytest.raises(db.DatabaseUnavailableError):
        await app_devices.authenticate("Bearer la_chua-tung-thay")


async def test_thu_hoi_co_hieu_luc_ngay(devices, monkeypatch):
    await _new_code(devices, monkeypatch)
    token, device = await app_devices.pair("K7QF3MXA")
    assert await app_devices.authenticate(f"Bearer {token}") == device
    listing = await app_devices.handle_command(OWNER, [])
    assert f"#{device.id} Phòng khách" in listing and "lần cuối" in listing

    assert "Không thấy" in await app_devices.handle_command(OWNER + 1, ["xoa", str(device.id)])
    assert "Đã thu hồi" in await app_devices.handle_command(OWNER, ["xoa", f"#{device.id}"])
    assert await app_devices.authenticate(f"Bearer {token}") is None  # không chờ hết cache
    assert "Chưa có thiết bị" in await app_devices.handle_command(OWNER, [])


async def test_tran_thiet_bi_va_huong_dan(devices, monkeypatch):
    for i in range(app_devices.MAX_DEVICES):
        monkeypatch.setattr(app_devices, "new_code", lambda i=i: f"ABCDEFG{i + 2}")
        assert "Mã ghép" in await app_devices.handle_command(OWNER, ["moi"])
    assert "đã có" in await app_devices.handle_command(OWNER, ["moi", "thêm"])
    assert "/app moi" in await app_devices.handle_command(OWNER, ["linh-tinh"])
    assert "Màn hình" in (await app_devices.handle_command(OWNER, []))  # tên mặc định


async def test_do_ma_sai_qua_nhieu_lan_bi_khoa(devices, monkeypatch):
    code = await _new_code(devices, monkeypatch)
    monkeypatch.setattr(app_devices, "_PAIR_FAIL_LIMIT", 3)
    for bad in ("ABCDEFGH", "ngắn", "ZZZZZZZZ"):
        with pytest.raises(app_devices.PairError):
            await app_devices.pair(bad)
    with pytest.raises(app_devices.PairError) as err:
        await app_devices.pair(code)  # đúng mã cũng phải chờ
    assert err.value.status == 429


# ─── Dữ liệu dashboard ───────────────────────────────────────────────────────


class Q:
    def __init__(self, symbol, price):
        self.symbol, self.price, self.change, self.change_pct = symbol, price, 500.0, 1.5
        self.date, self.is_realtime = "2026-10-30", True


@pytest.fixture
def sources(monkeypatch):
    calls = {"quote": 0, "rss": 0, "weather": [], "watchlist": ["FPT", "HPG"]}

    async def gold():
        return {
            "source": "vang.today",
            "updated": "09:00",
            "unit": "VND/lượng",
            "domestic": [
                {"name": f"Vàng {i}", "buy": 1.0e8 + i, "sell": 1.2e8, "changeSell": 0}
                for i in range(9)
            ],
            "world": {
                "priceUsdPerOunce": 4000.0,
                "change": 5.0,
                "conversion": {"vndPerTael": 1.1e8},
            },
            "fx": {
                "rates": [{"code": "USD", "transferBuy": 26000, "sell": 26300}, {"code": "THB"}]
            },
        }

    async def fuel():
        return {
            "source": "petrolimex",
            "updated": "30/10",
            "unit": "VND/lít",
            "petrolimex": [
                {"name": "Xăng RON 95-III", "zone1": 21000, "zone2": 21400, "change": -100}
            ],
        }

    async def get_weather(location, days=1):
        calls["weather"].append(location)
        return {
            "location": location or "TP.HCM",
            "source": "Open-Meteo",
            "now": {"description": "có mây", "temperature_c": 30.5, "humidity_percent": 70},
            "daily": [{"date": "2026-10-31", "weekday": "Thứ Bảy", "min_c": 25, "max_c": 33}],
        }

    async def get_hourly(location):
        base = dt.datetime(2026, 10, 31, 0, 0)
        return {
            "hours": [
                weather.HourPoint(base + dt.timedelta(hours=h), 10, 0.0, 26.0 + h, 3)
                for h in range(48)
            ]
        }

    async def home(user_id):
        return "Hà Nội" if user_id == OWNER else ""

    async def rss(limit=10):
        calls["rss"] += 1
        return [morning_news._NewsItem("F1I1", "Tin A", "https://a.vn/1", "VnExpress")]

    async def watchlist(user_id):
        return list(calls["watchlist"])

    async def quote(symbol):
        calls["quote"] += 1
        if symbol == "LOI":
            raise RuntimeError("DNSE lỗi")
        return Q(symbol, 25000.0)

    monkeypatch.setattr(price_lookup, "gold", gold)
    monkeypatch.setattr(price_lookup, "fuel", fuel)
    monkeypatch.setattr(weather, "get_weather", get_weather)
    monkeypatch.setattr(weather, "get_hourly", get_hourly)
    monkeypatch.setattr(weather_home, "get", home)
    monkeypatch.setattr(morning_news, "rss_headlines", rss)
    monkeypatch.setattr(db, "portfolio_watchlist", watchlist)
    monkeypatch.setattr(providers, "fetch_quote", quote)
    app_dashboard.clear_cache()
    yield calls
    app_dashboard.clear_cache()


NOW = dt.datetime(2026, 10, 30, 10, 20, tzinfo=VN)  # thứ Sáu, trong phiên


async def test_dashboard_du_cac_phan_va_gon(sources):
    data = await app_dashboard.dashboard(OWNER, now=NOW)
    assert data["refreshSec"] == 300
    assert data["calendar"]["today"]["date"] == "2026-10-30"
    assert (
        data["calendar"]["today"]["lunar"]["day"] and len(data["calendar"]["month"]["days"]) == 31
    )
    w = data["weather"]
    assert w["location"] == "Hà Nội"  # thành phố mặc định của chính người này
    assert w["now"]["temperatureC"] == 30.5 and w["now"]["code"] == 3
    assert w["hours"][0]["time"] == "2026-10-31T00:00"  # giờ đã qua bị bỏ
    assert len(w["hours"]) == 12
    assert len(data["gold"]["domestic"]) == 6
    assert data["gold"]["world"]["vndPerTael"] == 1.1e8
    assert [r["code"] for r in data["gold"]["fx"]] == ["USD"]
    assert data["fuel"]["items"][0] == {"name": "Xăng RON 95-III", "price": 21000, "change": -100}
    assert data["news"]["items"][0]["url"] == "https://a.vn/1"
    wl = data["watchlist"]
    assert wl["marketOpen"] is True
    assert [i["symbol"] for i in wl["items"]] == ["FPT", "HPG"] and wl["items"][0][
        "price"
    ] == 25000.0
    json.dumps(data, default=str)


async def test_gio_theo_gio_bat_dau_tu_gio_hien_tai(sources):
    data = await app_dashboard.dashboard(OWNER, now=dt.datetime(2026, 10, 31, 10, 20, tzinfo=VN))
    hours = data["weather"]["hours"]
    assert hours[0]["time"] == "2026-10-31T10:00" and hours[-1]["time"] == "2026-10-31T21:00"


async def test_dashboard_cache_va_dia_diem_rieng(sources):
    await app_dashboard.dashboard(OWNER, now=NOW)
    await app_dashboard.dashboard(OWNER, now=NOW)
    assert sources["quote"] == 2 and sources["rss"] == 1  # lần 2 lấy nguyên bản cache
    data = await app_dashboard.dashboard(OWNER, place="  Đà   Nẵng ", now=NOW)
    assert data["weather"]["location"] == "Đà Nẵng"
    # Bản cache theo địa điểm khác vẫn dùng chung cache giá + tin.
    assert sources["quote"] == 2 and sources["rss"] == 1


async def test_mot_phan_loi_khong_lam_hong_phan_khac(sources, monkeypatch):
    async def boom():
        raise RuntimeError("SJC sập")

    async def no_news(limit=10):
        return []

    monkeypatch.setattr(price_lookup, "gold", boom)
    monkeypatch.setattr(morning_news, "rss_headlines", no_news)
    sources["watchlist"] = ["FPT", "LOI"]
    data = await app_dashboard.dashboard(OWNER, now=NOW)
    assert "error" in data["gold"] and "error" in data["news"]
    assert data["fuel"]["items"] and data["weather"]["now"]
    assert data["watchlist"]["items"][1] == {"symbol": "LOI", "price": None}


async def test_gia_cu_giu_lai_khi_nguon_loi_thoang_qua(sources, monkeypatch):
    await app_dashboard.dashboard(OWNER, now=NOW)

    async def broken(symbol):
        raise RuntimeError("DNSE timeout")

    monkeypatch.setattr(providers, "fetch_quote", broken)
    monkeypatch.setattr(app_dashboard, "_RESULT_TTL_SEC", 0)
    monkeypatch.setattr(app_dashboard, "_QUOTE_TTL_OPEN_SEC", 0)
    data = await app_dashboard.dashboard(OWNER, now=NOW)
    assert data["watchlist"]["items"][0]["price"] == 25000.0


async def test_ngoai_phien_giu_gia_lau_hon(sources, monkeypatch):
    sunday = dt.datetime(2026, 11, 1, 20, 0, tzinfo=VN)
    await app_dashboard.dashboard(OWNER, now=sunday)
    monkeypatch.setattr(app_dashboard, "_RESULT_TTL_SEC", 0)
    data = await app_dashboard.dashboard(OWNER, now=sunday)
    assert data["watchlist"]["marketOpen"] is False
    assert sources["quote"] == 2  # không gọi DNSE lại ngoài phiên


# ─── HTTP API ────────────────────────────────────────────────────────────────


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(web_app_api.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_api_ghep_roi_lay_dashboard(client, devices, sources, monkeypatch):
    r = await client.get("/api/v1/app/dashboard")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert r.headers["www-authenticate"] == "Bearer"

    await _new_code(devices, monkeypatch)
    for body, code in (
        (b"not json", "INVALID_JSON"),
        (b'{"code": 5}', "INVALID_CODE"),
        (b'{"code": "ZZZZ-ZZZZ"}', "CODE_NOT_FOUND"),
    ):
        r = await client.post("/api/v1/app/pair", content=body)
        assert r.status_code in (400, 422) and r.json()["error"]["code"] == code
    r = await client.post("/api/v1/app/pair", json={"code": "k7qf-3mxa"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["device"]["name"] == "Phòng khách"

    r = await client.get(
        "/api/v1/app/dashboard",
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip" and r.headers["cache-control"] == "no-store"
    data = r.json()  # httpx tự giải nén
    assert data["device"]["name"] == "Phòng khách" and data["watchlist"]["items"]


async def test_api_db_sap_tra_503(client, devices, monkeypatch):
    devices.fail_lookup = True
    r = await client.get("/api/v1/app/dashboard", headers={"Authorization": "Bearer la_x"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DB_UNAVAILABLE"


async def test_api_ghep_khi_db_loi_tra_503(client, devices, monkeypatch):
    async def broken(code_hash, token_hash):
        raise OSError("mất kết nối")

    monkeypatch.setattr(db, "app_device_pair", broken)
    r = await client.post("/api/v1/app/pair", json={"code": "K7QF-3MXA"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DB_UNAVAILABLE"
