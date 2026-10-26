"""Unit test cho services/memory_service.py (trí nhớ v2: trích theo lô có
watermark, ADD/UPDATE/INVALIDATE/FORGET, chặn bí mật, cách trả lời, ghi chú/tóm
tắt phiên, ráp khối trí nhớ trong ngân sách) - xem docstring đầu module."""

import asyncio

import pytest

from _memory_fakes import FakeMemoryDB
from core import database as db
from services import memory_guard, memory_service, personal_settings

UID = 1


@pytest.fixture
def mem(monkeypatch):
    fake = FakeMemoryDB()
    fake.install(monkeypatch, db)
    fake.prompts = []
    fake.responses = []

    async def fake_utility(prompt):
        fake.prompts.append(prompt)
        return fake.responses.pop(0) if fake.responses else {"facts": []}

    monkeypatch.setattr(memory_service, "_utility_json", fake_utility)
    return fake


def _wm(fake, uid=UID):
    return int(fake.settings.get(personal_settings.memory_watermark_key(uid), "0"))


# ─── Buffer + flush ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_doi_du_luot_moi_goi_ai_mot_lan_cho_ca_cua_so(mem):
    mem.add_turn(UID, "anh làm kỹ sư phần mềm", "dạ")
    mem.add_turn(UID, "ok em", "dạ")  # tầm thường: không tính
    mem.add_turn(UID, "vợ anh tên Lan", "dạ")
    assert await memory_service.maybe_flush(UID) is False
    assert mem.prompts == []
    mem.add_turn(UID, "anh thích chạy bộ buổi sáng", "hay quá")
    mem.responses.append(
        {
            "facts": [
                {"op": "add", "key": "cong_viec", "value": "kỹ sư phần mềm", "importance": 3},
                {"op": "add", "key": "ten_vo", "value": "Lan", "category": "gia_dinh"},
            ],
            "highlights": ["Anh tập chạy bộ buổi sáng"],
            "session": {"title": "Làm quen", "bullets": ["Kể về gia đình"], "summary": "Anh kể."},
        }
    )
    assert await memory_service.maybe_flush(UID) is True
    assert len(mem.prompts) == 1
    prompt = mem.prompts[0]
    assert "kỹ sư phần mềm" in prompt and "vợ anh tên Lan" in prompt and "chạy bộ" in prompt
    facts = {f["key"]: f for f in mem.current(UID)}
    assert facts["cong_viec"]["importance"] == 3
    assert facts["ten_vo"]["category"] == "gia_dinh"
    assert [h["content"] for h in mem.highlights] == ["Anh tập chạy bộ buổi sáng"]
    assert _wm(mem) == mem.chat[-1]["id"]
    note = next(iter(mem.session_notes.values()))
    assert note["title"] == "Làm quen" and note["summary"] == "Anh kể."
    # Đã xử lý hết: lần sau không gọi AI.
    assert await memory_service.maybe_flush(UID) is False and len(mem.prompts) == 1


@pytest.mark.asyncio
async def test_toan_luot_tam_thuong_hoac_khong_phai_chat_tien_watermark_khong_goi_ai(mem):
    mem.add_turn(UID, "phân tích FPT", "📊 FPT", kind="stock")
    mem.add_turn(UID, "cảm ơn em", "dạ")
    mem.add_turn(UID, "👍", "dạ")
    assert await memory_service.maybe_flush(UID, explicit=True) is True
    assert mem.prompts == [] and _wm(mem) == mem.chat[-1]["id"]


@pytest.mark.asyncio
async def test_ai_loi_giu_watermark_de_lan_sau_thu_lai(mem, monkeypatch):
    async def none(prompt):
        return None

    monkeypatch.setattr(memory_service, "_utility_json", none)
    mem.add_turn(UID, "anh tên Trai", "dạ")
    assert await memory_service.maybe_flush(UID, explicit=True) is False
    assert _wm(mem) == 0


