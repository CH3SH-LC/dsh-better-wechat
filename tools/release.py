"""Deterministic source ZIP: inspect, scan, stage, verify, then publish locally."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class ReleaseError(Exception):
    pass


def linked(path):
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def collect(root):
    root = Path(root).resolve(strict=True)
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    version = package.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ReleaseError("INVALID_VERSION")
    entries = ["package.json", *package.get("files", [])]
    if not package.get("files") or len(entries) != len({str(n).casefold() for n in entries}):
        raise ReleaseError("INVALID_MANIFEST")
    contents = {}
    for name in sorted(entries):
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or PurePosixPath(name).is_absolute() or any(c in name for c in "*?[")):
            raise ReleaseError("INVALID_MANIFEST")
        path = root / name
        for node in (path, *path.parents):
            if node == root:
                break
            if not node.exists() or linked(node):
                raise ReleaseError("MISSING_OR_LINKED_SOURCE")
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ReleaseError("INVALID_SOURCE_ENTRY")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ReleaseError("SOURCE_LIMIT")
        contents[name] = path.read_bytes()
    if sum(map(len, contents.values())) > 64 * 1024 * 1024:
        raise ReleaseError("SOURCE_LIMIT")
    return version, contents


def scanner():
    spec = importlib.util.spec_from_file_location("wechat_release_scanner", ROOT / "tests/privacy_release_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.check_release


def build(root=ROOT):
    root = Path(root).resolve(strict=True)
    version, contents = collect(root)
    check = scanner()
    report = check(root)
    if not report["ok"]:
        raise ReleaseError("SOURCE_PRIVACY_CHECK_FAILED")
    dist = root / "dist"
    if dist.exists() and (linked(dist) or not dist.is_dir()):
        raise ReleaseError("UNSAFE_DIST")
    dist.mkdir(exist_ok=True)
    filename = f"dsh-better-wechat-{version}.zip"
    manifest_name = f"dsh-better-wechat-{version}.manifest.json"
    for name in (filename, manifest_name, "SHA256SUMS", "manifest.json"):
        path = dist / name
        if path.exists() and (linked(path) or not path.is_file()):
            raise ReleaseError("UNSAFE_OUTPUT")
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", suffix=".tmp", dir=dist)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        # Stored entries avoid compressor-version differences. Source is small;
        # byte-identical archives are preferable to marginal size savings.
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in contents.items():
                info = zipfile.ZipInfo("dsh-better-wechat/" + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != len(contents):
                raise ReleaseError("ARCHIVE_VERIFICATION_FAILED")
            for name, content in contents.items():
                if archive.read("dsh-better-wechat/" + name) != content:
                    raise ReleaseError("ARCHIVE_VERIFICATION_FAILED")
                if (root / name).read_bytes() != content:
                    raise ReleaseError("SOURCE_CHANGED_DURING_BUILD")
        report = check(temporary, root / "package.json")
        if not report["ok"]:
            raise ReleaseError("ARCHIVE_PRIVACY_CHECK_FAILED")
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        manifest = {"archive": filename, "version": version, "sha256": digest,
                    "file_count": len(contents), "round_trip_verified": True, "privacy_findings": 0,
                    "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in contents.items()]}
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        manifest_digest = hashlib.sha256(payload).hexdigest()
        # There is no final archive before both privacy checks have passed.
        os.replace(temporary, dist / filename)
        for name, data in [(manifest_name, payload), ("manifest.json", payload),
                           ("SHA256SUMS", f"{digest}  {filename}\n{manifest_digest}  {manifest_name}\n".encode())]:
            fd, staged = tempfile.mkstemp(prefix=".release-", suffix=".tmp", dir=dist)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            try:
                os.replace(staged, dist / name)
            finally:
                Path(staged).unlink(missing_ok=True)
        return {k: v for k, v in manifest.items() if k != "files"}
    finally:
        # A single file created by mkstemp; never recursively delete a directory.
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        print(json.dumps(build(), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"ok": False, "code": str(error) if isinstance(error, ReleaseError) else "RELEASE_BUILD_FAILED"}))
        raise SystemExit(1)
