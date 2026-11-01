"""web/app.py 的上传安全边界（不启动 HTTP 服务）。"""
import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WEB_APP = Path(__file__).parent.parent / "web" / "app.py"
spec = importlib.util.spec_from_file_location("web_app", WEB_APP)
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def test_zip_slip_is_rejected(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        web._safe_extract(z, tmp_path / "out")


def test_ui_has_no_internal_adr_or_tier_jargon():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "ADR" not in html
    assert "T2B" not in html
    assert 'id="stop"' in html
    assert 'id="reuse"' in html and 'id="rerunT0"' in html and 'id="rerunT1"' in html
    assert 'verdict-picker' in html and '选中调整' not in html


def test_stop_process_terminates():
    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    try:
        web._stop_process(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_copy_reuse_only_copies_t0_t1_artifacts(tmp_path):
    prev = tmp_path / "prev"
    (prev / "judges").mkdir(parents=True)
    (prev / "raw" / "llm-judge").mkdir(parents=True)
    (prev / "static.json").write_text("{}", encoding="utf-8")
    (prev / "run-meta.json").write_text("{}", encoding="utf-8")
    (prev / "judges" / "brevity.json").write_text("{}", encoding="utf-8")
    (prev / "raw" / "llm-judge" / "x.json").write_text("{}", encoding="utf-8")
    (prev / "report.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "dest"
    assert web._copy_reuse(prev, dest) is True
    assert (dest / "static.json").is_file()
    assert (dest / "judges" / "brevity.json").is_file()
    assert (dest / "raw" / "llm-judge" / "x.json").is_file()
    assert not (dest / "report.json").exists()


def test_valid_zip_finds_skill_root(tmp_path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("myskill/SKILL.md", "---\nname: s\ndescription: d\n---\nbody\n")
    dest = tmp_path / "out"
    web._safe_extract(z, dest)
    assert web._skill_root(dest) == dest / "myskill"