@pytest.mark.asyncio
async def test_phien_cu_ket_thuc_thi_flush_du_chua_du_nguong(mem):
    mem.add_turn(UID, "anh sắp chuyển nhà ra Hà Nội", "dạ", minutes_ago=60 * 8)
    mem.add_turn(UID, "chào em", "chào anh")
    assert await memory_service.maybe_flush(UID) is True
    assert len(mem.prompts) == 1 and "chuyển nhà" in mem.prompts[0]


@pytest.mark.asyncio
async def test_cua_so_qua_2_phien_tach_2_luot_ghi_chu_rieng(mem):
    for i in range(3):
        mem.add_turn(UID, f"phiên cũ chuyện số {i} về đầu tư", "dạ", minutes_ago=60 * 10)
    for i in range(3):
        mem.add_turn(UID, f"phiên mới chuyện số {i} về du lịch", "dạ")
    mem.responses += [{"session": {"title": "Cũ"}}, {"session": {"title": "Mới"}}]
    assert await memory_service.maybe_flush(UID) is True
    assert len(mem.prompts) == 2
    assert "đầu tư" in mem.prompts[0] and "du lịch" not in mem.prompts[0]
    titles = sorted(n["title"] for n in mem.session_notes.values())
    assert titles == ["Cũ", "Mới"]


@pytest.mark.asyncio
async def test_tri_nho_tat_bo_qua_va_khong_goi_ai(mem):
    mem.settings["memory_enabled_1"] = "0"
    mem.add_turn(UID, "anh tên Trai", "dạ")
    assert await memory_service.maybe_flush(UID, explicit=True) is True
    assert mem.prompts == [] and _wm(mem) == mem.chat[-1]["id"]
    assert await memory_service.build_memory_context(UID, "tên anh") == ""


@pytest.mark.asyncio
async def test_after_exchange_moi_nguoi_toi_da_mot_task(mem, monkeypatch):
    started = []
    gate = asyncio.Event()

    async def slow(user_id, **kwargs):
        started.append(user_id)
        await gate.wait()
        return True

    monkeypatch.setattr(memory_service, "maybe_flush", slow)
    memory_service.after_exchange(UID)
    memory_service.after_exchange(UID)
    memory_service.after_exchange(2)
    await asyncio.sleep(0)
    assert sorted(started) == [1, 2]
    gate.set()
    await memory_service.stop_background_tasks()


# ─── Thao tác trên fact ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_giu_gia_tri_cu_invalidate_va_bao_ve_fact_nguoi_dung(mem):
    await db.save_fact(UID, "cong_viec", "PM ở A")
    await db.save_fact(UID, "que", "Huế")
    await db.save_fact(UID, "ten", "Trai", source="user", pinned=True)
    ids = {f["key"]: f["id"] for f in mem.current(UID)}
    mem.add_turn(UID, "anh chuyển sang làm PM ở B rồi", "dạ")
    mem.responses.append(
        {
            "facts": [
                {"op": "update", "id": ids["cong_viec"], "value": "PM ở B"},
                {"op": "invalidate", "id": ids["que"]},
                {"op": "update", "id": ids["ten"], "value": "Tên khác"},  # bị bảo vệ
                {"op": "forget", "id": ids["ten"]},  # không có ý bảo quên -> bỏ
                {"op": "update", "id": 99999, "value": "id lạ"},  # id không hiển thị -> bỏ
            ]
        }
    )
    await memory_service.maybe_flush(UID, explicit=True)
    facts = {f["key"]: f["value"] for f in mem.current(UID)}
    assert facts == {"cong_viec": "PM ở B", "ten": "Trai"}
    old = await db.get_superseded_facts(UID)
    assert {f["value"] for f in old} == {"PM ở A", "Huế"}
    # Prompt hiển thị id + đánh dấu fact người dùng tự nhập.
    assert f"id {ids['ten']} | ten = Trai (người dùng tự nhập" in mem.prompts[0]


