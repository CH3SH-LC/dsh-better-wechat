"""Public onboarding surfaces tested using synthetic state only."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagnostics = load("wechat_diagnostics_test", "python/diagnostics.py")
setup = load("wechat_diagnostic_setup", "setup.py")
viewer = load("wechat_latest_test", "collector/open_latest.py")
demo = load("wechat_demo_test", "examples/demo.py")


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dsh-onboarding-synthetic-"))

    def test_diagnostics_do_not_create_state_or_reveal_paths(self):
        state = self.root / "absent"
        value = diagnostics.diagnose(state)
        self.assertTrue(value["read_only"])
        self.assertFalse(value["wechat_accessed"])
        self.assertFalse(state.exists())
        self.assertNotIn(str(self.root), json.dumps(value))

    def test_diagnostics_only_return_fixed_metadata(self):
        (self.root / "source-config.json").write_text(json.dumps({"mode": "native", "source": "SYNTHETIC_SECRET"}))
        (self.root / "plugin-runtime.json").write_text(json.dumps({"version": "0.0.1", "host_pid": 1234, "secret": "SYNTHETIC_SECRET"}))
        value = diagnostics.diagnose(self.root)
        self.assertEqual(value["source_mode"], "native")
        self.assertIn("PLUGIN_RELOAD_REQUIRED", value["checks"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(value))
        self.assertNotIn("host_pid", value)

    def test_corrupt_metadata_has_bounded_diagnostic(self):
        (self.root / "source-config.json").write_text("SYNTHETIC_PRIVATE_CANARY")
        value = diagnostics.diagnose(self.root)
        self.assertIn("PRIVATE_METADATA_UNREADABLE", value["checks"])
        self.assertNotIn("SYNTHETIC_PRIVATE_CANARY", json.dumps(value))

    def test_doctor_cli_cannot_capture_or_mutate_default_state(self):
        output = io.StringIO()
        state = self.root / "state"
        with patch.object(setup, "engine") as engine, patch.object(setup, "install_dependencies") as install, contextlib.redirect_stdout(output):
            code = setup.main(["--doctor", "--state", str(state), "--json"])
        self.assertEqual(code, 0)
        engine.assert_not_called()
        install.assert_not_called()
        self.assertFalse(state.exists())
        self.assertTrue(json.loads(output.getvalue())["data"]["read_only"])

    def test_doctor_rejects_combination_with_writes_and_selectors(self):
        for flags in [["--refresh"], ["--review"], ["--kind", "friend"], ["--install-collector-dependencies"]]:
            with patch.object(setup, "engine") as engine, contextlib.redirect_stdout(io.StringIO()):
                code = setup.main(["--doctor", "--json", *flags])
            self.assertEqual(code, 1)
            engine.assert_not_called()

    def test_open_latest_refuses_escape_and_failed_snapshots(self):
        run = self.root / "synthetic-run"
        (run / "export").mkdir(parents=True)
        (run / "export/index.html").write_text("<p>Synthetic text</p>")
        pointer = self.root / "latest.json"
        pointer.write_text(json.dumps({"run_dir": str(run), "status": "complete"}))
        self.assertEqual(viewer.latest_page(self.root), run / "export/index.html")
        pointer.write_text(json.dumps({"run_dir": str(self.root.parent), "status": "complete"}))
        with self.assertRaises(ValueError):
            viewer.latest_page(self.root)
        pointer.write_text(json.dumps({"run_dir": str(run), "status": "failed"}))
        with self.assertRaises(ValueError):
            viewer.latest_page(self.root)

    def test_latest_mode_does_not_require_capture_dependencies(self):
        bootstrap = load("wechat_bootstrap_test", "collector/bootstrap.py")
        with patch.object(bootstrap, "choose_python", side_effect=AssertionError("must not probe capture")), patch.object(bootstrap.subprocess, "call", return_value=0) as call:
            self.assertEqual(bootstrap.main(["latest", "--output", str(self.root)]), 0)
        self.assertIn("open_latest.py", call.call_args.args[0][3])

    def test_synthetic_demo_is_separate_and_respects_selection(self):
        result = demo.create_demo(self.root)
        self.assertEqual(result["messages_generated"], 3)
        self.assertEqual(result["selected_messages"], 2)
        self.assertEqual({r["kind"] for r in result["result"]["messages"]}, {"friend", "group"})
        self.assertFalse(result["wechat_accessed"])
        self.assertFalse(result["default_state_changed"])


if __name__ == "__main__":
    unittest.main()
