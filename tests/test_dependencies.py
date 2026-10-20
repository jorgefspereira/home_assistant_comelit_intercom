"""Catch container-only dependency conflicts and failed-install removal errors."""

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

import homeassistant
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_test_environment_use_same_pinned_source():
    manifest = json.loads(
        (ROOT / "custom_components/comelit_intercom/manifest.json").read_text()
    )
    requirement = manifest["requirements"][0]
    assert requirement in (ROOT / "requirements-test.txt").read_text().splitlines()
    parsed = Requirement(requirement)
    assert parsed.name == "aiortc"
    assert parsed.url.endswith("/34b5fd3409220e1aeb8b62f2c56a9ae2085ca8c8.zip")
    source = json.loads(
        importlib.metadata.distribution("aiortc").read_text("direct_url.json")
    )
    assert source["url"] == parsed.url


def test_aiortc_supports_home_assistant_pinned_av():
    constraints = (
        Path(homeassistant.__file__)
        .with_name("package_constraints.txt")
        .read_text()
        .splitlines()
    )
    av_pin = next(Requirement(line) for line in constraints if line.startswith("av=="))
    installed_av = importlib.metadata.version("av")
    assert installed_av in av_pin.specifier
    requirements = [Requirement(item) for item in importlib.metadata.requires("aiortc")]
    supported_av = next(item for item in requirements if item.name == "av")
    assert installed_av in supported_av.specifier


def test_root_and_config_flow_import_without_aiortc():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class NoVideoDependency(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'aiortc' or fullname.startswith('aiortc.'):
            raise ModuleNotFoundError('aiortc intentionally unavailable')
sys.meta_path.insert(0, NoVideoDependency())
import custom_components.comelit_intercom
import custom_components.comelit_intercom.config_flow
assert 'custom_components.comelit_intercom.media' not in sys.modules
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
