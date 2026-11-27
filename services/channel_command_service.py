"""Lệnh / trên Zalo/Zoom (kênh ngoài Telegram): bảng lệnh sinh từ danh bạ
services/command_catalog.py (cùng nguồn với bảng lệnh Telegram của bot_app.py), mỗi
bộ xử lý ở đây trả (tin gửi đi, "api" nếu dùng provider dự phòng)."""

import base64
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import messages
from ai import agnes_client, orchestrator, router9_client, tavily_client
from ai.timeouts import TurnTimeoutError
from core import config, database as db, version
from core.text_utils import parse_on_off
from services import (
    command_catalog,
    conversation_log,
    divination_commands,
    image_prompt,
    memory_commands,
    memory_service,
    price_search,
    prompt_templates,
    provider_health,
    reply_audience,
    search_status,
    translate_service,
    usage_quota,
    usage_stats,
)
from services import answer_feedback, daily_commands, expense_service, reminder_commands
from services import (
    attention,
    background_jobs,
    notify_hub,
    nutrition_service,
    release_notes,
    routines,
    voice_reply,
)
from services import runtime_status, turn_control
from services.telemetry import telemetry

# /help theo vai trò: thành viên Zalo chỉ thấy lệnh mình dùng được (không có
# mục "Chỉ admin" và /fb_*); admin Zalo và Zoom (chủ bot) thấy đủ.
_HELP_INTRO_ADMIN = """📖 Lệnh trên Zalo/Zoom
💬 Nhắn bình thường để trò chuyện. Nhắc tới 1 mã cổ phiếu là có giá realtime; nói "phân tích FPT" để xem báo cáo
(thành viên nhận bản tham khảo, admin nhận bản đầy đủ). Nói "đã mua FPT 100 giá 120" để ghi danh mục."""
_HELP_INTRO_MEMBER = """📖 Lệnh dùng được
💬 Nhắn bình thường để trò chuyện. Nhắc tới 1 mã cổ phiếu là có giá realtime; nói "phân tích FPT" để xem
báo cáo tham khảo. Nói "đã mua FPT 100 giá 120" để ghi danh mục.
⏳ Mỗi ngày có giới hạn số lượt trò chuyện và lượt tạo ảnh, luận giải chi tiết, tra web; mở lại lúc 0h."""
_HELP_COMMON = """Lịch, thần số học, tử vi, bát tự, kỳ môn, phong thủy
/lich <câu hỏi> — tra lịch tự do, không tốn AI: /lich, /lich 20/11, /lich rằm tháng 8,
  /lich tháng này, /lich trung thu, /lich ngày tốt tháng này, /lich tuổi 15/3/1990,
  /lich sao hạn 1990 nam năm 2027, /lich hội Lim, /lich lễ hội tháng Giêng ở Bắc Ninh
  (gõ /lich help để xem thêm ví dụ)
/tsh <ngày sinh> [họ tên] — thần số học: con số chủ đạo, biểu đồ ngày sinh (không tốn AI)
/tsh_chitiet <như trên> — AI diễn giải chi tiết
/tuvi <ngày sinh> <giờ> <nam|nữ> [năm xem] — an lá số tử vi kèm vận năm (không tốn AI),
  vd /tuvi 15/3/1990 9h30 nam năm 2027
/tuvi_chitiet <như trên> — AI luận giải lá số
/battu <ngày sinh> [giờ] <nam|nữ> — Bát Tự tứ trụ: tàng can, thập thần, đại vận (không tốn AI)
/battu_chitiet <như trên> — AI luận giải Bát Tự
/kymon [ngày] [giờ] — lập bàn Kỳ Môn Độn Giáp (không tốn AI), mặc định lúc này, vd /kymon 25/10/2026 9:00
/kymon_chitiet [thời điểm] <việc cần xem> — AI luận giải bàn Kỳ Môn cho việc đó
/loban <số đo> [cửa|giường|bàn thờ...] — thước Lỗ Ban: cung tốt xấu, số đo tốt gần nhất
/cungphi <năm sinh> <nam|nữ> — cung phi Bát Trạch, hướng nhà tốt xấu (không tốn AI)

Đời sống (số liệu thật, không tốn AI)
/vang [sjc|nhẫn|doji|pnj|bảo tín|thế giới] — giá vàng SJC, DOJI, PNJ, Bảo Tín, thế giới
/tygia [mã|100 usd] — tỷ giá Vietcombank, đổi tiền
/xang [xăng|diesel|dầu hỏa] — giá xăng dầu Petrolimex vùng 1/vùng 2, PVOIL
/xoso [mb|mn|mt|tỉnh] [hôm nay|hôm qua|dd/mm|chủ nhật] [số vé] — kết quả xổ số, dò vé
  (vd /xoso tây ninh, /xoso đà lạt 123456); /xoso tinh — lịch các đài, đài quay hôm nay
/thoitiet [địa điểm] — thời tiết; /thoitiet nha <tỉnh> — đặt thành phố của riêng mình
/bantin on|off|<giờ>|xem — bản tin sáng cá nhân (thời tiết, âm lịch, nhắc việc, vàng, danh mục, tin);
  /bantin xang on|off — thêm giá xăng dầu
/chuy — có gì cần mình chú ý: nhắc việc hôm nay, ngày nhớ, cảnh báo danh mục, hỏi thăm đang chờ
  (hoặc hỏi "hôm nay có gì cần lo")
/yenlang on|off|22-7 — giờ yên lặng: ban đêm chỉ nhắn ngay nhắc việc đã đặt, tin khác gom lại sáng gửi
/viec <yêu cầu> — giao việc cho Lan Anh làm ở nền, vẫn nhắn bình thường được, xong em báo;
  /viec — xem việc; /viec xem <số> — xem kết quả; /viec huy <số> — huỷ

Nhắc việc và ngày nhớ (giờ tính bằng code)
/nhac — xem nhắc việc đang chờ; /nhac 8h tối mai gọi mẹ — đặt nhắc;
  /nhac 7h sáng mỗi ngày uống thuốc, /nhac mùng 1 và rằm 6h thắp hương — nhắc lặp
/nhac huy <số> — huỷ; /nhac hoan <số> 30p — dời; vừa nhận tin nhắc thì trả lời "hoãn 10p" hoặc "xong"
/ngaynho — giỗ, sinh nhật, kỷ niệm; /ngaynho giỗ ông nội 12/3 âm trước 3 ngày; /ngaynho xoa <số>

Chi tiêu (cộng bằng code, riêng từng người)
Nhắn "cafe 45k", "đổ xăng 1tr2", "2 củ tiền điện" là Lan Anh ghi luôn (nhắn "huỷ" trong 10 phút nếu nhầm);
  gửi ảnh kèm chữ "hoá đơn" để đọc hoá đơn (lưu nháp)
/chitieu [hôm nay|tuần này|tháng trước|tháng 10] — tổng theo danh mục; /chitieu ds — khoản gần nhất
/chitieu cafe 45k — ghi 1 khoản; /chitieu xoa <số> — xoá

Ăn uống (kcal ước lượng theo bảng món Việt, riêng từng người)
/an phở bò sáng, /an 2 bát cơm, cá kho tối — ghi bữa ăn; /an — xem hôm nay; /an tuan — 7 ngày;
  /an xoa <số> — xoá 1 món

Chat và tiện ích
/prompt <mô tả> — viết prompt tạo ảnh
/anh <mô tả> — tạo ảnh thật (Agnes AI)
🖼️ Gửi 1 ảnh (Zalo) — nhận prompt tạo ảnh viết dựa trên ảnh đó; kèm câu hỏi ("ảnh này là gì?",
  "dịch giúp chữ trong ảnh") thì Lan Anh trả lời về ảnh
🎙️ Gửi tin thoại — Lan Anh nghe rồi trả lời như tin chữ (tin thoại không tự ghi danh mục, chi tiêu,
  nhắc việc - muốn ghi thì gõ chữ)
/noi on|off|auto — trả lời kèm tin thoại (auto: chỉ khi mình vừa gửi tin thoại; Zoom: kèm link nghe)
/noi giong <tên> — đổi giọng đọc (/noi giong: xem gợi ý)
📄 Gửi file PDF, TXT, Word (.docx) — tóm tắt hoặc trả lời câu hỏi kèm theo
/gia <sản phẩm> — tìm và so sánh giá
/dich [ja>vi|vi>ja] <nội dung> — dịch chat công việc Nhật-Việt
/reset — xoá ngữ cảnh chat
/history — xem 10 lượt gần nhất; hỏi "hôm trước em nói gì về ..." để tìm lại trong lịch sử chat
/memory [on|off] — xem trí nhớ dài hạn (đánh số), bật hoặc tắt
/quen <số|từ khoá> — quên 1 mục; /nho <điều cần nhớ> — ghim 1 điều; /ghim, /boghim <số>
/forget — xoá hết trí nhớ dài hạn
/notes [từ khoá] — xem/tìm ghi chú; /xoaghichu <số> — xoá 1 ghi chú
/xuatdulieu — xem dữ liệu em đang lưu; /xoadulieu — xoá dữ liệu cá nhân của mình
/hoithan on|off — em chủ động nhắn hỏi thăm việc sắp tới đã kể (mặc định tắt trên Zalo)
/hoc [ok|bo] — duyệt cách trả lời em tự rút ra (hoặc nhắn "ghi nhớ"/"bỏ" ngay sau câu em hỏi)

Danh mục cổ phiếu (riêng từng người)
/danhmuc — xem danh mục, lãi/lỗ, cảnh báo stop/target
/themcp <MÃ> <SỐ_LƯỢNG> <GIÁ_VỐN> [STOP] [TARGET] — thêm giao dịch mua (giá dưới 1.000 hiểu là nghìn đồng)
/capnhatcp <MÃ> <SỐ_LƯỢNG> <GIÁ_VỐN> [STOP] [TARGET] — ghi đè vị thế
/bancp <MÃ> [SỐ_LƯỢNG|hết] [GIÁ_BÁN] — bán bớt hoặc bán hết (có giá thì tính lãi/lỗ đã chốt)
/xoacp <MÃ> — xoá mã khỏi danh mục
/lichsucp [MÃ] — sổ giao dịch; /laicp [THÁNG] — lãi/lỗ đã chốt sau phí, thuế
/giaban <MÃ> <GIÁ> — bổ sung giá cho lệnh bán chưa có giá
/theodoi [MÃ...] — danh sách theo dõi; /botheodoi <MÃ> — bỏ theo dõi
/canhbaogia <MÃ> [về|lên] <GIÁ> — báo khi giá chạm ngưỡng đã đặt; /xoacanhbao <MÃ> [GIÁ]"""
_HELP_ADMIN_ONLY = """Chỉ admin
/chuy — admin thấy thêm bài Facebook chờ duyệt; chủ bot thấy thêm lỗi mới, provider, giám sát
/lichtacvu mỗi sáng thứ 2 8h tóm tắt tin về VNM cho anh — tác vụ định kỳ: đến giờ em chạy yêu cầu
  rồi gửi kết quả (khác /nhac chỉ nhắc câu cố định); /lichtacvu — xem; huy|tam dung|tiep <số>
/model [tên|auto] — xem hoặc đổi model
/status — xem trạng thái provider
/phienban [số bản|all] — phiên bản đang chạy và lịch sử cập nhật
/thongke [Nd|Ngiờ] — thống kê lượt gọi theo user/model, mặc định 7 ngày
/danhgia [Nd] — số 👍/👎 câu trả lời theo provider/model (chỉ chủ bot), mặc định 30 ngày
/userouter9 — thử lại 9Router
/router9 on|off — bật/tắt 9Router thủ công
/tavily on|off — bật/tắt tra web Tavily (tắt thì dùng web search của provider)
/anh on|off — bật/tắt tạo ảnh Agnes AI
/agent <câu hỏi> — agent tự tra cứu nhiều bước để trả lời (thử nghiệm)
/rag <câu hỏi> — tra cứu kiến thức trong thư mục rag/
/ragxuly <file> — dọn file md OCR trong rag/ ở nền, kết quả báo về Telegram chủ bot
/bangtinsang (hoặc /bantinsang) — gửi ngay Bản tin sáng tới cả Zalo + Zoom (bình thường tự gửi lúc 8h)
/tintuc — đọc Điểm tin mới ngay tại đúng kênh đang gọi lệnh
/nhomzalo [từ khoá] — mọi nhóm Zalo bot đang ở: tên, số thành viên, group ID
/nhom, /themnhom <số|group_id> [tên], /xoanhom <group_id|tên> — nhóm Zalo đang theo dõi
  (số thứ tự lấy từ /nhomzalo; bỏ trống tên thì em đặt theo tên nhóm)
/tongket <tên> [24h|7d|homnay|homqua] — tổng kết nhóm bằng AI; /dangnoi <tên> — xem nguyên văn hôm nay
/tongket on|off [tên] — bật/tắt tổng kết tự động hằng ngày (bỏ trống tên = mọi nhóm)
/tinhieu on|off [tên] [admin|tatca] — báo tín hiệu mua/bán cổ phiếu từ nhóm về Zoom + Zalo;
  /tinhieu <mã> — ai đã gợi ý mã đó; /tinhieu help — đủ cách dùng
  (dữ liệu nhóm Zalo, dùng được từ cả Zalo lẫn Zoom)"""
