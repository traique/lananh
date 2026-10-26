"""Golden set - tier B (gọi provider AI THẬT). Luôn skip trong CI/pytest mặc định.

Chạy local (key RIÊNG cho đánh giá, không dùng chung key production nếu được):

    RUN_LIVE_EVALS=1 GROQ_API_KEY=... pytest -q -m live_llm test/test_evals_live.py

- core.database thay bằng bản giả trong RAM (evals/harness.fake_database): không
  bao giờ đọc/ghi DB production, cooldown 429 của lượt đánh giá không lọt sang bot.
- Bộ kiểm tất định là cổng chặn (assert); giám khảo LLM (EVAL_JUDGE=1) chỉ in ra.
- Trần tổng lượt gọi provider EVAL_MAX_CALLS (mặc định 120; OpenRouter :free chỉ
  50 lượt/ngày khi chưa nạp 10 USD) - vượt trần thì skip các case còn lại.
"""

import os

import pytest

from evals import harness

pytestmark = [pytest.mark.live_llm, pytest.mark.network]

CASES = harness.load_cases()
_calls = {"total": 0}


def _skip_reason() -> str | None:
    if os.getenv("RUN_LIVE_EVALS") != "1":
        return "đánh giá live chỉ chạy khi RUN_LIVE_EVALS=1"
    if os.getenv("RENDER"):
        return "không chạy đánh giá trên Render"
    if not harness.available_providers():
        return "chưa có key provider (GROQ_API_KEY / OPENROUTER_API_KEY / GOOGLE_AI_STUDIO_*)"
    return None


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_golden_case(case):
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    cap = int(os.getenv("EVAL_MAX_CALLS", "120"))
    if _calls["total"] >= cap:
        pytest.skip(f"đã chạm trần {cap} lượt gọi provider")
    with harness.fake_database() as fake:
        result = await harness.run_case(case, fake, judge=os.getenv("EVAL_JUDGE") == "1")
    _calls["total"] += result.calls
    if result.judge:
        print(f"[giám khảo {result.judge['model']}] {case.id}: {result.judge['verdict']}")
    assert result.passed, (
        f"{case.id} ({result.provider}/{result.model}): {result.failures} | "
        f"trả lời: {result.reply[:300]}"
    )
