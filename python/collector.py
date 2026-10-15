"""Private Windows capture adapter. Importing this module never opens WeChat.

capture_latest(state_path) returns a verified snapshot directory for the local
indexer. Diagnostics crossing the subprocess boundary contain fixed codes and
counts only. No dependency installation or network access is performed here.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_ROOT = PACKAGE_ROOT / "collector"
MAX_SECONDS = 600
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
SUCCESS_STATES = {"complete", "complete_with_warnings"}
ERROR_CODES = {
    "COLLECTOR_WINDOWS_REQUIRED", "COLLECTOR_DEPENDENCIES_REQUIRED",
    "COLLECTOR_WECHAT_REQUIRED", "COLLECTOR_ACCOUNT_REQUIRED",
    "COLLECTOR_MULTIPLE_ACCOUNTS", "COLLECTOR_ACCOUNT_MISMATCH",
    "COLLECTOR_CONFIG_INVALID", "COLLECTOR_PRIVATE_STATE_REQUIRED",
    "COLLECTOR_BUSY", "COLLECTOR_TIMEOUT", "COLLECTOR_FAILED",
}
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
            "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "USERNAME",
            "USER", "LANG", "LC_ALL"}


class CollectorError(RuntimeError):
    def __init__(self, code="COLLECTOR_FAILED"):
        self.code = code if code in ERROR_CODES else "COLLECTOR_FAILED"
        super().__init__(self.code)


def _state(path):
    result = Path(path).expanduser().resolve()
    if result == PACKAGE_ROOT or PACKAGE_ROOT in result.parents:
        raise CollectorError("COLLECTOR_PRIVATE_STATE_REQUIRED")
    result.mkdir(parents=True, exist_ok=True, mode=0o700)
    return result


def _child(state, name):
    # Resolve the root here as well as the child. Callers legitimately pass the
    # raw CLI state path, and on macOS (/var -> /private/var) or on Windows
    # runners (8.3 short names) the resolved child is not relative to an
    # unresolved root. Comparing resolved-to-resolved keeps the escape guard
    # intact without rejecting a perfectly private state directory.
    root = Path(state).expanduser().resolve()
    result = (root / name).resolve()
    if result == root or root not in result.parents:
        raise CollectorError("COLLECTOR_PRIVATE_STATE_REQUIRED")
    return result


def _json(path):
    if not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        raise CollectorError("COLLECTOR_FAILED")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise CollectorError("COLLECTOR_FAILED")
    return value


def _config(state):
    path = _child(state, "collector-config.json")
    if not path.exists():
        return {"account_dir": "", "data_root": ""}
    try:
        config = _json(path)
        if set(config) - {"account_dir", "data_root"}:
            raise ValueError()
        result = {}
        for name in ("account_dir", "data_root"):
            value = config.get(name, "")
            if not isinstance(value, str) or len(value) > 32768:
                raise ValueError()
            if value:
                target = Path(os.path.expandvars(value)).expanduser()
                value = str((state / target).resolve() if not target.is_absolute() else target.resolve())
            result[name] = value
        return result
    except Exception:
        raise CollectorError("COLLECTOR_CONFIG_INVALID") from None


def _token(account):
    return hashlib.sha256(str(Path(account).resolve()).casefold().encode("utf-8")).hexdigest()


def _environment():
    result = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
    result.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return result


def _load(name):
    filename = COLLECTOR_ROOT / (name + ".py")
    spec = importlib.util.spec_from_file_location("_dsh_wechat_" + name, filename)
    if spec is None or spec.loader is None:
        raise CollectorError("COLLECTOR_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _selection(value):
    """Stage one has no selection: reject legacy scopes before any discovery."""
    if value is None or type(value) is dict and not value:
        return None
    raise CollectorError("COLLECTOR_CONFIG_INVALID")


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise CollectorError("COLLECTOR_TIMEOUT")
    return seconds


def _interpreter(state, deadline):
    bootstrap = _load("bootstrap")
    candidates = [state / "collector-runtime" / "Scripts" / "python.exe", Path(sys.executable)]
    candidates.extend(bootstrap.candidates())
    seen = set()
    for candidate in candidates:
        identity = str(candidate).casefold()
        if identity in seen or not candidate.is_file():
            continue
        seen.add(identity)
        try:
            result = subprocess.run(
                [str(candidate), "-X", "utf8", "-B", "-c",
                 "import sys,struct,cryptography,zstandard;sys.exit(0 if sys.version_info>=(3,10) and struct.calcsize('P')==8 else 1)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_environment(), timeout=min(15, _remaining(deadline)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    _remaining(deadline)
    raise CollectorError("COLLECTOR_DEPENDENCIES_REQUIRED")


@contextmanager
def _lock(state):
    import msvcrt
    path = _child(state, ".collector.lock")
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                raise CollectorError("COLLECTOR_BUSY") from None
            yield
        finally:
            if acquired:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _invoke(python, state, check, deadline, account_token=None, selection=None):
    selection = _selection(selection)
    request = {"account_token": account_token}
    payload = json.dumps(request, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise CollectorError("COLLECTOR_CONFIG_INVALID")
    try:
        command = (
            [str(python), "-X", "utf8", "-B", str(Path(__file__).resolve()),
             "--worker", "--state", str(state), "--check" if check else "--capture"]
        )
        with _worker_job() as attach:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8", errors="strict", env=_environment(), cwd=COLLECTOR_ROOT,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as child:
                try:
                    attach(child)
                    stdout, _stderr = child.communicate(payload,
                                                        timeout=_remaining(deadline))
                except BaseException:
                    attach.close()
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)
                    raise
                result = subprocess.CompletedProcess(command, child.returncode, stdout, "")
    except subprocess.TimeoutExpired:
        raise CollectorError("COLLECTOR_TIMEOUT") from None
    except OSError:
        raise CollectorError("COLLECTOR_FAILED") from None
    try:
        if len(result.stdout) > 4096:
            raise ValueError()
        answer = json.loads(result.stdout)
        if not isinstance(answer, dict) or answer.get("ok") is not True or result.returncode not in (0, 2):
            code = answer.get("error", {}).get("code") if isinstance(answer, dict) else None
            raise CollectorError(code)
        return answer["data"]
    except CollectorError:
        raise
    except Exception:
        raise CollectorError("COLLECTOR_FAILED") from None


@contextmanager
def _worker_job():
    """A Job owns only our child worker; bridge cancellation closes the handle.

    The worker's own PowerShell discovery child joins the Job automatically.
    The running WeChat process is never assigned to or modified by this Job.
    """
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel.SetHandleInformation.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise CollectorError("COLLECTOR_FAILED")
    def close():
        nonlocal handle
        if handle:
            kernel.CloseHandle(handle)
            handle = None
    try:
        if not kernel.SetHandleInformation(handle, 1, 0):  # HANDLE_FLAG_INHERIT off
            raise CollectorError("COLLECTOR_FAILED")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise CollectorError("COLLECTOR_FAILED")
        def attach(child):
            if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(child._handle))):
                raise CollectorError("COLLECTOR_FAILED")
        attach.close = close
        yield attach
    finally:
        close()


def _latest(state, expected_token=None, run_id=None, selection=None):
    _selection(selection)
    root = _child(state, "capture-output")
    pointer = _json(root / "latest.json")
    if pointer.get("status") not in SUCCESS_STATES or not isinstance(pointer.get("account_dir"), str):
        raise CollectorError("COLLECTOR_FAILED")
    if expected_token is not None and _token(pointer["account_dir"]) != expected_token:
        raise CollectorError("COLLECTOR_ACCOUNT_MISMATCH")
    if run_id is not None and pointer.get("run_id") != run_id:
        raise CollectorError("COLLECTOR_FAILED")
    snapshot = Path(pointer["run_dir"]).resolve(strict=True)
    if root not in snapshot.parents or not snapshot.is_dir():
        raise CollectorError("COLLECTOR_FAILED")
    capture = _json(_child(snapshot, "capture_report.json"))
    summary = _json(_child(snapshot, "export/export_summary.json"))
    run = _json(_child(snapshot, "run_report.json"))
    verified = capture.get("verified_databases")
    if (capture.get("status") != "verified" or capture.get("keys_persisted") is not False
            or type(verified) is not int or verified < 1 or verified != capture.get("total_databases")
            or summary.get("status") not in SUCCESS_STATES or run.get("status") not in SUCCESS_STATES
            or type(summary.get("messages")) is not int or summary["messages"] < 0
            or summary["messages"] != pointer.get("messages")
            or summary.get("selection") not in (None, {})
            or summary.get("filtered_out_rows", 0) != 0
            or summary.get("source_total_rows", summary["messages"]) != summary["messages"]
            or run.get("run_id") != pointer.get("run_id")):
        raise CollectorError("COLLECTOR_FAILED")
    choices = [_child(snapshot, "export/messages.jsonl"), _child(snapshot, "export/messages.jsonl.gz")]
    if sum(path.is_file() for path in choices) != 1:
        raise CollectorError("COLLECTOR_FAILED")
    return snapshot


def _worker(state, check, expected_token=None, selection=None):
    if sys.platform != "win32":
        raise CollectorError("COLLECTOR_WINDOWS_REQUIRED")
    selection = _selection(selection)
    private = _config(state)
    daily = _load("daily_export")
    config = dict(daily.DEFAULTS, **private, output_root=str(_child(state, "capture-output")),
                  open_after_export=False)
    config["selection"] = None
    try:
        dependencies = daily.dependencies()
    except Exception:
        raise CollectorError("COLLECTOR_DEPENDENCIES_REQUIRED") from None
    try:
        accounts = daily.discover_accounts(config)
    except Exception:
        raise CollectorError("COLLECTOR_ACCOUNT_REQUIRED") from None
    if not accounts:
        raise CollectorError("COLLECTOR_ACCOUNT_REQUIRED")
    if len(accounts) != 1:
        raise CollectorError("COLLECTOR_MULTIPLE_ACCOUNTS")
    account = accounts[0]
    token = _token(account)
    if expected_token is not None and token != expected_token:
        raise CollectorError("COLLECTOR_ACCOUNT_MISMATCH")
    output = _child(state, "capture-output")
    prior = output / "latest.json"
    if prior.exists():
        prior_account = _json(prior).get("account_dir")
        if not isinstance(prior_account, str) or _token(prior_account) != token:
            raise CollectorError("COLLECTOR_ACCOUNT_MISMATCH")
    try:
        processes = daily.discover_processes()
    except Exception:
        raise CollectorError("COLLECTOR_WECHAT_REQUIRED") from None
    if not processes:
        raise CollectorError("COLLECTOR_WECHAT_REQUIRED")
    daily.validate_output(output, account)
    if check:
        return {"ready": True, "account_token": token}, 0
    try:
        with daily.lock_output(output):
            code = daily.run_once(config, account, processes, dependencies, output, False)
    except daily.AlreadyRunning:
        raise CollectorError("COLLECTOR_BUSY") from None
    if code not in (0, 2):
        raise CollectorError("COLLECTOR_FAILED")
    _latest(state, token, selection=selection)
    pointer = _json(output / "latest.json")
    return {"captured": True, "run_id": pointer["run_id"], "with_warnings": code == 2}, code


def capture_latest(state_path, selection=None):
    """Capture one local account in at most 600 seconds; return its private snapshot.

    Optional local-only settings: state/collector-config.json with account_dir
    and data_root strings. This function never changes those settings. It keeps
    all outputs, including interrupted/failed snapshots, and never deletes them.
    Stage one always exports all records. Legacy nonempty selections are errors.
    """
    try:
        if sys.platform != "win32":
            raise CollectorError("COLLECTOR_WINDOWS_REQUIRED")
        selection = _selection(selection)
        state = _state(state_path)
        deadline = time.monotonic() + MAX_SECONDS
        with _lock(state):
            python = _interpreter(state, deadline)
            preflight = _invoke(python, state, True, deadline)
            token = preflight.get("account_token")
            if not isinstance(token, str) or len(token) != 64:
                raise CollectorError("COLLECTOR_FAILED")
            result = _invoke(python, state, False, deadline, token)
            _remaining(deadline)
            return _latest(state, token, result.get("run_id"), selection=selection)
    except CollectorError:
        raise
    except Exception:
        raise CollectorError("COLLECTOR_FAILED") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CollectorError("COLLECTOR_CONFIG_INVALID")


def main(argv=None):
    code = 1
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("--worker", action="store_true", required=True)
        parser.add_argument("--state", required=True)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--check", action="store_true")
        mode.add_argument("--capture", action="store_true")
        args = parser.parse_args(argv)
        if sys.platform != "win32":
            raise CollectorError("COLLECTOR_WINDOWS_REQUIRED")
        state = _state(args.state)
        request_text = sys.stdin.read(MAX_REQUEST_BYTES + 1)
        if len(request_text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise CollectorError("COLLECTOR_CONFIG_INVALID")
        request = json.loads(request_text or "{}")
        if not isinstance(request, dict) or set(request) - {"account_token", "selection"}:
            raise CollectorError("COLLECTOR_CONFIG_INVALID")
        selection = _selection(request.get("selection"))
        sys.path.insert(0, str(COLLECTOR_ROOT))
        logs = _child(state, "capture-output/logs")
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = "adapter-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".log"
        with (logs / name).open("x", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            data, code = _worker(state, args.check, request.get("account_token"), selection=selection)
        answer = {"ok": True, "data": data}
    except CollectorError as exc:
        answer = {"ok": False, "error": {"code": exc.code}}
    except BaseException:
        answer = {"ok": False, "error": {"code": "COLLECTOR_FAILED"}}
    sys.stdout.write(json.dumps(answer, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