_HELP_FACEBOOK = """📣 Zalo → Facebook Page (tách riêng khỏi /tongket, chỉ admin):
/fb_nhom — xem nhóm nguồn Facebook
/fb_themnhom <số|group_id> [tên] — thêm nhóm Zalo làm nguồn đăng Facebook (số lấy từ /nhomzalo)
/fb_xoanhom <group_id|tên> — bỏ nhóm khỏi luồng Facebook
/fb_pages — xem các Facebook Page đã cấu hình (fb_ok sẽ đăng lên tất cả)
/fb_market <stock|news> [dang] — xem thử bài Page chứng khoán/tin CafeF; thêm 'dang' để đăng thật
/fb_xem <post_id> — xem bài đang chờ duyệt
/fb_sua <post_id> <nội dung> — sửa nội dung bài chờ
/fb_link <post_id> <affiliate_url> — nhập short-link affiliate khi bài chỉ có 1 link Shopee
/fb_link <post_id> <source_url> <affiliate_url> — nhập từng link khi bài có nhiều link
(đủ link thì AI tự viết lại bài, link affiliate được thả ở bình luận đầu tiên)
/fb_ok <post_id> — duyệt và đăng lên TẤT CẢ Facebook Page đang cấu hình; page nào lỗi sẽ báo riêng,
  chạy lại /fb_ok để chỉ thử lại page lỗi, page đã đăng OK không bị đụng tới
/fb_check <post_id> — kiểm tra từng page đã published/công khai chưa, lấy permalink
/fb_reconcile — đối soát lượt đăng bị gián đoạn (bài kẹt POSTING/UNKNOWN)
/fb_boqua <post_id> — bỏ bài chờ
/fb_reset xacnhan — xóa toàn bộ bài Facebook đã lưu của tài khoản, reset ID về #1 khi có thể
/fb_loclai — chạy lại bộ lọc cho các bài đang chờ, bỏ bài không đạt
/fb_boloc [reset] — thống kê bộ lọc: bao nhiêu bài vào hàng chờ, bị bỏ/trùng vì sao, số bài đang chờ
(hàng chờ giữ tối đa 150 bài, bài trùng trong 7 ngày không được đưa vào)"""
_HELP_FOOTER = (
    "/huy — dừng câu trả lời đang chạy dở (trong lúc chờ vẫn dùng được /help, /notes, "
    "/lich, /tsh, /tuvi, /battu, /kymon, /loban, /cungphi, /history)\n"
    "/sai [lý do] — báo câu trả lời vừa rồi chưa ổn để Lan Anh rút kinh nghiệm\n"
    "/help — hướng dẫn ngắn; /help <chủ đề> (vd /help vàng); /help all — bản đầy đủ này"
)

