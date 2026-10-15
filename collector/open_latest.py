"""Open only the generated offline HTML for the most recent complete snapshot."""
import argparse
import json
import os
from pathlib import Path
import sys
import webbrowser


def latest_page(root):
    root = Path(root).expanduser().resolve(strict=True)
    pointer = root / "latest.json"
    if pointer.is_symlink() or pointer.stat().st_size > 65536:
        raise ValueError()
    with pointer.open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError()
    data = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError()
    if data.get("status") not in {"complete", "complete_with_warnings"}:
        raise ValueError()
    run = Path(data["run_dir"])
    run = (root / run).resolve(strict=True) if not run.is_absolute() else run.resolve(strict=True)
    if not run.is_relative_to(root) or run == root:
        raise ValueError()
    page = (run / "export" / "index.html").resolve(strict=True)
    if not page.is_relative_to(run) or not page.is_file():
        raise ValueError()
    return page


def default_output_root():
    """Private data root, keeping a directory an earlier project name filled."""
    parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    for name in ("dsh-better-wechat", "dsh-wechat-assistant"):
        candidate = parent / name / "capture-output"
        if candidate.is_dir():
            return candidate
    return parent / "dsh-better-wechat" / "capture-output"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the latest complete local HTML export; no capture.")
    parser.add_argument("--output", type=Path, default=default_output_root())
    args = parser.parse_args(argv)
    try:
        page = latest_page(args.output)
        if not webbrowser.open(page.as_uri()):
            raise ValueError()
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("No readable complete export is available. Run the fixed collection step first.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
