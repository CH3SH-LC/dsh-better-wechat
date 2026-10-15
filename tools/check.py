"""One offline acceptance entry. Never collects real WeChat data or starts a model."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def failure_tail(result, limit=200):
    """Bounded, redacted tail of a failed check so CI output stays diagnosable.

    The full output is never published: only the last lines, with the repository
    root replaced, and only when a check fails. Tests are synthetic by
    construction, so no chat content or private path can appear here.
    """
    text = (result.stdout or b"").decode("utf-8", "replace") + "\n" + (result.stderr or b"").decode("utf-8", "replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return [line.replace(str(ROOT), "<repo>") for line in lines[-limit:]]


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
        tail = []
        try:
            result = subprocess.run(command, cwd=ROOT, timeout=180, capture_output=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = result.returncode == 0
            if not ok:
                tail = failure_tail(result)
        except OSError:
            ok = False
            tail = ["<SPAWN_FAILED>"]
        except subprocess.TimeoutExpired:
            ok = False
            tail = ["<TIMEOUT>"]
        # Never publish whole test output or machine paths: a failing check gets a
        # short redacted tail, successes stay a single line.
        entry = {"check": name, "ok": ok}
        if tail:
            entry["tail"] = tail
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=True), flush=True)
    passed = all(item["ok"] for item in results)
    print(json.dumps({"ok": passed, "checks": results, "real_capture": False, "model_started": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