HELP_MEMBER = "\n\n".join((_HELP_INTRO_MEMBER, _HELP_COMMON, _HELP_FOOTER))
HELP_ADMIN = "\n\n".join(
    (_HELP_INTRO_ADMIN, _HELP_COMMON, _HELP_ADMIN_ONLY, _HELP_FACEBOOK, _HELP_FOOTER)
)
# Tên cũ (bản đầy đủ) - giữ cho nơi khác/test đang import.
HELP = HELP_ADMIN

# Ảnh do lệnh /anh tạo ra được "gửi kèm" bằng ContextVar thay vì đổi kiểu trả
# về của maybe_handle_command() (đang là tuple[list[str], str|None] và có
# ~30 điểm return rải khắp file) - ContextVar cô lập đúng theo từng asyncio
# Task, nên nhiều request Zalo/Zoom chạy đồng thời KHÔNG ghi đè ảnh của nhau,
# khác với 1 biến module thường. Nơi gọi maybe_handle_command() (xem
# services/channel_chat_service.py) phải gọi take_pending_image() ngay sau
# để lấy (và xoá) ảnh, nếu có.
_pending_image: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_pending_image_b64", default=None
)
# Riêng cho Zoom: Chatbot API của Zoom nhận thẳng URL ảnh công khai (Zoom tự
# tải về), KHÔNG cần base64/Buffer như Zalo - xem channels/zoom.py::send_image_message.
_pending_image_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_pending_image_url", default=None
)


