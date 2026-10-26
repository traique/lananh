"""Golden set - tier A (offline, chạy trong pytest mặc định, không mạng, không key).

- Schema test/evals/*.yaml: id duy nhất, 30-60 case, mỗi nhóm có đủ case nhưng < 20.
- Bộ kiểm tất định bắt đúng câu mẫu xấu và cho qua câu mẫu tốt của từng case.
- Router: tên tool khớp chính xác, giờ nhắc so với mốc đóng băng (dung sai ±2 phút).
- Rào dữ liệu ngoài: mọi mẫu injection chỉ còn đúng 1 cặp marker thật.
- DB giả của tier B không bao giờ chạm DB thật; run_suite có trần lượt gọi + cache.
"""

import json
from datetime import datetime

import pytest

from core import untrusted
from evals import harness

CASES = harness.load_cases()
WITH_EXAMPLES = [c for c in CASES if c.examples.get("good") or c.examples.get("bad")]
FROZEN_NOW = datetime(2026, 10, 26, 10, 0, tzinfo=harness.VN_TZ)


def test_bo_case_hop_le_va_du_so_luong():
    assert 30 <= len(CASES) <= 60
    assert len({c.id for c in CASES}) == len(CASES)
    counts = {category: 0 for category in harness.CATEGORIES}
    for case in CASES:
        counts[case.category] += 1
    assert all(n >= 2 for n in counts.values()), counts
    # Dưới 20 case/nhóm thì tỉ lệ đạt rất nhiễu: bộ kiểm tất định mới là cổng chặn.
    assert all(n < 20 for n in counts.values()), counts
    assert sum(c.kind == "router" for c in CASES) >= 8
    assert sum(c.user == "member" for c in CASES) >= 6


