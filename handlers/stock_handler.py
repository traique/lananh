import asyncio
import logging
from typing import NamedTuple

import messages
import tg_format
from handlers import common
from services.telemetry import telemetry
from stock import analysis as stock_analysis

logger = logging.getLogger(__name__)


class StockRouteResult(NamedTuple):
    handled: bool
    grounding: str


async def maybe_handle(update, user_id: int, text: str) -> StockRouteResult:
    symbols = await stock_analysis.find_valid_symbols(text)
    if not symbols:
        if stock_analysis.looks_like_price_question(text):
            await update.message.reply_text(messages.STOCK_SYMBOL_UNRESOLVED)
            return StockRouteResult(handled=True, grounding="")
        return StockRouteResult(handled=False, grounding="")

    if stock_analysis.wants_portfolio_analysis(text, symbols):
        await _handle_portfolio_analysis(update, user_id, symbols, text)
        return StockRouteResult(handled=True, grounding="")

    if stock_analysis.wants_full_analysis(text, symbols):
        await _handle_full_analysis(update, user_id, symbols, text)
        return StockRouteResult(handled=True, grounding="")

    if stock_analysis.wants_price_quote(text, symbols):
        await _handle_price_quote(update, user_id, symbols)
        return StockRouteResult(handled=True, grounding="")

    grounding = await stock_analysis.build_price_grounding(symbols)
    return StockRouteResult(handled=False, grounding=grounding)


async def _handle_portfolio_analysis(update, user_id: int, symbols: list[str], user_text: str) -> None:
    prompt_id = await telemetry.start(user_id, "portfolio_analysis", ",".join(symbols))
    status = await update.message.reply_text("🔍 Đang soi danh mục cho anh, đợi em xíu nha...")
    try:
        result_text = await stock_analysis.analyze_portfolio(symbols, user_text, user_id=user_id)
        await telemetry.success(prompt_id, "portfolio_analysis", result_text)
        await common.reply_long_text(update.message, result_text)
    except Exception as exc:
        logger.exception("Lỗi phân tích danh mục")
        await telemetry.failure(prompt_id, "portfolio_analysis", exc)
        await update.message.reply_text("❌ Có lỗi khi soi danh mục, anh thử lại sau nhé.")
    finally:
        try:
            await status.delete()
        except Exception:
            logger.debug("Không xóa được status phân tích danh mục", exc_info=True)


async def _handle_full_analysis(update, user_id: int, symbols: list[str], user_text: str) -> None:
    for symbol in symbols:
        prompt_id = await telemetry.start(user_id, "stock_analysis", symbol)
        status = await update.message.reply_text(f"🔍 Đang phân tích {symbol}...")
        try:
            result_text = await stock_analysis.analyze_symbol(symbol, user_text=user_text, user_id=user_id)
            await telemetry.success(prompt_id, "stock_analysis", result_text)
            await common.reply_long_text(update.message, result_text)
        except Exception as exc:
            logger.exception("Lỗi phân tích %s", symbol)
            await telemetry.failure(prompt_id, "stock_analysis", exc)
            await update.message.reply_text(messages.STOCK_ANALYZE_FAILED.format(symbol=symbol))
        finally:
            try:
                await status.delete()
            except Exception:
                logger.debug("Không xóa được status phân tích %s", symbol, exc_info=True)


async def _handle_price_quote(update, user_id: int, symbols: list[str]) -> None:
    prompt_ids = [await telemetry.start(user_id, "stock_price", symbol) for symbol in symbols]
    results = await asyncio.gather(
        *(stock_analysis.quick_quote(symbol) for symbol in symbols),
        return_exceptions=True,
    )
    ok_results: list[str] = []
    for symbol, prompt_id, result in zip(symbols, prompt_ids, results):
        if isinstance(result, BaseException):
            logger.error("Lỗi lấy giá cổ phiếu %s", symbol, exc_info=result)
            await telemetry.failure(prompt_id, "stock_price", result)
            await update.message.reply_text(messages.STOCK_QUOTE_FAILED.format(symbol=symbol))
            continue
        await telemetry.success(prompt_id, "stock_price", result)
        ok_results.append(result)
    if ok_results:
        await tg_format.reply_rich(update.message, "\n\n".join(ok_results))
