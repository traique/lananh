"""Việc nền /viec + bộ chạy lượt AI nền (services/background_jobs.py): trả lời ngay,
chạy ngoài khoá lượt người dùng nhưng vẫn chiếm chỗ semaphore tổng, hạn chót riêng,
huỷ task con an toàn, khởi động lại, side effect bị chặn, gửi kết quả đúng mức qua
services/notify_hub, quota lượt nặng của thành viên Zalo, mục /chuy."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

import test_channel_chat_service as _chat
import test_web_admin as _admin
from ai import timeouts
from core import config
from core import database as db
from services import background_jobs as bg
from services import concurrency, notify_hub, usage_quota

chat_fakes = _chat.chat_fakes
client = _admin.client
settings_store = _admin.settings_store
sleeps = _admin.sleeps
OWNER = 777
VN = bg.VN_TZ


class FakeJobs:
    """Bảng bg_jobs giả trong RAM (đúng các luật của core/repositories/jobs.py)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.recover_rows: list[dict] = []

    def add(self, uid: int, prompt: str, *, kind: str = "job", channel="telegram", **extra):
        row = {
            "id": self.next_id,
            "telegram_user_id": uid,
            "channel": channel,
            "kind": kind,
            "routine_id": extra.pop("routine_id", None),
            "prompt": prompt,
            "status": "queued",
            "result": None,
            "error": None,
            "no_reply": False,
            "attempts": 0,
            "tokens": None,
            "provider": None,
            "scheduled_for": extra.pop("scheduled_for", None),
            "created_at": dt.datetime.now(dt.timezone.utc),
            "started_at": None,
            "finished_at": None,
            **extra,
        }
        self.rows[row["id"]] = row
        self.next_id += 1
        return row

    async def insert(self, uid, channel, prompt, *, user_cap, global_cap):
        mine = [
            r
            for r in self.rows.values()
            if r["telegram_user_id"] == uid
            and r["kind"] == "job"
            and r["status"] in ("queued", "running")
        ]
        if len(mine) >= user_cap:
            return None, "user_cap"
        if sum(r["status"] == "queued" for r in self.rows.values()) >= global_cap:
            return None, "global_cap"
        return self.add(uid, prompt, channel=channel)["id"], ""

    async def claim(self, lease_sec):
        for row in sorted(self.rows.values(), key=lambda r: r["id"]):
            if row["status"] == "queued":
                row.update(status="running", attempts=row["attempts"] + 1)
                row["started_at"] = dt.datetime.now(dt.timezone.utc)
                return dict(row)
        return None

    async def renew(self, job_id, lease_sec):
        return self.rows.get(job_id, {}).get("status") == "running"

    async def finish(self, job_id, status, **fields):
        row = self.rows.get(job_id)
        if row is None or row["status"] != "running":
            return False
        row.update(status=status, finished_at=dt.datetime.now(dt.timezone.utc), **fields)
        return True

    async def cancel(self, uid, job_id):
        row = self.rows.get(job_id)
        if row is None or row["telegram_user_id"] != uid:
            return None
        if row["status"] not in ("queued", "running"):
            return None
        old = row["status"]
        row["status"] = "cancelled"
        return old

    async def get(self, uid, job_id):
        row = self.rows.get(job_id)
        return dict(row) if row and row["telegram_user_id"] == uid else None

    async def list(self, uid, limit=10):
        rows = [dict(r) for r in self.rows.values() if r["telegram_user_id"] == uid]
        return sorted(rows, key=lambda r: -r["id"])[:limit]

    async def active(self, uid):
        return [
            dict(r)
            for r in self.rows.values()
            if r["telegram_user_id"] == uid and r["status"] in ("queued", "running")
        ]

    async def position(self, job_id):
        return sum(
            1
            for r in self.rows.values()
            if r["id"] < job_id and r["status"] in ("queued", "running")
        )

    async def recover(self, max_attempts):
        rows, self.recover_rows = self.recover_rows, []
        return rows


