"""Ghim bất biến build (đọc file, không chạy Docker/mạng): image biên dịch sẵn
bytecode của app, requirements.txt không còn gói runtime không dùng, code runtime
không import lại các gói đã gỡ (chúng chỉ còn trong requirements-dev.txt)."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Gỡ khỏi requirements.txt 11/2026 (di sản vnstock, không module runtime nào dùng).
REMOVED = {
    "pandas",
    "numpy",
    "matplotlib",
    "mplfinance",
    "seaborn",
    "openpyxl",
    "cycler",
    "pytz",
    "packaging",
    "importlib-metadata",
    "psutil",
}
# Tên module import tương ứng (khác tên gói ở PyPI).
_MODULES = {name.replace("-", "_") for name in REMOVED}
_RUNTIME_DIRS = ("ai", "channels", "core", "handlers", "services", "stock")


def _req_names(path: str) -> set[str]:
    names = set()
    for raw in (ROOT / path).read_text("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[=<>!~\[;]", line, maxsplit=1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_image_bien_dich_san_bytecode_app():
    text = (ROOT / "Dockerfile").read_text("utf-8")
    copy_app = text.index("COPY . .")
    compile_step = text.index("python -m compileall")
    assert compile_step > copy_app
    line = text[compile_step : text.index("\n", compile_step)]
    # Hash nội dung, không theo mtime; bỏ node_modules của gateway; biên dịch /app.
    assert "--invalidation-mode checked-hash" in line
    assert "-x '/zalo-gateway/'" in line and line.rstrip().endswith("/app")


def test_requirements_khong_con_goi_runtime_khong_dung():
    runtime = _req_names("requirements.txt")
    assert not REMOVED & runtime
    # Dependency bắc cầu của gói runtime vẫn ghim để build lặp lại được.
    assert {"tenacity", "cffi", "requests", "pydantic"} <= runtime
    assert {"numpy", "pandas"} <= _req_names("requirements-dev.txt")


def _runtime_files():
    for folder in _RUNTIME_DIRS:
        yield from (ROOT / folder).rglob("*.py")
    yield from ROOT.glob("*.py")


def test_code_runtime_khong_import_goi_da_go():
    """Kể cả import lười trong hàm: gói đã gỡ không có trong image Render."""
    offenders = []
    for path in _runtime_files():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in _MODULES:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert offenders == []
