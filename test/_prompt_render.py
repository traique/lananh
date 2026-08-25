"""Đọc string constant từ file .py bằng ast thay vì import module.

handlers/commands.py và handlers/media_handler.py import httpx/telegram ở
module level - không phải môi trường test nào cũng cài các dependency đó.
Dùng chung ở test_prompt_templates.py, test_text_prompt_modes.py và
test_photo_prompt_modes.py.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_string_constant(filename: str, constant_name: str) -> str:
    module = ast.parse((ROOT / filename).read_text())
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == constant_name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing constant: {constant_name}")
