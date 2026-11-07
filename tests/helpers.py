"""Test helpers: isolated homes, a fake detect result, and fake install methods."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["ENV_WIZARD_ROOT"] = str(ROOT)

from env_wizard import methods  # noqa: E402
from env_wizard.catalog import load_catalog  # noqa: E402
from env_wizard.methods.base import Check, Ctx, Method, Outcome  # noqa: E402
from env_wizard.state import global_state, project_state  # noqa: E402
from env_wizard.util import Runner  # noqa: E402


def fake_detect(os_="macos", can_admin=True, elevated=False, online=True, free_mb=100_000) -> Dict:
    return {"platform": {"os": os_, "os_version": "14.0", "arch": "arm64", "supported": True, "support_note": None},
            "admin": {"can_admin": can_admin, "elevated": elevated},
            "proxy": {"proxy": False, "custom_ca": False},
            "disk_free_mb": {"home": free_mb, "project": free_mb},
            "network": {"online": online, "reachable": {}}, "managers": {}}


class FakeMethod(Method):
    """Pretends to install. `world` is a dict id -> version of what's 'installed'."""

    def __init__(self, name, world, fail_ids=(), smoke_fail_ids=()):
        self.name = name
        self.world = world
        self.fail_ids = set(fail_ids)
        self.smoke_fail_ids = set(smoke_fail_ids)
        self.installed = []
        self.uninstalled = []

    @staticmethod
    def key(ctx, iid, scope):
        return f"{ctx.project}:{iid}" if scope == "project" else iid

    def check(self, ctx, step):
        v = self.world.get(self.key(ctx, step["id"], step.get("scope")))
        if v is None:
            return Check(False)
        if self.name == "uv-standalone":  # like the real method: any existing uv is fine
            return Check(True, True, v)
        want = step.get("version")
        from env_wizard.util import version_satisfies
        return Check(True, version_satisfies(v, want) if want else True, v)

    def install(self, ctx, step):
        if self.name in ("handoff-check", "check-only"):  # only the user can do these
            return Outcome("needs_admin", detail="needs password",
                           handoff={"why": "x", "steps": [{"label": "run", "command": "echo hi"}], "after": "done"})
        if step["id"] in self.fail_ids:
            return Outcome("failed", detail="boom", error_text="curl: (6) Could not resolve host: example.org")
        k = self.key(ctx, step["id"], step.get("scope"))
        self.world[k] = step.get("version") or "1.0"
        self.installed.append(step["id"])
        return Outcome("ok", version=self.world[k], paths_created=[f"/fake/{step['id']}"])

    def uninstall(self, ctx, rec):
        self.world.pop(self.key(ctx, rec["id"], rec.get("scope")), None)
        self.uninstalled.append(rec["id"])
        return Outcome("ok")

    def smoke(self, ctx, step):
        if step["id"] in self.smoke_fail_ids:
            return Outcome("failed", detail="test failed", error_text="ImportError")
        return Outcome("ok", detail="works")


class EnvTestCase(unittest.TestCase):
    """Each test gets its own env-wizard home, user home and project folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ewtest-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self._env = {k: os.environ.get(k) for k in ("ENV_WIZARD_HOME", "ENV_WIZARD_USER_HOME")}
        os.environ["ENV_WIZARD_HOME"] = str(self.tmp / "ewhome")
        os.environ["ENV_WIZARD_USER_HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()
        self._registry = dict(methods.REGISTRY)
        from env_wizard import runner as _runner
        self._emit, _runner.emit = _runner.emit, lambda obj: None

    def tearDown(self):
        methods.REGISTRY.clear()
        methods.REGISTRY.update(self._registry)
        from env_wizard import runner as _runner
        _runner.emit = self._emit
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_all(self, world=None, **kw) -> Dict[str, FakeMethod]:
        """Replace every install method with a fake sharing one 'world'."""
        world = {} if world is None else world
        fakes = {}
        for name in list(methods.REGISTRY):
            if name in ("cc-permissions", "cc-mcp", "claude-md", "template"):
                continue  # real file-based methods are safe in a temp project
            fakes[name] = FakeMethod(name, world, **kw)
            methods.REGISTRY[name] = fakes[name]
        self.world = world
        return fakes

    def ctx(self, plat="macos", project=None) -> Ctx:
        project = project or self.project
        return Ctx(plat=plat, project=project, runner=Runner(), catalog=load_catalog(),
                   gstate=global_state(), pstate=project_state(project))
