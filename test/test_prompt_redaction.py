"""Che bí mật trong prompt gửi provider (services/prompt_redaction.py +
memory_guard.prompt_secret_spans): ẩn đúng phần giá trị, KHÔNG ẩn nhầm giá/khối
lượng cổ phiếu, số điện thoại, số tiền; ráp ở orchestrator chat/ask/utility_json/
analyze_image, /agent; router tool khôi phục tham số; DB vẫn lưu nguyên văn."""

import pytest

from ai import agent_service, official_client, openai_compatible, orchestrator
from ai.openai_compatible import ToolCallResponse
from ai.provider_state import provider_state
from core import config, database as db
from services import memory_guard, prompt_redaction, tools
from services import tool_registry as reg


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    store: dict[str, str] = {}

    async def get_setting(key):
        return store.get(key)

    async def set_setting(key, value):
        store[key] = value

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.delenv("PROMPT_REDACTION", raising=False)
    provider_state.router9_dead_since = None
    provider_state.router9_enabled = True
    provider_state.api_exhausted_until = {"groq": 0.0, "openrouter": 0.0, "api1": 0.0, "api2": 0.0}
    provider_state._loaded = True
    monkeypatch.setattr(config, "PROVIDER_ORDER", ["router9"])
    yield


def _mask(text: str) -> str:
    return prompt_redaction.Redactor(True).text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mật khẩu wifi là abc12345", "mật khẩu wifi là [đã ẩn: mật khẩu]"),
        ("mk: hoa1234 nhé", "mk: [đã ẩn: mật khẩu] nhé"),
        ("pass wifi: Hoasen2026", "pass wifi: [đã ẩn: mật khẩu]"),
        ("mật khẩu: 12345678", "mật khẩu: [đã ẩn: mật khẩu]"),
        ("OTP của anh 482913", "OTP của anh [đã ẩn: mã OTP]"),
        ("mã OTP là 482913, đừng nói ai", "mã OTP là [đã ẩn: mã OTP], đừng nói ai"),
        ("pin là 1234", "pin là [đã ẩn: mã PIN]"),
        ("stk 0123456789 Vietcombank", "stk [đã ẩn: số tài khoản] Vietcombank"),
        ("số tài khoản: 0123 456 789 ACB", "số tài khoản: [đã ẩn: số tài khoản] ACB"),
        ("cccd 079090001234", "cccd [đã ẩn: số giấy tờ tuỳ thân]"),
        ("hộ chiếu C1234567 hết hạn", "hộ chiếu C1234567 hết hạn"),  # 7 số: không phải
        ("hộ chiếu B12345678", "hộ chiếu [đã ẩn: số giấy tờ tuỳ thân]"),
        ("thẻ 4111 1111 1111 1111", "thẻ [đã ẩn: số thẻ]"),
        ("4111-1111-1111-1111 hết hạn 12/28", "[đã ẩn: số thẻ] hết hạn 12/28"),
        ("thẻ visa 4111111111111111", "thẻ visa [đã ẩn: số thẻ]"),
        ("key sk-proj-abcdefghijklmnopqrstu", "key [đã ẩn: API key]"),
        (
            "seed phrase: abandon ability able about above absent absorb abstract",
            "seed phrase: [đã ẩn: khoá bí mật]",
        ),
        # Ký tự vô hình không giấu được từ khoá.
        ("O​TP 482913", "OTP [đã ẩn: mã OTP]"),
    ],
)
def test_an_phan_gia_tri_bi_mat_giu_tu_khoa(text, expected):
    assert _mask(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "FPT giá 120.500, khối lượng 2.345.678 cp",
        "VNM KL 1234567 giá 65000, vốn hoá 123456789012345 đồng",
        "doanh thu 1234567890123 đồng",
        "số điện thoại 0912345678",
        "gọi anh số 0912 345 678 nhé",
        "chuyển khoản 5.000.000đ cho anh Nam",
        "số tài khoản có 1.500.000 đồng",
        "số tài khoản có 1500000 đồng",
        "mk thích cà phê muối",
        "anh quên mật khẩu rồi",
        "mật khẩu mạnh là phải có ít nhất 8 ký tự",
        "mật khẩu là gì vậy em",
        "mat khau la khong an toan",
        "mật khẩu - đổi định kỳ 3 tháng",
        "password manager là app quản lý mật khẩu",
        "pass lại xe - 15tr",
        "pass xe giá: 15000000",
        "pin: 5000mAh dùng được bao lâu",
        "pin là 5000 mAh",
        "OTP hết hạn sau 5 phút",
        "otp là mã dùng một lần",
        "private key là gì",
        "cmnd cấp năm 2010 tại Hà Nội",
        "HPG: hỗ trợ 25.500, kháng cự 28.000, stop 24.000",
    ],
)
def test_khong_an_nham_so_lieu_va_cau_hoi_thuong(text):
    assert _mask(text) == text
    assert memory_guard.prompt_secret_spans(text) == []


