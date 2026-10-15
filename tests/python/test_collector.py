"""Synthetic adapter checks. No WeChat discovery/capture; fixtures are retained."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("collector_contract", ROOT / "python" / "collector.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
WINDOWS = sys.platform == "win32"


class CollectorContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Never recursively delete fixtures, including after failures.
        cls.fixture = Path(tempfile.mkdtemp(prefix="dsh-collector-synthetic-"))

    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="state-", dir=self.fixture))
        self.account = self.state / "synthetic-account"
        self.account.mkdir()

    def rejects(self, code, function):
        with self.assertRaises(collector.CollectorError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def daily(self):
        return types.SimpleNamespace(
            DEFAULTS={}, dependencies=lambda: {},
            discover_accounts=lambda _config: [self.account],
            discover_processes=lambda: [{"pid": 1}],
            validate_output=lambda *_: None,
            lock_output=lambda _: contextlib.nullcontext(),
            run_once=lambda *_: 2,
            AlreadyRunning=type("AlreadyRunning", (Exception,), {}),
        )

    def snapshot(self):
        output = self.state / "capture-output"
        snapshot = output / "synthetic-run"
        (snapshot / "export").mkdir(parents=True)
        reports = {
            "capture_report.json": {"status": "verified", "keys_persisted": False,
                                    "verified_databases": 1, "total_databases": 1},
            "export/export_summary.json": {"status": "complete_with_warnings", "messages": 1},
            "run_report.json": {"status": "complete_with_warnings", "run_id": "synthetic-run"},
            "export/messages.jsonl": {},
        }
        for name, value in reports.items():
            (snapshot / name).write_text(json.dumps(value), encoding="utf-8")
        (output / "latest.json").write_text(json.dumps({
            "status": "complete_with_warnings", "account_dir": str(self.account),
            "run_dir": str(snapshot), "run_id": "synthetic-run", "messages": 1,
        }), encoding="utf-8")
        # The collector always reports a resolved snapshot path; expectations must
        # be resolved too, or macOS (/var -> /private/var) and Windows short-name
        # runners compare two spellings of the same directory.
        return snapshot.resolve()

    def test_unknown_private_config_field_fails_closed(self):
        self.assertEqual(collector._config(self.state), {"account_dir": "", "data_root": ""})
        (self.state / "collector-config.json").write_text('{"unexpected":"synthetic-private"}')
        self.rejects("COLLECTOR_CONFIG_INVALID", lambda: collector._config(self.state))

    def test_private_state_cannot_be_inside_plugin(self):
        self.rejects("COLLECTOR_PRIVATE_STATE_REQUIRED",
                     lambda: collector._state(ROOT / "must-not-create-private-state"))

    def test_child_environment_excludes_injected_python_path_and_credentials(self):
        with patch.dict(collector.os.environ, {"PYTHONPATH": "synthetic-private", "API_KEY": "synthetic-private"}):
            result = collector._environment()
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("API_KEY", result)

    @unittest.skipUnless(WINDOWS, "Windows capture lock")
    def test_second_capture_is_rejected(self):
        with collector._lock(self.state):
            self.rejects("COLLECTOR_BUSY", lambda: collector._lock(self.state).__enter__())

    def test_preflight_requires_one_account_and_a_running_process(self):
        daily = self.daily()
        with patch.object(collector.sys, "platform", "win32"), patch.object(collector, "_load", return_value=daily):
            daily.discover_accounts = lambda _: []
            self.rejects("COLLECTOR_ACCOUNT_REQUIRED", lambda: collector._worker(self.state, True))
            daily.discover_accounts = lambda _: [self.account, self.state / "synthetic-other"]
            self.rejects("COLLECTOR_MULTIPLE_ACCOUNTS", lambda: collector._worker(self.state, True))
            daily.discover_accounts = lambda _: [self.account]
            daily.discover_processes = lambda: []
            self.rejects("COLLECTOR_WECHAT_REQUIRED", lambda: collector._worker(self.state, True))
            daily.discover_processes = lambda: [{"pid": 1}]
            answer, code = collector._worker(self.state, True)
            self.assertEqual(code, 0)
            self.assertTrue(answer["ready"])
            self.rejects("COLLECTOR_ACCOUNT_MISMATCH", lambda: collector._worker(self.state, False, "0" * 64))

    def test_missing_dependencies_do_not_install_packages(self):
        with patch.object(collector, "_load", return_value=types.SimpleNamespace(candidates=lambda: [])), \
             patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.rejects("COLLECTOR_DEPENDENCIES_REQUIRED",
                         lambda: collector._interpreter(self.state, time.monotonic() + 30))
            for call in run.call_args_list:
                self.assertNotIn("pip", call.args[0])

    def test_warning_code_two_is_successful_only_with_verified_snapshot(self):
        snapshot = self.snapshot()
        with patch.object(collector.sys, "platform", "win32"), patch.object(collector, "_load", return_value=self.daily()):
            answer, code = collector._worker(self.state, False, collector._token(self.account))
        self.assertEqual(code, 2)
        self.assertTrue(answer["with_warnings"])
        self.assertEqual(collector._latest(self.state, collector._token(self.account), "synthetic-run"), snapshot)

    def test_capture_api_returns_only_verified_snapshot(self):
        snapshot = self.snapshot()
        answers = [{"ready": True, "account_token": collector._token(self.account)},
                   {"captured": True, "run_id": "synthetic-run"}]
        with patch.object(collector.sys, "platform", "win32"), \
             patch.object(collector, "_lock", return_value=contextlib.nullcontext()), \
             patch.object(collector, "_interpreter", return_value=Path(sys.executable)), \
             patch.object(collector, "_invoke", side_effect=answers):
            self.assertEqual(collector.capture_latest(self.state), snapshot)

    def test_stage_one_rejects_nonempty_selection_before_interpreter(self):
        scopes = [{"kinds": ["friend"]}, {"start_time": "2026-10-13T00:00:00+08:00"}, {"conversation_ids": ["synthetic"]}, {"exclude_conversation_ids": []}]
        with patch.object(collector.sys, "platform", "win32"), patch.object(collector, "_interpreter") as interpreter:
            for scope in scopes:
                self.rejects("COLLECTOR_CONFIG_INVALID", lambda: collector.capture_latest(self.state, selection=scope))
            interpreter.assert_not_called()

    def test_full_capture_rejects_legacy_scoped_receipt(self):
        snapshot = self.snapshot()
        summary = snapshot / "export/export_summary.json"
        value = json.loads(summary.read_text(encoding="utf-8"))
        value["selection"] = {"kinds": ["friend"]}
        summary.write_text(json.dumps(value), encoding="utf-8")
        self.rejects("COLLECTOR_FAILED", lambda: collector._latest(self.state))

    def test_full_capture_rejects_receipt_with_filtered_rows(self):
        snapshot = self.snapshot()
        summary = snapshot / "export/export_summary.json"
        for fields in ({"filtered_out_rows": 1}, {"source_total_rows": 2}):
            summary.write_text(json.dumps(dict(status="complete", messages=1, **fields)), encoding="utf-8")
            self.rejects("COLLECTOR_FAILED", lambda: collector._latest(self.state))

    def test_invalid_selection_rejected_before_interpreter_or_capture(self):
        with patch.object(collector.sys,"platform","win32"), patch.object(collector,"_interpreter") as interpreter:
            self.rejects("COLLECTOR_CONFIG_INVALID", lambda: collector.capture_latest(self.state, selection={"kinds":["text"]}))
            interpreter.assert_not_called()

    def test_worker_validates_selection_before_discovery(self):
        daily = self.daily()
        daily.discover_accounts = lambda _: self.fail("discovery must not run")
        real_load = collector._load
        load = lambda name: daily if name == "daily_export" else real_load(name)
        with patch.object(collector.sys,"platform","win32"), patch.object(collector,"_load",side_effect=load):
            self.rejects("COLLECTOR_CONFIG_INVALID", lambda: collector._worker(self.state, True, selection={"kinds":["text"]}))

    def test_worker_forces_full_export_without_selection_in_request_or_output(self):
        self.snapshot()
        daily = self.daily()
        daily.DEFAULTS = {"selection": {"kinds": ["group"]}}
        seen = []
        daily.run_once = lambda config,*_: seen.append(config["selection"]) or 2
        with patch.object(collector.sys,"platform","win32"), patch.object(collector,"_load",return_value=daily):
            result, _ = collector._worker(self.state, False)
        self.assertEqual(seen, [None])
        self.assertNotIn("selection", result)

    def test_worker_input_limit_counts_utf8_bytes_before_dispatch(self):
        stdout = io.StringIO()
        payload = json.dumps({"selection":{"conversation_ids":["界"*25000]}},ensure_ascii=False)
        with patch.object(collector.sys,"platform","win32"), \
             patch.object(collector,"_worker") as worker, \
             patch.object(collector.sys,"stdin",io.StringIO(payload)), contextlib.redirect_stdout(stdout):
            code = collector.main(["--worker","--state",str(self.state),"--check"])
        self.assertEqual(code,1)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"],"COLLECTOR_CONFIG_INVALID")
        worker.assert_not_called()

    def test_worker_rejects_legacy_scope_without_dispatch(self):
        payload = json.dumps({"selection": {"exclude_conversation_ids": ["synthetic-private"]}})
        stdout = io.StringIO()
        with patch.object(collector.sys,"platform","win32"), patch.object(collector,"_worker") as worker, patch.object(collector.sys,"stdin",io.StringIO(payload)), contextlib.redirect_stdout(stdout):
            code = collector.main(["--worker","--state",str(self.state),"--check"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "COLLECTOR_CONFIG_INVALID")
        self.assertNotIn("synthetic-private", stdout.getvalue())
        worker.assert_not_called()

    def test_partial_snapshot_is_rejected(self):
        snapshot = self.snapshot()
        (snapshot / "capture_report.json").write_text('{"status":"failed"}')
        self.rejects("COLLECTOR_FAILED", lambda: collector._latest(self.state))

    def test_latest_pointer_cannot_escape_capture_output(self):
        self.snapshot()
        pointer = self.state / "capture-output" / "latest.json"
        value = json.loads(pointer.read_text(encoding="utf-8"))
        value["run_dir"] = str(self.account)
        pointer.write_text(json.dumps(value), encoding="utf-8")
        self.rejects("COLLECTOR_FAILED", lambda: collector._latest(self.state))

    def test_worker_stdout_never_contains_raw_exception(self):
        stdout = io.StringIO()
        with patch.object(collector.sys, "platform", "win32"), \
             patch.object(collector, "_worker", side_effect=ValueError("synthetic-private-path")), \
             patch.object(collector.sys, "stdin", io.StringIO("{}")), contextlib.redirect_stdout(stdout):
            code = collector.main(["--worker", "--state", str(self.state), "--check"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": False, "error": {"code": "COLLECTOR_FAILED"}})

    @unittest.skipUnless(WINDOWS, "Windows process Jobs")
    def test_job_close_terminates_only_its_synthetic_worker(self):
        with collector._worker_job() as attach:
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            attach(child)
        child.wait(timeout=5)
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(WINDOWS, "Windows worker")
    def test_real_worker_invalid_config_fails_before_discovery(self):
        (self.state / "collector-config.json").write_text('{"unexpected":"synthetic-private"}')
        self.rejects("COLLECTOR_CONFIG_INVALID",
                     lambda: collector._invoke(Path(sys.executable), self.state, True, time.monotonic() + 20))

    def test_other_platforms_do_not_start_a_worker(self):
        with patch.object(collector.sys, "platform", "linux"), patch.object(collector, "_invoke") as invoke:
            self.rejects("COLLECTOR_WINDOWS_REQUIRED", lambda: collector.capture_latest(self.state))
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
