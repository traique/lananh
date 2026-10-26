from services import memory_stats


def test_snapshot_has_core_fields_and_never_raises():
    snap = memory_stats.snapshot()
    assert set(snap) == {"python", "container", "processes", "heavy_modules_loaded"}
    assert snap["python"]["rss_mib"] is None or snap["python"]["rss_mib"] > 0
    assert any(p["name"] == "web (python)" for p in snap["processes"]) or not snap["processes"]


def test_unlimited_cgroup_value_is_reported_as_none():
    assert memory_stats._bytes_to_mib(str(1 << 62)) is None
    assert memory_stats._bytes_to_mib(str(512 * 1024 * 1024)) == 512.0
    assert memory_stats._bytes_to_mib("max") is None


def _cgroup(tmp_path, monkeypatch, **files):
    for name, content in files.items():
        path = tmp_path / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(memory_stats, "CGROUP_ROOT", tmp_path)


def test_working_set_tru_page_cache_cgroup_v2(tmp_path, monkeypatch):
    mib = 1024 * 1024
    _cgroup(
        tmp_path,
        monkeypatch,
        **{
            "memory.current": str(450 * mib),
            "memory.max": str(512 * mib),
            "memory.stat": f"anon {300 * mib}\nfile {150 * mib}\ninactive_file {130 * mib}\n",
        },
    )
    ws = memory_stats.working_set()
    assert ws == {"working_set_mib": 320.0, "limit_mib": 512.0, "percent": 62.5}


def test_working_set_cgroup_v1_va_khong_gioi_han(tmp_path, monkeypatch):
    mib = 1024 * 1024
    _cgroup(
        tmp_path,
        monkeypatch,
        **{
            "memory__memory.usage_in_bytes": str(200 * mib),
            "memory__memory.limit_in_bytes": str(1 << 62),
            "memory__memory.stat": f"total_inactive_file {50 * mib}\n",
        },
    )
    ws = memory_stats.working_set()
    assert ws == {"working_set_mib": 150.0, "limit_mib": None, "percent": None}


def test_memory_events_va_khi_khong_doc_duoc(tmp_path, monkeypatch):
    _cgroup(tmp_path, monkeypatch)
    assert memory_stats.memory_events() == {}
    assert memory_stats.working_set()["percent"] is None
    _cgroup(
        tmp_path,
        monkeypatch,
        **{"memory.events": "low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\noom_group_kill 0\n"},
    )
    events = memory_stats.memory_events()
    assert events["max"] == 12 and events["oom_kill"] == 1