@pytest.fixture
def jobs(monkeypatch):
    fake = FakeJobs()
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(db, "bg_job_insert", fake.insert)
    monkeypatch.setattr(db, "bg_job_claim", fake.claim)
    monkeypatch.setattr(db, "bg_job_renew", fake.renew)
    monkeypatch.setattr(db, "bg_job_finish", fake.finish)
    monkeypatch.setattr(db, "bg_job_cancel", fake.cancel)
    monkeypatch.setattr(db, "bg_job_get", fake.get)
    monkeypatch.setattr(db, "bg_job_list", fake.list)
    monkeypatch.setattr(db, "bg_job_active", fake.active)
    monkeypatch.setattr(db, "bg_job_position", fake.position)
    monkeypatch.setattr(db, "bg_job_recover", fake.recover)
    for name in (
        "BACKGROUND_JOBS_ENABLED",
        "BACKGROUND_JOBS_MEMBERS",
        "BACKGROUND_JOB_TIMEOUT_SEC",
        "BACKGROUND_JOBS_MAX_PER_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    sent: list[dict] = []

    async def send(channel, user_id, text, *, source, level=None, dedup=None):
        sent.append({"channel": channel, "user_id": user_id, "text": text, "source": source})
        return True

    monkeypatch.setattr(notify_hub, "send", send)
    recorded: list[tuple] = []

    async def record(user_id, user_text, reply, kind="chat", **kwargs):
        recorded.append((user_id, user_text, reply, kind, kwargs))
        return True

    monkeypatch.setattr("services.conversation_log.record_exchange", record)

    async def no_symbols(text):
        return []

    monkeypatch.setattr("stock.analysis.find_valid_symbols", no_symbols)

    async def zalo_users():
        return [
            SimpleNamespace(
                internal_user_id=-5,
                external_id="z5",
                is_active=True,
                is_admin=False,
                address_form="chị",
            ),
            SimpleNamespace(
                internal_user_id=-6,
                external_id="z6",
                is_active=True,
                is_admin=True,
                address_form=None,
            ),
        ]

    monkeypatch.setattr("channels.zalo_users.list_users", zalo_users)
    monkeypatch.setenv("ZALO_CONTROLLER_ID", "boss")
    return SimpleNamespace(db=fake, sent=sent, recorded=recorded)


def _turn(text="kết quả", **kwargs):
    from services.channel_chat_service import ChatTurn

    return ChatTurn(text, provider="groq", **kwargs)


def _runner(monkeypatch, fn):
    monkeypatch.setattr(bg, "run_ai_turn", fn)


async def _claim(jobs) -> dict:
    job = await jobs.db.claim(bg.LEASE_SEC)
    assert job is not None
    return job


# ─── /viec ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_giao_viec_tra_loi_ngay_va_xep_hang(jobs):
    reply = await bg.handle_command("/viec", "so sánh giá iPhone 16 và 17", OWNER, "telegram")
    assert reply and "#1" in reply[0] and "báo anh" in reply[0]
    assert jobs.db.rows[1]["status"] == "queued"
    assert jobs.db.rows[1]["prompt"] == "so sánh giá iPhone 16 và 17"
    assert jobs.sent == []  # chưa chạy gì cả


@pytest.mark.asyncio
async def test_tran_viec_moi_nguoi_va_liet_ke(jobs):
    for i in range(3):
        await bg.submit(OWNER, "telegram", f"việc số {i}")
    refused = await bg.submit(OWNER, "telegram", "việc thứ tư")
    assert "3 việc nền chưa xong" in refused and len(jobs.db.rows) == 3
    listing = await bg.list_text(OWNER)
    assert "#3" in listing and "đang chờ" in listing
    assert (await bg.handle_command("/viec", "", OWNER, "telegram"))[0] == listing


@pytest.mark.asyncio
async def test_thanh_vien_zalo_tat_bang_env_admin_van_dung(jobs, monkeypatch):
    monkeypatch.setenv("BACKGROUND_JOBS_MEMBERS", "0")
    refused = await bg.submit(-5, "zalo", "tóm tắt tin vàng", is_admin=False)
    assert "chưa mở" in refused and not jobs.db.rows
    ok = await bg.submit(-6, "zalo", "tóm tắt tin vàng", is_admin=True)
    assert "#1" in ok


def test_doc_tham_so_viec():
    assert bg.parse_argument("") == ("list", "")
    assert bg.parse_argument("huy 12") == ("cancel", "12")
    assert bg.parse_argument("huỷ #12") == ("cancel", "12")
    assert bg.parse_argument("xem 3") == ("show", "3")
    assert bg.parse_argument("huy hợp đồng thuê nhà, tóm tắt giúp anh")[0] == "submit"
    assert bg.is_submission("tóm tắt tin VNM") and not bg.is_submission("xem 2")


def test_viec_la_luot_nang_cua_thanh_vien():
    assert usage_quota.heavy_kind("/viec tóm tắt tin VNM") == usage_quota.KIND_JOB
    assert usage_quota.heavy_kind("/viec") is None
    assert usage_quota.heavy_kind("/viec huy 3") is None
    assert usage_quota.heavy_kind("/viec xem 3") is None
    assert {"viec", "routine"} <= set(usage_quota.HEAVY_KINDS)


@pytest.mark.asyncio
async def test_het_luot_nang_thi_chan_giao_viec(monkeypatch):
    usage_quota.reset()

    async def since(channel, user_id, start, heavy_kinds):
        return 3, 5  # đã dùng hết 5 lượt nặng

    async def get_setting(key):
        return None

    monkeypatch.setattr(db, "user_calls_since", since)
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.delenv("ZALO_MEMBER_DAILY_HEAVY", raising=False)
    member = {"channel": "zalo", "is_admin": False}
    blocked = await usage_quota.check(-9, "/viec tóm tắt tin", **member)
    assert not blocked.allowed and blocked.reason == "heavy"
    assert (await usage_quota.check(-9, "/viec", **member)).allowed
    assert (await usage_quota.check(-9, "/viec tóm tắt", channel="zalo", is_admin=True)).allowed
    assert await usage_quota.heavy_exhausted(-9, **member)
    usage_quota.reset()


# ─── Bộ chạy ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chay_nen_khong_giu_khoa_luot_nguoi_dung(jobs, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(job, recipient, budget_sec):
        started.set()
        await release.wait()
        return _turn("xong rồi")

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc dài")
    task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.wait_for(started.wait(), 1)
    # Việc nền đang chạy: chủ bot vẫn vào lượt chat của mình ngay (không chờ khoá).
    assert not concurrency.is_busy(concurrency.OWNER_TURN_KEY)
    async with asyncio.timeout(1):
        async with concurrency.assistant_turn(concurrency.OWNER_TURN_KEY):
            pass
    release.set()
    outcome = await task
    assert outcome.status == bg.SUCCEEDED and jobs.db.rows[1]["status"] == "succeeded"
    assert jobs.db.rows[1]["result"] == "xong rồi" and jobs.db.rows[1]["provider"] == "groq"
    assert jobs.sent[0]["source"] == "background_job" and "xong rồi" in jobs.sent[0]["text"]
    # Kết quả vào lịch sử chat (nhãn việc nền, không trích trí nhớ).
    assert jobs.recorded[0][1] == "[Việc nền #1] việc dài"
    assert jobs.recorded[0][3] == "background"


@pytest.mark.asyncio
async def test_viec_nen_chiem_cho_semaphore_tong(jobs, monkeypatch):
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(concurrency, "_turn_slots", slots)
    started = asyncio.Event()

    async def runner(job, recipient, budget_sec):
        started.set()
        return _turn()

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc")
    await slots.acquire()  # 1 lượt chat đang giữ chỗ cuối cùng
    task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.sleep(0.05)
    assert not started.is_set()
    slots.release()
    await asyncio.wait_for(task, 1)
    assert started.is_set()


@pytest.mark.asyncio
async def test_viec_cua_thanh_vien_lay_cho_thanh_vien_khong_dung_cho_chu_bot(jobs, monkeypatch):
    member_slots = asyncio.Semaphore(1)
    monkeypatch.setattr(concurrency, "_member_slots", member_slots)
    seen: list[int] = []

    async def runner(job, recipient, budget_sec):
        seen.append(recipient.user_id)
        return _turn()

    _runner(monkeypatch, runner)
    await bg.submit(-5, "zalo", "việc của chị")
    await bg.submit(OWNER, "telegram", "việc của anh")
    await member_slots.acquire()  # thành viên khác đang chat
    member_task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.wait_for(bg.run_job(await _claim(jobs)), 1)  # việc chủ bot vẫn chạy
    assert seen == [OWNER]
    member_slots.release()
    await asyncio.wait_for(member_task, 1)
    assert seen == [OWNER, -5]


@pytest.mark.asyncio
async def test_han_chot_rieng_va_khong_ghi_du_lieu(jobs, monkeypatch):
    seen = {}

    async def run_chat_turn(user_id, text, **kwargs):
        seen.update(kwargs, user_id=user_id, remaining=timeouts.remaining_turn_seconds())
        return _turn("ok")

    monkeypatch.setattr("services.channel_chat_service.run_chat_turn", run_chat_turn)
    await bg.submit(-5, "zalo", "tóm tắt tin vàng hôm nay")
    outcome = await bg.run_job(await _claim(jobs))
    assert outcome.status == bg.SUCCEEDED
    assert seen["user_id"] == -5 and seen["address_form"] == "chị" and not seen["is_admin"]
    assert seen["allow_side_effects"] is False and seen["record_history"] is False
    assert seen["deadline_sec"] == 600 and seen["telemetry_kind"] == "viec"
    # Hạn chót là của việc nền (600s), dài hơn CHAT_TURN_TIMEOUT_SEC của lượt chat.
    assert config.CHAT_TURN_TIMEOUT_SEC < seen["remaining"] <= 600
    assert "chạy nền" in seen["style_hint"]
    assert jobs.sent[0]["channel"] == "zalo" and "chị" in jobs.sent[0]["text"]


@pytest.mark.asyncio
async def test_qua_han_chot_thi_timed_out(jobs, monkeypatch):
    monkeypatch.setattr(bg, "job_timeout_sec", lambda: 0.05)
    monkeypatch.setattr(bg, "_HARD_GRACE_SEC", 0.05)

    async def runner(job, recipient, budget_sec):
        await asyncio.sleep(5)

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc treo")
    outcome = await asyncio.wait_for(bg.run_job(await _claim(jobs)), 2)
    assert outcome.status == bg.TIMED_OUT and jobs.db.rows[1]["status"] == "timed_out"
    assert "chạy quá" in jobs.sent[0]["text"]


@pytest.mark.asyncio
async def test_luot_ai_bao_het_han_chot_thi_timed_out(jobs, monkeypatch):
    async def runner(job, recipient, budget_sec):
        return _turn("", failed=True, timed_out=True, error_detail="hết giờ")

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc")
    assert (await bg.run_job(await _claim(jobs))).status == bg.TIMED_OUT


@pytest.mark.asyncio
async def test_huy_viec_dang_chay_chi_huy_task_con(jobs, monkeypatch):
    started = asyncio.Event()

    async def runner(job, recipient, budget_sec):
        started.set()
        await asyncio.sleep(10)

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc dài")
    task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.wait_for(started.wait(), 1)
    reply = await bg.handle_command("/viec", "huy 1", OWNER, "telegram")
    assert "huỷ việc #1" in reply[0]
    outcome = await asyncio.wait_for(task, 1)  # task worker kết thúc bình thường
    assert outcome.status == bg.CANCELLED
    assert jobs.db.rows[1]["status"] == "cancelled" and jobs.sent == []
    assert bg._running == {}


@pytest.mark.asyncio
async def test_huy_viec_dang_cho_va_viec_nguoi_khac(jobs):
    await bg.submit(OWNER, "telegram", "việc")
    assert "không thấy" in await bg.cancel(-5, 1)  # không huỷ được việc người khác
    assert "huỷ" in await bg.cancel(OWNER, 1)
    assert await jobs.db.claim(bg.LEASE_SEC) is None
    assert "không thấy" in await bg.cancel(OWNER, 1)


@pytest.mark.asyncio
async def test_viec_bi_huy_o_instance_khac_dung_o_nhip_gia_han(jobs, monkeypatch):
    monkeypatch.setattr(bg, "HEARTBEAT_SEC", 0.02)

    async def runner(job, recipient, budget_sec):
        await asyncio.sleep(10)

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc")
    task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.sleep(0.01)
    jobs.db.rows[1]["status"] = "cancelled"  # lệnh huỷ ghi DB từ instance khác
    assert (await asyncio.wait_for(task, 1)).status == bg.CANCELLED


@pytest.mark.asyncio
async def test_tat_app_khong_danh_dau_viec_bi_huy(jobs, monkeypatch):
    started = asyncio.Event()

    async def runner(job, recipient, budget_sec):
        started.set()
        await asyncio.sleep(10)

    _runner(monkeypatch, runner)
    await bg.submit(OWNER, "telegram", "việc")
    task = asyncio.create_task(bg.run_job(await _claim(jobs)))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()  # shutdown
    with pytest.raises(asyncio.CancelledError):
        await task
    # Vẫn running: hết lease thì vòng dọn đưa về hàng đợi để chạy lại.
    assert jobs.db.rows[1]["status"] == "running" and jobs.sent == []


@pytest.mark.asyncio
async def test_khoi_dong_lai_bao_viec_bi_gian_doan_lan_2(jobs):
    row = jobs.db.add(OWNER, "việc bị gián đoạn")
    row.update(status="failed", error="interrupted", attempts=2)
    jobs.db.recover_rows = [dict(row)]
    assert await bg.recover() == 1
    assert "gián đoạn vì bot khởi động lại" in jobs.sent[0]["text"]


@pytest.mark.asyncio
async def test_nguoi_nhan_da_bi_go_thi_failed(jobs, monkeypatch):
    jobs.db.add(-99, "việc của người đã bị gỡ", channel="zalo")
    outcome = await bg.run_job(await _claim(jobs))
    assert outcome.status == bg.FAILED and jobs.db.rows[1]["error"] == "recipient_gone"
    assert jobs.sent == []


@pytest.mark.asyncio
async def test_tac_vu_dinh_ky_no_reply_khong_gui(jobs, monkeypatch):
    async def runner(job, recipient, budget_sec):
        return _turn("**NO_REPLY**")

    _runner(monkeypatch, runner)
    jobs.db.add(OWNER, "kiểm tra tin VNM", kind="routine", routine_id=3)
    outcome = await bg.run_job(await _claim(jobs))
    assert outcome.status == bg.SUCCEEDED and outcome.no_reply
    assert jobs.db.rows[1]["no_reply"] is True and jobs.sent == [] and jobs.recorded == []
    assert "không có gì đáng báo" in await bg.show_text(OWNER, 1)


def test_nhan_dien_no_reply():
    assert bg.is_no_reply("NO_REPLY") and bg.is_no_reply(" `NO_REPLY`. ")
    assert bg.is_no_reply("NO_REPLY - hôm nay không có tin mới")
    assert not bg.is_no_reply("Hôm nay VNM tăng 2%")


@pytest.mark.asyncio
async def test_tac_vu_dinh_ky_gui_muc_normal_viec_gui_muc_urgent(jobs, monkeypatch):
    async def runner(job, recipient, budget_sec):
        return _turn("VNM có tin chia cổ tức")

    _runner(monkeypatch, runner)
    jobs.db.add(OWNER, "tóm tắt tin VNM", kind="routine", routine_id=3)
    await bg.run_job(await _claim(jobs))
    assert jobs.sent[-1]["source"] == "routine" and "Tác vụ định kỳ #3" in jobs.sent[-1]["text"]
    assert notify_hub.SOURCES["routine"].level == notify_hub.NORMAL
    assert notify_hub.SOURCES["routine"].digest
    assert notify_hub.SOURCES["background_job"].level == notify_hub.URGENT


# ─── notify_hub.send ─────────────────────────────────────────────────────────


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_ID", OWNER)
    monkeypatch.setattr(notify_hub, "_started", True)
    monkeypatch.setattr(notify_hub, "_pending_hint", set())
    state = SimpleNamespace(direct=[], queued=[], now=None)

    async def telegram(uid, text):
        state.direct.append((uid, text))

    async def enqueue(channel, user_id, level, source, content, dedup, cap):
        state.queued.append((channel, level, source, content))
        return len(state.queued), 0

    async def get_setting(key):
        return None

    monkeypatch.setattr(notify_hub, "_telegram_sender", telegram)
    monkeypatch.setattr(db, "notify_enqueue", enqueue)
    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(notify_hub, "_now", lambda: state.now)
    monkeypatch.delenv("NOTIFY_QUIET_HOURS", raising=False)
    return state


@pytest.mark.asyncio
async def test_ket_qua_viec_urgent_gui_ngay_ban_ngay_giu_lai_ban_dem(hub):
    hub.now = dt.datetime(2026, 10, 26, 14, 0, tzinfo=VN)
    assert await notify_hub.send("telegram", OWNER, "xong việc", source="background_job")
    assert hub.direct == [(OWNER, "xong việc")] and hub.queued == []
    hub.now = dt.datetime(2026, 10, 26, 23, 30, tzinfo=VN)
    assert await notify_hub.send("telegram", OWNER, "xong việc 2", source="background_job")
    assert hub.queued == [("telegram", "urgent", "background_job", "xong việc 2")]
    assert len(hub.direct) == 1


@pytest.mark.asyncio
async def test_ket_qua_tac_vu_vao_hang_doi_normal(hub, monkeypatch):
    hub.now = dt.datetime(2026, 10, 26, 23, 30, tzinfo=VN)  # trong giờ yên lặng: chờ
    assert await notify_hub.send("telegram", OWNER, "tổng kết tuần", source="routine")
    assert hub.queued == [("telegram", "normal", "routine", "tổng kết tuần")]
    assert hub.direct == []


@pytest.mark.asyncio
async def test_hub_tat_thi_gui_thang(hub, monkeypatch):
    monkeypatch.setattr(notify_hub, "_started", False)
    assert await notify_hub.send("telegram", OWNER, "tin", source="routine")
    assert hub.direct == [(OWNER, "tin")]
    assert not await notify_hub.send("telegram", 12345, "người lạ", source="routine")


# ─── Side effect bị chặn với run_chat_turn thật ─────────────────────────────


@pytest.mark.asyncio
async def test_luot_nen_that_chan_tool_ghi_va_khong_luu_lich_su(jobs, chat_fakes, monkeypatch):
    from services import channel_chat_service as ccs
    from services import tool_registry as reg

    seen = {}

    async def tool(user_id, text, **kwargs):
        seen.update(kwargs)
        return reg.ToolOutcome(
            status=reg.STATUS_BLOCKED,
            name="set_reminder",
            side_effect=True,
            effect_label="nhắc việc",
            text="lượt này chỉ đọc",
        )

    kinds = []

    async def start(user_id, action, prompt, channel="telegram"):
        kinds.append(action)
        return 11

    monkeypatch.setattr(ccs.tools, "maybe_run_tool", tool)
    monkeypatch.setattr(ccs.telemetry, "start", start)
    jobs.db.add(OWNER, "nhắc anh 8h mai uống thuốc", kind="routine", routine_id=1)
    outcome = await bg.run_job(await _claim(jobs))
    assert seen == {"allow_side_effects": False}
    assert kinds == ["routine"]
    assert chat_fakes["saved"] == []  # run_chat_turn không tự lưu lịch sử
    assert outcome.status == bg.SUCCEEDED and "chưa lưu nhắc việc" in (outcome.result or "")


# ─── /chuy ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_muc_chuy_viec_nen_dang_chay(jobs):
    from services import attention

    await bg.submit(OWNER, "telegram", "so sánh gói cước 5G")
    ctx = attention.AttentionContext(
        user_id=OWNER,
        channel="telegram",
        now=dt.datetime.now(VN),
        is_owner=True,
        is_admin=True,
    )
    text = await bg._attention_jobs(ctx)
    assert text and "Việc nền" in text and "so sánh gói cước 5G" in text
    ctx_other = attention.AttentionContext(-5, "zalo", ctx.now, False, False, "chị")
    assert await bg._attention_jobs(ctx_other) is None  # chỉ thấy việc của chính mình


# ─── /admin ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_bang_viec_nen(client, jobs, monkeypatch):
    row = jobs.db.add(OWNER, "so sánh giá " * 20)
    row.update(
        status="succeeded",
        tokens=321,
        started_at=dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.timezone.utc),
        finished_at=dt.datetime(2026, 10, 26, 3, 1, tzinfo=dt.timezone.utc),
    )

    async def recent(limit=30):
        return [dict(r) for r in jobs.db.rows.values()]

    async def all_routines(limit=50):
        return [
            {
                "id": 3,
                "telegram_user_id": -5,
                "channel": "zalo",
                "prompt": "tóm tắt tin VNM",
                "recurrence": {"type": "weekly", "days": [0], "at": "08:00"},
                "status": "active",
                "next_run_at": dt.datetime(2026, 11, 2, 1, 0, tzinfo=dt.timezone.utc),
            }
        ]

    async def runs_since(since):
        return 4

    monkeypatch.setattr(db, "bg_job_recent_all", recent)
    monkeypatch.setattr(db, "routine_all", all_routines)
    monkeypatch.setattr(db, "routine_runs_since", runs_since)
    assert (await client.get("/admin/api/jobs")).status_code == 403
    await _admin._login(client)
    data = (await client.get("/admin/api/jobs")).json()
    job = data["jobs"][0]
    assert job["status"] == "succeeded" and job["duration_sec"] == 60 and job["tokens"] == 321
    assert len(job["prompt"]) <= 80
    assert data["routines"][0]["schedule"] == "08:00 hằng tuần (T2)"
    assert data["routine_runs_today"] == 4 and data["routine_max_runs_per_day"] == 20
