"""Test services/weather_home.py: thành phố thời tiết riêng theo internal user id."""

import pytest

from services import weather, weather_home


@pytest.fixture
def store(monkeypatch):
    weather_home.clear_cache()
    data: dict[str, str] = {}
    reads = []

    async def get_setting(key):
        reads.append(key)
        return data.get(key)

    async def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(weather_home.db, "get_setting", get_setting)
    monkeypatch.setattr(weather_home.db, "set_setting", set_setting)
    yield data, reads
    weather_home.clear_cache()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("anh ở Hà Nội", "Hà Nội"),
        ("nhà em ở Đà Nẵng nha", "Đà Nẵng"),
        ("mình sống ở Cần Thơ", "Cần Thơ"),
        ("tui ở tại Nha Trang đó", "Nha Trang"),
        ("em ở Sài Gòn", "TP.HCM"),
        ("anh ở nhà", None),  # không phải địa danh quen
        ("anh đang ở Hà Nội", None),  # đi công tác/du lịch: không đổi mặc định
        ("hôm nay anh ở Hà Nội mệt quá", None),
        ("thời tiết Hà Nội", None),
        # Câu hỏi không phải câu khai báo: không được ghi đè thành phố đã lưu.
        ("em ở Đà Nẵng à?", None),
        ("em ở Đà Nẵng à", None),
        ("anh ở Hà Nội hả", None),
        ("em ở Huế không", None),
        ("anh ở Hà Nội ko", None),
        ("em ở Cần Thơ sao", None),
        ("mình ở Hà Nội?", None),
        ("em ở Hà Nội ạ", "Hà Nội"),  # "ạ" lễ phép, vẫn là khai báo
        ("em o Ha Noi a", "Hà Nội"),  # gõ không dấu: không phân biệt được, giữ như cũ
    ],
)
def test_parse_statement(text, expected):
    assert weather_home.parse_statement(text) == expected


async def test_dat_dia_danh_quen_khong_can_geocode(store, monkeypatch):
    data, reads = store

    async def no_geocode(name):
        raise AssertionError("địa danh quen không cần tra toạ độ")

    monkeypatch.setattr(weather, "resolve_location", no_geocode)
    assert await weather_home.set_home(-4, "hà nội") == "Hà Nội"
    assert data == {"weather_home:-4": "Hà Nội"}
    assert await weather_home.get(-4) == "Hà Nội" and reads == []  # cache sau khi ghi


async def test_dia_danh_la_phai_tra_duoc_toa_do(store, monkeypatch):
    data, _ = store

    async def resolve(name):
        return ("Quận 7, Hồ Chí Minh", 10.7, 106.7) if name == "Quận 7" else None

    monkeypatch.setattr(weather, "resolve_location", resolve)
    assert await weather_home.set_home(42, "Quận 7") == "Quận 7"
    with pytest.raises(weather_home.HomeError, match="không tìm thấy"):
        await weather_home.set_home(42, "Xyzabc")
    with pytest.raises(weather_home.HomeError):
        await weather_home.set_home(42, "<script>")
    assert data == {"weather_home:42": "Quận 7"}


async def test_geocode_loi_bao_thu_lai(store, monkeypatch):
    async def down(name):
        raise weather.WeatherError("down")

    monkeypatch.setattr(weather, "resolve_location", down)
    with pytest.raises(weather_home.HomeError, match="thử lại sau"):
        await weather_home.set_home(42, "Quận 7")


async def test_get_doc_db_mot_lan_va_xoa(store):
    data, reads = store
    data["weather_home:-9"] = "Huế"
    assert await weather_home.get(-9) == "Huế"
    assert await weather_home.get(-9) == "Huế"
    assert reads == ["weather_home:-9"]
    await weather_home.clear_home(-9)
    assert await weather_home.get(-9) == "" and data["weather_home:-9"] == ""


async def test_doc_db_loi_tra_rong_khong_cache(store, monkeypatch):
    async def boom(key):
        raise RuntimeError("db down")

    monkeypatch.setattr(weather_home.db, "get_setting", boom)
    assert await weather_home.get(-1) == ""
    assert -1 not in weather_home._cache


def test_cache_co_tran(store):
    for i in range(weather_home._CACHE_MAX + 20):
        weather_home._remember(-i, "Huế")
    assert len(weather_home._cache) == weather_home._CACHE_MAX
