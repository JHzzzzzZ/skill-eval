"""web/app.py 的上传安全边界（不启动 HTTP 服务）。"""
import importlib.util
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
    assert "ADR-" not in html
    assert "T2B" not in html


def test_valid_zip_finds_skill_root(tmp_path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("myskill/SKILL.md", "---\nname: s\ndescription: d\n---\nbody\n")
    dest = tmp_path / "out"
    web._safe_extract(z, dest)
    assert web._skill_root(dest) == dest / "myskill"
