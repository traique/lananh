"""Ghim các bất biến vận hành/CI (đọc file cấu hình, không chạy Docker/mạng):
deploy chỉ khi CI xanh, health check nhẹ, smoke test image, kho vnstocks.com chỉ
dùng cho đúng 3 package --no-deps, base image ghim distro."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text("utf-8"))


def _req_names(path: str) -> dict[str, str]:
    names = {}
    for raw in (ROOT / path).read_text("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, _, version = line.partition("==")
        names[re.sub(r"\[.*\]", "", name).strip().lower().replace("_", "-")] = version.strip()
    return names


def test_render_chi_deploy_khi_ci_xanh_va_health_check_van_la_root():
    service = _yaml("render.yaml")["services"][0]
    assert service["autoDeployTrigger"] == "checksPass"
    assert "autoDeploy" not in service  # trường cũ đã deprecated
    assert service["healthCheckPath"] == "/"  # quyết định của chủ bot: không đổi sang /healthz
    assert service["plan"] == "free" and service["numInstances"] == 1


def test_quet_cve_khong_nam_trong_ci_push():
    """checksPass chờ MỌI check của commit: audit (đỏ khi có CVE mới bất kể commit)
    phải ở workflow riêng, không chạy khi push lên main."""
    ci = _yaml(".github/workflows/ci.yml")
    assert "dependency-audit" not in ci["jobs"]
    runs = [step.get("run", "") for job in ci["jobs"].values() for step in job["steps"]]
    assert not any("pip-audit" in run or "npm audit" in run for run in runs)
    audit = _yaml(".github/workflows/audit.yml")
    triggers = audit[True]  # PyYAML đọc khoá "on" thành True
    assert "push" not in triggers
    assert {"pull_request", "schedule"} <= set(triggers)
    assert audit["permissions"] == {"contents": "read"}


def test_docker_build_chay_smoke_test_trong_image():
    job = _yaml(".github/workflows/ci.yml")["jobs"]["docker-build"]
    build = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/build-push"))
    assert build["with"]["load"] is True and build["with"]["push"] is False
    tag = build["with"]["tags"]
    smoke = next(s for s in job["steps"] if s.get("name") == "Smoke test image")["run"]
    assert tag in smoke
    for needle in ("import web, bot_app, scheduler", "pip check", "node --version", "zca-js"):
        assert needle in smoke


def test_kho_vnstocks_chi_dung_cho_3_package_no_deps():
    vendor = "vnstocks.com/api/simple"
    for path in (".github/workflows/ci.yml", ".github/workflows/audit.yml", "Dockerfile"):
        text = (ROOT / path).read_text("utf-8")
        assert "--extra-index-url" not in text, path
        assert vendor not in text, path
    main = _req_names("requirements.txt")
    pinned = _req_names("requirements-vnstock.txt")
    assert set(pinned) == {"vnstock", "vnai", "vnstock-ezchart"}
    assert all(pinned.values()), "phải ghim == từng package của kho riêng"
    assert not set(pinned) & set(main)
    vendor_lines = (ROOT / "requirements-vnstock.txt").read_text("utf-8")
    assert f"--index-url https://{vendor}" in vendor_lines
    assert vendor not in (ROOT / "requirements.txt").read_text("utf-8")
    # Docker + mọi job CI cài file kho riêng với --no-deps rồi pip check.
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert "pip install --no-deps -r requirements-vnstock.txt" in dockerfile
    assert "pip check" in dockerfile
    ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    assert ci.count("pip install --no-deps -r requirements-vnstock.txt") == ci.count(
        "pip install -r requirements-dev.txt"
    )


def test_dependency_that_cua_vnstock_duoc_liet_ke_tren_pypi():
    """vnstock/vnai/vnstock_ezchart cài --no-deps: thiếu dependency nào trong
    requirements.txt thì image hỏng lúc import (pip check bắt ở build)."""
    main = _req_names("requirements.txt")
    needed = {
        # vnstock 4.0.9
        "requests", "beautifulsoup4", "pandas", "seaborn", "openpyxl", "pydantic",
        "psutil", "packaging", "importlib-metadata", "tenacity", "pytz",
        # vnai 2.6.2
        "cffi",
        # vnstock_ezchart 1.0.2
        "numpy", "matplotlib", "cycler", "mplfinance",
    }  # fmt: skip
    assert needed <= set(main), needed - set(main)
    major, minor = (int(x) for x in main["numpy"].split(".")[:2])
    assert (major, minor) < (2, 3)  # vnstock_ezchart: numpy<2.3


def test_base_image_ghim_distro():
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text("utf-8"), re.M)
    assert froms, "Dockerfile không có FROM"
    for image in froms:
        assert re.search(r"-(bookworm|trixie)(-slim)?(@sha256:[0-9a-f]{64})?$", image), image


def test_dependabot_co_cooldown_va_theo_doi_docker():
    updates = _yaml(".github/dependabot.yml")["updates"]
    ecosystems = {u["package-ecosystem"]: u for u in updates}
    assert {"pip", "npm", "github-actions", "docker"} <= set(ecosystems)
    for update in updates:
        assert update["cooldown"]["default-days"] >= 7, update["package-ecosystem"]
    ignored = {i["dependency-name"] for i in ecosystems["pip"]["ignore"]}
    assert {"vnstock", "vnai", "vnstock_ezchart"} <= ignored


def test_workflow_khong_xin_quyen_ghi():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        data = yaml.safe_load(path.read_text("utf-8"))
        assert data.get("permissions") == {"contents": "read"}, path.name
