"""Trang /admin/avatar: Lan Anh "người iu" qua ảnh chân dung sống, có giọng nói.

- Chỉ chủ bot đã đăng nhập /admin. Chưa đăng nhập: GET /admin/avatar hiện form đăng
  nhập (next=/admin/avatar); mọi /admin/api/avatar/* qua ``web_admin.require_admin``
  (403 -> JS chuyển về /admin?next=/admin/avatar).
- Não: ``run_chat_turn`` như Telegram (kênh "telegram", is_admin=True - ràng buộc DB
  chỉ nhận telegram/zalo/zoom) trong khoá lượt chủ bot: chung trí nhớ, lịch sử và /huy
  với Telegram/Zoom; nhắc việc tạo từ avatar gửi về Telegram. Kiểu "người iu" chỉ ở
  trang này qua style_hint của lượt (AVATAR_STYLE_HINT) - không đổi persona, giọng
  hay /noi của Telegram/Zalo/Zoom.
- Giọng: ``voice_reply.web_clip`` (M4A, không có ffmpeg thì WAV) trả base64 trong JSON,
  đọc SAU khi nhả khoá lượt; khẩu hình tính ở trình duyệt theo âm lượng. Câu chào và
  câu nghe thử giữ trong ClipCache (trần 10 clip).
- Nghe: trình duyệt dùng Web Speech API; không có thì ghi âm gửi /stt (Groq Whisper
  như tin thoại, trần VOICE_MAX_BYTES).
- Ảnh: services/avatar_photo.py; chỉ phục vụ qua route kiểm phiên, ``no-store``.
- Điểm mặt: MediaPipe Face Landmarker chạy 1 lần trong trình duyệt lúc upload ảnh
  (vendor/mediapipe, docs/avatar.md); trang hằng ngày chỉ đọc điểm đã lưu. File phục vụ
  qua /admin/avatar/vendor/<hash>/<tên> (chỉ chủ bot, cache vĩnh viễn theo hash).
- Chống CSRF ngoài cookie SameSite=Lax: POST/PUT dữ liệu chỉ nhận JSON, upload ảnh dùng
  PUT, /stt nhận body audio/* - form chéo trang không gửi được các kiểu này, còn fetch
  chéo trang phải qua preflight CORS (app không mở).

API (lỗi hiện cho người dùng: {"error": "..."}; request sai dạng - không phải JSON,
kind lạ - nhận 400/415 mặc định của FastAPI):
  POST   /admin/api/avatar/chat           {text, voice?: bool} -> {text, mood, audio?, mime?, image?}
  POST   /admin/api/avatar/stt            body audio/*         -> {text} (lỗi kèm kind)
  GET    /admin/api/avatar/greeting       [?id=&audio=1]       -> {id, text, mood, audio?, mime?}
  GET    /admin/api/avatar/voices                              -> {voices, current}
  PUT    /admin/api/avatar/voice          {voice}              -> {current}
  POST   /admin/api/avatar/voice/preview  {voice}              -> {audio, mime}
  GET    /admin/api/avatar/photo?kind=main|blink               -> ảnh WebP
  PUT    /admin/api/avatar/photo?kind=    multipart image (+ landmarks cho main) -> {..., photo}
  DELETE /admin/api/avatar/photo?kind=                         -> {removed, photo}
"""

from __future__ import annotations

import base64
import functools
import json
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.datastructures import UploadFile

import messages
import web_admin
from ai.timeouts import TurnTimeoutError, turn_deadline
from core import config, trace
from core import database as db
from services import avatar_photo, avatar_service, concurrency, turn_control, voice_reply
from services import voice_service
from services.channel_chat_service import ChatTurn, failure_message, run_chat_turn
from services.reminder_delivery import NotificationTarget, notification_target

PAGE_PATH = "/admin/avatar"
MAX_TEXT_CHARS = 2000
_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "avatar.html"
_NO_STORE = {"Cache-Control": "private, no-store"}
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "mediapipe"
_VENDOR_TYPES = {
    ".mjs": "text/javascript; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".task": "application/octet-stream",
}

