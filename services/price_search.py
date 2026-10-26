"""Tra giá sản phẩm (/gia) dùng chung cho Telegram, Zalo/Zoom và tool
tim_gia của /agent: tìm web (Tavily -> grounded fallback), cache ngắn hạn
theo tên sản phẩm và kiểm tra link chết trong kết quả.
"""

import asyncio
import logging
import re
import time

import httpx

from ai import orchestrator, tavily_client
from ai.timeouts import turn_deadline
from services import web_search
from services.public_http import public_status

logger = logging.getLogger(__name__)

# Cache ngắn hạn cho /gia
_PRICE_CACHE: dict[str, tuple[float, str]] = {}
_PRICE_CACHE_TTL_SECONDS = 30 * 60  # 30 phút


def _normalize_product_key(product_name: str) -> str:
    return " ".join(product_name.lower().split())


def get_cached_price(product_name: str) -> str | None:
    key = _normalize_product_key(product_name)
    cached = _PRICE_CACHE.get(key)
    if not cached:
        return None
    saved_at, text = cached
    age = time.time() - saved_at
    if age > _PRICE_CACHE_TTL_SECONDS:
        _PRICE_CACHE.pop(key, None)
        return None
    minutes = max(1, int(age // 60))
    return f"{text}\n\n*(⏱️ Kết quả tra cứu cách đây {minutes} phút, lấy từ cache anh nhé)*"


def set_cached_price(product_name: str, text: str) -> None:
    now = time.time()
    for key in [
        key for key, (ts, _) in _PRICE_CACHE.items() if now - ts >= _PRICE_CACHE_TTL_SECONDS
    ]:
        _PRICE_CACHE.pop(key, None)
    if len(_PRICE_CACHE) >= 128:
        _PRICE_CACHE.pop(min(_PRICE_CACHE, key=lambda key: _PRICE_CACHE[key][0]), None)
    _PRICE_CACHE[_normalize_product_key(product_name)] = (now, text)


_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
_LINK_CHECK_TIMEOUT = 6.0


async def _check_url(client: httpx.AsyncClient, url: str) -> bool | None:
    """True = link sống. False = CHẮC CHẮN hỏng (404/410/không kết nối
    được). None = không chắc (403 bị chặn bot, timeout, lỗi 5xx tạm thời...)
    - những trường hợp này KHÔNG gắn cảnh báo để tránh báo nhầm link tốt
    thành hỏng chỉ vì trang có chặn request tự động."""
    try:
        status = await public_status(client, url, method="HEAD", timeout=_LINK_CHECK_TIMEOUT)
        if status in (403, 405):
            status = await public_status(client, url, method="GET", timeout=_LINK_CHECK_TIMEOUT)
        if status < 400:
            return True
        if status in (404, 410):
            return False
        return None
    except httpx.ConnectError:
        return False
    except Exception:
        return None


async def verify_links(text: str) -> str:
    """Gắn cảnh báo vào link markdown chắc chắn đã chết; lỗi bất kỳ thì trả nguyên text."""
    matches = list(_MD_LINK_RE.finditer(text))
    if not matches:
        return text

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LanAnhBot/1.0)"}
        async with httpx.AsyncClient(headers=headers) as client:
            results = await asyncio.gather(
                *(_check_url(client, m.group(2)) for m in matches),
                return_exceptions=False,
            )
    except Exception:
        logger.exception("Lỗi khi verify link giá sản phẩm, giữ nguyên text gốc")
        return text

    out: list[str] = []
    last_end = 0
    for match, is_alive in zip(matches, results, strict=False):
        out.append(text[last_end : match.start()])
        label, url = match.group(1), match.group(2)
        if is_alive is False:
            out.append(f"[⚠️ {label} (link có thể đã đổi/hết hàng)]({url})")
        else:
            out.append(match.group(0))
        last_end = match.end()
    out.append(text[last_end:])
    return "".join(out)


PRICE_SEARCH_SYSTEM = """Bạn là trợ lý Lan Anh. Nhiệm vụ của bạn là sử dụng công cụ tìm kiếm web để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam.

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng.
2. BẮT BUỘC phải trích xuất URL (đường link) gốc của trang sản phẩm để người dùng bấm vào xem.
3. Không tự bịa giá. Nếu hệ thống báo hết hàng hoặc không có giá, hãy ghi chú rõ.
4. BẮT BUỘC dùng công cụ tìm kiếm web TRƯỚC, rồi mới trả lời - không được trả lời dựa trên trí nhớ/kiến thức đã học sẵn của bạn. Kiến thức nội bộ của bạn có thể đã LỖI THỜI (sản phẩm mới ra mắt sau thời điểm bạn được huấn luyện). Nếu kết quả tìm kiếm cho thấy sản phẩm đã có bán/có giá, PHẢI tin theo kết quả tìm kiếm dù điều đó trái với những gì bạn "nhớ". Chỉ được kết luận "chưa ra mắt" hoặc "chưa có giá" khi kết quả tìm kiếm thực sự không tìm thấy thông tin nào về sản phẩm này.
5. ƯU TIÊN SO SÁNH NHIỀU CỬA HÀNG KHÁC NHAU (vd Thế Giới Di Động, CellphoneS, FPT Shop, Hoàng Hà Mobile...) - hãy chủ động tìm kiếm thêm để có giá từ ít nhất 2-3 cửa hàng khác nhau nếu có thể. CHỈ liệt kê nhiều dòng của CÙNG 1 cửa hàng (vd nhiều dung lượng/màu) khi thực sự không tìm được cửa hàng nào khác bán sản phẩm này.

Trình bày kết quả theo ĐÚNG định dạng list (KHÔNG dùng bảng markdown vì Telegram không hiển thị được bảng) và văn phong sau:

**{product_name}** — giá cập nhật mới nhất

Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:

🏨 **[Tên shop 1]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

🏨 **[Tên shop 2]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

(lặp lại 1 khối như trên cho mỗi shop tìm được, tối đa 5 shop)

🔥 **Chỗ rẻ nhất em thấy:**
👉 **[Tên shop rẻ nhất]**: [Giá rẻ nhất]đ cho [Màu/phiên bản].

*(Lưu ý nhỏ: Giá này em tra cứu online ngay lúc này, có thể thay đổi tùy tồn kho từng chi nhánh hoặc flash sale anh nhé).*

QUAN TRỌNG VỀ LINK: mỗi link BẮT BUỘC viết đúng cú pháp markdown [Chữ hiển thị](https://url-that-page), không được dán URL trần, không được để URL trong ngoặc đơn kèm mô tả."""

# Biến thể của PRICE_SEARCH_SYSTEM dùng khi đã có sẵn kết quả tìm kiếm thật
# (từ Tavily, xem search_price() bên dưới) thay vì yêu cầu model tự gọi
# công cụ tìm kiếm web - giữ đúng văn phong/định dạng trả lời, chỉ đổi phần
# nguồn dữ liệu và nhắc model không tự bịa từ trí nhớ.
PRICE_SEARCH_SYSTEM_FROM_RESULTS = """Bạn là trợ lý Lan Anh. Dưới đây là kết quả tìm kiếm web THẬT (qua Tavily) cho sản phẩm "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam.

YÊU CẦU QUAN TRỌNG:
1. CHỈ được dùng thông tin có trong KẾT QUẢ TÌM KIẾM bên dưới. TUYỆT ĐỐI KHÔNG dùng trí nhớ/kiến thức đã học sẵn, không tự bịa giá, không ước lượng.
2. So khớp CHÍNH XÁC phiên bản/dung lượng.
3. BẮT BUỘC trích xuất đúng URL (đường link) gốc của trang sản phẩm có trong kết quả tìm kiếm để người dùng bấm vào xem.
4. Nếu kết quả tìm kiếm không đủ dữ liệu giá cho một shop, bỏ qua shop đó thay vì đoán. Nếu KHÔNG có shop nào đủ dữ liệu, nói thẳng là chưa tra được giá.
5. Kết quả tìm kiếm bên dưới có thể đến từ NHIỀU cửa hàng khác nhau. HÃY SO SÁNH GIÁ TỪ CÀNG NHIỀU CỬA HÀNG KHÁC NHAU CÀNG TỐT - liệt kê MỖI CỬA HÀNG khác nhau tìm được tối đa 1 dòng (chọn phiên bản/dung lượng phổ biến nhất của cửa hàng đó nếu có nhiều lựa chọn). CHỈ liệt kê nhiều dòng của CÙNG 1 cửa hàng khi kết quả tìm kiếm thực sự chỉ có đúng 1 cửa hàng bán sản phẩm này.

Trình bày kết quả theo ĐÚNG định dạng list (KHÔNG dùng bảng markdown vì Telegram không hiển thị được bảng) và văn phong sau:

**{product_name}** — giá cập nhật mới nhất

Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:

🏨 **[Tên shop 1]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

🏨 **[Tên shop 2]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

(lặp lại 1 khối như trên cho mỗi shop tìm được, tối đa 5 shop)

🔥 **Chỗ rẻ nhất em thấy:**
👉 **[Tên shop rẻ nhất]**: [Giá rẻ nhất]đ cho [Màu/phiên bản].

*(Lưu ý nhỏ: Giá này em tra cứu online ngay lúc này, có thể thay đổi tùy tồn kho từng chi nhánh hoặc flash sale anh nhé).*

QUAN TRỌNG VỀ LINK: mỗi link BẮT BUỘC viết đúng cú pháp markdown [Chữ hiển thị](https://url-that-page), không được dán URL trần, không được để URL trong ngoặc đơn kèm mô tả.

KẾT QUẢ TÌM KIẾM (Tavily):
{search_results}"""


# Tavily yếu/lỗi -> Groq browser_search trước, rồi Google Search grounding.
# Không dùng OpenRouter ở nhánh /gia vì không có đảm bảo tool search thật.
_PRICE_FALLBACK_PROVIDERS = ["groq", "api1", "api2"]


async def search_price(product_name: str) -> tuple[str, bool]:
    """Tìm giá sản phẩm: Tavily trước (1 request duy nhất, search_depth="advanced"
    để có nguồn đa dạng hơn basic, kèm giới hạn tối đa 2 kết quả/domain để 1
    shop SEO mạnh không chiếm hết top-N - xem tavily_client.search()), lỗi/
    rỗng/yếu thì fallback Groq browser_search -> Google Search (api1 -> api2). Dùng
    chung cho Telegram (handlers/commands.py::price_cmd), Zalo/Zoom
    (services/channel_command_service.py::_price) và tool tim_gia của /agent.

    Trả về (text, used_fallback); caller giữ contract cũ để gắn nhãn API khi
    response provider có đánh dấu fallback.

    MỘT hạn chót CHAT_TURN_TIMEOUT_SEC cho cả lượt (Tavily rồi tối đa 2 lượt hỏi
    AI): trước đây mỗi orchestrator.ask tự mở 200s riêng nên /gia có thể giữ khoá
    lượt gấp đôi. Hết hạn -> TurnTimeoutError (caller báo "⏱️ ..."). Gọi lồng
    trong lượt khác (tool tim_gia của /agent) thì giữ hạn SỚM hơn của lượt ngoài.
    verify_links() chạy sau, ngoài hạn này (mỗi link có timeout riêng).
    """
    with turn_deadline():
        return await _search_price(product_name)


async def _search_price(product_name: str) -> tuple[str, bool]:
    search_results: str | None = None
    try:
        tavily_response = await tavily_client.search_results(
            f"giá {product_name} chính hãng Việt Nam mua ở đâu",
            max_results=12,
            search_depth="advanced",
            max_results_per_domain=2,
        )
        if web_search.is_tavily_quality_sufficient(tavily_response, min_results=3, min_domains=3):
            search_results = tavily_client.format_search_results(tavily_response)
        else:
            logger.info(
                "Tavily tìm giá %r chỉ có %d kết quả/%d domain; chuyển grounded fallback.",
                product_name,
                len(tavily_response.results),
                len(tavily_response.unique_domains),
            )
    except tavily_client.TavilyError:
        logger.warning(
            "Tavily lỗi khi tìm giá %r, chuyển grounded fallback.",
            product_name,
            exc_info=True,
        )

    if search_results:
        try:
            instruction = PRICE_SEARCH_SYSTEM_FROM_RESULTS.format(
                product_name=product_name, search_results=search_results
            )
            response = await orchestrator.ask(instruction)
            result_text = (response.text or "").strip()
            if result_text:
                return result_text, bool(getattr(response, "used_fallback", False))
            logger.warning(
                "Tavily có kết quả nhưng model không trả lời được cho %r, chuyển grounded fallback.",
                product_name,
            )
        except Exception:
            logger.exception(
                "Lỗi khi định dạng giá từ kết quả Tavily cho %r, chuyển grounded fallback.",
                product_name,
            )

    instruction = PRICE_SEARCH_SYSTEM.format(product_name=product_name)
    response = await orchestrator.ask(
        instruction,
        enable_search=True,
        require_real_search=True,
        providers_override=_PRICE_FALLBACK_PROVIDERS,
    )
    result_text = (response.text or "").strip()
    return result_text, bool(getattr(response, "used_fallback", False))