def take_pending_image() -> str | None:
    image = _pending_image.get()
    if image is not None:
        _pending_image.set(None)
    return image


def take_pending_image_url() -> str | None:
    url = _pending_image_url.get()
    if url is not None:
        _pending_image_url.set(None)
    return url


def help_text(user_id: int, argument: str, is_admin: bool, channel: str) -> str:
    """/help: bản ngắn "cứ nhắn tự nhiên như..." + danh sách chủ đề; /help <chủ đề>:
    lệnh của chủ đề đó (services/command_catalog.py); /help all: bản đầy đủ cũ. Thành
    viên Zalo không thấy lệnh/chủ đề admin."""
    from services import nl_commands

    role = nl_commands.role_for(user_id, is_admin)
    you = reply_audience.forms()[0]
    text = nl_commands.help_reply(argument, role, channel, you)
    if text is None:
        return HELP_ADMIN if is_admin else HELP_MEMBER
    return text


def _about(user_id: int) -> str:
    """Cách gọi người gõ lệnh trong câu trả lời: chủ bot "anh", thành viên Zalo
    theo cách xưng hô riêng (core/address.py), chưa đặt thì trung tính."""
    if config.is_owner_user_id(user_id):
        return "anh"
    return reply_audience.current().address_form or ""


async def attention_text(user_id: int, channel: str, is_admin: bool) -> str:
    """Tin /chuy của người gõ: chủ bot (Telegram/Zoom hoặc Zalo controller) thấy cả
    mục hệ thống; admin Zalo thấy bài Facebook chờ duyệt; thành viên chỉ dữ liệu mình."""
    return await attention.build(
        user_id,
        channel,
        is_owner=await reply_audience.is_owner(user_id),
        is_admin=is_admin,
        address_form=reply_audience.current().address_form,
    )


def _arg(text: str) -> str:
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


async def _prompt(
    user_id: int, description: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not description:
        return ["Cú pháp: /prompt <mô tả muốn tạo prompt>"], None
    instruction, identity = prompt_templates.build_text_instruction(description)
    hint = identity.mode_hint
    prompt_id = await telemetry.start(user_id, "prompt_generator", description, channel=channel)
    try:
        response = await image_prompt.from_text(instruction)
        output = (response.text or "").strip()
        await telemetry.success(prompt_id, "prompt_generator", output or "(không có nội dung)")
        if not output:
            return [await reply_audience.error_text("prompt_empty", user_id)], None
        # Prompt là 1 tin riêng (Zalo/Zoom không có nút chép khối như Telegram).
        return [f"📝 Prompt gợi ý ở tin dưới - nhấn giữ để chép.\n{hint}", output], (
            "api" if getattr(response, "used_fallback", False) else None
        )
    except Exception as exc:
        await telemetry.failure(prompt_id, "prompt_generator", exc)
        detail = messages.exception_detail(exc)
        return [await reply_audience.error_text("prompt", user_id, detail)], None


async def _price(user_id: int, product: str, channel: str = "zalo") -> tuple[list[str], str | None]:
    if not product:
        return ["Cú pháp: /gia <tên sản phẩm>, ví dụ /gia iPhone 16 Pro"], None
    cached = price_search.get_cached_price(product)
    if cached:
        return [cached], None
    prompt_id = await telemetry.start(user_id, "price_search", product, channel=channel)
    try:
        output, used_fallback = await price_search.search_price(product)
        output = await price_search.verify_links(output)
        if output:
            price_search.set_cached_price(product, output)
        await telemetry.success(prompt_id, "price_search", output or "(không có nội dung)")
        return [output or "Không tìm được giá lúc này."], ("api" if used_fallback else None)
    except TurnTimeoutError as exc:
        await telemetry.failure(prompt_id, "price_search", exc)
        return [await reply_audience.error_text("timeout", user_id, str(exc))], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "price_search", exc)
        detail = messages.exception_detail(exc)
        return [await reply_audience.error_text("price", user_id, detail)], None


