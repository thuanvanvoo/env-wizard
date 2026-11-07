import json
import unittest

from helpers import EnvTestCase, fake_detect

from env_wizard import health, resolver, runner, undo
from env_wizard.methods.claude import MD_START
from env_wizard.state import global_state, project_state


def plan_for(tc, det=None, **req):
    ctx = tc.ctx(plat=(det or fake_detect())["platform"]["os"])
    plan = resolver.build_plan(ctx.catalog, det or fake_detect(), ctx, resolver.Request(**req))
    return plan, ctx


def ids(plan, action=None):
    return [s["id"] for s in plan["steps"] if action is None or s["action"] == action]


class ResolverTests(EnvTestCase):
    def test_data_profile_order_and_deps(self):
        self.fake_all()
        plan, _ = plan_for(self, profiles=["data"])
        order = ids(plan)
        self.assertEqual(order[:4], ["uv", "python", "python-env", "python-packages"])
        self.assertEqual(order[-1], "claude-md")
        pkgs = next(s for s in plan["steps"] if s["id"] == "python-packages")["packages"]
        self.assertEqual({p["name"] for p in pkgs}, {"pandas", "openpyxl", "matplotlib", "jupyterlab"})
        env = next(s for s in plan["steps"] if s["id"] == "python-env")
        self.assertEqual(env["python_version"], "3.13")

    def test_existing_tools_are_reused(self):
        self.fake_all({"uv": "0.9.0", "python": "3.13.1"})
        plan, _ = plan_for(self, profiles=["data"])
        self.assertIn("uv", ids(plan, "reuse"))
        self.assertIn("python", ids(plan, "reuse"))
        self.assertEqual(plan_resolved_mb(plan, "uv"), 0)

    def test_conflicting_version_installs_alongside(self):
        self.fake_all({"uv": "0.9.0", "python": "3.9.6"})
        plan, _ = plan_for(self, items=[("python", "3.12")])
        py = next(s for s in plan["steps"] if s["id"] == "python")
        self.assertEqual((py["action"], py["version"], py["alongside"]), ("install", "3.12", "3.9.6"))

    def test_off_catalog_and_bad_versions_declined(self):
        self.fake_all()
        plan, _ = plan_for(self, items=[("photoshop", None), ("python", "2.7")],
                           packages={"python": ["numpy"], "npm": []})
        reasons = {d["reason"] for d in plan["declined"]}
        self.assertEqual(reasons, {"not_in_catalog", "not_on_allow_list", "version_not_allowed"})

    def test_helpers_pruned_when_runtime_already_present(self):
        self.fake_all({"node": "24.1.0", "git": "2.47.0"})
        plan, _ = plan_for(self, profiles=["web"])
        self.assertNotIn("fnm", ids(plan))
        self.assertNotIn("homebrew", ids(plan))

    def test_homebrew_pulled_in_and_handed_off_on_mac(self):
        self.fake_all()
        plan, _ = plan_for(self, items=[("pandoc", None)])
        self.assertEqual(ids(plan)[:2], ["homebrew", "pandoc"])
        self.assertEqual(next(s for s in plan["steps"] if s["id"] == "homebrew")["action"], "handoff")

    def test_windows_admin_items_are_handoffs(self):
        self.fake_all({"winget": "1.9"})
        plan, _ = plan_for(self, det=fake_detect("windows", can_admin=True, elevated=False), items=[("git", None)])
        self.assertEqual(next(s for s in plan["steps"] if s["id"] == "git")["action"], "handoff")
        plan, _ = plan_for(self, det=fake_detect("windows", elevated=True), items=[("git", None)])
        self.assertEqual(next(s for s in plan["steps"] if s["id"] == "git")["action"], "install")

    def test_unsupported_platform_blocks_dependents(self):
        self.fake_all()
        plan, _ = plan_for(self, det=fake_detect("linux"), items=[("gh", None)])
        acts = {s["id"]: s["action"] for s in plan["steps"]}
        self.assertEqual(acts["git"], "unsupported")
        self.assertEqual(acts["gh"], "unsupported")

    def test_preflight_low_disk_and_offline(self):
        self.fake_all()
        plan, _ = plan_for(self, det=fake_detect(free_mb=50, online=False), profiles=["data"])
        kinds = {p["kind"] for p in plan["preflight"]["problems"]}
        self.assertEqual(kinds, {"low_disk", "offline"})

    def test_saved_plan_cannot_be_tampered(self):
        self.fake_all()
        plan, _ = plan_for(self, profiles=["data"])
        path = resolver.save_plan(plan)
        self.assertEqual(resolver.load_plan(plan["plan_id"])["plan_id"], plan["plan_id"])
        data = json.loads(path.read_text())
        data["steps"][0]["params"] = {"evil": True}
        path.write_text(json.dumps(data))
        with self.assertRaises(resolver.PlanError):
            resolver.load_plan(plan["plan_id"])


