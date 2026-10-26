"""services/notify_hub.py: phân loại mức, gộp tin, giờ yên lặng, xả khi người dùng
nhắn, và các nguồn thật (bản tin sáng, danh mục, cảnh báo chủ bot) đi qua hub.

DB giả trong RAM (cùng hợp đồng với core/repositories/notify.py); bản SQL thật ở
test_notify_queue_sql.py (PGlite)."""

import asyncio
import datetime as dt
import itertools

import pytest

from core import alerts, config
from core import database as db
from services import notify_hub as hub

VN = hub.VN_TZ
OWNER = 777
NOON = dt.datetime(2026, 10, 26, 12, 0, tzinfo=VN)
NIGHT = dt.datetime(2026, 10, 26, 23, 30, tzinfo=VN)


class FakeQueue:
    def __init__(self):
        self.rows: list[dict] = []
        self.settings: dict[str, str] = {}
        self.ids = itertools.count(1)
        self.zalo_outbox: list[tuple[str, str]] = []
        self.clock = NOON

    def _pending(self, channel, user_id):
        return [
            r
            for r in self.rows
            if r["channel"] == channel and r["user_id"] == user_id and r["status"] == "pending"
        ]

    async def enqueue(self, channel, user_id, level, source, content, dedup, cap):
        if dedup and any(
            r["dedup"] == dedup and r["channel"] == channel and r["user_id"] == user_id
            for r in self.rows
        ):
            return None, 0
        row = {
            "id": next(self.ids),
            "channel": channel,
            "user_id": user_id,
            "level": level,
            "source": source,
            "content": content,
            "dedup": dedup,
            "status": "pending",
            "created_at": self.clock,
            "reported": False,
        }
        self.rows.append(row)
        pending = self._pending(channel, user_id)
        dropped = 0
        for old in pending[: max(0, len(pending) - cap)]:
            if old["level"] == "normal":
                old["status"] = "dropped"
                dropped += 1
        return row["id"], dropped

    async def recipients(self, priority, limit=100):
        out = {}
        for r in self.rows:
            if r["status"] != "pending":
                continue
            key = (r["channel"], r["user_id"])
            item = out.setdefault(
                key,
                {
                    "channel": key[0],
                    "user_id": key[1],
                    "oldest": r["created_at"],
                    "has_priority": False,
                    "pending": 0,
                },
            )
            item["oldest"] = min(item["oldest"], r["created_at"])
            item["has_priority"] |= r["level"] == "urgent" or r["source"] in priority
            item["pending"] += 1
        return list(out.values())

    def _ordered(self, channel, user_id):
        return sorted(
            self._pending(channel, user_id), key=lambda r: (r["level"] != "urgent", r["id"])
        )

    async def claim(self, channel, user_id, limit, lease):
        rows = [r for r in self._ordered(channel, user_id) if not r.get("claimed")][:limit]
        for r in rows:
            r["claimed"] = True
        return [dict(r) for r in rows]

    async def mark_sent(self, ids):
        for r in self.rows:
            if r["id"] in ids:
                r["status"], r["claimed"] = "sent", False

    async def release(self, ids):
        for r in self.rows:
            if r["id"] in ids:
                r["claimed"] = False

    async def take_dropped(self, channel, user_id):
        rows = [
            r
            for r in self.rows
            if r["channel"] == channel
            and r["user_id"] == user_id
            and r["status"] == "dropped"
            and not r["reported"]
        ]
        for r in rows:
            r["reported"] = True
        return len(rows)

    async def flush_zalo(self, user_id, limit, account_id, recipient, compose):
        rows = self._ordered("zalo", user_id)[:limit]
        if not rows:
            return 0
        text = compose([dict(r) for r in rows], await self.take_dropped("zalo", user_id))
        for r in rows:
            r["status"] = "sent"
        if text:
            self.zalo_outbox.append((recipient, text))
        return len(rows)

    async def drop_pending(self, channel, user_id):
        rows = self._pending(channel, user_id)
        for r in rows:
            r["status"] = "dropped"
        return len(rows)

    async def count_pending(self, user_id):
        return sum(1 for r in self.rows if r["user_id"] == user_id and r["status"] == "pending")

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value

    def pending_texts(self, channel="telegram", user_id=OWNER):
        return [r["content"] for r in self._pending(channel, user_id)]


