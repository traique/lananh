"""Cache phần đắt của /phantich (stock/analysis.analyze_symbol).

Trước đây cache 15 phút chỉ giữ bản tổng hợp khi KHÔNG có câu hỏi, mà mọi nơi
gọi đều truyền câu hỏi nên cache chết: hỏi lại "phân tích HPG" sau 5 phút chạy
lại 7+ lượt mạng và tới 5 lượt LLM. Nay ngữ cảnh dữ liệu + khối debate được cache
theo (mã, đang giữ, full_access); chỉ bước tổng hợp chạy lại với câu hỏi mới.
Dữ liệu giả lập hoàn toàn (không mạng, không DB).
"""

import asyncio
from datetime import date, timedelta

import pytest

from ai import orchestrator
from stock import analysis as sa
from stock import corporate_actions, providers, schemas

_N = 120


def _dates(count: int) -> list[str]:
    out, day = [], date.today() - timedelta(days=1)
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return out[::-1]


class _World:
    """Nguồn dữ liệu + LLM giả, đếm số lượt gọi."""

    def __init__(self):
        self.builds = 0
        self.utility: list[str] = []
        self.asks: list[str] = []
        self.sleeps: list[float] = []
        self.holding_reads = 0
        self.utility_ok = True


@pytest.fixture
def world(monkeypatch):
    w = _World()
    dates = _dates(_N)
    closes = [30_000.0 + i * 25 for i in range(_N)]
    series = providers.OhlcvSeries(
        "FPT",
        closes,
        [c * 1.01 for c in closes],
        [c * 0.99 for c in closes],
        [1e6] * _N,
        dates,
        "dnse",
    )
    vn_closes = [1_000.0 + i for i in range(_N)]
    vnindex = providers.OhlcvSeries(
        "VNINDEX",
        vn_closes,
        [c + 5 for c in vn_closes],
        [c - 5 for c in vn_closes],
        [1e6] * _N,
        dates,
        "dnse",
    )
    headline = providers.NewsHeadline("FPT lãi kỷ lục", "CafeF", "", "", 0.5, True)

    async def fake_ohlcv(symbol, days=90):
        return vnindex if symbol == "VNINDEX" else series

    async def none(*args, **kwargs):
        return None

    async def news(symbol):
        return [headline]

    async def blank(*args, **kwargs):
        return ""

    async def hose(symbol):
        return "HOSE"

    async def no_events(symbol):
        return [], []

    monkeypatch.setattr(providers, "fetch_ohlcv", fake_ohlcv)
    monkeypatch.setattr(providers, "fetch_quote", none)
    monkeypatch.setattr(providers, "fetch_news", news)
    monkeypatch.setattr(providers, "fetch_symbol_exchange", hose)
    monkeypatch.setattr(sa, "_safe_sector_prompt", blank)
    monkeypatch.setattr(sa, "_safe_fundamentals_prompt", blank)
    monkeypatch.setattr(corporate_actions, "fetch_corporate_events_cached", no_events)

    real_build = sa.build_context

    async def counting_build(*args, **kwargs):
        w.builds += 1
        return await real_build(*args, **kwargs)

    async def fake_utility(prompt):
        w.utility.append(prompt)
        if not w.utility_ok:
            return None
        if "bull:" in prompt:
            return {
                "bull": {"thesis": "Xu hướng tăng bền", "points": ["MA tăng"]},
                "bear": {"thesis": "Thanh khoản chưa bùng nổ", "points": ["Volume đi ngang"]},
            }
        return {
            "relevant": True,
            "sentiment_label": "tích cực",
            "key_points": ["Lãi kỷ lục"],
            "impact_note": "Hỗ trợ tâm lý",
        }

    class _Reply:
        used_fallback = False

        def __init__(self, text):
            self.text = text

    async def fake_ask(prompt):
        w.asks.append(prompt)
        if '"thesis"' in prompt:  # lượt JSON dự phòng của bước debate
            return _Reply(
                '{"bull": {"thesis": "a", "points": []}, "bear": {"thesis": "b", "points": []}}'
            )
        if '"relevant"' in prompt:  # lượt JSON dự phòng của bước tin
            return _Reply(
                '{"relevant": false, "sentiment_label": "trung lập", "key_points": [], "impact_note": ""}'
            )
        return _Reply("Diễn giải: xu hướng kỹ thuật ổn định, thanh khoản bình thường.")

    async def fake_sleep(seconds):
        w.sleeps.append(seconds)

    from core import config

    monkeypatch.setattr(config, "ROUTER9_STEP_DELAY_SEC", 3)
    monkeypatch.setattr(sa, "build_context", counting_build)
    monkeypatch.setattr(orchestrator, "utility_json", fake_utility)
    monkeypatch.setattr(orchestrator, "ask", fake_ask)
    monkeypatch.setattr(schemas, "_sleep", fake_sleep)
    monkeypatch.setattr(sa, "_cache", {})
    return w


