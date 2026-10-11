"""Helper dùng chung: core/text_utils.py và core/envutil.py."""

import pytest

from core.envutil import env_int, env_on
from core.text_utils import parse_on_off, strip_json_fence


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("on", True),
        (" BẬT ", True),
        ("bat", True),
        ("off", False),
        ("Tắt", False),
        ("tat", False),
        ("", None),
        ("auto", None),
        ("on off", None),
    ],
)
def test_parse_on_off(arg, expected):
    assert parse_on_off(arg) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  ```json\n{"a": 1}\n```  ', '{"a": 1}'),
        ('```JSON {"a": 1}```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        (None, ""),
    ],
)
def test_strip_json_fence(raw, expected):
    assert strip_json_fence(raw) == expected


def test_env_on_mac_dinh_bat_chi_tat_khi_bang_0(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert env_on("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", " 0 ")
    assert env_on("X_FLAG") is False
    monkeypatch.setenv("X_FLAG", "false")
    assert env_on("X_FLAG") is True


def test_env_int_kep_bien_va_mac_dinh_khi_loi(monkeypatch):
    monkeypatch.setenv("X_NUM", "999")
    assert env_int("X_NUM", 5, 1, 20) == 20
    monkeypatch.setenv("X_NUM", "abc")
    assert env_int("X_NUM", 5, 1, 20) == 5
    monkeypatch.delenv("X_NUM")
    assert env_int("X_NUM", 5, 1, 20) == 5
