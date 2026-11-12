"""scripts/safe_push.py: the YAML check may not silently switch itself off."""
import builtins
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sp():
    spec = importlib.util.spec_from_file_location(
        "safe_push_under_test", ROOT / "scripts" / "safe_push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_yaml(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.delitem(sys.modules, "yaml", raising=False)


def test_missing_pyyaml_is_an_error_in_ci(sp, no_yaml, monkeypatch, tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n")
    monkeypatch.delenv("SAFE_PUSH_REQUIRE_YAML", raising=False)
    monkeypatch.setenv("CI", "true")
    assert "PyYAML is not installed" in sp.yaml_check(f)[0]
    monkeypatch.delenv("CI")
    assert sp.yaml_check(f) == []                    # local: still optional
    monkeypatch.setenv("SAFE_PUSH_REQUIRE_YAML", "1")
    assert sp.yaml_check(f)


def test_yaml_is_actually_parsed_when_available(sp, tmp_path):
    pytest.importorskip("yaml")
    bad = tmp_path / "bad.yml"
    bad.write_text("a: [1, 2\nb: {")
    assert sp.yaml_check(bad)
    assert sp.yaml_check(ROOT / "render.yaml") == []
    assert sp.yaml_check(ROOT / ".github" / "workflows" / "ci.yml") == []


def test_dormant_push_to_main_workflow_is_gone():
    assert not (ROOT / ".github" / "workflows" / "apply-patch.yml").exists()


def test_deploy_job_is_serialised_and_checks_health():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "group: deploy-main" in text and "cancel-in-progress: false" in text
    assert "python-version-file: '.python-version'" in text
    assert "/healthz" in text and "/readyz" in text
    assert "RENDER_CRON_DEPLOY_HOOK_URL" in text
    assert "pyyaml" in text and "SAFE_PUSH_REQUIRE_YAML" in text