async def _translate(
    user_id: int, argument: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not argument.strip():
        return [
            "Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n"
            "Không chỉ định chiều thì tự nhận diện theo chữ Nhật trong câu.\n"
            "Câu tiếng Anh (không dấu) em sẽ không tự đoán chiều - chỉ định chiều dịch giúp em nhé.\n"
            "Ví dụ: /dich お世話になります。確認お願いします。"
        ], None

    first_token, _, rest = argument.partition(" ")
    direction = translate_service.parse_explicit_direction(first_token)
    text = rest if direction else argument
    if not text.strip():
        return ["Cú pháp: /dich [ja>vi|vi>ja] <nội dung>"], None

    # Text Latin thuần không dấu tiếng Việt (tiếng Anh...) - KHÔNG đoán mò
    # chiều "Việt→Nhật" như trước, hỏi lại để người dùng chủ động chọn.
    if direction is None and translate_service.looks_english(text):
        return [
            "Câu này là tiếng Anh nên em không tự đoán được cần dịch sang ngôn ngữ nào.\n"
            "Chỉ định giúp em nhé:\n"
            "• /dich ja>vi <nội dung> — dịch sang tiếng Việt\n"
            "• /dich vi>ja <nội dung> — dịch sang tiếng Nhật"
        ], None

    prompt_id = await telemetry.start(user_id, "translate", text, channel=channel)
    try:
        result, resolved, response = await conversation_log.run_and_record(
            user_id,
            text,
            conversation_log.KIND_TRANSLATE,
            lambda: translate_service.translate(text, direction),
        )
    except translate_service.TextTooLongError as exc:
        return [str(exc)], None
    except ValueError as exc:
        return [f"Cú pháp: /dich [ja>vi|vi>ja] <nội dung>\n({exc})"], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "translate", exc)
        return ["❌ Không dịch được lúc này, thử lại sau nhé."], None

    if not result:
        await telemetry.success(prompt_id, "translate", "(không có nội dung)")
        return ["Chưa dịch được câu này, thử lại nhé."], None

    await telemetry.success(prompt_id, "translate", result)
    label = translate_service.direction_label(resolved)
    provider = "api" if getattr(response, "used_fallback", False) else None
    return [f"🇯🇵↔🇻🇳 {label}\n\n{result}"], provider


async def _agent(
    user_id: int, question: str, channel: str = "zalo"
) -> tuple[list[str], str | None]:
    if not question:
        return [
            "Dùng: /agent <câu hỏi>\nVí dụ: /agent so sánh giá iPhone 15 và iPhone 15 Pro"
        ], None
    from ai import agent_service

    prompt_id = await telemetry.start(user_id, "agent", question, channel=channel)
    try:
        # user_id của chính người hỏi (Zalo tách Telegram): persona, trí nhớ, lịch sử;
        # ask_agent tự lưu Q&A (kind agent) qua conversation_log.record_exchange.
        text, provider = await agent_service.ask_agent(
            question, user_id=user_id, address_form=reply_audience.current().address_form
        )
        await telemetry.success(prompt_id, "agent", text)
        return [f"{text}\n\n⚙️ {provider}"], None
    except Exception as exc:
        await telemetry.failure(prompt_id, "agent", exc)
        return ["Agent gặp lỗi, thử lại sau hoặc dùng lệnh thường (/gia, /thongke...) nhé."], None


async def _rag(user_id: int, question: str, channel: str) -> tuple[list[str], str | None]:
    if not question:
        return [
            "Dùng: /rag <câu hỏi>\n"
            "Ví dụ: /rag chiến lược vào tiền khi thị trường sideway\n"
            "Em tìm trong các file .md anh để ở thư mục rag/."
        ], None
    from services import rag_service

    # Timeout của orchestrator lo phần chờ AI; ở đây không cần tự bọc thêm.
    # /rag chỉ tới được đây khi is_admin (chặn ở maybe_handle_command): persona
    # admin + cách gọi riêng của người gửi như lượt chat thường.
    text = await rag_service.ask(
        user_id,
        question,
        channel=channel,
        is_admin=True,
        address_form=reply_audience.current().address_form,
    )
    return [text], None


async def _ragxuly(user_id: int, name: str) -> tuple[list[str], str | None]:
    if not name:
        return [
            "Dùng: /ragxuly <tên-file-trong-rag>\n"
            "Ví dụ: /ragxuly ghichu.md\n"
            "Em dọn file md OCR (gộp dòng ngắt, bỏ số trang, thêm heading), "
            "backup bản gốc vào rag/_goc/ rồi ghi đè file chính."
        ], None
    from core import alerts
    from services import rag_clean_service

    async def _run() -> None:
        # Chạy nền ngoài khoá lượt (file dài = hàng chục lượt AI, Zalo gateway
        # chỉ chờ 240s). Kết quả gửi về Telegram chủ bot qua kênh cảnh báo.
        try:
            text = await rag_clean_service.clean_file(name)
        except Exception as exc:
            text = f"❌ /ragxuly {name} lỗi: {exc}"
        # Kết quả lệnh chủ bot vừa gọi: gửi ngay, không gom (services/notify_hub.py).
        alerts.send(f"/ragxuly {name}:\n{text}", urgent=True)

    if not rag_clean_service.start_background(name, _run()):
        return ["⏳ Đang có một lượt /ragxuly khác chạy. Chờ xong rồi gọi lại nhé."], None
    return [
        f"⏳ Đang dọn {name} ở nền (trần {rag_clean_service.total_timeout_sec() // 60} phút). "
        "Xong em báo về Telegram chủ bot."
    ], None


async def _morning_news_now() -> list[str]:
    from services import morning_news

    result = await morning_news.run_once(force=True)
    if result.content is None:
        return [
            "Không có nội dung để gửi (tất cả nguồn RSS đều lỗi, hoặc model tổng hợp trả lời bất thường - xem log)."
        ]
    if not result.sent_zalo and not result.sent_zoom:
        return [
            "Đã tổng hợp được bản tin nhưng KHÔNG gửi được tới đâu cả (chưa pair Zalo lẫn Zoom, hoặc gửi bị lỗi - xem log)."
        ]
    kenh = []
    if result.sent_zalo:
        kenh.append("Zalo")
    if result.sent_zoom:
        kenh.append("Zoom")
    if len(kenh) < 2:
        thieu = "Zoom" if "Zoom" not in kenh else "Zalo"
        return [
            f"✅ Đã gửi Bản tin sáng qua {' và '.join(kenh)} (KHÔNG gửi được {thieu} - chưa pair hoặc lỗi, xem log)."
        ]
    return ["✅ Đã gửi Bản tin sáng tới Zalo và Zoom."]