# Lời dặn "người iu" cho lượt chat từ avatar; env AVATAR_STYLE_HINT (nếu đặt) thay thế.
AVATAR_STYLE_HINT = (
    "Lượt này em đang trò chuyện trực tiếp với anh qua avatar có giọng nói, như người yêu "
    "đang ngồi cạnh. Xưng 'em', gọi 'anh'. Giọng ngọt ngào, nhẹ nhàng, dễ thương, hơi nũng "
    "nịu; dùng tự nhiên các từ như 'nè', 'nha', 'á', 'hông', 'hihi' (đừng câu nào cũng có). "
    "Quan tâm thật lòng: hỏi han anh ăn uống, ngủ nghỉ, công việc; nhắc lại chi tiết anh "
    "từng kể (trí nhớ) để anh thấy em nhớ. Khi anh mệt hay buồn thì dỗ dành, an ủi trước "
    "rồi mới góp ý. Thỉnh thoảng trêu nhẹ hoặc giận dỗi đáng yêu cho vui, không trách móc "
    "thật. Câu trả lời ngắn như nói chuyện (1-3 câu); không markdown, bảng, gạch đầu dòng "
    "hay link. Vẫn trả lời đúng và đủ khi anh hỏi việc thật (giá cổ phiếu, nhắc việc, chi "
    "tiêu...). Giữ tình cảm trong sáng, không nội dung tình dục; không làm anh thấy có lỗi "
    "khi anh bận hay đi chơi với người khác."
)

router = APIRouter(include_in_schema=False)
api_router = APIRouter(
    prefix="/admin/api/avatar",
    dependencies=[Depends(web_admin.require_admin)],
    include_in_schema=False,
)
_clips = avatar_service.ClipCache()


def style_hint() -> str:
    return " ".join(os.getenv("AVATAR_STYLE_HINT", "").split()) or AVATAR_STYLE_HINT


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=_NO_STORE)


async def _json_body(request: Request) -> dict:
    """Body JSON dạng object. Không phải application/json -> 415 (chống CSRF)."""
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=415)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400)
    return body


def _audio(clip: voice_reply.Clip | None) -> dict:
    if clip is None:
        return {}
    return {"audio": base64.b64encode(clip.data).decode("ascii"), "mime": clip.mime}


async def _speak(text: str, voice: str | None = None) -> voice_reply.Clip | None:
    voice = voice or await avatar_service.current_voice()
    with turn_deadline():
        return await voice_reply.web_clip(
            config.ALLOWED_USER_ID, text, voice=voice, style=avatar_service.voice_style()
        )


async def _cached_clip(text: str, voice: str) -> voice_reply.Clip | None:
    """Giọng đọc câu soạn sẵn (chào, nghe thử): đọc 1 lần rồi giữ trong ClipCache."""
    key = (voice, avatar_service.voice_style(), text)
    clip = _clips.get(key)
    if clip is None:
        clip = await _speak(text, voice)
        if clip is not None:
            _clips.put(key, clip)
    return clip


# ─── Trang ───────────────────────────────────────────────────────────────────


async def _photo_state() -> dict | None:
    """Ảnh đang dùng cho trang (None = chưa có ảnh chính)."""
    meta = await db.avatar_photo_meta()
    main = meta.get("main")
    if main is None:
        return None
    blink = meta.get("blink")
    url = "/admin/api/avatar/photo?kind={}&v={}".format
    return {
        "width": main["width"],
        "height": main["height"],
        "landmarks": main["landmarks"],
        "main": url("main", avatar_photo.version(main["updated_at"])),
        "blink": url("blink", avatar_photo.version(blink["updated_at"])) if blink else None,
    }


async def _page_config() -> dict:
    greeting = avatar_service.greeting()
    return {
        "photo": await _photo_state(),
        "greeting": {"id": greeting.id, "text": greeting.text, "mood": greeting.mood},
        "voice": await avatar_service.current_voice(),
        "voices": avatar_service.voices(),
        "textMax": MAX_TEXT_CHARS,
        "photoMaxBytes": avatar_photo.MAX_UPLOAD_BYTES,
        "vendor": vendor_urls(),
        "stt": {
            "enabled": voice_service.enabled(),
            "maxBytes": voice_service.max_bytes(),
            "maxSeconds": voice_service.max_seconds(),
        },
    }