@pytest.fixture
def q(monkeypatch):
    store = FakeQueue()
    for name, fn in {
        "notify_enqueue": store.enqueue,
        "notify_pending_recipients": store.recipients,
        "notify_claim": store.claim,
        "notify_mark_sent": store.mark_sent,
        "notify_release": store.release,
        "notify_take_dropped": store.take_dropped,
        "notify_flush_zalo": store.flush_zalo,
        "notify_drop_pending": store.drop_pending,
        "notify_count_pending": store.count_pending,
        "get_setting": store.get_setting,
        "set_setting": store.set_setting,
    }.items():
        monkeypatch.setattr(db, name, fn)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(hub, "_started", True)
    monkeypatch.setattr(hub, "_pending_hint", set())
    monkeypatch.setattr(hub, "_activity_tasks", {})
    monkeypatch.delenv("NOTIFY_QUIET_HOURS", raising=False)
    store.sent = []

    async def telegram(uid, text):
        store.sent.append((uid, text))

    monkeypatch.setattr(hub, "_telegram_sender", telegram)
    monkeypatch.setattr(hub, "_now", lambda: store.clock)
    return store


# ─── Phân loại ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hub_tat_hoac_chua_chay_thi_nguon_gui_thang(q, monkeypatch):
    monkeypatch.setenv("NOTIFY_HUB_ENABLED", "0")
    assert not await hub.offer("telegram", OWNER, "x", source="followup", now=NOON)
    monkeypatch.setenv("NOTIFY_HUB_ENABLED", "1")
    monkeypatch.setattr(hub, "_started", False)
    assert not await hub.offer("telegram", OWNER, "x", source="followup", now=NOON)
    assert q.rows == []


@pytest.mark.asyncio
async def test_urgent_gui_ngay_normal_vao_hang_doi(q):
    assert not await hub.offer(
        "telegram", OWNER, "🚨 HPG thủng stop", source="holding_alert", now=NOON
    )
    assert not await hub.offer("telegram", OWNER, "🔔 giá", source="price_alert", now=NOON)
    assert await hub.offer("telegram", OWNER, "Hỏi thăm", source="followup", now=NOON)
    assert await hub.offer("telegram", OWNER, "📅 quyền", source="portfolio_event", now=NOON)
    assert q.pending_texts() == ["Hỏi thăm", "📅 quyền"]
    assert q.sent == []  # NORMAL chưa tới lượt xả


@pytest.mark.asyncio
async def test_nguoi_nhan_la_khong_vao_hang_doi(q):
    assert not await hub.offer("telegram", 123, "x", source="followup", now=NOON)
    assert not await hub.offer("zalo", 5, "x", source="followup", now=NOON)  # id Zalo phải âm
    assert q.rows == []


@pytest.mark.asyncio
async def test_gio_yen_lang_chi_nhac_viec_va_canh_bao_nghiem_trong_di_ngay(q):
    assert not await hub.offer("telegram", OWNER, "⏰ nhắc", source="reminder", now=NIGHT)
    assert not await hub.offer("telegram", OWNER, "🚨 OOM", source="system_critical", now=NIGHT)
    # URGENT khác (bài FB chờ duyệt) bị giữ, đứng đầu khi xả.
    assert await hub.offer("telegram", OWNER, "Hỏi thăm", source="followup", now=NIGHT)
    assert await hub.offer("telegram", OWNER, "📝 BÀI FB #3", source="facebook_review", now=NIGHT)
    assert await hub.run_tick(NIGHT + dt.timedelta(hours=3)) == 0  # vẫn trong giờ yên lặng
    assert await hub.run_tick(dt.datetime(2026, 10, 27, 6, 31, tzinfo=VN)) == 2
    ((uid, text),) = q.sent
    assert text.index("BÀI FB #3") < text.index("Hỏi thăm")
    assert "giữ lại trong giờ yên lặng" in text


@pytest.mark.asyncio
async def test_router_canh_bao_chu_bot(q, monkeypatch):
    direct = []

    async def callback(text):
        direct.append(text)

    monkeypatch.setattr(alerts, "_callback", callback)
    await hub.route_owner_alert("🚨 Toàn bộ provider chết", None)
    await hub.route_owner_alert("✅ hồi phục", True)
    await hub.route_owner_alert("⚠️ RAM cao", None)
    await hub.route_owner_alert("🚨 nhưng ép gom", False)
    assert direct == ["🚨 Toàn bộ provider chết", "✅ hồi phục"]
    assert q.pending_texts() == ["⚠️ RAM cao", "🚨 nhưng ép gom"]