async def _news_now() -> list[str]:
    """Tạo điểm tin nhưng KHÔNG broadcast.

    ``maybe_handle_command`` trả chuỗi này cho channel_chat_service, nên nội
    dung chỉ đi ngược về đúng Zalo/Zoom nơi người dùng vừa gõ ``/tintuc``.
    """
    from services import morning_news

    content = await morning_news.build_digest(mode="on_demand")
    if not content:
        return [
            "Không tổng hợp được điểm tin lúc này (các nguồn RSS đều lỗi hoặc AI trả lời bất thường)."
        ]
    return [content]


async def _generate_image(
    argument: str, is_admin: bool, channel: str, user_id: int = 0
) -> tuple[list[str], str | None]:
    enabled = parse_on_off(argument)
    if enabled is not None:
        if not is_admin:
            return ["Lệnh này chỉ dành cho admin."], None
        await agnes_client.set_enabled(enabled)
        return (
            ["✅ Đã bật tạo ảnh (Agnes AI)."]
            if enabled
            else ["🔴 Đã tắt tạo ảnh cho tới khi bật lại bằng /anh on."]
        ), None
    if not argument.strip():
        enabled = await agnes_client.get_enabled()
        return [
            f"🖼️ Tạo ảnh (Agnes AI) đang {'BẬT' if enabled else 'TẮT'}.\n"
            "Dùng /anh <mô tả> để tạo ảnh, hoặc /anh on|off để bật/tắt (admin)."
        ], None
    try:
        image = await agnes_client.generate_image(argument.strip())
    except agnes_client.AgnesError as exc:
        return [f"❌ Không tạo được ảnh: {exc}"], None
    if user_id:
        # /anh không đi qua telemetry: ghi user_calls kind "image" (thống kê + trần
        # lượt nặng của thành viên Zalo, services/usage_quota.py).
        await usage_quota.record(user_id, channel, usage_quota.KIND_IMAGE)

    if channel == "zoom":
        # Zoom gửi ảnh qua URL công khai (Zoom tự tải), KHÔNG qua base64 -
        # xem channels/zoom.py::send_image_message + web.py::_process_zoom_event.
        if not image.url:
            return ["❌ Tạo ảnh thành công nhưng thiếu URL để gửi qua Zoom - thử lại nhé."], None
        _pending_image_url.set(image.url)
        return [], None

    # Zalo (và mọi kênh khác dùng ChannelResult.image_b64 sau này): gửi qua
    # base64 để zalo-gateway tự dựng Buffer, xem take_pending_image() đầu file.
    _pending_image.set(base64.b64encode(image.data).decode("ascii"))
    return [], None


# ─── Bảng lệnh Zalo/Zoom ─────────────────────────────────────────────────────
# Lệnh nào tới bộ xử lý nào, lệnh nào chỉ admin, tên phụ: khai báo ở
# services/command_catalog.py (``chat``, ``chat_admin``, ``chat_aliases``) - cùng nguồn
# với bảng lệnh Telegram của bot_app.py. Mỗi bộ xử lý đăng ký bằng @_handles("<tên>").

UNSUPPORTED = "Lệnh chưa được hỗ trợ. Gõ /help để xem danh sách."
ADMIN_ONLY = "Lệnh này chỉ dành cho admin."

Reply = tuple[list[str], str | None]


@dataclass(frozen=True, slots=True)
class CommandCall:
    user_id: int
    command: str  # "/tuvi": chữ thường, đã bỏ "@tên_bot"
    argument: str
    is_admin: bool
    channel: str


Handler = Callable[[CommandCall], Awaitable[Reply | None]]
_HANDLERS: dict[str, Handler] = {}


