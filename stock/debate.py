"""Pipeline debate tuần tự cho stock/analysis.py.

4 bước, gọi LLM TUẦN TỰ (không song song) - bước sau thấy kết quả bước
trước, giống cơ chế debate của TradingAgents (Bull/Bear researcher rồi
Research Manager chốt action):

    news_analysis -> bull_case -> bear_case (bear thấy bull để phản biện)
                                        -> manager (FinalDecision, ĐƯỢC PHÉP
                                           chọn action khác action hệ thống)

Bước tổng hợp cuối cùng (viết tin nhắn gửi người dùng) KHÔNG nằm ở đây - vẫn
là 1 lần gọi orchestrator.ask() ở stock/analysis.py::analyze_symbol() như
trước, chỉ khác là prompt của nó giờ có thêm 4 block này làm ngữ liệu, và
PHẢI hiển thị SONG SONG action hệ thống (rule-based) với FinalDecision.action
(AI, có thể khác) để người dùng tự đối chiếu - không được chỉ in 1 trong 2.

Nguyên tắc bất biến (xem stock/schemas.py để biết chi tiết): action là field
DUY NHẤT được phép khác code (theo yêu cầu, ở bước manager). Mọi con số
giá/entry/stop/target/tỷ trọng vẫn TUYỆT ĐỐI do stock/policy.py chốt - nếu
FinalDecision.action khác action hệ thống thì KHÔNG có vùng giá nào cho
action mới đó, layer hiển thị phải nói rõ "chưa qua gate định lượng" thay vì
tự suy ra 1 con số.

Lỗi ở BẤT KỲ bước nào (parse JSON lỗi liên tục, LLM timeout...) không được
làm sập pipeline: hàm gọi ở analysis.py nhận None cho bước đó và vẫn tiếp
tục các bước sau / vẫn tổng hợp báo cáo cuối như khi thiếu hoàn toàn phần
debate (xem stock_analysis_prompt.j2, các block đều bọc trong {% if %}).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from stock import report_format as rfmt
from stock.schemas import BearCase, BullCase, FinalDecision, NewsAnalysis, ask_structured

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
    ranked = sorted(ctx.news, key=lambda n: not rfmt.title_mentions_symbol(n.title, ctx.symbol))[:5]
    news_lines = "\n".join(
        f"- {n.title} ({n.source}, {rfmt.fmt_news_date(n.pub_date)}) - "
        f"{'nhắc đúng mã' if rfmt.title_mentions_symbol(n.title, ctx.symbol) else 'CHỈ tin ngành/thị trường chung, không nhắc tên mã'}"
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
    """Bước 4 (Manager) - nghe hết news/bull/bear + quyết định gốc của code, tự chọn action cuối.

    Đây là bước DUY NHẤT được phép ra action khác code. Prompt CỐ Ý không
    đưa entry/stop/target/tỷ trọng cụ thể vào cho Manager cân nhắc - Manager
    chỉ thấy action/confidence/lý do của code, không thấy vùng giá, để
    không có cửa nào "tiện tay" chỉnh số nếu đổi action.
    """
    news_block = f"\n[TÓM TẮT TIN TỨC]\n{news.model_dump_json(indent=2)}" if news else ""
    bull_block = f"\n[PHE LẠC QUAN]\n{bull.model_dump_json(indent=2)}" if bull else "\n[PHE LẠC QUAN]: không có dữ liệu"
    bear_block = f"\n[PHE THẬN TRỌNG]\n{bear.model_dump_json(indent=2)}" if bear else "\n[PHE THẬN TRỌNG]: không có dữ liệu"
    d = ctx.decision
    prompt = (
        f"Bạn là Research Manager, nghe xong buổi tranh luận nội bộ về mã {ctx.symbol} và phải chốt 1 action "
        f"CUỐI CÙNG. Bạn ĐƯỢC PHÉP giữ nguyên hoặc đổi khác với action của hệ thống rule-based bên dưới, dựa "
        f"trên sức thuyết phục của 2 phe tranh luận và tin tức.\n\n"
        f"[QUYẾT ĐỊNH CỦA HỆ THỐNG RULE-BASED - đã qua backtest, có gate định lượng, nhưng bạn không bắt buộc "
        f"phải đồng ý]\n"
        f"Action: {d.action} | Confidence: {d.confidence} | Setup: {d.setup_type} | Regime: {d.market_regime}\n"
        f"Lý do hệ thống: {'; '.join(d.reasons[:6]) if d.reasons else '(không có)'}"
        f"{news_block}{bull_block}{bear_block}\n\n"
        f"Nếu bạn chọn action KHÁC action hệ thống, reasoning PHẢI nêu rõ vì sao dám đi ngược lại 1 hệ thống đã "
        f"backtest - đây là quyết định định tính, chưa có kiểm định số liệu, nên lý do phải thật thuyết phục "
        f"(ví dụ: tin tức quá mới/quá lớn mà hệ thống kỹ thuật chưa kịp phản ánh), không đổi chỉ vì thích khác."
    )
    return await ask_structured(FinalDecision, prompt, step_name="manager")


async def run_debate(
    ctx: "StockContext",
) -> tuple[NewsAnalysis | None, BullCase | None, BearCase | None, FinalDecision | None]:
    """Chạy đúng 4 bước TUẦN TỰ (không asyncio.gather) - mỗi bước cần thấy bước trước."""
    news = await run_news_step(ctx)
    bull = await run_bull_step(ctx, news)
    bear = await run_bear_step(ctx, news, bull)
    final_decision = await run_manager_step(ctx, news, bull, bear)
    return news, bull, bear, final_decision
