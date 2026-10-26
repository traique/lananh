"""Mọi biến môi trường mà code đọc phải có dòng trong .env.example (secret để
trống, biến tuỳ chọn có thể là dòng chú thích ``# TEN=``) - thêm biến mới mà quên
tài liệu thì test này đỏ.

Quét (không chạy code, không mạng):
- Python ngoài test/: ``os.getenv("X")``, ``os.environ.get("X")``,
  ``os.environ["X"]``, ``env.get("X")`` và các helper ``_env_int``/``env_on``...
  với tên viết thẳng hoặc hằng chuỗi cấp module; cộng tên biến trong
  ``db_maintenance.RETENTION_RULES`` và ``quota_burn.specs()``.
- Gateway Zalo (zalo-gateway/src/*.ts): ``process.env.X``, ``process.env["X"]``
  và helper trong config.ts (``number("X", ...)``...).
- Script shell chạy trong container (``${X:-...}``).

Bỏ qua biến do nền tảng tự đặt (PORT, RENDER, RENDER_*)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from services import db_maintenance, quota_burn

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".venv", ".claude", ".git", "node_modules", "test", "__pycache__", "dist"}
_NAME = re.compile(r"[A-Z][A-Z0-9_]+")
_ENV_HELPER = re.compile(r"^_?env(_[a-z]+)*$|^getenv$")
_PLATFORM = re.compile(r"^(PORT|RENDER(_[A-Z0-9_]+)?)$")
_EXAMPLE_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", re.MULTILINE)


def _python_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if not _SKIP_DIRS.intersection(path.relative_to(ROOT).parts[:-1])
    ]


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _is_env_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return bool(_ENV_HELPER.match(func.id))
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv" or _ENV_HELPER.match(func.attr):
        return True
    owner = func.value
    owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
    return func.attr == "get" and owner_name in {"environ", "env"}


def _python_env_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text("utf-8"))
        consts = _module_constants(tree)
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            arg: ast.expr | None = None
            if isinstance(node, ast.Call) and node.args and _is_env_call(node.func):
                arg = node.args[0]
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
            ):
                arg = node.slice
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                value = arg.value
            elif isinstance(arg, ast.Name) and arg.id in consts:
                value = consts[arg.id]
            else:
                continue
            if _NAME.fullmatch(value):
                names.setdefault(value, rel)
    for rule in db_maintenance.RETENTION_RULES:
        names.setdefault(rule.env, "services/db_maintenance.py")
    for spec in quota_burn.specs():
        if spec.limit_env:
            names.setdefault(spec.limit_env, "services/quota_burn.py")
    return names


_TS_PATTERNS = (
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)"),
    re.compile(r"process\.env\[\s*[\"']([A-Z][A-Z0-9_]+)[\"']\s*\]"),
)
# config.ts đọc env qua helper: required("X"), number("X", ...), flag("X", ...)...
_TS_CONFIG_HELPER = re.compile(r"\b[a-zA-Z]+\(\s*\"([A-Z][A-Z0-9_]+)\"")
_SHELL_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]+)")


def _gateway_and_shell_env_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for path in sorted((ROOT / "zalo-gateway" / "src").glob("*.ts")):
        text = path.read_text("utf-8")
        rel = path.relative_to(ROOT).as_posix()
        patterns = (*_TS_PATTERNS, _TS_CONFIG_HELPER) if path.name == "config.ts" else _TS_PATTERNS
        for pattern in patterns:
            for name in pattern.findall(text):
                names.setdefault(name, rel)
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        for name in _SHELL_VAR.findall(path.read_text("utf-8")):
            names.setdefault(name, path.relative_to(ROOT).as_posix())
    return names


def _documented() -> set[str]:
    return set(_EXAMPLE_LINE.findall((ROOT / ".env.example").read_text("utf-8")))


def test_bo_quet_tim_thay_bien_quen_thuoc():
    """Bảo đảm bộ quét không âm thầm rỗng (vd đổi tên helper env)."""
    python = _python_env_names()
    for name in ("TELEGRAM_TOKEN", "MAX_CONCURRENT_TURNS", "RETENTION_TURN_METRICS_DAYS"):
        assert name in python, name
    assert "FREE_TIER_RPD_OPENROUTER" in python
    gateway = _gateway_and_shell_env_names()
    for name in ("ZALO_BRIDGE_SECRET", "ZALO_DIRECT_CONCURRENCY", "ZALO_NODE_HEAP_MB"):
        assert name in gateway, name


def test_moi_bien_moi_truong_deu_co_trong_env_example():
    documented = _documented()
    found = {**_gateway_and_shell_env_names(), **_python_env_names()}
    missing = sorted(
        f"{name} ({where})"
        for name, where in found.items()
        if name not in documented and not _PLATFORM.match(name)
    )
    assert not missing, "Thêm vào .env.example (secret để trống): " + ", ".join(missing)