def _handles(key: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        _HANDLERS[key] = handler
        return handler

    return register


def handler_keys() -> frozenset[str]:
    """Tên các bộ xử lý (test kiểm danh bạ lệnh chỉ trỏ tới tên có thật)."""
    return frozenset(_HANDLERS)


def _listed(outputs: list[str] | None) -> Reply | None:
    return None if outputs is None else (outputs, None)


@_handles("help")
async def _help_cmd(call: CommandCall) -> Reply:
    argument = call.argument if call.command == "/help" else ""
    return [help_text(call.user_id, argument, call.is_admin, call.channel)], None


@_handles("feedback")
async def _feedback_cmd(call: CommandCall) -> Reply | None:
    # /sai [lý do], /danhgia: đánh giá câu trả lời (services/answer_feedback.py).
    return _listed(
        await answer_feedback.handle_command(
            call.command, call.argument, call.user_id, call.channel
        )
    )


@_handles("chuy")
async def _chuy_cmd(call: CommandCall) -> Reply:
    # "Có gì cần chú ý" (code, không AI).
    return [await attention_text(call.user_id, call.channel, call.is_admin)], None


@_handles("yenlang")
async def _yenlang_cmd(call: CommandCall) -> Reply:
    # Giờ yên lặng của chính người gõ.
    you = reply_audience.forms()[0]
    return [await notify_hub.yenlang(call.user_id, call.argument, you=you)], None


@_handles("viec")
async def _viec_cmd(call: CommandCall) -> Reply | None:
    # Giao việc chạy nền.
    return _listed(
        await background_jobs.handle_command(
            call.command, call.argument, call.user_id, call.channel, is_admin=call.is_admin
        )
    )


@_handles("lichtacvu")
async def _lichtacvu_cmd(call: CommandCall) -> Reply | None:
    # Tác vụ định kỳ chạy 1 yêu cầu AI theo lịch.
    return _listed(
        await routines.handle_command(
            call.command, call.argument, call.user_id, call.channel, is_admin=call.is_admin
        )
    )


@_handles("huy")
async def _huy_cmd(call: CommandCall) -> Reply:
    # /huy thật được xử lý TRƯỚC khoá lượt (bridge Zalo, webhook Zoom); tới
    # được đây nghĩa là không còn việc gì đang chạy để dừng.
    return [await turn_control.nothing_to_cancel_for(call.user_id)], None


@_handles("divination")
async def _divination_cmd(call: CommandCall) -> Reply | None:
    # /lich, /tsh, /tuvi, /battu, /kymon...: tính bằng code (không AI); *_chitiet mới gọi AI.
    return _listed(
        await divination_commands.handle(call.command, call.argument, call.user_id, call.channel)
    )


@_handles("daily")
async def _daily_cmd(call: CommandCall) -> Reply | None:
    # /vang, /tygia, /xang, /xoso, /thoitiet, /bantin: số liệu thật tính bằng code.
    return _listed(
        await daily_commands.handle(call.command, call.argument, call.user_id, call.channel)
    )


@_handles("reminder")
async def _reminder_cmd(call: CommandCall) -> Reply | None:
    # /nhac, /ngaynho: nhắc việc + ngày nhớ (giờ tính bằng code).
    return _listed(
        await reminder_commands.handle(call.command, call.argument, call.user_id, call.channel)
    )


@_handles("expense")
async def _expense_cmd(call: CommandCall) -> Reply | None:
    # /chitieu, /chitieu_xoa: chi tiêu cá nhân (cộng bằng SQL).
    return _listed(await expense_service.handle_command(call.command, call.argument, call.user_id))


@_handles("nutrition")
async def _nutrition_cmd(call: CommandCall) -> Reply | None:
    # /an: nhật ký ăn uống (kcal ước lượng tra bảng soạn sẵn, không AI mặc định).
    return _listed(
        await nutrition_service.handle_command(call.command, call.argument, call.user_id)
    )


@_handles("noi")
async def _noi_cmd(call: CommandCall) -> Reply:
    # /noi on|off|auto: trả lời kèm tin thoại của CHÍNH người gõ.
    return [await voice_reply.noi_command(call.user_id, call.argument, channel=call.channel)], None


@_handles("prompt")
async def _prompt_cmd(call: CommandCall) -> Reply:
    return await _prompt(call.user_id, call.argument, call.channel)


@_handles("gia")
async def _gia_cmd(call: CommandCall) -> Reply:
    return await _price(call.user_id, call.argument, call.channel)


@_handles("dich")
async def _dich_cmd(call: CommandCall) -> Reply:
    return await _translate(call.user_id, call.argument, call.channel)


@_handles("anh")
async def _anh_cmd(call: CommandCall) -> Reply:
    return await _generate_image(call.argument, call.is_admin, call.channel, call.user_id)


@_handles("reset")
async def _reset_cmd(call: CommandCall) -> Reply:
    await memory_service.flush_before_reset(call.user_id)
    await orchestrator.reset_chat()
    await db.clear_chat(call.user_id)
    return ["🔄 Đã xoá ngữ cảnh hội thoại."], None


@_handles("memory")
async def _memory_cmd(call: CommandCall) -> Reply | None:
    # /memory, /forget, /quen, /nho, /ghim, /notes, /xoaghichu, /xuatdulieu,
    # /xoadulieu: trí nhớ/dữ liệu của CHÍNH người gõ (services/memory_commands.py).
    reply = await memory_commands.handle(
        call.command, call.argument, call.user_id, channel=call.channel, about=_about(call.user_id)
    )
    return None if reply is None else (reply.messages, None)


@_handles("history")
async def _history_cmd(call: CommandCall) -> Reply:
    rows = await db.get_history(call.user_id, limit=10)
    return (
        ["Chưa có lịch sử nào."]
        if not rows
        else [
            "🕙 10 lượt gần nhất:\n"
            + "\n".join(
                f"• [{kind}] {prompt[:80]} ({created[:16].replace('T', ' ')})"
                for kind, prompt, created, _ in rows
            )
        ]
    ), None


# Các lệnh dưới (trừ /loi) có chat_admin=True trong danh bạ: /status, /userouter9, /model
# đổi CẤU HÌNH TOÀN CỤC (provider, model chat) - ảnh hưởng MỌI người dùng trên MỌI kênh;
# /rag, /agent đụng kho kiến thức riêng của chủ bot. Chỉ admin (Zalo: role=admin; Zoom chỉ
# có đúng 1 người pair nên is_admin mặc định True) mới tới được các bộ xử lý này.


@_handles("agent")
async def _agent_cmd(call: CommandCall) -> Reply:
    return await _agent(call.user_id, call.argument, call.channel)


@_handles("rag")
async def _rag_cmd(call: CommandCall) -> Reply:
    return await _rag(call.user_id, call.argument, call.channel)


@_handles("ragxuly")
async def _ragxuly_cmd(call: CommandCall) -> Reply:
    return await _ragxuly(call.user_id, call.argument)


@_handles("bangtinsang")
async def _bangtinsang_cmd(call: CommandCall) -> Reply:
    return await _morning_news_now(), None


@_handles("tintuc")
async def _tintuc_cmd(call: CommandCall) -> Reply:
    return await _news_now(), None


@_handles("phienban")
async def _phienban_cmd(call: CommandCall) -> Reply:
    return [release_notes.history_text(call.argument)], None


@_handles("status")
async def _status_cmd(call: CommandCall) -> Reply:
    state = orchestrator.get_provider_state_snapshot()
    latency = "\n".join(await provider_health.latency_lines())
    live = "\n".join(await provider_health.live_lines())
    return [
        f"🏷️ Phiên bản: {version.label()} (/phienban)\n"
        f"📡 Provider: {state['active_provider']}\nThứ tự: {' → '.join(config.PROVIDER_ORDER)}\nModel API: {config.GOOGLE_AI_STUDIO_MODEL}\n"
        f"{await search_status.search_mode_line()}\n"
        f"{runtime_status.db_pool_line()}"
        + (f"\n{live}" if live else "")
        + (f"\n\n{latency}" if latency else "")
    ], None


@_handles("loi")
async def _loi_cmd(call: CommandCall) -> Reply:
    # Lỗi gom theo loại (services/error_digest.py) - chỉ chủ bot (Zoom; Telegram có
    # handler riêng), admin Zalo không xem log hệ thống.
    if not await reply_audience.is_owner(call.user_id):
        return ["Lệnh này chỉ dành cho chủ bot."], None
    from services import error_digest

    return [await error_digest.build_report()], None


@_handles("thongke")
async def _thongke_cmd(call: CommandCall) -> Reply:
    # Dùng chung logic/format với Telegram - xem services/usage_stats.py.
    hours = usage_stats.parse_thongke_hours(call.argument)
    return [await usage_stats.build_thongke_text(hours, use_html=False)], None


@_handles("userouter9")
async def _userouter9_cmd(call: CommandCall) -> Reply:
    ok, detail = await orchestrator.try_router9_now()
    return (
        ["✅ 9Router hoạt động, đã chuyển về 9Router."]
        if ok
        else [f"❌ 9Router vẫn lỗi: {detail[:250]}"]
    ), None


@_handles("router9")
async def _router9_cmd(call: CommandCall) -> Reply:
    toggle = parse_on_off(call.argument)
    if toggle is True:
        await orchestrator.set_router9_enabled(True)
        return ["✅ Đã bật 9Router."], None
    if toggle is False:
        await orchestrator.set_router9_enabled(False)
        return ["🔴 Đã tắt 9Router cho tới khi bật lại bằng /router9 on."], None
    enabled = orchestrator.get_provider_state_snapshot()["router9_enabled"]
    return [
        f"9Router đang {'BẬT' if enabled else 'TẮT'}. Dùng /router9 on hoặc /router9 off để đổi."
    ], None


@_handles("tavily")
async def _tavily_cmd(call: CommandCall) -> Reply:
    toggle = parse_on_off(call.argument)
    if toggle is True:
        await tavily_client.set_enabled(True)
        return [
            f"✅ Đã bật tra web Tavily trước khi trả lời.\n{await search_status.search_mode_line()}"
        ], None
    if toggle is False:
        await tavily_client.set_enabled(False)
        return [
            "🔴 Đã tắt Tavily. Câu cần thông tin mới em sẽ dùng web search của chính "
            f"provider đang trả lời.\n{await search_status.search_mode_line()}"
        ], None
    enabled = await tavily_client.get_enabled()
    return [
        f"Tavily đang {'BẬT' if enabled else 'TẮT'}.\n{await search_status.search_mode_line()}\n"
        "Dùng /tavily on hoặc /tavily off để đổi."
    ], None


@_handles("model")
async def _model_cmd(call: CommandCall) -> Reply:
    argument = call.argument
    if not argument:
        current = await router9_client.get_preferred_model_name()
        return [
            f"🧠 Model hiện tại: {current or 'tự động'}\nĐổi bằng /model <tên>; mặc định bằng /model auto\n"
            f"⚠️ Model này chỉ áp dụng khi dùng 9Router — nếu bot tự chuyển sang API dự "
            f"phòng, API luôn dùng model mặc định ({config.GOOGLE_AI_STUDIO_MODEL})."
        ], None
    if argument.lower() in {"auto", "default", "reset"}:
        await router9_client.set_preferred_model_name(None)
        await orchestrator.reset_chat()
        return ["🔄 Đã về model tự động."], None
    name = await router9_client.find_model(argument)
    if name is None:
        return [f"Không tìm thấy model khớp “{argument}”."], None
    await router9_client.set_preferred_model_name(name)
    await orchestrator.reset_chat()
    return [f"✅ Đã đổi model sang {name}."], None


async def maybe_handle_command(
    user_id: int, text: str, is_admin: bool = True, channel: str = "zalo"
) -> tuple[list[str], str | None] | None:
    """Lệnh / trên Zalo/Zoom theo bảng lệnh sinh từ services/command_catalog.py; None =
    không phải lệnh. Lệnh danh mục, nhóm Zalo, Facebook (CHAT_UPSTREAM) đã được nhánh
    trước nhận - tới đây thì như lệnh lạ."""
    if not text.startswith("/"):
        return None
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0]
    entry = command_catalog.chat_entry(command)
    if entry is not None and entry.chat_admin and not is_admin:
        return [ADMIN_ONLY], None
    handler = _HANDLERS.get(entry.chat) if entry is not None else None
    if handler is None:
        return [UNSUPPORTED], None
    result = await handler(CommandCall(user_id, command, _arg(text), is_admin, channel))
    return result if result is not None else ([UNSUPPORTED], None)