def _final_asks(w: _World) -> list[str]:
    return [p for p in w.asks if '"thesis"' not in p and '"relevant"' not in p]


@pytest.mark.asyncio
async def test_hoi_lai_cung_ma_chi_chay_lai_buoc_tong_hop(world):
    reports = []

    async def on_rule_report(text):
        reports.append(text)

    first = await sa.analyze_symbol("FPT", "phân tích FPT", on_rule_report=on_rule_report)
    assert world.builds == 1
    assert len(world.utility) == 2 and len(world.asks) == 1  # tin + debate + tổng hợp = 3 lượt
    assert world.sleeps == []  # JSON qua utility_json: không nghỉ nhịp 9Router
    assert "Xu hướng tăng bền" in world.asks[0] and "vai Research Manager" in world.asks[0]
    assert sa._STALE_NOTE not in first

    second = await sa.analyze_symbol("fpt", "FPT còn lên không?", on_rule_report=on_rule_report)
    assert world.builds == 1  # 0 lượt mạng
    assert len(world.utility) == 2 and len(world.asks) == 2  # chỉ thêm 1 lượt tổng hợp
    assert "FPT còn lên không?" in world.asks[1]
    assert "Xu hướng tăng bền" in world.asks[1]  # debate dùng lại từ cache
    assert second.endswith(sa._STALE_NOTE)
    assert len(reports) == 2  # bản số liệu vẫn gửi trước ở lượt cache


@pytest.mark.asyncio
async def test_force_refresh_lay_lai_toan_bo(world):
    await sa.analyze_symbol("FPT", "phân tích FPT")
    out = await sa.analyze_symbol("FPT", "phân tích lại FPT", force_refresh=True)
    assert world.builds == 2 and len(world.utility) == 4 and len(world.asks) == 2
    assert sa._STALE_NOTE not in out


@pytest.mark.asyncio
async def test_cache_het_han_sau_15_phut(world):
    await sa.analyze_symbol("FPT", "phân tích FPT")
    entry = sa._cache[("FPT", False, True)]
    entry.created -= sa._CACHE_TTL_SEC + 1
    out = await sa.analyze_symbol("FPT", "phân tích FPT")
    assert world.builds == 2 and sa._STALE_NOTE not in out


@pytest.mark.asyncio
async def test_ban_day_du_va_ban_tham_khao_khong_dung_chung_debate(world):
    await sa.analyze_symbol("FPT", "phân tích FPT", full_access=True)
    member = await sa.analyze_symbol("FPT", "phân tích FPT", full_access=False)
    assert set(sa._cache) == {("FPT", False, True), ("FPT", False, False)}
    assert sa._cache[("FPT", False, False)].debate_case is None
    # Bản tham khảo chỉ chạy bước tóm tắt tin, prompt không có tranh luận/action.
    assert len(world.utility) == 3
    member_prompt = world.asks[-1]
    assert "Xu hướng tăng bền" not in member_prompt and "Research Manager" not in member_prompt
    assert sa._STALE_NOTE not in member