@pytest.mark.asyncio
async def test_alerts_send_di_qua_router_khi_hub_chay(q, monkeypatch):
    seen = []

    async def callback(text):
        seen.append(text)

    monkeypatch.setattr(alerts, "_callback", callback)
    monkeypatch.setattr(alerts, "_router", hub.route_owner_alert)
    assert alerts.send("🐞 Lỗi mới: KeyError")
    assert alerts.send("🚨 Kernel kill")
    await asyncio.gather(*list(alerts.tasks))
    assert seen == ["🚨 Kernel kill"]
    assert q.pending_texts() == ["🐞 Lỗi mới: KeyError"]


@pytest.mark.asyncio
async def test_db_loi_thi_fail_open(q, monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("db")

    monkeypatch.setattr(db, "notify_enqueue", broken)
    assert not await hub.offer("telegram", OWNER, "x", source="followup", now=NOON)


# ─── Gộp ────────────────────────────────────────────────────────────────────


def test_compose_mot_tin_giu_nguyen_nhieu_tin_gop_lai():
    one = [{"content": "Chỉ 1 tin", "level": "normal", "created_at": NOON}]
    assert hub.compose(one) == "Chỉ 1 tin"
    many = [
        {"content": "📝 FB", "level": "urgent", "created_at": NOON},
        {"content": "A", "level": "normal", "created_at": NOON - dt.timedelta(minutes=30)},
        {"content": " ", "level": "normal", "created_at": NOON},
    ]
    text = hub.compose(many, dropped=2, you="chị")
    assert text.startswith("🔔 Em gom 2 thông báo từ 11:30 cho chị nè:")
    assert "📝 FB" in text and "A" in text and hub._SEPARATOR.strip() in text
    assert "2 thông báo cũ hơn đã được lược bớt" in text
    assert hub.compose([], dropped=0) is None


@pytest.mark.asyncio
async def test_vuot_tran_bao_so_tin_bi_luoc(q, monkeypatch):
    monkeypatch.setenv("NOTIFY_QUEUE_MAX_PER_USER", "5")
    for i in range(7):
        await hub.offer("telegram", OWNER, f"tin {i}", source="system", now=NOON)
    assert q.pending_texts() == [f"tin {i}" for i in range(2, 7)]
    assert await hub.flush_recipient("telegram", OWNER) == 5
    assert "2 thông báo cũ hơn đã được lược bớt" in q.sent[0][1]


@pytest.mark.asyncio
async def test_gui_loi_thi_giu_lai_vong_sau_gui(q, monkeypatch):
    await hub.offer("telegram", OWNER, "A", source="system", now=NOON)

    async def broken(uid, text):
        raise RuntimeError("telegram sập")

    monkeypatch.setattr(hub, "_telegram_sender", broken)
    assert await hub.flush_recipient("telegram", OWNER) == 0
    assert q.pending_texts() == ["A"]
    sent = []

    async def ok(uid, text):
        sent.append(text)

    monkeypatch.setattr(hub, "_telegram_sender", ok)
    assert await hub.flush_recipient("telegram", OWNER) == 1
    assert sent == ["A"] and q.pending_texts() == []


@pytest.mark.asyncio
async def test_lich_xa_theo_tuoi_tin_cu_nhat(q, monkeypatch):
    monkeypatch.setenv("NOTIFY_BATCH_MINUTES", "60")
    await hub.offer("telegram", OWNER, "A", source="system", now=NOON)
    assert await hub.run_tick(NOON + dt.timedelta(minutes=30)) == 0
    q.clock = NOON + dt.timedelta(minutes=40)
    await hub.offer("telegram", OWNER, "B", source="followup", now=q.clock)
    assert await hub.run_tick(NOON + dt.timedelta(minutes=61)) == 2
    assert q.sent[0][1].count("- - -") == 1  # A và B trong cùng 1 tin


@pytest.mark.asyncio
async def test_tin_tong_hop_xa_ngay_kem_tin_dang_cho(q):
    await hub.offer("telegram", OWNER, "⚠️ RAM", source="system", now=NOON)
    assert await hub.offer(
        "telegram", OWNER, "☀️ Bản tin", source="morning_brief", dedup="b1", now=NOON
    )
    ((uid, text),) = q.sent
    assert "☀️ Bản tin" in text and "⚠️ RAM" in text
    # Nguồn chạy lại cùng ngày: dedup, không gửi lần 2.
    assert await hub.offer(
        "telegram", OWNER, "☀️ Bản tin", source="morning_brief", dedup="b1", now=NOON
    )
    assert len(q.sent) == 1


@pytest.mark.asyncio
async def test_tin_tong_hop_trong_gio_yen_lang_cho_het_gio(q):
    early = dt.datetime(2026, 10, 27, 5, 0, tzinfo=VN)
    q.clock = early
    assert await hub.offer("telegram", OWNER, "☀️ Bản tin 5h", source="morning_brief", now=early)
    assert q.sent == []
    assert await hub.run_tick(early.replace(hour=6, minute=35)) == 1


# ─── Giờ yên lặng ───────────────────────────────────────────────────────────


def test_parse_window():
    t = dt.time
    assert hub.parse_window("22:30-06:30") == (t(22, 30), t(6, 30))
    assert hub.parse_window("22-7") == (t(22), t(7))
    assert hub.parse_window("22h-6h30") == (t(22), t(6, 30))
    assert hub.parse_window("13-14") == (t(13), t(14))
    for bad in ("", "abc", "25-7", "22-22", "22:75-7"):
        assert hub.parse_window(bad) is None
    assert hub.in_window(t(23), (t(22), t(7))) and hub.in_window(t(6, 59), (t(22), t(7)))
    assert not hub.in_window(t(7), (t(22), t(7)))
    assert hub.in_window(t(13, 30), (t(13), t(14)))


@pytest.mark.asyncio
async def test_yenlang_tung_nguoi(q, monkeypatch):
    assert await hub.is_quiet(OWNER, NIGHT)
    text = await hub.yenlang(OWNER, "21-6")
    assert "BẬT 21:00-06:00" in text and "giờ riêng" in text
    assert await hub.is_quiet(OWNER, NIGHT.replace(hour=21, minute=15))
    assert not await hub.is_quiet(-5, NIGHT.replace(hour=21, minute=15))  # người khác: mặc định
    assert "TẮT" in await hub.yenlang(OWNER, "off")
    assert not await hub.is_quiet(OWNER, NIGHT)
    assert "BẬT 22:30-06:30" in await hub.yenlang(OWNER, "on")
    assert "chưa hiểu" in await hub.yenlang(OWNER, "nửa đêm")
    member = await hub.yenlang(-5, "", you="chị")
    assert "cảnh báo hệ thống" not in member and "nhắc việc chị tự đặt" in member
    monkeypatch.setenv("NOTIFY_QUIET_HOURS", "off")
    assert not await hub.is_quiet(-5, NIGHT)


# ─── Xả khi người dùng nhắn ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nguoi_dung_nhan_thi_xa_ca_trong_gio_yen_lang(q, monkeypatch):
    monkeypatch.setattr(hub, "_ACTIVITY_DELAY_SEC", 0)
    q.clock = NIGHT
    await hub.offer("telegram", OWNER, "Hỏi thăm", source="followup", now=NIGHT)
    assert not hub.note_activity(-9)  # người không có tin chờ: không tốn câu SQL nào
    assert hub.note_activity(OWNER)
    assert not hub.note_activity(OWNER)  # đã lên lịch rồi
    await asyncio.gather(*list(hub._activity_tasks.values()))
    assert q.sent == [(OWNER, "Hỏi thăm")]


@pytest.mark.asyncio
async def test_xa_zalo_qua_outbox_theo_cach_goi_rieng(q, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("ZALO_ENABLED", "true")
    monkeypatch.setenv("ZALO_BRIDGE_SECRET", "s")
    member = SimpleNamespace(
        internal_user_id=-5, external_id="z5", is_active=True, address_form="chị"
    )

    async def zalo_user(uid):
        return member if uid == -5 else None

    async def account():
        return "acc"

    async def noop():
        return None

    monkeypatch.setattr(hub, "_zalo_user", zalo_user)
    monkeypatch.setattr("channels.zalo_repository.resolve_active_account_id", account)
    monkeypatch.setattr("channels.zalo_repository.ensure_schema", noop)
    await hub.offer("zalo", -5, "**Hỏi thăm**", source="followup", now=NOON)
    await hub.offer("zalo", -5, "📅 VCB", source="portfolio_event", now=NOON)
    assert await hub.flush_recipient("zalo", -5) == 2
    ((recipient, text),) = q.zalo_outbox
    assert recipient == "z5" and "cho chị" in text and "**" not in text
    # Người đã gỡ pairing: bỏ tin chờ, không gửi.
    await hub.offer("zalo", -6, "x", source="followup", now=NOON)
    assert await hub.flush_recipient("zalo", -6) == 0
    assert q.pending_texts("zalo", -6) == []


# ─── Nguồn thật đi qua hub ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_portfolio_push_phan_loai_theo_kind(q, monkeypatch):
    from services import portfolio_push

    logged = []

    async def log(uid, kind, ref, day):
        logged.append((kind, ref))

    direct = []

    async def notify(uid, text):
        direct.append(text)

    monkeypatch.setattr(db, "portfolio_log_alert", log)
    monkeypatch.setattr(portfolio_push, "_notify", notify)
    today = NOON.date().isoformat()
    assert await portfolio_push.deliver(
        OWNER, "🚨 HPG stop", kind="stop", ref_key="HPG:25000", today=today
    )
    assert await portfolio_push.deliver(
        OWNER, "🔁 quy đổi", kind="corp", ref_key="FPT:x", today=today
    )
    assert await portfolio_push.deliver(
        OWNER, "📅 lịch", kind="event", ref_key="FPT:DIV:d", today=today
    )
    assert direct == ["🚨 HPG stop"]
    assert q.pending_texts() == ["🔁 quy đổi", "📅 lịch"]
    # Nhật ký chống gửi trùng vẫn ghi cho cả tin đã xếp hàng.
    assert [k for k, _ in logged] == ["stop", "corp", "event"]


@pytest.mark.asyncio
async def test_canh_bao_cuoi_phien_urgent_khi_co_stop(q):
    from services import portfolio_alerts

    day = NOON.date()
    assert not await portfolio_alerts._offer(OWNER, "🔔 *Cảnh báo*\n• HPG: 🚨 chạm stop", day)
    assert await portfolio_alerts._offer(OWNER, "🔔 *Cảnh báo*\n• FPT: 📈 breakout", day)
    assert q.pending_texts() == ["🔔 *Cảnh báo*\n• FPT: 📈 breakout"]


@pytest.mark.asyncio
async def test_ban_tin_sang_qua_hub_xa_ngay_va_danh_dau(q, monkeypatch):
    from services import morning_brief

    async def build(*args, **kwargs):
        return "☀️ Bản tin"

    legacy = []

    async def notify(uid, text):
        legacy.append(text)

    monkeypatch.setattr(morning_brief, "build", build)
    monkeypatch.setattr(morning_brief, "_notify_callback", notify)
    target = morning_brief.BriefTarget("telegram", OWNER, morning_brief.BriefSettings(on=True))
    await hub.offer("telegram", OWNER, "Hỏi thăm", source="followup", now=NOON)
    assert await morning_brief._deliver(target, NOON, {})
    assert legacy == []
    ((_, text),) = q.sent
    assert "☀️ Bản tin" in text and "Hỏi thăm" in text
    assert await morning_brief._already_sent("telegram", OWNER, NOON)
    # Chạy lại cùng ngày (vd guard ghi lỗi): dedup, không gửi lần 2.
    assert await morning_brief._deliver(target, NOON, {})
    assert len(q.sent) == 1


@pytest.mark.asyncio
async def test_hoi_tham_vao_hang_doi_thay_vi_gui_thang(q, monkeypatch):
    from services import proactive_service

    legacy = []

    async def notify(uid, text):
        legacy.append(text)

    monkeypatch.setattr(proactive_service, "_notify_callback", notify)
    await proactive_service._send(OWNER, "Phỏng vấn sao rồi anh?", None, 12)
    await proactive_service._send(OWNER, "Phỏng vấn sao rồi anh?", None, 12)  # thử lại
    assert legacy == [] and q.pending_texts() == ["Phỏng vấn sao rồi anh?"]


@pytest.mark.asyncio
async def test_bai_facebook_cho_duyet_giu_lai_trong_gio_yen_lang(q):
    from channels import facebook_commands

    assert not await facebook_commands._held_in_quiet_hours_owner("📝 BÀI #1")
    q.clock = NIGHT
    assert await facebook_commands._held_in_quiet_hours_owner("📝 BÀI #2")
    assert q.pending_texts() == ["📝 BÀI #2"]


@pytest.mark.asyncio
async def test_dispatch_zalo_bao_hub_nguoi_dung_vua_nhan(monkeypatch):
    from services import channel_dispatch

    seen = []
    monkeypatch.setattr(hub, "note_activity", lambda uid: seen.append(uid) or True)

    async def cached(*args):
        return {"messages": ["đã trả lời"]}

    async def save(*args):
        return None

    async def chat(*args, **kwargs):
        raise AssertionError("không chạy lại lượt đã cache")

    req = channel_dispatch.DispatchRequest(
        account_id="acc",
        event_id="m1",
        cache_kind="text",
        user_id=-5,
        is_admin=False,
        channel="zalo",
        text="chào em",
        turn_key="zalo:-5",
    )
    payload = await channel_dispatch.dispatch(
        req, channel_dispatch.DispatchHandlers(chat=chat, get_cached=cached, save_cached=save)
    )
    assert payload["messages"] == ["đã trả lời"] and seen == [-5]