@pytest.mark.asyncio
async def test_forget_chi_khi_nguoi_dung_chu_dong_bao_quen_va_khong_hoc_lai(mem):
    await db.save_fact(UID, "so_thich", "uống bia")
    fact_id = mem.current(UID)[0]["id"]
    mem.add_turn(UID, "quên chuyện anh thích uống bia đi nhé", "dạ")
    mem.responses.append({"facts": [{"op": "forget", "id": fact_id}]})
    await memory_service.maybe_flush(UID, explicit=True)
    assert mem.current(UID) == [] and await db.get_superseded_facts(UID) == []
    # Lượt sau AI định học lại -> bị chặn bởi danh sách "đừng học lại".
    mem.add_turn(UID, "hôm qua anh đi nhậu", "dạ")
    mem.responses.append({"facts": [{"op": "add", "key": "so_thich", "value": "uống bia"}]})
    await memory_service.maybe_flush(UID, explicit=True)
    assert mem.current(UID) == []
    assert "so_thich" in mem.prompts[-1].split("### Người dùng đã yêu cầu quên")[1]


@pytest.mark.asyncio
async def test_dinh_dang_cu_delete_thanh_invalidate_va_chan_bi_mat(mem):
    await db.save_fact(UID, "nghe_nghiep", "kỹ sư")
    mem.add_turn(UID, "mật khẩu wifi nhà anh là abc12345", "dạ")
    mem.responses.append(
        {
            "facts": [
                {"key": "nghe_nghiep", "delete": True},
                {"key": "mat_khau_wifi", "value": "abc12345"},
                {"key": "wifi", "value": "mật khẩu là abc12345"},
                {"key": "so_the", "value": "4111 1111 1111 1111"},
            ],
            "highlights": ["OTP 123456 để đăng nhập"],
            "session": {"title": "Wifi", "summary": "Anh nói mật khẩu: abc12345 cho em."},
        }
    )
    await memory_service.maybe_flush(UID, explicit=True)
    assert mem.current(UID) == []  # nghe_nghiep bị vô hiệu hoá (vẫn còn trong lịch sử)
    assert [f["value"] for f in await db.get_superseded_facts(UID)] == ["kỹ sư"]
    assert mem.highlights == []
    note = next(iter(mem.session_notes.values()))
    assert "abc12345" not in note["summary"] and "[đã ẩn mật khẩu]" in note["summary"]
    # Cửa sổ gửi AI cũng đã ẩn bí mật.
    assert "abc12345" not in mem.prompts[0]


@pytest.mark.asyncio
async def test_style_rules_luu_cach_tra_loi_va_chen_moi_luot(mem):
    mem.add_turn(UID, "trả lời ngắn thôi em, đừng chèn emoji", "dạ")
    mem.responses.append({"style_rules": "- Trả lời ngắn gọn\n- Không dùng emoji"})
    await memory_service.maybe_flush(UID, explicit=True)
    row = next(f for f in mem.current(UID) if f["key"] == memory_service.STYLE_KEY)
    assert row["category"] == memory_service.STYLE_KEY and row["importance"] == 3
    turn = await memory_service.build_turn_memory(UID, "giá vàng hôm nay")
    assert "Không dùng emoji" in turn.style_rules and "KHÔNG đổi cách xưng hô" in turn.style_rules
    # Không nằm trong khối trí nhớ (có thể bị cắt giữa chừng).
    assert "Không dùng emoji" not in turn.context


@pytest.mark.asyncio
async def test_trim_giu_fact_ghim_va_quan_trong(mem, monkeypatch):
    monkeypatch.setattr(memory_service, "MAX_FACTS_PER_USER", 3)
    await db.save_fact(UID, "ngay_sinh", "15/3/1990", importance=3)
    await db.save_fact(UID, "ghim", "x", pinned=True, importance=1, source="user")
    mem.add_turn(UID, "chuyện vặt", "dạ")
    mem.responses.append(
        {
            "facts": [
                {"op": "add", "key": f"vat_{i}", "value": "y", "importance": 1} for i in range(4)
            ]
        }
    )
    await memory_service.maybe_flush(UID, explicit=True)
    keys = {f["key"] for f in mem.current(UID)}
    assert {"ngay_sinh", "ghim"} <= keys and len(keys) == 3