@pytest.mark.asyncio
async def test_khoi_debate_loi_duoc_thu_lai_o_luot_sau(world, monkeypatch):
    world.utility_ok = False
    good_ask = orchestrator.ask

    async def broken_ask(prompt):
        world.asks.append(prompt)
        raise RuntimeError("provider-chain sập")

    monkeypatch.setattr(orchestrator, "ask", broken_ask)
    out = await sa.analyze_symbol("FPT", "phân tích FPT")
    entry = sa._cache[("FPT", False, True)]
    assert out == sa.rfmt.ensure_disclaimer(sa._fallback_text(entry.ctx))
    assert entry.news_analysis is None and entry.debate_case is None and entry.report is None
    # Rơi về provider-chain liên tiếp (tin, debate, tổng hợp): chỉ nghỉ nhịp từ lượt
    # chain thứ 2 - 2 lần thay vì 4.
    assert world.sleeps == [3.0, 3.0]

    world.utility_ok = True
    monkeypatch.setattr(orchestrator, "ask", good_ask)
    await sa.analyze_symbol("FPT", "phân tích FPT")
    assert world.builds == 1
    assert entry.news_analysis is not None and entry.debate_case is not None


@pytest.mark.asyncio
async def test_khong_cau_hoi_tra_ban_tong_hop_da_cache_khong_goi_llm(world):
    first = await sa.analyze_symbol("FPT")
    calls = (len(world.utility), len(world.asks))
    second = await sa.analyze_symbol("FPT")
    assert (len(world.utility), len(world.asks)) == calls
    assert second == first + sa._STALE_NOTE


@pytest.mark.asyncio
async def test_with_debate_false_chi_mot_luot_tong_hop(world):
    out = await sa.analyze_symbol("FPT", "so sánh", with_debate=False)
    assert world.utility == [] and len(world.asks) == 1
    assert "Không có tranh luận nội bộ lần này" in world.asks[0]
    assert out


@pytest.mark.asyncio
async def test_vi_the_chi_doc_db_mot_lan_moi_luot(world, monkeypatch):
    from stock import portfolio

    reads = []

    async def get_holding(user_id, symbol):
        reads.append(symbol)
        return portfolio.Holding(user_id, symbol, 100, 25_000)

    monkeypatch.setattr(portfolio, "get_holding", get_holding)
    await sa.analyze_symbol("FPT", "phân tích FPT", user_id=7)
    assert reads == ["FPT"]
    assert ("FPT", True, True) in sa._cache
    assert "Vị thế đang ghi nhận" in world.asks[-1]


@pytest.mark.asyncio
async def test_lay_truoc_song_song_roi_dung_khong_gan_ghi_chu_cu(world):
    started = []
    real_build = sa.build_context

    async def slow_build(symbol, **kwargs):
        started.append(symbol)
        await asyncio.sleep(0.02)
        return await real_build(symbol, **kwargs)

    sa.build_context = slow_build
    try:
        await sa.prefetch_contexts(["FPT", "HPG", "VNM"])
    finally:
        sa.build_context = real_build
    assert sorted(started) == ["FPT", "HPG", "VNM"]
    assert world.builds == 3
    out = await sa.analyze_symbol("HPG", "so sánh FPT HPG VNM")
    assert world.builds == 3  # dùng ngữ cảnh đã lấy trước
    assert sa._STALE_NOTE not in out
    again = await sa.analyze_symbol("HPG", "HPG thì sao?")
    assert again.endswith(sa._STALE_NOTE)
    # Đã có trong cache thì lấy trước không gọi lại.
    await sa.prefetch_contexts(["FPT"])
    assert world.builds == 3


def test_cache_co_tran_32_entry():
    original = dict(sa._cache)
    sa._cache.clear()
    try:
        for i in range(40):
            entry = sa._AnalysisEntry(ctx=None)
            entry.created += i * 1e-6
            sa._cache_set(f"M{i:02d}", False, entry)
        assert len(sa._cache) == sa._CACHE_MAX_ENTRIES == 32
        assert ("M39", False, True) in sa._cache and ("M00", False, True) not in sa._cache
    finally:
        sa._cache.clear()
        sa._cache.update(original)
