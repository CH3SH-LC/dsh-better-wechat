"""Check only an explicitly declared release surface; never print matched text.

Usage: python tests/privacy_release_check.py RELEASE_DIR_OR_ZIP_OR_TGZ
       python tests/privacy_release_check.py release.tgz --manifest package.json

For directory checks package.json ``files`` is the release allowlist. Archive
checks also reject unlisted members. No other source tree or user files are read.
Synthetic values must use the explicit markers documented in SYNTHETIC_MARKERS;
test directories are not exempt. This is a release guard, not a proof that arbitrary
prose is anonymous. Authorized runtime messages require separate fidelity tests;
their deliberately returned original text is not a release-data leak.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from typing import Iterable
import zipfile


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024 * 1024
MAX_FILES = 2048
# Only these conspicuous literal placeholder tokens are exempt. A whole file,
# directory, generic "test" substring, or arbitrary user marker is never exempt.
SYNTHETIC_MARKERS = (
    "SYNTHETIC_USER", "SYNTHETIC_OS_USER", "OSUSER_CANARY",
    "SYNTHETIC_SECRET", "WXID_CANARY", "wxid_synthetic", "wxid_synthetic_fixture", "wxid_test_fixture",
    "gh_synthetic_fixture",
)
# Explicit namespaces for serialized documentation fixtures. This is an
# individual-field convention, not permission to exempt an entire tests tree.
SYNTHETIC_IDENTITY = re.compile(r"^(?:synthetic[-_][A-Za-z0-9_-]+|合成.{1,40}|示例.{1,40})$", re.I)
PRIVATE_DIRS = {
    ".git", ".codex", ".agents", ".venv", "venv", "__pycache__", ".pytest_cache",
    "node_modules", "private", ".private", "state", ".state", "user-data",
    "userdata", "logs", "cache", ".cache", "captures", "decrypted", "review-output",
    "data", ".data", "exports", "output", "outputs",
}
PRIVATE_NAMES = {
    ".env", "config.json", "settings.json", "credentials.json", "secrets.json",
    "tokens.json", "capture_report.json", "export_summary.json", "contacts.json",
    "conversations.json", "chat_input.json", "latest.json", "index_manifest.json",
    "privacy-key", "identity-map.json", "source.json",
}
PRIVATE_SUFFIXES = (
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
    ".jsonl", ".jsonl.gz", ".ndjson", ".log", ".pyc", ".pyo", ".pem", ".key",
    ".bak", ".dump", ".dmp",
)
RULES = {
    "ABSOLUTE_WINDOWS_PATH": re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])[^\r\n\"'<>|]*"),
    "UNC_PATH": re.compile(r"(?<![\\\w])\\\\[A-Za-z0-9_.-]+\\[^\s\"'<>|]+"),
    "FILE_URI": re.compile(r"(?i)file:/{1,3}(?=[A-Za-z0-9%._~\u0080-\uffff-])[^\s\"'<>]+"),
    "ABSOLUTE_USER_PATH": re.compile(r"(?<![\w:])/(?:Users|home|root|mnt/[a-z]|media)/[^\s\"'<>]+"),
    "WECHAT_IDENTIFIER": re.compile(r"(?i)(?<![A-Za-z0-9_])(?:wxid_[A-Za-z0-9_-]{3,}|gh_[A-Za-z0-9]{6,}|[0-9]{5,}@chatroom)(?![A-Za-z0-9_])"),
    "URL_SECRET_PARAMETER": re.compile(r"(?i)[?&](?:access_token|authkey|aeskey|pass_ticket|uin|key|ticket|token)=[A-Za-z0-9%_+/.=-]{5,}"),
    "XML_MEDIA_SECRET": re.compile(r"(?i)\b(?:aeskey|authkey|cdnmidimgurl|cdnthumburl)\s*=\s*[\"'][A-Za-z0-9+/=_-]{12,}[\"']"),
}


@dataclass(frozen=True, order=True)
class Finding:
    file: str
    rule: str
    line: int = 0


def _username_values() -> tuple[str, ...]:
    # Read environment values only. Never read profiles, print, persist, or bake
    # these values into fixtures. Single-character account names are checked in
    # exact quoted/path components to avoid flagging every variable named x.
    return tuple(sorted({value for key in ("USERNAME", "USER", "LOGNAME")
                         if (value := os.environ.get(key, "").strip())}, key=len, reverse=True))


def _synthetic(value: str) -> bool:
    return any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(marker)
                         + r"(?:_[A-Za-z0-9]+)*(?![A-Za-z0-9_])", value, re.I)
               for marker in SYNTHETIC_MARKERS)


def _safe_label(value: str, usernames: tuple[str, ...]) -> str:
    # File names can themselves contain private data. Reports retain only a
    # relative, sanitized label rather than echoing attacker-controlled names.
    value = value.replace("\\", "/")
    for username in usernames:
        value = re.sub(re.escape(username), "[USER]", value, flags=re.I)
    for pattern in RULES.values():
        value = pattern.sub("[REDACTED]", value)
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", "_", value)
    return value[:240]


def _safe_member(name: str) -> str | None:
    if "\\" in name or "\x00" in name or ":" in name or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/")):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts):
        return None
    return path.as_posix().rstrip("/")


def _read_manifest(data: bytes) -> list[str]:
    document = json.loads(data.decode("utf-8-sig"))
    values = document.get("files")
    if not isinstance(values, list) or not values:
        raise ValueError("MANIFEST_ALLOWLIST_REQUIRED")
    allowlist = ["package.json"]
    for value in values:
        if not isinstance(value, str) or not value or value in {".", "*", "**", "**/*", "/"}:
            raise ValueError("MANIFEST_ALLOWLIST_INVALID")
        normalized = _safe_member(value.rstrip("/"))
        if normalized is None or value.startswith("!"):
            raise ValueError("MANIFEST_ALLOWLIST_INVALID")
        allowlist.append(normalized)
    return allowlist


def _allowed(name: str, allowlist: list[str]) -> bool:
    for allowed in allowlist:
        if name == allowed:
            return True
        if not any(char in allowed for char in "*?[") and name.startswith(allowed + "/"):
            return True
        if any(char in allowed for char in "*?[") and fnmatch.fnmatchcase(name, allowed):
            return True
    return False


def _private_name(name: str) -> bool:
    parts = PurePosixPath(name.lower()).parts
    base = parts[-1] if parts else ""
    return (any(part in PRIVATE_DIRS for part in parts)
            or base in PRIVATE_NAMES or base.startswith(".env.")
            or base.endswith(PRIVATE_SUFFIXES))


def _text_findings(name: str, data: bytes, usernames: tuple[str, ...]) -> Iterable[Finding]:
    label = _safe_label(name, usernames)
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        for rule, pattern in RULES.items():
            if any(not _synthetic(match.group()) for match in pattern.finditer(line)):
                yield Finding(label, rule, number)
        for username in usernames:
            if len(username) == 1:
                pattern = r"(?<=[\"'/\\])" + re.escape(username) + r"(?=[\"'/\\])"
            else:
                # Boundaries deliberately use ASCII account-token characters:
                # adjacent Chinese prose must not hide a Latin account name.
                pattern = r"(?<![A-Za-z0-9_.@-])" + re.escape(username) + r"(?![A-Za-z0-9_.@-])"
            if re.search(pattern, line, re.I):
                yield Finding(label, "CURRENT_OS_USERNAME", number)
                break


def _serialized_chat(value) -> bool:
    if isinstance(value, list):
        return any(_serialized_chat(item) for item in value)
    if not isinstance(value, dict):
        return False
    identity_keys = {"account_id", "sender_id", "conversation_id", "sender_name",
                     "conversation_name", "sender_label", "conversation_label"}
    content_keys = {"content_decoded", "message_content", "text", "content", "record"}
    identities = [item for key, item in value.items() if key in identity_keys and isinstance(item, str)]
    if identities and content_keys.intersection(value) and not all(
            _synthetic(item) or SYNTHETIC_IDENTITY.fullmatch(item) for item in identities):
        return True
    return any(_serialized_chat(item) for item in value.values())


def _serialized_findings(name: str, data: bytes, usernames: tuple[str, ...]) -> Iterable[Finding]:
    """Catch raw records renamed to .txt/.json without treating code as data."""
    decoded = data.decode("utf-8-sig", errors="replace")
    candidates = [(0, decoded)]
    if "\n" in decoded:
        candidates.extend(enumerate(decoded.splitlines(), 1))
    for line, candidate in candidates:
        if not candidate.lstrip().startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if _serialized_chat(parsed):
            yield Finding(_safe_label(name, usernames), "UNMARKED_CHAT_DATA", line)
            return


def _scan(entries: Iterable[tuple[str, bytes | None, str | None]], allowlist: list[str],
          usernames: tuple[str, ...], reject_unlisted: bool) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    count = total = 0
    seen: set[str] = set()
    for name, data, error in entries:
        label = _safe_label(name, usernames)
        if name.casefold() in seen:
            findings.append(Finding(label, "DUPLICATE_MEMBER"))
        seen.add(name.casefold())
        if not _allowed(name, allowlist):
            if reject_unlisted:
                findings.append(Finding(label, "NOT_IN_RELEASE_ALLOWLIST"))
            continue
        count += 1
        if count > MAX_FILES:
            findings.append(Finding("<release>", "FILE_COUNT_LIMIT"))
            break
        if _private_name(name):
            findings.append(Finding(label, "PRIVATE_FILE"))
        findings.extend(_text_findings(name, name.encode("utf-8"), usernames))
        if error:
            findings.append(Finding(label, error))
            continue
        assert data is not None
        total += len(data)
        if total > MAX_RELEASE_BYTES:
            findings.append(Finding("<release>", "RELEASE_SIZE_LIMIT"))
            break
        if len(data) > MAX_FILE_BYTES:
            findings.append(Finding(label, "FILE_SIZE_LIMIT"))
            continue
        if data.startswith(b"SQLite format 3\x00"):
            findings.append(Finding(label, "SQLITE_CONTENT"))
        if data.startswith((b"PK\x03\x04", b"\x1f\x8b", b"\x28\xb5\x2f\xfd")):
            findings.append(Finding(label, "NESTED_ARCHIVE_FORBIDDEN"))
        if b"\x00" in data:
            findings.append(Finding(label, "UNREVIEWABLE_BINARY_CONTENT"))
        findings.extend(_text_findings(name, data, usernames))
        findings.extend(_serialized_findings(name, data, usernames))
    # Every explicitly listed file must be present, including in archives.
    # Wildcard/directory declarations remain supported for older callers.
    for required in allowlist:
        if not any(c in required for c in "*?[") and required.casefold() not in seen and not any(n.startswith(required.casefold() + "/") for n in seen):
            findings.append(Finding(_safe_label(required, usernames), "DECLARED_FILE_MISSING"))
    return sorted(set(findings)), count


def _directory_entries(root: Path, allowlist: list[str]):
    # Do not recursively enumerate unrelated project files. Start only from the
    # literal prefix of each declared release entry and filter before reading.
    candidates: set[Path] = set()
    for entry in allowlist:
        prefix_parts = []
        for part in PurePosixPath(entry).parts:
            if any(char in part for char in "*?["):
                break
            prefix_parts.append(part)
        start = root.joinpath(*prefix_parts) if prefix_parts else root
        if (start.is_symlink() or not start.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in start.parents if parent.is_relative_to(root))):
            yield entry, None, "SYMLINK_FORBIDDEN"
            continue
        if start.is_file():
            candidates.add(start)
        elif start.is_dir():
            for parent, dirs, files in os.walk(start, followlinks=False):
                for directory in list(dirs):
                    target = Path(parent) / directory
                    if target.is_symlink():
                        dirs.remove(directory)
                        relative = target.relative_to(root).as_posix()
                        if _allowed(relative, allowlist):
                            yield relative, None, "SYMLINK_FORBIDDEN"
                for filename in files:
                    target = Path(parent) / filename
                    if _allowed(target.relative_to(root).as_posix(), allowlist):
                        candidates.add(target)
        elif not any(char in entry for char in "*?["):
            yield entry, None, "DECLARED_FILE_MISSING"
    for path in sorted(candidates):
        name = path.relative_to(root).as_posix()
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                yield name, None, "SYMLINK_FORBIDDEN"
            elif path.stat().st_size > MAX_FILE_BYTES:
                yield name, None, "FILE_SIZE_LIMIT"
            else:
                yield name, path.read_bytes(), None
        except OSError:
            yield name, None, "READ_FAILED"


def check_release(target: str | Path, manifest: str | Path | None = None) -> dict:
    """Return only counts and sanitized relative-file/rule/line findings."""
    usernames = _username_values()
    target = Path(target).resolve()
    try:
        if target.is_dir():
            source = Path(manifest) if manifest is not None else target / "package.json"
            allowlist = _read_manifest(source.read_bytes())
            findings, count = _scan(_directory_entries(target, allowlist), allowlist, usernames, False)
        else:
            with (zipfile.ZipFile(target) if zipfile.is_zipfile(target)
                  else tarfile.open(target, "r:*")) as archive:
                is_zip = isinstance(archive, zipfile.ZipFile)
                members = archive.infolist() if is_zip else archive.getmembers()
                if len(members) > MAX_FILES:
                    raise ValueError("FILE_COUNT_LIMIT")
                names = [(member.filename if is_zip else member.name, member) for member in members]
                manifest_names = [name for name, _ in names if name == "package.json" or name.endswith("/package.json")]
                if not manifest_names:
                    raise ValueError("MANIFEST_MISSING")
                minimum = min(name.count("/") for name in manifest_names)
                top = [name for name in manifest_names if name.count("/") == minimum]
                if len(top) != 1:
                    raise ValueError("MANIFEST_AMBIGUOUS")
                manifest_name = top[0]
                prefix = manifest_name[:-len("package.json")]

                def read_member(member):
                    size = member.file_size if is_zip else member.size
                    if size > MAX_FILE_BYTES:
                        raise ValueError("FILE_SIZE_LIMIT")
                    if is_zip:
                        with archive.open(member) as stream:
                            return stream.read(MAX_FILE_BYTES + 1)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("UNSUPPORTED_ARCHIVE_MEMBER")
                    with stream:
                        return stream.read(MAX_FILE_BYTES + 1)

                own_manifest = next(member for name, member in names if name == manifest_name)
                own_bytes = read_member(own_manifest)
                allowlist = _read_manifest(Path(manifest).read_bytes() if manifest is not None else own_bytes)
                if manifest is not None and _read_manifest(own_bytes) != allowlist:
                    raise ValueError("MANIFEST_ALLOWLIST_MISMATCH")

                def archive_entries():
                    for original, member in names:
                        is_dir = member.is_dir() if is_zip else member.isdir()
                        if is_dir:
                            continue
                        normalized = _safe_member(original)
                        if normalized is None or not original.startswith(prefix):
                            yield "<invalid-member>", None, "UNSAFE_ARCHIVE_MEMBER"
                            continue
                        name = original[len(prefix):]
                        is_link = (((member.external_attr >> 16) & 0o170000) == 0o120000
                                   if is_zip else not member.isfile())
                        if is_link:
                            yield name, None, "SYMLINK_FORBIDDEN"
                            continue
                        if not _allowed(name, allowlist):
                            yield name, None, None
                            continue
                        try:
                            yield name, read_member(member), None
                        except Exception:
                            yield name, None, "ARCHIVE_MEMBER_READ_FAILED"
                findings, count = _scan(archive_entries(), allowlist, usernames, True)
        return {"ok": not findings, "checked_files": count,
                "findings": [asdict(finding) for finding in findings]}
    except Exception as error:
        # Never expose an exception message; standard-library messages often
        # contain the actual filesystem path or an attacker-controlled string.
        known = {"MANIFEST_ALLOWLIST_REQUIRED", "MANIFEST_ALLOWLIST_INVALID", "FILE_COUNT_LIMIT",
                 "MANIFEST_MISSING", "MANIFEST_AMBIGUOUS", "FILE_SIZE_LIMIT",
                 "MANIFEST_ALLOWLIST_MISMATCH", "UNSUPPORTED_ARCHIVE_MEMBER"}
        rule = str(error) if isinstance(error, ValueError) and str(error) in known else "RELEASE_CHECK_FAILED"
        return {"ok": False, "checked_files": 0,
                "findings": [{"file": "<release>", "rule": rule, "line": 0}]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--manifest", type=Path)
    arguments = parser.parse_args(argv)
    result = check_release(arguments.target, arguments.manifest)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
