"""Packaging, manifest contract, and UI security verification tests."""

import glob
import json
import os
import unittest
import zipfile

from openhop_meshhub_bot import __version__
from openhop_meshhub_bot.config import DEFAULT_CONFIG


class TestPackagingAndManifest(unittest.TestCase):
    def test_manifest_conforms_to_upstream_schema_1(self):
        with open("openhop-plugin.json", "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        with open("config.default.json", "r", encoding="utf-8") as fh:
            default_cfg = json.load(fh)

        # Upstream PluginManifest schema-1 allowed top-level keys only
        allowed_keys = {
            "schema",
            "id",
            "name",
            "version",
            "description",
            "runtime",
            "ui",
            "config",
        }
        self.assertEqual(set(manifest.keys()), allowed_keys)
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["id"], "openhop.meshhub-bot")
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(manifest["runtime"], {"type": "python", "entrypoint": "meshhub-bot"})
        self.assertEqual(manifest["ui"], {"type": "application", "entry": "ui/index.html"})

        # Verify config.defaults, config.default.json, and DEFAULT_CONFIG are identical
        self.assertEqual(manifest["config"]["defaults"], default_cfg)
        self.assertEqual(default_cfg, DEFAULT_CONFIG)

    def test_ui_has_csp_and_no_innerhtml(self):
        with open("ui/index.html", "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("Content-Security-Policy", html)

        with open("ui/app.js", "r", encoding="utf-8") as fh:
            js = fh.read()
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertIn("pymc_jwt_token", js)

    def test_built_wheel_has_single_manifest_and_single_ui_tree(self):
        wheels = sorted(glob.glob(f"dist/openhop_meshhub_bot-{__version__}-*.whl"))
        if not wheels:
            return
        whl_path = wheels[-1]
        with zipfile.ZipFile(whl_path) as zf:
            names = zf.namelist()
            manifests = [
                n for n in names if n.endswith("/openhop-plugin.json") or n == "openhop-plugin.json"
            ]
            ui_entries = [n for n in names if n.endswith("/ui/index.html") or n == "ui/index.html"]
            self.assertEqual(
                len(manifests),
                1,
                f"Expected exactly 1 openhop-plugin.json in wheel, found: {manifests}",
            )
            self.assertEqual(
                len(ui_entries),
                1,
                f"Expected exactly 1 ui/index.html in wheel, found: {ui_entries}",
            )


if __name__ == "__main__":
    unittest.main()