# ─── Đọc: ráp khối trí nhớ ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dau_phien_trong_ngan_sach_khong_cat_giua_va_uu_tien_fact_ben(mem, monkeypatch):
    from ai import prompt_budget

    monkeypatch.setattr(prompt_budget, "MEMORY_MAX_CHARS", 900)
    for i in range(30):
        await db.save_fact(UID, f"chuyen_{i}", "một chi tiết vụn vặt khá dài " * 2, importance=1)
    await db.save_fact(UID, "ten", "Trai", importance=3)
    await db.save_fact(UID, "ngay_sinh", "15/3/1990", importance=3)
    mem.summary[UID] = "Câu một rất dài. " * 80
    for i in range(25):
        await db.add_highlight(UID, f"Chi tiết số {i}", 25)
    ctx = await memory_service.build_memory_context(UID, "")
    assert len(ctx) <= 900
    assert "ten = Trai" in ctx and "ngay_sinh = 15/3/1990" in ctx
    assert "(từ " in ctx  # mốc thời gian của fact bền
    assert "[... đã lược bớt" not in prompt_budget.truncate_middle(ctx, 900)
    for line in ctx.splitlines():
        if line.startswith("Tóm tắt cũ:"):
            assert line.endswith(".") or line.endswith("…")


@pytest.mark.asyncio
async def test_dau_phien_co_ghi_chu_lien_quan_va_cac_phien_truoc(mem):
    await db.add_note(UID, "size giày của vợ là 38")
    await db.add_note(UID, "mua sữa cho bé")
    await db.upsert_session_note(
        UID, 5, title="Bàn chuyện FPT", bullets="- Hỏi giá FPT", started_at=mem.now
    )
    ctx = await memory_service.build_memory_context(UID, "size giày vợ anh bao nhiêu nhỉ")
    assert "size giày của vợ là 38" in ctx and "sữa" not in ctx
    assert "Bàn chuyện FPT" in ctx


@pytest.mark.asyncio
async def test_giua_phien_co_tom_tat_phien_va_noi_dung_lien_quan(mem):
    mem.add_turn(UID, "chuyện đầu phiên", "dạ")
    first_id = mem.chat[0]["id"]
    await db.upsert_session_note(
        UID, first_id, summary="Đầu phiên anh hỏi về FPT.", started_at=mem.now
    )
    await db.save_fact(UID, "mon_an_thich", "phở bò")
    await db.save_fact(UID, "xe", "Vinfast VF8")
    await db.add_note(UID, "size giày của vợ là 38")
    ctx = await memory_service.build_memory_context(UID, "tối nay ăn phở không")
    assert "TÓM TẮT PHIÊN NÀY" in ctx and "Đầu phiên anh hỏi về FPT." in ctx
    assert "mon_an_thich=phở bò" in ctx and "Vinfast" not in ctx and "giày" not in ctx
    ctx = await memory_service.build_memory_context(UID, "size giày của vợ")
    assert "size giày của vợ là 38" in ctx


@pytest.mark.asyncio
async def test_giua_phien_hoi_truoc_day_co_gia_tri_cu(mem):
    mem.add_turn(UID, "chào", "chào anh")
    await db.save_fact(UID, "cong_viec", "PM ở A")
    await db.save_fact(UID, "cong_viec", "PM ở B")
    ctx = await memory_service.build_memory_context(UID, "trước đây anh làm công việc gì")
    assert "Trước đây: cong_viec = PM ở A" in ctx


@pytest.mark.asyncio
async def test_doc_tri_nho_db_loi_tra_rong(monkeypatch):
    async def broken(*args, **kwargs):
        raise ConnectionError("db down")

    monkeypatch.setattr(db, "get_setting", broken)
    turn = await memory_service.build_turn_memory(1, "hỏi")
    assert turn == memory_service.TurnMemory()


@pytest.mark.asyncio
async def test_clear_memory_xoa_het_va_bo_qua_lich_su_cu(mem):
    await db.save_fact(UID, "ten", "Trai")
    await db.add_highlight(UID, "x", 25)
    await db.upsert_session_note(UID, 1, title="t", started_at=mem.now)
    mem.summary[UID] = "cũ"
    mem.add_turn(UID, "anh tên Trai", "dạ")
    await memory_service.clear_memory(UID)
    assert mem.facts == [] and mem.highlights == [] and mem.session_notes == {}
    assert mem.summary[UID] == "" and _wm(mem) == mem.chat[-1]["id"]