def plan_resolved_mb(plan, iid):
    return next(s for s in plan["steps"] if s["id"] == iid)["size_mb"]


class RunnerTests(EnvTestCase):
    def apply(self, plan, ctx, **kw):
        return runner.apply_plan(plan, ctx, check_network=False, **kw)

    def test_full_run_records_state_and_files(self):
        self.fake_all()
        plan, ctx = plan_for(self, profiles=["data"])
        out = self.apply(plan, ctx)
        self.assertEqual(out["status"], "done", out)
        g, p = global_state(), project_state(self.project)
        self.assertTrue(g.get("python@3.13")["installed_by_plugin"])
        self.assertIn(str(self.project), g.get("python@3.13")["used_by"])
        self.assertIn("python-packages", p.items)
        self.assertIn(MD_START, (self.project / "CLAUDE.md").read_text())
        settings = json.loads((self.project / ".claude" / "settings.json").read_text())
        self.assertIn("Bash(uv run:*)", settings["permissions"]["allow"])

    def test_failure_continues_and_blocks_dependents_then_resume(self):
        fakes = self.fake_all(fail_ids={"python"})
        plan, ctx = plan_for(self, profiles=["data"])
        out = self.apply(plan, ctx)
        r = out["results"]
        self.assertEqual(out["status"], "partial")
        self.assertEqual(r["python"]["status"], "failed")
        self.assertEqual(r["python"]["diagnosis"]["kind"], "network")
        self.assertEqual(r["python-env"]["status"], "blocked")
        self.assertEqual(r["python-packages"]["status"], "blocked")
        self.assertEqual(r["uv"]["status"], "ok")
        self.assertEqual(r["cc-permissions:python"]["status"], "ok")  # independent work still happened
        # the network comes back: resume finishes the rest without redoing uv
        for f in fakes.values():
            f.fail_ids.clear()
        installed_before = list(fakes["uv-standalone"].installed)
        out2 = self.apply(plan, self.ctx(), resume=True)
        self.assertEqual(out2["status"], "done")
        self.assertEqual(out2["run_id"], out["run_id"])
        self.assertEqual(fakes["uv-standalone"].installed, installed_before)

    def test_smoke_test_failure_is_reported(self):
        self.fake_all(smoke_fail_ids={"python-packages"})
        plan, ctx = plan_for(self, profiles=["data"])
        out = self.apply(plan, ctx)
        self.assertEqual(out["results"]["python-packages"]["status"], "test_failed")
        self.assertIn("python-packages", out["failed"])

    def test_handoff_waits_then_continues(self):
        self.fake_all()
        plan, ctx = plan_for(self, items=[("pandoc", None)])
        out = self.apply(plan, ctx)
        self.assertEqual(out["results"]["homebrew"]["status"], "needs_admin")
        self.assertTrue(out["results"]["homebrew"]["handoff"]["steps"])
        self.assertEqual(out["results"]["pandoc"]["status"], "blocked")
        self.world["homebrew"] = "4.4"  # the user ran the command themselves
        out2 = self.apply(plan, self.ctx(), resume=True)
        self.assertEqual(out2["status"], "done")
        rec = global_state().get("homebrew")
        self.assertFalse(rec["installed_by_plugin"])
        self.assertTrue(rec["via_handoff"])

    def test_preflight_stops_before_changes(self):
        self.fake_all()
        plan, ctx = plan_for(self, profiles=["data"])
        plan["totals"]["download_mb"] = 10 ** 9
        out = self.apply(plan, ctx)
        self.assertEqual(out["status"], "stopped")
        self.assertFalse(global_state().items)


