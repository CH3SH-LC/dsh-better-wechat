"""One offline acceptance entry. Never collects real WeChat data or starts a model."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", help="Optional local DSH installation for the isolated runtime probe")
    args = parser.parse_args(argv)
    node = shutil.which("node")
    if not node:
        print(json.dumps({"ok": False, "code": "NODE_REQUIRED"}))
        return 1
    checks = [
        ("javascript", [node, "--test", *[str(p) for p in sorted((ROOT / "tests").glob("*.test.mjs"))]]),
        ("python", [sys.executable, "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests/python"]),
        ("synthetic_demo", [sys.executable, "-X", "utf8", "-B", "examples/demo.py"]),
        ("source_privacy", [sys.executable, "-X", "utf8", "-B", "tests/privacy_release_check.py", "."]),
    ]
    if args.installation:
        checks.append(("installed_runtime", [node, "tools/verify-installed-runtime.mjs", "--installation", args.installation]))
    results = []
    for name, command in checks:
        try:
            result = subprocess.run(command, cwd=ROOT, timeout=180, capture_output=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        # Do not put stack traces, temp paths or whole test output into a
        # copyable report. Failures can be investigated with the individual command.
        results.append({"check": name, "ok": ok})
        print(json.dumps(results[-1]), flush=True)
    passed = all(item["ok"] for item in results)
    print(json.dumps({"ok": passed, "checks": results, "real_capture": False, "model_started": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
