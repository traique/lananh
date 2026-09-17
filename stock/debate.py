"""Pipeline debate tuần tự cho stock/analysis.py.

4 bước, gọi LLM TUẦN TỰ (không song song) - bước sau thấy kết quả bước
trước, giống cơ chế debate của TradingAgents (Bull/Bear researcher rồi
Research Manager chốt action):

    news_analysis -> bull_case -> bear_case (bear thấy bull để phản biện)
                                        -> manager (FinalDecision, phản biện
                                           nhưng giữ action hệ thống)

Bước tổng hợp cuối cùng (viết tin nhắn gửi người dùng) KHÔNG nằm ở đây - vẫn
là 1 lần gọi orchestrator.ask() ở stock/analysis.py::analyze_symbol() như
trước, chỉ khác là prompt của nó giờ có thêm 4 block này làm ngữ liệu, và
Manager chỉ bổ sung góc nhìn định tính/confidence/reasoning; action cuối cùng
luôn là action đã qua gate định lượng của stock/policy.py.

Nguyên tắc bất biến: action và mọi con số giá/entry/stop/target/tỷ trọng
đều do stock/policy.py chốt. Manager có thể phản biện trong reasoning nhưng
không được thay action đã qua gate định lượng.

Lỗi ở BẤT KỲ bước nào (parse JSON lỗi liên tục, LLM timeout...) không được
làm sập pipeline: hàm gọi ở analysis.py nhận None cho bước đó và vẫn tiếp
tục các bước sau / vẫn tổng hợp báo cáo cuối như khi thiếu hoàn toàn phần
debate (xem stock_analysis_prompt.j2, các block đều bọc trong {% if %}).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from stock import backtest
from stock import report_format as rfmt
from stock.schemas import BearCase, BullCase, FinalDecision, NewsAnalysis, ask_structured
from core import config

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


async def run_news_step(ctx: "StockContext") -> NewsAnalysis | None:
    """Bước 1: tóm tắt tác động tin tức thay vì nhét cả list tin thô vào prompt tổng hợp."""
    if not ctx.news:
        return None
    ranked = sorted(ctx.news, key=lambda n: not rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed))[:5]
    news_lines = "\n".join(
        f"- {n.title} ({n.source}, {rfmt.fmt_news_date(n.pub_date)}) - "
        f"{'tin đúng mã đã xác nhận' if rfmt.is_news_relevant(n.title, ctx.symbol, n.confirmed) else 'CHỈ tin ngành/thị trường chung, chưa xác nhận đúng mã'}"
        for n in ranked
    )
    prompt = (
        f"Bạn là chuyên viên phân tích tin tức chứng khoán Việt Nam. Đọc danh sách tin dưới đây về mã "
        f"{ctx.symbol} và tóm tắt tác động tới nhận định kỹ thuật đang có.\n\n"
        f"[TIN TỨC]\n{news_lines}\n\n"
        f"Chỉ coi là 'relevant' nếu có ít nhất 1 tin nhắc đúng tên mã (không phải tin ngành/thị trường chung). "
        f"key_points tối đa 3 gạch đầu dòng, mỗi dòng 1 sự kiện cụ thể kèm ngày, không suy diễn thêm ngoài tin đã cho."
    )
    return await ask_structured(NewsAnalysis, prompt, step_name="news")


async def run_bull_step(ctx: "StockContext", news: NewsAnalysis | None) -> BullCase | None:
    """Bước 2: luận điểm tăng giá mạnh nhất có thể, chỉ dựa trên số liệu đã có."""
    news_block = f"\n[TÓM TẮT TIN TỨC]\n{news.model_dump_json(indent=2)}" if news else ""
    prompt = (
        f"Bạn đang đóng vai nhà phân tích LẠC QUAN (bull) trong 1 buổi tranh luận nội bộ trước khi ra báo cáo "
        f"cho khách hàng. Dựa trên dữ liệu dưới đây, hãy đưa ra luận điểm ủng hộ chiều TĂNG GIÁ mạnh nhất có "
        f"thể bảo vệ được bằng chính số liệu này (không cần đồng ý với action hệ thống nếu action không phải "
        f"BUY - nếu action là SELL/NO_TRADE thì đây là 'kịch bản đảo chiều cần theo dõi', không phải khuyến nghị mua).\n\n"
        f"[DỮ LIỆU]\n{_decision_block(ctx)}{news_block}\n\n{_NO_INVENT_RULE}\n"
        f"points tối đa 4 gạch đầu dòng, mỗi dòng bám vào 1 chỉ báo/dữ kiện cụ thể đã cho ở trên."
    )
    return await ask_structured(BullCase, prompt, step_name="bull")


async def run_bear_step(ctx: "StockContext", news: NewsAnalysis | None, bull: BullCase | None) -> BearCase | None:
    """Bước 3: luận điểm rủi ro, ĐƯỢC THẤY bull_case để phản biện trực tiếp (giống debate thật)."""
    news_block = f"\n[TÓM TẮT TIN TỨC]\n{news.model_dump_json(indent=2)}" if news else ""
    bull_block = f"\n[LUẬN ĐIỂM PHE LẠC QUAN VỪA ĐƯA RA - hãy phản biện trực tiếp nếu có điểm yếu]\n{bull.model_dump_json(indent=2)}" if bull else ""
    prompt = (
        f"Bạn đang đóng vai nhà phân tích THẬN TRỌNG (bear) trong cùng buổi tranh luận nội bộ đó. Nhiệm vụ: chỉ "
        f"ra rủi ro/điểm yếu lớn nhất của mã này, và nếu phe lạc quan vừa nêu luận điểm ở dưới thì phải phản "
        f"biện thẳng vào điểm đó (không né tránh).\n\n"
        f"[DỮ LIỆU]\n{_decision_block(ctx)}{news_block}{bull_block}\n\n{_NO_INVENT_RULE}\n"
        f"points tối đa 4 gạch đầu dòng, mỗi dòng bám vào 1 chỉ báo/dữ kiện cụ thể đã cho ở trên."
    )
    return await ask_structured(BearCase, prompt, step_name="bear")


async def run_manager_step(
    ctx: "StockContext", news: NewsAnalysis | None, bull: BullCase | None, bear: BearCase | None,
) -> FinalDecision | None:
    """Bước 4 (Manager) - phản biện định tính nhưng KHÔNG đổi action policy.

    Prompt không đưa entry/stop/target/tỷ trọng cụ thể; Manager chỉ đánh giá
    mức thuyết phục/rủi ro quanh action đã qua gate định lượng.
    """
    news_block = f"\n[TÓM TẮT TIN TỨC]\n{news.model_dump_json(indent=2)}" if news else ""
    bull_block = f"\n[PHE LẠC QUAN]\n{bull.model_dump_json(indent=2)}" if bull else "\n[PHE LẠC QUAN]: không có dữ liệu"
    bear_block = f"\n[PHE THẬN TRỌNG]\n{bear.model_dump_json(indent=2)}" if bear else "\n[PHE THẬN TRỌNG]: không có dữ liệu"
    d = ctx.decision
    setup_backtest_line = backtest.format_setup_stats_line(d.setup_type)
    if setup_backtest_line:
        backtest_context = (
            "Có thống kê backtest lịch sử cho đúng setup hiện tại (chỉ là dữ liệu quá khứ, không phải bảo đảm):\n"
            f"{setup_backtest_line}\n"
        )
    else:
        backtest_context = (
            "Hiện KHÔNG có thống kê backtest đủ điều kiện cho setup này trong runtime/report; "
            "không được mô tả hệ thống là 'đã qua backtest'.\n"
        )
    prompt = (
        f"Bạn là Research Manager, nghe xong buổi tranh luận nội bộ về mã {ctx.symbol}. "
        f"Action định lượng đã được hệ thống policy chốt là {d.action}; bạn KHÔNG được đổi action này. "
        f"Nhiệm vụ của bạn là đánh giá mức thuyết phục của action đó, nêu rủi ro/phản biện quan trọng nhất "
        f"và cho confidence định tính riêng.\n\n"
        f"[QUYẾT ĐỊNH RULE-BASED ĐÃ QUA GATE ĐỊNH LƯỢNG]\n"
        f"Action bắt buộc giữ nguyên: {d.action} | Confidence hệ thống: {d.confidence} | Setup: {d.setup_type} | Regime: {d.market_regime}\n"
        f"Lý do hệ thống: {'; '.join(d.reasons[:6]) if d.reasons else '(không có)'}\n"
        f"{backtest_context}"
        f"{news_block}{bull_block}{bear_block}\n\n"
        f"Trường action trong JSON PHẢI là {d.action}. Nếu bạn không đồng ý, hãy nói rõ trong reasoning vì sao "
        f"nhưng vẫn giữ action={d.action}; tuyệt đối không tạo action giao dịch mới chưa qua gate."
    )
    result = await ask_structured(FinalDecision, prompt, step_name="manager")
    if result is not None and result.action != d.action:
        result = result.model_copy(update={"action": d.action})
    return result


async def run_debate(
    ctx: "StockContext",
) -> tuple[NewsAnalysis | None, BullCase | None, BearCase | None, FinalDecision | None]:
    """Chạy đúng 4 bước TUẦN TỰ (không asyncio.gather) - mỗi bước cần thấy bước trước.
    Có nghỉ ROUTER9_STEP_DELAY_SEC giây giữa mỗi bước để giãn tải gateway LLM,
    CHỈ khi bước trước đã thật sự gọi LLM (bước trả None = không gọi, không ngủ)."""
    delay = config.ROUTER9_STEP_DELAY_SEC

    async def _pace(previous_ran: bool) -> None:
        if delay and previous_ran:
            await asyncio.sleep(delay)

    news = await run_news_step(ctx)
    await _pace(news is not None)
    bull = await run_bull_step(ctx, news)
    await _pace(bull is not None)
    bear = await run_bear_step(ctx, news, bull)
    await _pace(bear is not None)
    final_decision = await run_manager_step(ctx, news, bull, bear)
    return news, bull, bear, final_decision