class UndoTests(EnvTestCase):
    def test_undo_keeps_preexisting_and_shared(self):
        self.fake_all({"uv": "0.9.0"})
        other = self.tmp / "other"
        other.mkdir()
        plan, ctx = plan_for(self, profiles=["data"])
        runner.apply_plan(plan, ctx, check_network=False)
        # a second project also relies on the same Python
        ctx2 = self.ctx(project=other)
        plan2 = resolver.build_plan(ctx2.catalog, fake_detect(), ctx2,
                                    resolver.Request(packages={"python": ["requests"], "npm": []}))
        runner.apply_plan(plan2, ctx2, check_network=False)

        ctx = self.ctx()
        up = undo.plan_undo(ctx, "all")
        removed = {r["id"] for r in up["remove"]}
        kept = {r["id"]: r["reason"] for r in up["keep"]}
        self.assertIn("python-packages", removed)
        self.assertIn("claude-md", removed)
        self.assertIn("uv", kept)          # was already there
        self.assertIn("python@3.13", kept)  # still used by the other project
        out = undo.execute(ctx, up)
        self.assertTrue(all(r["status"] == "removed" for r in out["results"]))
        self.assertFalse((self.project / "CLAUDE.md").exists())
        self.assertFalse((self.project / ".claude" / "settings.json").exists())
        self.assertFalse((self.project / ".env-wizard").exists())
        self.assertEqual(self.world.get("uv"), "0.9.0")
        self.assertIn("python", self.world)

    def test_undo_preserves_user_content(self):
        self.fake_all()
        (self.project / "CLAUDE.md").write_text("# My notes\nKeep me.\n")
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "x"}))
        plan, ctx = plan_for(self, profiles=["data"])
        runner.apply_plan(plan, ctx, check_network=False)
        ctx = self.ctx()
        undo.execute(ctx, undo.plan_undo(ctx, "all"))
        self.assertEqual((self.project / "CLAUDE.md").read_text(), "# My notes\nKeep me.\n")
        settings = json.loads((self.project / ".claude" / "settings.json").read_text())
        self.assertEqual(settings, {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "x"})


class HealthTests(EnvTestCase):
    def test_silent_when_healthy_one_line_when_broken(self):
        self.fake_all()
        plan, ctx = plan_for(self, profiles=["data"])
        runner.apply_plan(plan, ctx, check_network=False)
        res = health.healthcheck(self.ctx(), use_cache=False)
        self.assertTrue(res["ok"])
        self.assertIsNone(health.hook_output(res))
        self.world.pop(f"{self.project}:python-env")  # someone deleted .venv
        res = health.healthcheck(self.ctx(), use_cache=False)
        self.assertFalse(res["ok"])
        out = health.hook_output(res)
        self.assertIn("/env-wizard repair", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_unapplied_manifest_is_mentioned(self):
        self.fake_all()
        (self.project / "env-wizard.yaml").write_text("version: 1\nitems: [uv]\n")
        res = health.healthcheck(self.ctx(), use_cache=False)
        self.assertEqual(res["kind"], "manifest_not_applied")


class DiagnoseTests(unittest.TestCase):
    def test_patterns(self):
        cases = {
            "SSL: CERTIFICATE_VERIFY_FAILED certificate verify failed": "proxy",
            "Could not resolve host: github.com": "network",
            "OSError: [Errno 28] No space left on device": "disk",
            "Permission denied (publickey)": "permission",
            "npm error code EROFS": "permission",
            "No matching distribution found for pandas==99": "not_found",
            "weird": "unknown",
        }
        for text, kind in cases.items():
            self.assertEqual(runner.diagnose(text)["kind"], kind, text)


if __name__ == "__main__":
    unittest.main()
