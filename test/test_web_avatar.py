"""Trang /admin/avatar (web_avatar.py): chỉ chủ bot đã đăng nhập, lượt chat như
Telegram trong khoá chủ bot với lời dặn "người iu", giọng đọc sau khi nhả khoá, câu
chào/nghe thử có cache, chọn giọng, chép lời, upload ảnh chân dung."""

import base64
import hashlib
import io
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

import web_admin
import web_avatar
from core import config
from core import database as db
from services import avatar_service, concurrency, reminder_delivery, voice_reply, voice_service
from services.channel_chat_service import ChatTurn

ROOT = Path(__file__).resolve().parent.parent
OWNER = 777
CLIP = voice_reply.Clip(b"M4A-DATA", "audio/mp4", "m4a", 1200, False)
AUDIO = base64.b64encode(CLIP.data).decode()
POINTS = json.dumps([[0.5, 0.5]] * 478)


@pytest.fixture
def store(monkeypatch):
    """settings + bảng avatar_photo giả trong RAM."""
    settings: dict[str, str] = {}
    photos: dict[str, dict] = {}

    async def get_setting(key):
        return settings.get(key)

    async def set_setting(key, value):
        settings[key] = value

    async def meta():
        return {k: {f: v for f, v in row.items() if f != "data"} for k, row in photos.items()}

    async def photo_get(kind):
        return photos.get(kind)

    async def put(kind, **kw):
        photos[kind] = {**kw, "updated_at": None}

    async def delete(kind):
        removed = [k for k in list(photos) if k == kind or (kind == "main" and k == "blink")]
        for key in removed:
            del photos[key]
        return len(removed)

    async def record_turn_metrics(**row):
        pass

    for name, fake in {
        "get_setting": get_setting,
        "set_setting": set_setting,
        "avatar_photo_meta": meta,
        "avatar_photo_get": photo_get,
        "avatar_photo_put": put,
        "avatar_photo_delete": delete,
        "record_turn_metrics": record_turn_metrics,
    }.items():
        monkeypatch.setattr(db, name, fake)
    for name in ("AVATAR_STYLE_HINT", "AVATAR_TTS_VOICE", "AVATAR_VOICE_STYLE", "VOICE_TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)
    return {"settings": settings, "photos": photos}


@pytest.fixture
async def client(monkeypatch, store):
    monkeypatch.setattr(config, "ADMIN_USER", "admin")
    monkeypatch.setattr(config, "ADMIN_PASS", "pass")
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(web_admin, "_login_limiter", web_admin.LoginRateLimiter())
    monkeypatch.setattr(web_admin, "_login_slowdown", web_admin.LoginSlowdown())
    monkeypatch.setattr(web_admin, "_session_epoch", web_admin.SessionEpoch())
    monkeypatch.setattr(web_avatar, "_clips", avatar_service.ClipCache())
    app = FastAPI()
    for router in (
        web_admin.router,
        web_admin.api_router,
        web_avatar.router,
        web_avatar.api_router,
    ):
        app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def _login(client):
    r = await client.post("/admin/login", data={"username": "admin", "password": "pass"})
    assert r.status_code == 303


@pytest.fixture
def speech(monkeypatch):
    """web_clip giả: ghi lại câu đọc + khoá chủ bot có đang giữ lúc đọc không."""
    calls = []

    async def web_clip(user_id, text, *, voice, style):
        calls.append(
            {
                "user_id": user_id,
                "text": text,
                "voice": voice,
                "style": style,
                "busy": concurrency.is_busy(concurrency.OWNER_TURN_KEY),
            }
        )
        return CLIP

    monkeypatch.setattr(voice_reply, "web_clip", web_clip)
    return calls


@pytest.fixture
def chat(monkeypatch):
    """run_chat_turn giả: ghi lại tham số + ngữ cảnh lượt; trả ``state["turn"]``."""
    state = {"turn": ChatTurn(text="Em thương anh nhiều lắm 🥰"), "calls": []}

    async def run_chat_turn(user_id, text, **kwargs):
        target = reminder_delivery.current_target(user_id)
        state["calls"].append(
            {
                "user_id": user_id,
                "text": text,
                **kwargs,
                "busy": concurrency.is_busy(concurrency.OWNER_TURN_KEY),
                "target": (target.channel, target.recipient_id),
            }
        )
        return state["turn"]

    monkeypatch.setattr(web_avatar, "run_chat_turn", run_chat_turn)
    return state


def _jpeg(size=(800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "pink").save(out, "JPEG")
    return out.getvalue()


# ─── Quyền truy cập ──────────────────────────────────────────────────────────


async def test_chua_dang_nhap_api_403_trang_hien_form_dang_nhap(client):
    page = await client.get("/admin/avatar")
    assert page.status_code == 200 and 'name="next" value="/admin/avatar"' in page.text
    assert "avatar-config" not in page.text
    requests = [
        ("POST", "/admin/api/avatar/chat", {"json": {"text": "chào em"}}),
        (
            "POST",
            "/admin/api/avatar/stt",
            {"content": b"OggS", "headers": {"content-type": "audio/ogg"}},
        ),
        ("GET", "/admin/api/avatar/greeting", {}),
        ("GET", "/admin/api/avatar/voices", {}),
        ("PUT", "/admin/api/avatar/voice", {"json": {"voice": "Leda"}}),
        ("POST", "/admin/api/avatar/voice/preview", {"json": {"voice": "Leda"}}),
        ("GET", "/admin/api/avatar/photo?kind=main", {}),
        ("PUT", "/admin/api/avatar/photo?kind=main", {"files": {"image": ("a.jpg", b"x")}}),
        ("DELETE", "/admin/api/avatar/photo?kind=main", {}),
    ]
    for method, url, kwargs in requests:
        assert (await client.request(method, url, **kwargs)).status_code == 403, url


async def test_trang_avatar_nhung_cau_hinh_khong_cache(client, store):
    await _login(client)
    store["settings"][avatar_service.VOICE_SETTING_KEY] = "Leda"
    page = await client.get("/admin/avatar")
    assert page.status_code == 200 and page.headers["cache-control"] == "private, no-store"
    raw = re.search(r'id="avatar-config">(.*?)</script>', page.text, re.S).group(1)
    data = json.loads(raw)
    assert data["photo"] is None and data["voice"] == "Leda" and data["textMax"] == 2000
    assert data["greeting"]["text"] and data["greeting"]["mood"] in avatar_service.MOODS
    assert data["voices"][0]["name"] == "HoaiMy" and data["stt"]["maxBytes"] > 0
    assert data["vendor"]["vision_bundle.mjs"].startswith("/admin/avatar/vendor/")


def test_mediapipe_dong_goi_khop_manifest_chi_ban_simd():
    vendor = ROOT / "vendor" / "mediapipe"
    manifest = json.loads((vendor / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["package"] == "@mediapipe/tasks-vision" and manifest["license"] == "Apache-2.0"
    assert set(manifest["files"]) == {
        "vision_bundle.mjs",
        "vision_wasm_internal.js",
        "vision_wasm_internal.wasm",
        "face_landmarker.task",
    }
    total = 0
    for name, meta in manifest["files"].items():
        data = (vendor / name).read_bytes()
        assert len(data) == meta["bytes"], name
        assert hashlib.sha256(data).hexdigest() == meta["sha256"], name
        total += len(data)
    assert total < 16 * 1024 * 1024
    assert "Apache License" in (vendor / "LICENSE").read_text(encoding="utf-8")


async def test_file_mediapipe_chi_chu_bot_cache_vinh_vien_theo_hash(client):
    urls = web_avatar.vendor_urls()
    assert (await client.get(urls["vision_bundle.mjs"])).status_code == 403
    await _login(client)
    r = await client.get(urls["vision_bundle.mjs"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    r = await client.get(urls["vision_wasm_internal.wasm"])
    assert r.headers["content-type"] == "application/wasm" and r.content[:4] == b"\0asm"
    digest = urls["face_landmarker.task"].split("/")[-2]
    for bad in (
        "/admin/avatar/vendor/0000000000000000/vision_bundle.mjs",  # hash cũ/sai
        f"/admin/avatar/vendor/{digest}/vision_bundle.mjs",  # hash của file khác
        f"/admin/avatar/vendor/{digest}/MANIFEST.json",
        f"/admin/avatar/vendor/{digest}/..%2F..%2Fweb.py",
    ):
        assert (await client.get(bad)).status_code == 404, bad


def test_giao_dien_avatar_chay_khong_can_cdn_va_vua_dien_thoai():
    html = (ROOT / "templates" / "avatar.html").read_text(encoding="utf-8")
    assert html.count("<!--CONFIG-->") == 1 and '<script type="module">' in html
    assert "viewport-fit=cover" in html and "safe-area-inset-bottom" in html
    assert "prefers-reduced-motion" in html
    # MediaPipe nạp động từ route vendor của trang (theo cấu hình), không phải CDN.
    assert "import(cfg.vendor['vision_bundle.mjs'])" in html and "cdn" not in html.lower()
    # Hết phiên: API trả 403 -> quay về đăng nhập rồi trở lại trang.
    assert "'/admin?next=' + encodeURIComponent('/admin/avatar')" in html


def test_link_avatar_trong_trang_admin():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'href="/admin/avatar"' in html


# ─── Trò chuyện ──────────────────────────────────────────────────────────────


async def test_chat_nhu_telegram_trong_khoa_chu_bot_giong_doc_sau_khi_nha_khoa(
    client, chat, speech
):
    await _login(client)
    r = await client.post("/admin/api/avatar/chat", json={"text": "  nhớ em hông  "})
    assert r.status_code == 200
    assert r.json() == {
        "text": "Em thương anh nhiều lắm 🥰",
        "mood": "love",
        "audio": AUDIO,
        "mime": "audio/mp4",
    }
    assert chat["calls"] == [
        {
            "user_id": OWNER,
            "text": "nhớ em hông",
            "channel": "telegram",
            "is_admin": True,
            "style_hint": web_avatar.AVATAR_STYLE_HINT,
            "busy": True,  # trong khoá lượt chủ bot (chung Telegram/Zoom)
            "target": ("telegram", str(OWNER)),  # nhắc việc tạo từ avatar về Telegram
        }
    ]
    assert speech[0]["busy"] is False  # đọc sau khi nhả khoá
    assert speech[0]["voice"] == "HoaiMy" and speech[0]["user_id"] == OWNER
    assert speech[0]["style"] == avatar_service.DEFAULT_VOICE_STYLE
    # Tắt loa: không tốn lượt TTS.
    r = await client.post("/admin/api/avatar/chat", json={"text": "hi", "voice": False})
    assert "audio" not in r.json() and len(speech) == 1


async def test_chat_chu_rong_qua_dai_hoac_khong_phai_json(client, chat, speech):
    await _login(client)
    for body in ({"text": "   "}, {"text": 5}, {}, {"text": "a" * 2001}):
        r = await client.post("/admin/api/avatar/chat", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    r = await client.post(
        "/admin/api/avatar/chat", content="text=hi", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415
    r = await client.post("/admin/api/avatar/chat", json=["hi"])
    assert r.status_code == 400
    assert chat["calls"] == [] and speech == []


async def test_chat_luot_hong_buon_khong_doc(client, chat, speech):
    await _login(client)
    chat["turn"] = ChatTurn(failed=True, error_detail="RuntimeError: x")
    r = (await client.post("/admin/api/avatar/chat", json={"text": "hi"})).json()
    assert r["mood"] == "sad" and r["text"] and "audio" not in r and speech == []
    chat["turn"] = ChatTurn(text="")
    r = (await client.post("/admin/api/avatar/chat", json={"text": "hi"})).json()
    assert r["mood"] == "sad" and "audio" not in r and speech == []


async def test_chat_lenh_nguyen_van_bo_markdown_kem_anh(client, chat, speech):
    await _login(client)
    chat["turn"] = ChatTurn(
        verbatim=True, messages=("**Lịch** hôm nay", "- Ngày tốt 😊"), image_b64="/9j/AAAA"
    )
    r = (await client.post("/admin/api/avatar/chat", json={"text": "xem lịch"})).json()
    assert r["text"] == "Lịch hôm nay\n\nNgày tốt 😊" and r["mood"] == "happy"
    assert r["image"] == "data:image/jpeg;base64,/9j/AAAA" and speech[-1]["text"] == r["text"]


async def test_loi_dan_nguoi_iu_doi_duoc_qua_env(client, chat, speech, monkeypatch):
    await _login(client)
    monkeypatch.setenv("AVATAR_STYLE_HINT", "  Nói   ngắn thôi. ")
    await client.post("/admin/api/avatar/chat", json={"text": "hi"})
    assert chat["calls"][-1]["style_hint"] == "Nói ngắn thôi."
    assert "Xưng 'em', gọi 'anh'" in web_avatar.AVATAR_STYLE_HINT


async def test_chat_bi_huy_bang_lenh_huy(client, chat, speech, monkeypatch):
    await _login(client)

    async def cancelled(key, factory):
        raise concurrency.TurnCancelled()

    monkeypatch.setattr(concurrency, "run_cancellable", cancelled)
    r = (await client.post("/admin/api/avatar/chat", json={"text": "hi"})).json()
    assert "dừng" in r["text"] and "audio" not in r and speech == []


# ─── Câu chào, giọng ─────────────────────────────────────────────────────────


async def test_cau_chao_kem_giong_doc_co_cache(client, speech):
    await _login(client)
    first = (await client.get("/admin/api/avatar/greeting")).json()
    assert first["id"] and "audio" not in first and speech == []
    params = {"id": first["id"], "audio": "1"}
    with_audio = (await client.get("/admin/api/avatar/greeting", params=params)).json()
    assert with_audio["text"] == first["text"] and with_audio["mime"] == "audio/mp4"
    await client.get("/admin/api/avatar/greeting", params=params)
    assert len(speech) == 1  # lần 2 lấy từ cache
    r = await client.get("/admin/api/avatar/greeting", params={"id": "dem:9", "audio": "1"})
    assert r.status_code == 404


async def test_chon_giong_va_nghe_thu(client, store, speech, monkeypatch):
    await _login(client)
    monkeypatch.setenv("AVATAR_TTS_VOICE", "Sulafat")
    voices = (await client.get("/admin/api/avatar/voices")).json()
    assert voices["current"] == "Sulafat" and voices["voices"][0]["name"] == "HoaiMy"
    r = await client.put("/admin/api/avatar/voice", json={"voice": "giọng lạ"})
    assert r.status_code == 400
    r = await client.put("/admin/api/avatar/voice", json={"voice": "leda"})
    assert r.json() == {"current": "Leda"}
    # Giọng chọn trên trang ưu tiên hơn env.
    assert (await client.get("/admin/api/avatar/voices")).json()["current"] == "Leda"
    assert store["settings"][avatar_service.VOICE_SETTING_KEY] == "Leda"
    r = await client.post("/admin/api/avatar/voice/preview", json={"voice": "x"})
    assert r.status_code == 400
    r = await client.post("/admin/api/avatar/voice/preview", json={"voice": "Vindemiatrix"})
    assert r.json() == {"audio": AUDIO, "mime": "audio/mp4"}
    assert (
        speech[-1]["voice"] == "Vindemiatrix" and speech[-1]["text"] == avatar_service.SAMPLE_TEXT
    )
    await client.post("/admin/api/avatar/voice/preview", json={"voice": "Vindemiatrix"})
    assert len(speech) == 1  # nghe thử lần 2: cache, không tốn lượt TTS


async def test_nghe_thu_tts_loi_bao_503(client, monkeypatch):
    await _login(client)

    async def no_clip(user_id, text, *, voice, style):
        return None

    monkeypatch.setattr(voice_reply, "web_clip", no_clip)
    r = await client.post("/admin/api/avatar/voice/preview", json={"voice": "Leda"})
    assert r.status_code == 503 and r.json()["error"]


# ─── Chép lời ────────────────────────────────────────────────────────────────


async def test_chep_loi_ghi_am(client, monkeypatch):
    await _login(client)
    seen = []

    async def transcribe(audio, *, user_id, channel):
        seen.append((audio, user_id, channel))
        if audio == b"big":
            raise voice_service.VoiceError("too_big")
        if audio == b"noise":
            raise voice_service.VoiceError("empty")
        return "anh nhớ em"

    monkeypatch.setattr(voice_service, "transcribe", transcribe)
    audio_headers = {"content-type": "audio/webm;codecs=opus"}
    r = await client.post("/admin/api/avatar/stt", content=b"\x1aE\xdf\xa3", headers=audio_headers)
    assert r.json() == {"text": "anh nhớ em"} and seen[0][1:] == (OWNER, "telegram")
    r = await client.post("/admin/api/avatar/stt", content=b"big", headers=audio_headers)
    assert r.status_code == 413 and r.json()["error"]
    r = await client.post("/admin/api/avatar/stt", content=b"noise", headers=audio_headers)
    assert r.status_code == 422 and r.json()["error"] and r.json()["kind"] == "empty"
    r = await client.post("/admin/api/avatar/stt", content=b"", headers=audio_headers)
    assert r.status_code == 400
    r = await client.post(
        "/admin/api/avatar/stt", content=b"x", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415 and len(seen) == 3


# ─── Ảnh ─────────────────────────────────────────────────────────────────────


async def _put(client, kind, data, landmarks=POINTS):
    fields = {"landmarks": landmarks} if landmarks is not None else {}
    return await client.put(
        f"/admin/api/avatar/photo?kind={kind}",
        files={"image": ("anh.jpg", data, "image/jpeg")},
        data=fields,
    )


async def test_upload_anh_tu_choi_sai_dinh_dang_kich_thuoc_diem_mat(client, store):
    await _login(client)
    r = await client.put("/admin/api/avatar/photo?kind=main", json={"image": "x"})
    assert r.status_code == 415
    r = await _put(client, "main", b"%PDF-1.7 hello")
    assert r.status_code == 400 and "PNG, JPEG" in r.json()["error"]
    r = await _put(client, "main", b"\xff\xd8" + b"0" * (5 * 1024 * 1024 + 10))
    assert r.status_code == 413
    r = await _put(client, "main", _jpeg(), landmarks=json.dumps([[0.5, 0.5]] * 10))
    assert r.status_code == 400 and "điểm mặt" in r.json()["error"]
    r = await _put(client, "main", _jpeg(), landmarks=None)
    assert r.status_code == 400
    r = await _put(client, "blink", _jpeg(), landmarks=None)  # chưa có ảnh chính
    assert r.status_code == 400
    r = await _put(client, "other", _jpeg())
    assert r.status_code == 400
    assert store["photos"] == {}


async def test_upload_xem_xoa_anh_chi_qua_phien_admin(client, store):
    await _login(client)
    r = await _put(client, "main", _jpeg())
    assert r.status_code == 200
    body = r.json()
    assert body["width"] == 800 and body["photo"]["landmarks"][0] == [0.5, 0.5]
    assert body["photo"]["main"].startswith("/admin/api/avatar/photo?kind=main")
    assert body["photo"]["blink"] is None
    r = await _put(client, "blink", _jpeg((640, 480)), landmarks=None)
    assert r.status_code == 400 and "800x600" in r.json()["error"]
    r = await _put(client, "blink", _jpeg(), landmarks=None)
    assert r.status_code == 200 and r.json()["photo"]["blink"]
    image = await client.get("/admin/api/avatar/photo?kind=main")
    assert image.headers["content-type"] == "image/webp"
    assert image.headers["cache-control"] == "private, no-store"
    with Image.open(io.BytesIO(image.content)) as saved:
        assert saved.format == "WEBP" and not saved.getexif()
    r = await client.delete("/admin/api/avatar/photo?kind=main")
    assert r.json() == {"removed": 2, "photo": None}
    assert (await client.get("/admin/api/avatar/photo?kind=main")).status_code == 404