def test_case_sai_schema_bi_tu_choi(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "- id: x1x\n  category: persona\n  prompt: chào\n  checks: {khong_biet: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(harness.CaseError):
        harness.load_cases(tmp_path)
    (tmp_path / "bad.yaml").write_text(
        "- id: router-a\n  category: router\n  kind: router\n  prompt: nhắc anh\n",
        encoding="utf-8",
    )
    with pytest.raises(harness.CaseError, match="expect.tool"):
        harness.load_cases(tmp_path)


@pytest.mark.parametrize("case", WITH_EXAMPLES, ids=[c.id for c in WITH_EXAMPLES])
def test_bo_kiem_tat_dinh_tren_cau_mau(case):
    for good in case.examples.get("good", []):
        assert harness.run_checks(case, good) == [], f"câu tốt bị đánh trượt: {good}"
    for bad in case.examples.get("bad", []):
        assert harness.run_checks(case, bad), f"câu xấu lọt qua: {bad}"


def test_moi_case_chat_deu_co_cau_mau():
    missing = [c.id for c in CASES if c.kind == "chat" and not c.examples.get("bad")]
    assert missing == []


def test_cau_tra_loi_rong_luon_truot():
    case = CASES[0]
    assert harness.run_checks(case, "   ") == ["câu trả lời rỗng"]


def _router_case(case_id):
    return next(c for c in CASES if c.id == case_id)


def test_router_gio_nhac_tinh_bang_code_dung_sai_2_phut():
    case = _router_case("router-nhac-gio-toi-mai")
    ok = {"tool": "set_reminder", "args": {"message": "gọi mẹ", "when": "8h tối mai"}}
    assert harness.check_router(case, ok, FROZEN_NOW) == []
    structured = {
        "tool": "set_reminder",
        "args": {"message": "Gọi mẹ", "date": "27/10/2026", "time": "20:00"},
    }
    assert harness.check_router(case, structured, FROZEN_NOW) == []
    wrong_time = {"tool": "set_reminder", "args": {"message": "gọi mẹ", "when": "9h tối mai"}}
    assert harness.check_router(case, wrong_time, FROZEN_NOW)
    wrong_tool = {"tool": "save_note", "args": {"content": "gọi mẹ"}}
    assert "khác kỳ vọng" in harness.check_router(case, wrong_tool, FROZEN_NOW)[0]
    assert harness.check_router(case, None, FROZEN_NOW) == ["router không trả JSON object"]


def test_router_nhac_sau_n_phut_va_tham_so_chu():
    case = _router_case("router-nhac-sau-30-phut")
    data = {"tool": "set_reminder", "args": {"message": "tắt bếp", "in_minutes": 31}}
    assert harness.check_router(case, data, FROZEN_NOW) == []
    # Code ưu tiên thời điểm viết bằng chữ ("when") rồi mới tới câu gốc / số phút.
    late = {"tool": "set_reminder", "args": {"message": "tắt bếp", "when": "45 phút nữa"}}
    assert harness.check_router(case, late, FROZEN_NOW)
    weather = _router_case("router-thoi-tiet-da-lat")
    assert (
        harness.check_router(
            weather, {"tool": "get_weather", "args": {"location": "Đà Lạt"}}, FROZEN_NOW
        )
        == []
    )
    assert harness.check_router(
        weather, {"tool": "get_weather", "args": {"location": "Hà Nội"}}, FROZEN_NOW
    )
    none_case = _router_case("router-khong-can-tool")
    assert harness.check_router(none_case, {"tool": "none"}, FROZEN_NOW) == []


INJECTION = [c for c in CASES if c.untrusted]


@pytest.mark.parametrize("case", INJECTION, ids=[c.id for c in INJECTION])
def test_rao_du_lieu_ngoai_chi_con_mot_cap_marker_that(case, monkeypatch):
    monkeypatch.setattr(untrusted, "_flag_injection", lambda source, phrase: None)
    wrapped = untrusted.wrap_untrusted(case.untrusted["text"], case.untrusted["source"])
    markers = list(untrusted._MARKER_RE.finditer(wrapped))
    assert [m.group(1) for m in markers] == [untrusted.OPEN_TAG, untrusted.CLOSE_TAG]
    assert markers[0].group(2) == markers[1].group(2)
    body = wrapped[markers[0].end() : markers[1].start()]
    assert "<<<" not in body and ">>>" not in body
    assert "​" not in body


def test_mau_injection_duoc_nhan_ra():
    flagged = {
        c.id: bool(untrusted.find_injection(untrusted.sanitize(c.untrusted["text"])))
        for c in INJECTION
    }
    assert flagged["injection-trang-web-chen-link"]
    assert flagged["injection-rss-doi-vai"]
    assert flagged["injection-ky-tu-vo-hinh"]


def test_grounding_cua_case_duoc_rao():
    case = _router_case("injection-trang-web-chen-link")
    grounding = harness.grounding_for(case)
    assert untrusted.contains_untrusted(grounding)


async def test_db_gia_khong_bao_gio_cham_db_that():
    from core import config
    from core import database as db

    before = config.DATABASE_URL
    with harness.fake_database() as fake:
        assert config.DATABASE_URL == ""
        await db.set_setting("provider_cooldown", "x")
        assert await db.get_setting("provider_cooldown") == "x"
        fake.history[1] = [("user", "a"), ("model", "b")]
        assert await db.get_session_messages(1, 1, 60) == [("model", "b")]
        with pytest.raises(harness.EvalDatabaseBlocked):
            await db.get_pool()
    assert config.DATABASE_URL == before
    assert not isinstance(db.get_pool, type(fake.get_pool))


async def test_run_suite_tran_luot_goi_va_cache(monkeypatch, tmp_path):
    from ai import orchestrator
    from core import database as db

    persona = next(c for c in CASES if c.id == "persona-chao-chu-bot-ngan")
    router = _router_case("router-ghi-chu")
    seen = []

    async def fake_chat(user_id, prompt, **kwargs):
        seen.append(("chat", kwargs["search_mode"]))
        await db.record_provider_call("groq", "gpt-oss-test", outcome="ok")
        return type(
            "R",
            (),
            {"text": persona.examples["good"][0], "provider": "groq", "model": "gpt-oss-test"},
        )()

    async def fake_utility(prompt):
        seen.append(("router", "tool" in prompt))
        await db.record_provider_call("groq", "gpt-oss-test", outcome="ok")
        return {"tool": "save_note", "args": {"content": "mật khẩu wifi hoa-sen-2026"}}

    monkeypatch.setattr(orchestrator, "chat", fake_chat)
    monkeypatch.setattr(orchestrator, "utility_json", fake_utility)
    cache = tmp_path / "cache.json"
    report = await harness.run_suite([persona, router], max_calls=5, cache_path=cache)
    assert report["summary"]["passed"] == 2 and report["provider_calls"] == 2
    assert seen == [("chat", "off"), ("router", True)]
    assert report["cases"][0]["provider"] == "groq"
    assert report["summary"]["by_provider"]["groq"]["passed"] == 2
    json.dumps(report, ensure_ascii=False)  # báo cáo ghi được ra JSON
    # Lần 2: lấy kết quả từ cache, không gọi lại provider.
    again = await harness.run_suite([persona, router], max_calls=5, cache_path=cache)
    assert all(case["cached"] for case in again["cases"]) and len(seen) == 2
    # Trần lượt gọi: case vượt trần bị bỏ qua và ghi lại.
    capped = await harness.run_suite([persona, router], max_calls=1, cache_path=None)
    assert capped["skipped"] == ["router-ghi-chu"]
    assert capped["summary"]["skipped_calls_cap"] == 1


async def test_case_loi_provider_thanh_that_bai_khong_dung_ca_bo(monkeypatch):
    from ai import orchestrator

    async def boom(*args, **kwargs):
        raise RuntimeError("Groq HTTP 429")

    monkeypatch.setattr(orchestrator, "chat", boom)
    case = CASES[0]
    with harness.fake_database() as fake:
        result = await harness.run_case(case, fake)
    assert not result.passed and "RuntimeError" in result.error


async def test_giam_khao_chi_tham_khao_va_ghim_model(monkeypatch):
    from ai import groq_client

    monkeypatch.setenv("EVAL_JUDGE_MODEL", "model-ghim")

    async def generate(prompt, **kwargs):
        assert kwargs["model"] == "model-ghim" and kwargs["temperature"] == 0.0
        return type("R", (), {"text": '{"verdict": "dat", "reason": "đúng giọng"}'})()

    monkeypatch.setattr(groq_client, "generate", generate)
    verdict = await harness.judge_reply(CASES[0], "Dạ em chào anh ạ")
    assert verdict == {"verdict": "dat", "reason": "đúng giọng", "model": "groq/model-ghim"}


def test_marker_live_llm_da_dang_ky(pytestconfig):
    assert any(line.startswith("live_llm") for line in pytestconfig.getini("markers"))


def test_script_run_evals_tu_choi_khi_chua_bat(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_evals", harness.ROOT / "scripts" / "run_evals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("RUN_LIVE_EVALS", raising=False)
    assert module.main([]) == 2
    monkeypatch.setenv("RUN_LIVE_EVALS", "1")
    monkeypatch.setenv("RENDER", "true")
    assert module.main([]) == 2
    assert "Render" in capsys.readouterr().out


async def test_duong_chat_that_chay_tron_tren_db_gia(monkeypatch):
    """orchestrator.chat + utility_json THẬT (chỉ thay HTTP của Groq): mọi chỗ đụng DB
    trên đường đi phải nằm trong bản giả - không lượt nào rơi vào get_pool bị chặn."""
    from ai import groq_client, orchestrator
    from ai.openai_compatible import Response
    from ai.provider_state import provider_state

    persona = next(c for c in CASES if c.id == "persona-thanh-vien-goi-chi")
    router = _router_case("router-ghi-chu")
    seen = {}

    async def generate(prompt, *, system_instruction=None, history=None, **kwargs):
        seen["system"] = system_instruction or ""
        return Response(persona.examples["good"][0])

    async def generate_json(prompt, **kwargs):
        return {"tool": "save_note", "args": {"content": "wifi hoa-sen-2026"}}

    async def model():
        return "gpt-oss-test"

    monkeypatch.setattr(orchestrator.config, "PROVIDER_ORDER", ["groq"])
    monkeypatch.setattr(orchestrator.config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(groq_client, "generate", generate)
    monkeypatch.setattr(groq_client, "generate_json", generate_json)
    monkeypatch.setattr(groq_client, "_model", model)
    monkeypatch.setattr(provider_state, "_loaded", False)
    provider_state.api_exhausted_until.pop("groq", None)
    with harness.fake_database() as fake:
        chat = await harness.run_case(persona, fake)
        routed = await harness.run_case(router, fake)
    assert chat.passed and chat.error == "", chat.failures
    assert (chat.provider, chat.model) == ("groq", "gpt-oss-test")
    assert '"chị"' in seen["system"] or "chị" in seen["system"]
    assert routed.passed, routed.failures
    assert [row["provider"] for row in fake.provider_calls] == ["groq", "groq"]


# ─── /xuateval: 👎 của chủ bot trên Telegram -> case ứng viên ────────────────


async def test_xuat_case_ung_vien_chi_tu_telegram_cua_chu_bot(monkeypatch, tmp_path):
    from datetime import timezone

    from core import config
    from core import database as db
    from services import eval_export

    seen = {}

    async def owner_down(user_id, days, limit):
        seen.update(user_id=user_id, days=days, limit=limit)
        return [
            {
                "id": 41,
                "prompt_text": "giá vàng hôm nay",
                "answer_text": "Vàng khoảng 149 triệu 复利",
                "reason": "bịa số",
                "provider": "groq",
                "model": "gpt-oss",
                "created_at": datetime(2026, 10, 25, 3, tzinfo=timezone.utc),
            },
            {"id": 42, "prompt_text": "  ", "answer_text": "x", "reason": None},
        ]

    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    monkeypatch.setattr(db, "answer_feedback_owner_down", owner_down)
    name, data, count = await eval_export.export("7d")
    assert seen == {"user_id": 777, "days": 7, "limit": eval_export.MAX_CASES}
    assert name.startswith("eval-candidates-") and count == 1
    text = data.decode("utf-8")
    assert "Bỏ thông tin cá nhân" in text and "lý do: bịa số" in text
    # Cùng định dạng golden set: nạp được bằng harness, câu bị chê nằm ở examples.bad.
    (tmp_path / "cand.yaml").write_text(text, encoding="utf-8")
    [case] = harness.load_cases(tmp_path)
    assert case.id == "fb-41" and case.user == "owner" and case.channel == "telegram"
    assert harness.run_checks(case, case.examples["bad"][0])  # chữ CJK -> trượt


async def test_xuat_case_khong_co_gi_hoac_khong_co_chu_bot(monkeypatch):
    from core import config
    from core import database as db
    from services import eval_export

    async def empty(user_id, days, limit):
        return []

    monkeypatch.setattr(db, "answer_feedback_owner_down", empty)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 777)
    assert await eval_export.export("") is None
    monkeypatch.setattr(config, "ALLOWED_USER_ID", 0)
    assert await eval_export.candidate_cases() == []
    assert eval_export.parse_days("") == 30 and eval_export.parse_days("999") == 180
    assert eval_export.parse_days("7 ngày") == 7 and eval_export.parse_days("abc") == 30


async def test_lenh_xuateval_gui_file(monkeypatch):
    from types import SimpleNamespace

    from handlers import common, feedback_commands

    sent = {}

    class Message:
        async def reply_document(self, document, filename, caption):
            sent.update(data=document.read(), filename=filename, caption=caption)

        async def reply_text(self, text):
            sent["text"] = text

    async def export(argument):
        sent["arg"] = argument
        return "eval-candidates-20261026.yaml", b"- id: fb-1\n", 1

    monkeypatch.setattr(common, "check_access", lambda update: True)
    monkeypatch.setattr(feedback_commands.eval_export, "export", export)
    update = SimpleNamespace(message=Message(), effective_user=SimpleNamespace(id=777))
    await feedback_commands.xuateval_cmd(update, SimpleNamespace(args=["14d"]))
    assert sent["arg"] == "14d" and sent["data"] == b"- id: fb-1\n"
    assert "repo public" in sent["caption"]

    async def none(argument):
        return None

    monkeypatch.setattr(feedback_commands.eval_export, "export", none)
    await feedback_commands.xuateval_cmd(update, SimpleNamespace(args=[]))
    assert "Chưa có" in sent["text"]


def test_lenh_xuateval_co_trong_menu_va_help():
    import bot_app
    from handlers import commands

    assert any(cmd.command == "xuateval" for cmd in bot_app.COMMANDS)
    assert "/xuateval" in commands.HELP_TEXT


def test_workflow_danh_gia_chi_chay_tay_va_khong_co_db():
    import yaml

    path = harness.ROOT / ".github" / "workflows" / "evals.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}  # không lịch, không push/PR
    assert data["permissions"] == {"contents": "read"}
    env = data["jobs"]["evals"]["env"]
    assert env["DATABASE_URL"] == "" and env["RUN_LIVE_EVALS"] == "1"
    assert all("EVAL_" in str(env[k]) for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY"))
    text = path.read_text(encoding="utf-8")
    # Input không ghép thẳng vào lệnh shell (chống chèn lệnh qua workflow_dispatch).
    run_steps = [s.get("run", "") for s in data["jobs"]["evals"]["steps"]]
    assert not any("${{ inputs." in step for step in run_steps)
    assert "--max-calls" in text
