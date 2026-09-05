"""Lint tiếng Việt cho các file có nội dung xuất ra cho người dùng thật.

Dùng validator vendor từ skill `vietnamese-language-skill`
(tools/vn_lint/, xem tools/vn_lint/README-VENDORED.md) để bắt trước khi
merge:

- NFC001: text lỡ tay ở dạng NFD (bàn phím/macOS, copy-paste PDF).
- TONE001: lẫn lộn 2 kiểu dấu thanh trong cùng 1 file (vd "hoà" và "hòa").
- DIA001: tiếng Việt không dấu lọt vào nội dung hiển thị cho người dùng.
- FIN001/LAW001...: ngôn từ tài chính/quảng cáo bị luật cấm.

CHỈ chặn ở mức LỖI (errors), không chặn ở mức CẢNH BÁO (warnings) - đúng
triết lý gốc của validator: một cảnh báo như LAW001 bắt "duy nhất"/"thấp
nhất" thường là false positive trong văn cảnh code/hướng dẫn nội bộ (vd
"CHỈ xuất hiện MỘT LẦN DUY NHẤT" trong prompt kỹ thuật, không phải quảng
cáo sản phẩm) - cần người đọc quyết định, không nên fail CI vì nó.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "tools" / "vn_lint" / "scripts" / "validate_copy.py"

# File có nội dung tiếng Việt xuất trực tiếp cho người dùng (persona, prompt
# template, message string) - nơi một lỗi encoding/dấu thật sự tới tay
# người dùng. KHÔNG đưa cả repo vào đây: code Python thường (logic, test)
# không phải nội dung tiếng Việt cần lint văn phong.
VN_CONTENT_FILES = [
    "chat_skill.yaml",
    "messages.py",
    "templates/chat_skill_prompt.j2",
    "stock/templates/stock_analysis_prompt.j2",
    "handlers/commands.py",
    "services/morning_news.py",
    "services/translate_service.py",
    "services/portfolio_service.py",
    "channels/zalo_summary.py",
    "services/channel_chat_service.py",
]


def _run_validator(path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), (
        f"validate_copy.py lỗi invocation trên {path}: {proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("relpath", VN_CONTENT_FILES)
def test_no_vietnamese_lint_errors(relpath):
    path = ROOT / relpath
    assert path.exists(), f"File không tồn tại: {relpath} (danh sách test đã lỗi thời?)"
    report = _run_validator(path)
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    assert not errors, (
        f"{relpath} có {len(errors)} lỗi tiếng Việt: "
        + "; ".join(f"{e['rule']} dòng {e['line']}: {e['message']}" for e in errors)
    )
