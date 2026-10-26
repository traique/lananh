"""Chạy golden set (tier B, gọi provider AI thật) và ghi báo cáo JSON.

    RUN_LIVE_EVALS=1 GROQ_API_KEY=... python scripts/run_evals.py \\
        --max-calls 80 --only persona,router --judge --out eval-report.json

- Chỉ chạy khi đặt RUN_LIVE_EVALS=1 (chủ động) và có ít nhất 1 key provider;
  từ chối chạy trên Render (biến RENDER) - đánh giá chạy local hoặc GitHub Actions
  (workflow_dispatch .github/workflows/evals.yml) với key RIÊNG cho đánh giá.
- core.database được thay bằng bản giả trong RAM: không bao giờ chạm DATABASE_URL.
- --max-calls: trần tổng lượt gọi provider (kể cả giám khảo); case vượt trần bị
  bỏ qua và ghi trong báo cáo. Cache .eval-cache/results.json (case + persona
  không đổi, chưa quá --cache-ttl-hours) thì không gọi lại.
- Mã thoát 1 khi có case trượt bộ kiểm tất định (giám khảo chỉ tham khảo).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from evals import harness  # noqa: E402


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chạy golden set đánh giá Lan Anh (tier B).")
    parser.add_argument("--max-calls", type=int, default=int(os.getenv("EVAL_MAX_CALLS", "120")))
    parser.add_argument("--only", default="", help="category hoặc id, cách nhau dấu phẩy")
    parser.add_argument("--judge", action="store_true", help="bật giám khảo LLM (tham khảo)")
    parser.add_argument("--out", default="eval-report.json")
    parser.add_argument("--cache", default=str(ROOT / ".eval-cache" / "results.json"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--cache-ttl-hours", type=float, default=24.0)
    parser.add_argument(
        "--delay-sec", type=float, default=2.0, help="nghỉ giữa 2 case (tránh 429/phút)"
    )
    parser.add_argument("--no-fail", action="store_true", help="luôn thoát 0 (chỉ báo cáo)")
    return parser.parse_args(argv)


def _guard() -> str | None:
    if os.getenv("RUN_LIVE_EVALS") != "1":
        return "Đặt RUN_LIVE_EVALS=1 để chạy đánh giá gọi provider thật."
    if os.getenv("RENDER"):
        return "Không chạy đánh giá trên Render (512 MB RAM, dùng chung quota với bot)."
    if not harness.available_providers():
        return "Chưa có key provider nào (GROQ_API_KEY, OPENROUTER_API_KEY, GOOGLE_AI_STUDIO_*)."
    return None


async def _close_clients() -> None:
    from ai import groq_client, official_client, openrouter_client, router9_client

    for client in (groq_client, openrouter_client, router9_client, official_client):
        try:
            await client.close()
        except Exception:
            pass


def _print_summary(report: dict) -> None:
    summary = report["summary"]
    print(f"Golden set: {summary['passed']}/{summary['total']} đạt", end="")
    print(f", {report['provider_calls']} lượt gọi provider, bỏ qua {len(report['skipped'])}")
    for category, row in sorted(summary["by_category"].items()):
        print(f"  {category:<10} {row['passed']}/{row['total']}")
    for provider, row in sorted(summary["by_provider"].items()):
        print(f"  [{provider}] {row['passed']}/{row['total']}")
    if summary["judge_pass_rate"] is not None:
        print(f"  Giám khảo (tham khảo): {summary['judge_pass_rate']:.0%} đạt")
    for case in report["cases"]:
        if not case["passed"]:
            print(f"  ✗ {case['id']} ({case['provider']}/{case['model']}): {case['failures']}")


async def _run(args: argparse.Namespace, cases: list) -> dict:
    try:
        return await harness.run_suite(
            cases,
            max_calls=max(1, args.max_calls),
            judge=args.judge,
            cache_path=None if args.no_cache else Path(args.cache),
            cache_ttl_hours=args.cache_ttl_hours,
            delay_sec=max(0.0, args.delay_sec),
        )
    finally:
        await _close_clients()


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    reason = _guard()
    if reason:
        print(reason)
        return 2
    only = {item.strip() for item in args.only.split(",") if item.strip()} or None
    cases = harness.load_cases(only=only)
    if not cases:
        print("Không có case nào khớp --only.")
        return 1
    report = asyncio.run(_run(args, cases))
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_summary(report)
    return 0 if args.no_fail or report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
