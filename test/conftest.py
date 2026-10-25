"""Fixture dùng chung cho toàn bộ test.

- sys.path: thư mục gốc repo (pyproject đã có ``pythonpath = ["."]``; giữ thêm
  ở đây cho các runner không đọc pyproject).
- Chặn mạng thật: test đơn vị không được mở kết nối TCP ra ngoài (Tavily,
  DNSE, Telegram...). Test cần mạng/ DB thật tự bật qua marker ``network``
  hoặc env ``ALLOW_TEST_NETWORK=1`` (vd test PGlite dùng subprocess, không
  socket; test DNS giả lập ``socket.getaddrinfo`` nên không bị ảnh hưởng).
"""

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test được phép mở kết nối mạng thật")


class _NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_real_network(request, monkeypatch):
    if os.getenv("ALLOW_TEST_NETWORK") == "1" or request.node.get_closest_marker("network"):
        yield
        return

    def _blocked_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return _real_connect(self, address, *args, **kwargs)
        raise _NetworkBlocked(
            f"Test cố mở kết nối mạng thật tới {address!r}. Mock HTTP (httpx.MockTransport/"
            "ASGITransport) hoặc đánh dấu @pytest.mark.network."
        )

    _real_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    yield


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """core.settings_cache giữ giá trị settings trong RAM của process: xoá giữa
    các test để giá trị db.get_setting giả của test này không rò sang test khác."""
    from core import settings_cache

    settings_cache.clear()
    yield
    settings_cache.clear()