def test_danh_so_nhan_trung_gia_tri_va_khoi_phuc():
    redactor = prompt_redaction.Redactor(True)
    first = redactor.text("mật khẩu wifi là abc12345, mật khẩu email là Zz9988xx")
    again = redactor.text("nhắc lại: mật khẩu là abc12345")
    assert first == "mật khẩu wifi là [đã ẩn: mật khẩu], mật khẩu email là [đã ẩn: mật khẩu 2]"
    assert again == "nhắc lại: mật khẩu là [đã ẩn: mật khẩu]"
    assert redactor.labels == ("mật khẩu",)
    assert redactor.restore_obj({"a": ["[đã ẩn: mật khẩu 2]", 3], "b": "[đã ẩn: mật khẩu]"}) == {
        "a": ["Zz9988xx", 3],
        "b": "abc12345",
    }


def test_tat_bang_env_va_khong_co_bi_mat_thi_khong_them_dong_he_thong(monkeypatch):
    monkeypatch.setenv("PROMPT_REDACTION", "0")
    off = prompt_redaction.Redactor()
    assert off.text("OTP 482913") == "OTP 482913" and not off.redacted
    monkeypatch.delenv("PROMPT_REDACTION")
    clean = prompt_redaction.Redactor()
    assert clean.with_note(clean.text("chào em")) == "chào em" and clean.note() == ""


@pytest.mark.asyncio
async def test_chat_che_cau_hoi_lich_su_tri_nho_grounding_va_bao_model(monkeypatch):
    captured = {}

    async def fake_router9(prompt, **kwargs):
        captured.update(kwargs, prompt=prompt)
        return openai_compatible.Response("Dạ.")

    async def history(*args):
        return [("user", "stk 0123456789 Vietcombank"), ("model", "dạ em nhớ rồi")]

    async def preferred():
        return None

    monkeypatch.setattr(config, "load_chat_skill", lambda *a, **k: "PERSONA")
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(db, "get_session_messages", history)

    await orchestrator.chat(
        777,
        "OTP của anh 482913, FPT giá 120.500 thì sao",
        grounding="cccd 079090001234",
        memory_context="Mật khẩu wifi là abc12345",
    )

    prompt = captured["prompt"]
    for secret in ("482913", "079090001234", "abc12345"):
        assert secret not in prompt
    assert "[đã ẩn: mã OTP]" in prompt and "FPT giá 120.500" in prompt
    assert captured["history"][0] == ("user", "stk [đã ẩn: số tài khoản] Vietcombank")
    assert captured["system_instruction"].endswith(prompt_redaction.SYSTEM_NOTE)


@pytest.mark.asyncio
async def test_chat_khong_bi_mat_thi_prompt_nguyen_van(monkeypatch):
    captured = {}

    async def fake_router9(prompt, **kwargs):
        captured.update(kwargs, prompt=prompt)
        return openai_compatible.Response("Dạ.")

    async def history(*args):
        return []

    async def preferred():
        return None

    monkeypatch.setattr(config, "load_chat_skill", lambda *a, **k: "PERSONA")
    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)
    monkeypatch.setattr(db, "get_session_messages", history)

    await orchestrator.chat(777, "VNM KL 1234567 giá 65000, gọi 0912345678")
    assert captured["prompt"].endswith("VNM KL 1234567 giá 65000, gọi 0912345678")
    assert captured["system_instruction"] == "PERSONA"


