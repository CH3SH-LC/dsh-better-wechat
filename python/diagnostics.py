"""Bounded, read-only environment diagnostics. No Store, process scan or network."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import platform
import re
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value) else None


def read_object(path):
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise ValueError("UNSAFE_DIAGNOSTIC_FILE")
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("DIAGNOSTIC_LIMIT")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("INVALID_DIAGNOSTIC")
    return result


def diagnose(state):
    state = Path(state).expanduser().resolve()
    if state == ROOT or ROOT in state.parents:
        raise ValueError("UNSAFE_STATE")
    dependencies = {}
    for name in ("cryptography", "zstandard"):
        try:
            dependencies[name] = version(importlib.metadata.version(name)) or "installed"
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "missing"
    checks = []
    source_mode = "unconfigured"
    runtime_version = None
    try:
        if (state / "source-config.json").exists():
            mode = read_object(state / "source-config.json").get("mode")
            source_mode = mode if mode in {"native", "snapshot", "latest_complete"} else "invalid"
        if (state / "plugin-runtime.json").exists():
            runtime_version = version(read_object(state / "plugin-runtime.json").get("version"))
    except (OSError, ValueError, UnicodeError):
        checks.append("PRIVATE_METADATA_UNREADABLE")
    package_version = version(read_object(ROOT / "package.json").get("version"))
    if runtime_version and runtime_version != package_version:
        checks.append("PLUGIN_RELOAD_REQUIRED")
    if sys.platform != "win32":
        checks.append("NATIVE_CAPTURE_WINDOWS_ONLY")
    if struct.calcsize("P") != 8:
        checks.append("NATIVE_CAPTURE_REQUIRES_64_BIT")
    if "missing" in dependencies.values():
        checks.append("CURRENT_PYTHON_MISSING_CAPTURE_DEPENDENCIES")
    if source_mode == "unconfigured":
        checks.append("SOURCE_NOT_CONFIGURED")
    if source_mode == "invalid":
        checks.append("SOURCE_CONFIGURATION_INVALID")
    return {
        "package_version": package_version, "last_loaded_plugin_version": runtime_version,
        "python_version": platform.python_version(), "python_bits": struct.calcsize("P") * 8,
        "platform": {"win32": "windows", "darwin": "macos"}.get(sys.platform, "other"),
        "state_present": state.is_dir(), "index_present": (state / "index.sqlite3").is_file(),
        "source_mode": source_mode, "capture_dependencies_current_python": dependencies,
        "private_capture_runtime_present": (state / "collector-runtime" / "Scripts" / "python.exe").is_file(),
        "checks": checks, "read_only": True, "wechat_accessed": False,
        "paths_included": False, "message_content_included": False,
    }