# ─── Khoá theo user ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_khoa_theo_user_khong_chay_chong_nhau(mem, monkeypatch):
    order: list[str] = []
    release_first = asyncio.Event()

    async def fake_utility(prompt):
        if "cham" in prompt.split("Các lượt CHƯA xử lý")[1]:
            order.append("bat_dau_cham")
            await release_first.wait()
            order.append("ket_thuc_cham")
        else:
            order.append("bat_dau_nhanh")
        return {"facts": []}

    monkeypatch.setattr(memory_service, "_utility_json", fake_utility)
    mem.add_turn(UID, "cham", "phan hoi")
    task_cham = asyncio.create_task(memory_service.maybe_flush(UID, explicit=True))
    await asyncio.sleep(0.01)
    mem.add_turn(UID, "nhanh", "phan hoi")
    task_nhanh = asyncio.create_task(memory_service.maybe_flush(UID, explicit=True))
    await asyncio.sleep(0.01)
    assert order == ["bat_dau_cham"]
    release_first.set()
    await asyncio.gather(task_cham, task_nhanh)
    assert order == ["bat_dau_cham", "ket_thuc_cham", "bat_dau_nhanh"]


@pytest.mark.asyncio
async def test_flush_khac_user_khong_bi_chan_nhau(mem, monkeypatch):
    release = asyncio.Event()
    done: list[int] = []

    async def fake_utility(prompt):
        if "user1" in prompt.split("Các lượt CHƯA xử lý")[1]:
            await release.wait()
            done.append(1)
        else:
            done.append(2)
        return {"facts": []}

    monkeypatch.setattr(memory_service, "_utility_json", fake_utility)
    mem.add_turn(1, "user1 nói", "x")
    mem.add_turn(2, "user2 nói", "x")
    t1 = asyncio.create_task(memory_service.maybe_flush(1, explicit=True))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(memory_service.maybe_flush(2, explicit=True))
    await asyncio.sleep(0.01)
    assert done == [2]
    release.set()
    await asyncio.gather(t1, t2)


# ─── Chặn bí mật, câu tầm thường ─────────────────────────────────────────────


def test_memory_guard_bat_bi_mat_theo_tu_khoa_khong_bat_so_tran():
    assert memory_guard.find_secret("mật khẩu wifi là abc12345") == "mật khẩu"
    assert memory_guard.find_secret("mk: hoa1234") == "mật khẩu"
    assert memory_guard.find_secret("OTP của anh 482913") == "mã OTP"
    assert memory_guard.find_secret("stk 0123456789 Vietcombank") == "số tài khoản"
    assert memory_guard.find_secret("cccd 079090001234") == "số giấy tờ tuỳ thân"
    assert memory_guard.find_secret("thẻ 4111 1111 1111 1111") == "số thẻ"
    assert memory_guard.find_secret("key sk-proj-abcdefghijklmnopqrstu") == "API key"
    # Không bắt nhầm giá, khối lượng, số điện thoại, "mk" = "mình".
    for text in (
        "FPT giá 120.500, khối lượng 2.345.678 cp",
        "số điện thoại 0912345678",
        "mk thích cà phê muối",
        "anh quên mật khẩu rồi",
        "doanh thu 1234567890123 đồng",
    ):
        assert memory_guard.find_secret(text) is None, text
    assert memory_guard.redact("OTP 482913 nhé") == "[đã ẩn mã OTP] nhé"


def test_cau_tam_thuong():
    for text in ("ok", "Ok em", "ừm", "cảm ơn em", "👍", "hihi", "Dạ"):
        assert memory_service.is_trivial(text), text
    for text in ("ok mai anh đi phỏng vấn", "anh tên Trai"):
        assert not memory_service.is_trivial(text), text


def test_cat_o_ranh_gioi_cau():
    text = "Câu một ngắn. Câu hai dài hơn chút. Câu ba."
    assert memory_service.cut_at_sentence(text, 30) == "Câu một ngắn."
    assert memory_service.cut_at_sentence("không có dấu chấm nào cả", 12).endswith("…")


