"""Private Telegram controls for Zalo login and controller pairing."""
import asyncio
import base64
from io import BytesIO
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from handlers import common

_CONTROL = "http://127.0.0.1:9901"

async def _json(method: str, path: str):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.request(method, _CONTROL + path)
        response.raise_for_status()
        return response.json()

async def _send_qr(message, qr: str) -> None:
    image = base64.b64decode(qr.split(",", 1)[-1])
    await message.reply_photo(
        photo=BytesIO(image),
        caption="📷 Quét bằng Zalo B rồi bấm Xác nhận đăng nhập trên điện thoại. Sau khi xác nhận xong, gửi lại /zalo để lấy mã ghép đôi A.",
    )

@common.restricted
async def zalo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    try:
        status = await _json("GET", "/status")
        if status.get("connected"):
            if status.get("controllerPaired"):
                await message.reply_text(f"✅ Zalo đã kết nối và ghép đôi A\nAccount B: {status.get('accountId', 'không rõ')}")
            else:
                pairing = await _json("POST", "/pairing/start")
                code = pairing["code"]
                await message.reply_text(f"🔐 Mã ghép đôi: {code}\n\nTừ tài khoản Zalo A, nhắn riêng cho B đúng nội dung:\n/pair {code}\n\nMã hết hạn sau 5 phút và chỉ dùng một lần.")
            return

        state = status.get("state")
        if state == "scanned":
            await message.reply_text("📱 QR đã được quét. Hãy mở Zalo trên điện thoại B và bấm Xác nhận đăng nhập; chưa cần quét lại QR.")
            return
        if state in {"saving_session", "restoring"}:
            await message.reply_text("⏳ Zalo đã xác thực, đang lưu và khôi phục session. Hãy chờ vài giây rồi gửi lại /zalo.")
            return
        if status.get("qr"):
            await _send_qr(message, status["qr"])
            return
        if state == "waiting_backend":
            await message.reply_text("⏳ Gateway đang chờ Python khởi động. Hãy thử lại /zalo sau vài giây.")
            return

        await message.reply_text("⏳ Đang tạo QR đăng nhập Zalo B...")
        await _json("POST", "/login/qr")
        for _ in range(15):
            await asyncio.sleep(1)
            status = await _json("GET", "/status")
            if status.get("connected"):
                await message.reply_text("✅ B đã đăng nhập. Gửi lại /zalo để lấy mã ghép đôi A.")
                return
            if status.get("state") == "scanned":
                await message.reply_text("📱 QR đã được quét. Hãy bấm Xác nhận đăng nhập trên điện thoại B, sau đó gửi lại /zalo.")
                return
            if status.get("qr"):
                await _send_qr(message, status["qr"])
                return
        await message.reply_text("❌ Chưa lấy được QR. Hãy thử lại /zalo.")
    except httpx.HTTPError:
        await message.reply_text("❌ Gateway chưa sẵn sàng. Kiểm tra ZALO_ENABLED=true và log Render.")

@common.restricted
async def zalologout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _json("POST", "/logout")
        await update.effective_message.reply_text("🚪 Đã đăng xuất B và xoá liên kết A.")
    except httpx.HTTPError:
        await update.effective_message.reply_text("❌ Không kết nối được gateway.")