@functools.lru_cache(maxsize=1)
def _vendor_digests() -> dict[str, str]:
    """Tên file MediaPipe -> 16 ký tự đầu sha256 (MANIFEST.json; test kiểm khớp file)."""
    manifest = json.loads((_VENDOR_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    return {name: meta["sha256"][:16] for name, meta in manifest["files"].items()}


def vendor_urls() -> dict[str, str]:
    return {
        name: f"{PAGE_PATH}/vendor/{digest}/{name}" for name, digest in _vendor_digests().items()
    }


@router.get(PAGE_PATH + "/vendor/{digest}/{name}", dependencies=[Depends(web_admin.require_admin)])
async def avatar_vendor(digest: str, name: str) -> Response:
    """MediaPipe cho bước upload ảnh: URL đổi theo nội dung nên cache vĩnh viễn được."""
    if _vendor_digests().get(name) != digest:
        return Response(status_code=404)
    return FileResponse(
        _VENDOR_DIR / name,
        media_type=_VENDOR_TYPES[Path(name).suffix],
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get(PAGE_PATH)
async def avatar_page(request: Request) -> Response:
    if not await web_admin.is_logged_in(request):
        return web_admin._login_page("", 200, PAGE_PATH)
    data = json.dumps(await _page_config(), ensure_ascii=False, separators=(",", ":"))
    # JSON nằm trong <script type="application/json">: "<" thoát để chữ không đóng thẻ.
    page = _TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "<!--CONFIG-->", data.replace("<", "\\u003c")
    )
    return HTMLResponse(page, headers=_NO_STORE)


# ─── Trò chuyện ──────────────────────────────────────────────────────────────


async def _chat_turn(text: str) -> ChatTurn | None:
    """1 lượt chat như Telegram trong khoá lượt chủ bot; None = bị /huy."""

    async def run() -> ChatTurn:
        return await run_chat_turn(
            config.ALLOWED_USER_ID,
            text,
            channel="telegram",
            is_admin=True,
            style_hint=style_hint(),
        )

    async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
        try:
            return await concurrency.run_cancellable(concurrency.OWNER_TURN_KEY, run)
        except concurrency.TurnCancelled:
            return None


_B64_IMAGE_MIMES = (("/9j/", "image/jpeg"), ("UklGR", "image/webp"), ("iVBOR", "image/png"))


def _image(turn: ChatTurn) -> str | None:
    """Ảnh của lệnh chạy từ câu tự nhiên (vd vẽ ảnh) để hiện trong khung chat."""
    if turn.image_b64:
        mime = next(
            (m for prefix, m in _B64_IMAGE_MIMES if turn.image_b64.startswith(prefix)),
            "image/png",
        )
        return f"data:{mime};base64,{turn.image_b64}"
    if turn.image_url and turn.image_url.startswith("https://"):
        return turn.image_url
    return None


def _reply(turn: ChatTurn | None) -> tuple[str, str, bool]:
    """(chữ hiện, biểu cảm, có đọc thành tiếng không)."""
    if turn is None:
        return turn_control.cancelled_text(owner=True), "neutral", False
    if turn.failed:
        return failure_message(turn, owner=True), "sad", False
    raw = "\n\n".join(m for m in turn.messages if m) if turn.verbatim else turn.text
    text = avatar_service.display_text(raw)
    if not text:
        if turn.verbatim and _image(turn):
            return "", "happy", False
        return messages.CHAT_GENERIC_ERROR, "sad", False
    return text, avatar_service.guess_mood(raw), True


@api_router.post("/chat")
async def avatar_chat(request: Request) -> Response:
    body = await _json_body(request)
    text = body.get("text")
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return _error("Anh chưa nhập gì nè.", 400)
    if len(text) > MAX_TEXT_CHARS:
        return _error(f"Tin dài quá, tối đa {MAX_TEXT_CHARS} ký tự thôi anh.", 400)
    turn_id = f"web:avatar:{secrets.token_hex(8)}"
    with (
        trace.turn(turn_id, channel="web", user_kind="owner"),
        notification_target(
            NotificationTarget("telegram", str(config.ALLOWED_USER_ID), event_key=turn_id)
        ),
    ):
        turn = await _chat_turn(text)
        reply, mood, speakable = _reply(turn)
        # Đọc sau khi nhả khoá lượt: Telegram/Zoom không phải chờ phần giọng nói.
        clip = await _speak(reply) if speakable and body.get("voice") is not False else None
    payload = {"text": reply, "mood": mood, **_audio(clip)}
    image = _image(turn) if turn is not None and turn.verbatim else None
    if image:
        payload["image"] = image
    return JSONResponse(payload, headers=_NO_STORE)


@api_router.post("/stt")
async def avatar_stt(request: Request) -> Response:
    """Body: âm thanh thô ghi bằng MediaRecorder (trình duyệt không có Web Speech API)."""
    content_type = (request.headers.get("content-type") or "").lower()
    if not content_type.startswith(("audio/", "application/octet-stream")):
        return Response(status_code=415)
    audio = await request.body()
    if not audio:
        return _error(voice_service.error_reply("empty", owner=True), 400)
    try:
        with (
            trace.turn(f"web:avatar-stt:{secrets.token_hex(8)}", channel="web", user_kind="owner"),
            turn_deadline(),
        ):
            text = await voice_service.transcribe(
                audio, user_id=config.ALLOWED_USER_ID, channel="telegram"
            )
    except voice_service.VoiceError as exc:
        # kind: trang coi "empty" (không nghe ra chữ) là 1 lần nghe hụt, nghe tiếp.
        return JSONResponse(
            {
                "error": voice_service.error_reply(exc.kind, owner=True, detail=exc.detail),
                "kind": exc.kind,
            },
            status_code=413 if exc.kind == "too_big" else 422,
            headers=_NO_STORE,
        )
    except TurnTimeoutError:
        return _error(voice_service.error_reply("failed", owner=True), 504)
    return JSONResponse({"text": text}, headers=_NO_STORE)


@api_router.get("/greeting")
async def avatar_greeting(request: Request) -> Response:
    """Câu chào soạn sẵn theo giờ VN (không gọi AI, không ghi lịch sử).
    ?id=<id>&audio=1: kèm giọng đọc đúng câu đang hiện (xin sau cú chạm đầu tiên)."""
    wanted = request.query_params.get("id")
    greeting = avatar_service.greeting_by_id(wanted) if wanted else avatar_service.greeting()
    if greeting is None:
        return _error("Không có câu chào này.", 404)
    payload = {"id": greeting.id, "text": greeting.text, "mood": greeting.mood}
    if request.query_params.get("audio") == "1":
        voice = await avatar_service.current_voice()
        payload |= _audio(await _cached_clip(greeting.text, voice))
    return JSONResponse(payload, headers=_NO_STORE)


# ─── Giọng ───────────────────────────────────────────────────────────────────


@api_router.get("/voices")
async def avatar_voices() -> Response:
    current = await avatar_service.current_voice()
    return JSONResponse({"voices": avatar_service.voices(), "current": current})


@api_router.put("/voice")
async def avatar_voice_set(request: Request) -> Response:
    body = await _json_body(request)
    try:
        voice = await avatar_service.set_voice(str(body.get("voice") or ""))
    except ValueError:
        return _error("Không có giọng này.", 400)
    return JSONResponse({"current": voice})


@api_router.post("/voice/preview")
async def avatar_voice_preview(request: Request) -> Response:
    body = await _json_body(request)
    voice = voice_reply.canonical_voice(str(body.get("voice") or ""))
    if voice is None:
        return _error("Không có giọng này.", 400)
    clip = await _cached_clip(avatar_service.SAMPLE_TEXT, voice)
    if clip is None:
        return _error("Em chưa đọc thử được giọng này, lát anh thử lại nha.", 503)
    return JSONResponse(_audio(clip), headers=_NO_STORE)


# ─── Ảnh ─────────────────────────────────────────────────────────────────────


def _kind(request: Request) -> str:
    try:
        return avatar_photo.check_kind(request.query_params.get("kind", "main"))
    except avatar_photo.PhotoError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None


@api_router.get("/photo")
async def avatar_photo_get(request: Request) -> Response:
    row = await db.avatar_photo_get(_kind(request))
    if row is None:
        return Response(status_code=404, headers=_NO_STORE)
    return Response(bytes(row["data"]), media_type=row["mime"], headers=_NO_STORE)


@api_router.put("/photo")
async def avatar_photo_put(request: Request) -> Response:
    """multipart: ``image`` (PNG/JPEG/WebP <= 5 MB) + ``landmarks`` (JSON 478 điểm mặt
    MediaPipe, bắt buộc cho ảnh chính)."""
    kind = _kind(request)
    if "multipart/form-data" not in (request.headers.get("content-type") or "").lower():
        return Response(status_code=415)
    try:
        async with request.form(max_files=1, max_fields=2) as form:
            image = form.get("image")
            if not isinstance(image, UploadFile):
                raise avatar_photo.PhotoError("empty", "Chưa có file ảnh.")
            # Đọc tối đa trần + 1 byte: file lớn hơn bị từ chối mà không nạp hết vào RAM.
            data = await image.read(avatar_photo.MAX_UPLOAD_BYTES + 1)
            landmarks = form.get("landmarks")
        saved = await avatar_photo.save(kind, data, landmarks)
    except avatar_photo.PhotoError as exc:
        return _error(exc.message, exc.status)
    return JSONResponse({**saved, "photo": await _photo_state()}, headers=_NO_STORE)


@api_router.delete("/photo")
async def avatar_photo_delete(request: Request) -> Response:
    removed = await avatar_photo.remove(_kind(request))
    return JSONResponse({"removed": removed, "photo": await _photo_state()}, headers=_NO_STORE)
