"""Pipeline debate cho stock/analysis.py.

Tối đa 2 lượt JSON trước bước tổng hợp (rút gọn từ cơ chế debate của
TradingAgents - Bull/Bear researcher rồi Research Manager):

    news_analysis (chỉ khi có tin) -> debate: bull + bear trong CÙNG 1 lượt
                                       (phe thận trọng phản biện thẳng luận điểm
                                        phe lạc quan ngay trong câu trả lời)

Vai Research Manager (đánh giá mức thuyết phục của action hệ thống, phản biện
quan trọng nhất) gộp vào bước tổng hợp cuối - 1 lần orchestrator.ask() ở
stock/analysis.py::analyze_symbol(), xem khối "ĐÁNH GIÁ SAU TRANH LUẬN" trong
stock_analysis_prompt.j2. Trước đây là 4 lượt JSON tuần tự (news, bull, bear,
manager) + tổng hợp = 5 lượt, nghỉ 3s giữa mỗi lượt; nay tối đa 3 lượt, và các
lượt JSON đi utility_json (Groq/Gemini) nên chỉ nghỉ khi rơi về provider-chain
(schemas.ChainPacer).

Nguyên tắc bất biến: action và mọi con số giá/entry/stop/target/tỷ trọng
đều do stock/policy.py chốt. Schema debate KHÔNG có field action/giá; bản tổng
hợp cuối vẫn bị analysis._enforce_policy_contract kiểm lại.

Lỗi ở BẤT KỲ bước nào (JSON lỗi liên tục, LLM timeout...) không được làm sập
pipeline: hàm gọi ở analysis.py nhận None cho bước đó và vẫn tổng hợp báo cáo
cuối như khi thiếu hoàn toàn phần debate (xem stock_analysis_prompt.j2, các
block đều bọc trong {% if %}).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stock import report_format as rfmt
from stock.schemas import ChainPacer, DebateCase, NewsAnalysis, ask_structured
from core.untrusted import wrap_untrusted

if TYPE_CHECKING:
    from stock.analysis import StockContext

_NO_INVENT_RULE = (
    "TUYỆT ĐỐI không tự bịa hoặc đổi bất kỳ con số giá/entry/stop/target/tỷ trọng nào - "
    "chỉ được dùng lại ĐÚNG các con số đã cho ở trên. Nếu cần nhắc tới một mốc giá, "
    "phải là mốc đã có sẵn trong dữ liệu, không tự tính mốc mới."
)


def _decision_block(ctx: "StockContext") -> str:
    d = ctx.decision
    lines = [
        f"Mã: {ctx.symbol} | Giá: {rfmt.fmt_price(ctx.price)} VND",
        f"Action hệ thống (đã chốt, không được đổi): {d.action} | Confidence: {d.confidence} | "
        f"Setup: {d.setup_type} | Market regime: {d.market_regime} | Risk level: {d.risk_level}",
    ]
    if d.reasons:
        lines.append("Lý do hệ thống: " + "; ".join(d.reasons[:6]))
    if d.stop_price is not None:
        lines.append(f"Stop đã chốt: {rfmt.fmt_price(d.stop_price)}")
    if d.target_price is not None:
        lines.append(f"Target đã chốt: {rfmt.fmt_price(d.target_price)}")
    if ctx.indicator_summary:
        lines.append(ctx.indicator_summary)
    if ctx.sector_prompt:
        lines.append(ctx.sector_prompt)
    if ctx.fundamentals_prompt:
        lines.append(ctx.fundamentals_prompt)
    return "\n".join(lines)


async def run_news_step(
    ctx: "StockContext", pacer: ChainPacer | None = None
) -> NewsAnalysis | None:
    """Bước 1: tóm tắt tác động tin tức thay vì nhét cả list tin thô vào prompt tổng hợp."""
    if not ctx.news:
        return None
    ranked = sorted(
        ctx.news, key=lambda n: not rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed)
    )[:5]
    news_lines = "\n".join(
        f"- {n.title} ({n.source}, {rfmt.fmt_news_date(n.pub_date)}) - "
        f"{'tin đúng mã đã xác nhận' if rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed) else 'CHỈ tin ngành/thị trường chung, chưa xác nhận đúng mã'}"
        for n in ranked
    )
    prompt = (
        f"Bạn tóm tắt tin tức chứng khoán Việt Nam cho một báo cáo thông tin tham khảo. Đọc danh sách tin dưới đây về mã "
        f"{ctx.symbol} và tóm tắt tác động tới nhận định kỹ thuật đang có.\n\n"
        f"[TIN TỨC]\n{wrap_untrusted(news_lines, 'tin tức Google News')}\n\n"
        f"Chỉ coi là 'relevant' nếu có ít nhất 1 tin nhắc đúng tên mã (không phải tin ngành/thị trường chung). "
        f"key_points tối đa 3 gạch đầu dòng, mỗi dòng 1 sự kiện cụ thể kèm ngày, không suy diễn thêm ngoài tin đã cho."
    )
    return await ask_structured(NewsAnalysis, prompt, step_name="news", pacer=pacer)


async def run_debate_step(
    ctx: "StockContext", news: NewsAnalysis | None, pacer: ChainPacer | None = None
) -> DebateCase | None:
    """Bước 2: bull + bear trong 1 lượt - bear phản biện thẳng luận điểm bull vừa viết
    (giống debate thật), không tốn thêm 1 lượt LLM tuần tự cho phe thứ hai."""
    news_block = f"\n[TÓM TẮT TIN TỨC]\n{news.model_dump_json(indent=2)}" if news else ""
    prompt = (
        f"Bạn ghi lại một buổi tranh luận nội bộ về mã {ctx.symbol} trước khi tổng hợp báo cáo "
        f"thông tin tham khảo. Viết CẢ HAI phe trong cùng một câu trả lời:\n"
        f"- bull: nhà phân tích LẠC QUAN - luận điểm ủng hộ chiều TĂNG GIÁ mạnh nhất có thể bảo vệ "
        f"được bằng chính số liệu bên dưới (không cần đồng ý với action hệ thống nếu action không "
        f"phải BUY - nếu action là SELL/NO_TRADE thì đây là 'kịch bản đảo chiều cần theo dõi', "
        f"không phải khuyến nghị mua).\n"
        f"- bear: nhà phân tích THẬN TRỌNG - rủi ro/điểm yếu lớn nhất của mã này, và PHẢI phản "
        f"biện thẳng vào luận điểm phe bull vừa viết ở trên (không né tránh).\n\n"
        f"[DỮ LIỆU]\n{_decision_block(ctx)}{news_block}\n\n{_NO_INVENT_RULE}\n"
        f"Mỗi phe: thesis 1-2 câu; points tối đa 3 gạch đầu dòng, mỗi dòng 1 câu ngắn bám vào "
        f"1 chỉ báo/dữ kiện cụ thể đã cho ở trên."
    )
    return await ask_structured(DebateCase, prompt, step_name="debate", pacer=pacer)


async def run_debate(
    ctx: "StockContext", pacer: ChainPacer | None = None
) -> tuple[NewsAnalysis | None, DebateCase | None]:
    """2 bước TUẦN TỰ (bước debate cần thấy tóm tắt tin). ``pacer`` dùng chung với
    bước tổng hợp cuối để giãn nhịp các lượt provider-chain liên tiếp."""
    pacer = pacer or ChainPacer()
    news = await run_news_step(ctx, pacer)
    debate = await run_debate_step(ctx, news, pacer)
    return news, debate
