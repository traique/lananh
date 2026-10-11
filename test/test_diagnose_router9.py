import io
import sys
from types import SimpleNamespace

import diagnose_router9
import web


async def test_main_writes_to_given_stream_not_stdout(monkeypatch, capsys):
    monkeypatch.setattr(diagnose_router9.config, "ROUTER9_API_KEY", "key")

    async def list_models():
        return ["model-a"]

    async def boom(prompt):
        raise RuntimeError("router down")

    monkeypatch.setattr(diagnose_router9.router9_client, "list_models", list_models)
    monkeypatch.setattr(diagnose_router9.orchestrator, "ask", boom)
    out = io.StringIO()
    await diagnose_router9.main(out)
    text = out.getvalue()
    assert "ROUTER9_API_KEY  : OK" in text
    assert " - model-a" in text
    assert "RuntimeError: router down" in text  # traceback cũng vào stream
    assert "DONE" in text
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


async def test_main_defaults_to_stdout_for_cli(monkeypatch, capsys):
    monkeypatch.setattr(diagnose_router9.config, "ROUTER9_API_KEY", None)
    await diagnose_router9.main()
    assert "Không có API key 9Router." in capsys.readouterr().out


async def test_diagnose_endpoint_does_not_redirect_process_stdout(monkeypatch):
    monkeypatch.setattr(web.config, "DIAGNOSE_SECRET", "s3cret")
    original_stdout = sys.stdout

    async def fake_main(out):
        # Trong lúc chờ mạng, stdout của process phải giữ nguyên.
        assert sys.stdout is original_stdout
        print("hello from diagnose", file=out)
        raise ValueError("oops")

    monkeypatch.setattr(web, "diagnose_main", fake_main)
    request = SimpleNamespace(headers={"X-Diagnose-Token": "s3cret"})
    response = await web.diagnose(request)
    body = response.body.decode()
    assert "hello from diagnose" in body
    assert "Lỗi ngoài dự kiến: ValueError: oops" in body


async def test_diagnose_endpoint_rejects_bad_token(monkeypatch):
    monkeypatch.setattr(web.config, "DIAGNOSE_SECRET", "s3cret")
    for token in ("wrong", "", "sécret"):
        response = await web.diagnose(SimpleNamespace(headers={"X-Diagnose-Token": token}))
        assert response.status_code == 403