@pytest.mark.asyncio
async def test_ask_va_anh_che_prompt_kem_dong_he_thong(monkeypatch):
    prompts = []

    async def fake_router9(prompt, **kwargs):
        prompts.append(prompt)
        return openai_compatible.Response("ok")

    async def fake_image(instruction, image_path):
        prompts.append(instruction)
        return openai_compatible.Response("ok")

    async def preferred():
        return None

    monkeypatch.setattr(orchestrator.router9_client, "generate", fake_router9)
    monkeypatch.setattr(orchestrator.router9_client, "generate_image_prompt", fake_image)
    monkeypatch.setattr(orchestrator.router9_client, "get_preferred_model_name", preferred)

    await orchestrator.ask("dịch giúp: mật khẩu là Abc@2024")
    await orchestrator.analyze_image("ảnh chụp thẻ 4111 1111 1111 1111 này là thẻ gì", "/x.jpg")
    assert prompts[0].startswith(prompt_redaction.SYSTEM_NOTE)
    assert "Abc@2024" not in prompts[0] and "[đã ẩn: mật khẩu]" in prompts[0]
    assert "4111" not in prompts[1] and "[đã ẩn: số thẻ]" in prompts[1]


@pytest.mark.asyncio
async def test_router_tool_khoi_phuc_gia_tri_goc_cho_ghi_chu(monkeypatch):
    """Provider chỉ thấy nhãn; ghi chú lưu trong DB của bot vẫn đúng mật khẩu."""
    seen = []
    saved = []

    async def fake_utility(prompt):
        seen.append(prompt)
        return {"tool": "save_note", "args": {"content": "mật khẩu wifi là [đã ẩn: mật khẩu]"}}

    async def fake_add_note(uid, content, **delivery):
        saved.append(content)

    async def no_history(user_id, turns=3):
        return ""

    monkeypatch.setattr(official_client, "generate_utility_json", fake_utility)
    monkeypatch.setattr(db, "add_note", fake_add_note)
    monkeypatch.setattr(tools, "_history_text", no_history)

    outcome = await tools.maybe_run_tool(1, "ghi chú giúp anh: mật khẩu wifi là hoa-sen-2026")

    assert "hoa-sen-2026" not in seen[0] and "[đã ẩn: mật khẩu]" in seen[0]
    assert saved == ["mật khẩu wifi là hoa-sen-2026"]
    assert outcome.status == reg.STATUS_OK


@pytest.mark.asyncio
async def test_agent_che_cau_hoi_lich_su_va_ket_qua_tool(monkeypatch):
    from dataclasses import replace

    from ai import groq_client
    from services import memory_service

    seen = {}
    saved = []
    calls = iter(
        [
            ToolCallResponse("", [{"id": "c1", "name": "list_notes", "arguments": {}}]),
            ToolCallResponse("Dạ.", []),
        ]
    )

    async def history(user_id, k, session_timeout_sec):
        return [("user", "OTP 482913"), ("model", "dạ")]

    async def memory(user_id, query=""):
        return memory_service.TurnMemory(context="stk 0123456789")

    async def add_turn(user_id, user_text, model_text, *, kind="chat"):
        saved.append(user_text)

    async def fake_groq(messages, tools, **kwargs):
        seen.setdefault("messages", []).append([dict(m) for m in messages])
        return next(calls)

    async def notes(user_id, **_):
        return "- mật khẩu wifi là abc12345"

    async def usable(name):
        return name == "groq"

    spec = agent_service._TOOLS["list_notes"]
    monkeypatch.setitem(agent_service._TOOLS, "list_notes", replace(spec, handler=notes))
    monkeypatch.setattr(db, "get_session_messages", history)
    monkeypatch.setattr(db, "add_chat_turn", add_turn)
    monkeypatch.setattr(memory_service, "build_turn_memory", memory)
    monkeypatch.setattr(agent_service, "_tool_provider_usable", usable)
    monkeypatch.setattr(groq_client, "generate_with_tools", fake_groq)

    await agent_service.ask_agent("mật khẩu wifi là Zz9988xx, ghi chú có gì", user_id=-7)

    first, second = seen["messages"]
    dumped = repr(second)
    for secret in ("482913", "0123456789", "Zz9988xx", "abc12345"):
        assert secret not in dumped
    assert first[0]["content"].endswith(prompt_redaction.SYSTEM_NOTE)
    assert second[-1]["role"] == "tool" and "[đã ẩn: mật khẩu" in second[-1]["content"]
    # Lịch sử bot vẫn lưu câu gốc.
    assert saved == ["/agent mật khẩu wifi là Zz9988xx, ghi chú có gì"]