def test_select_relevant_memory_chon_theo_token_trung_va_ngan_sach():
    facts = [
        ("ten_con", "Bé Bin 5 tuổi"),
        ("so_thich", "chạy bộ buổi sáng"),
        ("cong_ty", "Nắng Decor"),
    ]
    highlights = ["Anh đang tập chạy half marathon tháng 11", "Anh thích cà phê muối"]
    chosen_facts, chosen_hl = memory_service.select_relevant_memory(
        "lịch chạy bộ tuần này nên thế nào", facts, highlights
    )
    assert chosen_facts == [("so_thich", "chạy bộ buổi sáng")]
    assert chosen_hl == ["Anh đang tập chạy half marathon tháng 11"]
    assert memory_service.select_relevant_memory("thời tiết Hà Nội", facts, highlights) == ([], [])
    assert memory_service.select_relevant_memory("ừ", facts, highlights) == ([], [])
    many = [(f"k{i}", "chạy bộ " * 20) for i in range(10)]
    chosen, _ = memory_service.select_relevant_memory("chạy bộ", many, [], max_chars=400)
    assert 0 < len(chosen) < 10


# ─── Vòng quét nền, gộp fact, /reset ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_quet_nen_flush_khi_ranh_va_gop_fact_moi_ngay_mot_lan(mem, monkeypatch):
    monkeypatch.setattr(memory_service, "MAX_FACTS_PER_USER", 5)
    for i in range(4):
        await db.save_fact(UID, f"so_thich_{i}", "cà phê")
    ids = [f["id"] for f in mem.current(UID)]
    mem.add_turn(UID, "anh mới mua xe máy điện", "dạ", minutes_ago=45)
    mem.responses += [
        {"facts": []},
        {
            "ops": [
                {"op": "update", "id": ids[0], "value": "cà phê"},
                {"op": "invalidate", "id": ids[1]},
            ]
        },
    ]
    await memory_service.sweep_once()
    assert len(mem.prompts) == 2
    assert "mua xe máy điện" in mem.prompts[0]  # rảnh > MEMORY_IDLE_FLUSH_SEC
    assert "TRÙNG NGHĨA" in mem.prompts[1]
    assert len(mem.current(UID)) == 3
    await memory_service.sweep_once()  # đã gộp hôm nay, không còn lượt chưa xử lý
    assert len(mem.prompts) == 2


@pytest.mark.asyncio
async def test_gop_fact_tat_bang_bien_moi_truong(mem, monkeypatch):
    monkeypatch.setenv("MEMORY_CONSOLIDATION", "0")
    monkeypatch.setattr(memory_service, "MAX_FACTS_PER_USER", 2)
    await db.save_fact(UID, "a", "x")
    await db.save_fact(UID, "b", "y")
    await memory_service._consolidate_if_due(UID)
    assert mem.prompts == []


@pytest.mark.asyncio
async def test_flush_truoc_reset_co_tran_thoi_gian(mem, monkeypatch):
    async def slow(prompt):
        await asyncio.sleep(5)

    monkeypatch.setattr(memory_service, "_utility_json", slow)
    mem.add_turn(UID, "anh tên Trai", "dạ")
    started = asyncio.get_running_loop().time()
    await memory_service.flush_before_reset(UID, timeout_sec=0.05)
    assert asyncio.get_running_loop().time() - started < 2
    assert _wm(mem) == 0


def test_loi_xung_ho_khong_con_keo_tri_nho_giua_phien():
    """Chủ bot gần như luôn viết "anh"/"em": trước đây mỗi câu đều khớp fact có
    chữ "anh" và nhồi trí nhớ vào giữa phiên."""
    facts = [("anh_trai", "anh Tuấn ở Đà Nẵng"), ("thoi_quen", "anh hay dậy sớm")]
    assert memory_service.select_relevant_memory("anh ơi em có nhớ gì không", facts, []) == (
        [],
        [],
    )
    assert memory_service.select_relevant_memory("anh trai anh ở đâu", facts, [])[0] == [
        ("anh_trai", "anh Tuấn ở Đà Nẵng")
    ]
