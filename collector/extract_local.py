# SPDX-License-Identifier: Apache-2.0
# Portions adapted from fanyuantaier/wechatauto-replica; see THIRD_PARTY_NOTICES.md.
# Modified for read-only verified snapshots, no persisted keys, and strict WAL validation.
"""Read-only local Weixin capture. Keys never leave this process's memory.

The source DB/WAL/SHM files and Weixin process are never opened for writing.
Every output must be a fresh directory outside the account directory.
A verified result means an optimistic, byte-stable per-database snapshot plus
page HMAC and SQLite integrity checks. It is not a locked SQLite transaction,
a cross-database atomic snapshot, or proof that phone history is complete.

WAL-index/reset rules: https://www.sqlite.org/walformat.html and
https://github.com/sqlite/sqlite/blob/master/src/wal.c (walIndexRecover,
walRestartHdr). Cipher layout: wechatauto-replica/wechatauto/db.py.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wal_snapshot import parse_committed_wal

PAGE = 4096
MARKER = b'com.Tencent.WCDB.Config.Cipher'
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
HEX = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
REQUIRED_DATABASES = ('contact/contact.db', 'session/session.db')


class SnapshotError(RuntimeError):
    """A safe diagnostic containing only structural metadata, never data/keys."""


class CaptureError(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _now():
    return datetime.now(timezone.utc).isoformat()


def _emit(progress, event):
    if progress is not None:
        progress(json.dumps(event, ensure_ascii=False))


def _safe_error(exc):
    if isinstance(exc, (SnapshotError, CaptureError)):
        return str(exc)
    if isinstance(exc, OSError):
        return f'{type(exc).__name__} (OS code {getattr(exc, "winerror", None) or exc.errno})'
    return type(exc).__name__


class Region(C.Structure):
    _fields_ = [('base', C.c_void_p), ('alloc', C.c_void_p),
                ('alloc_protect', W.DWORD), ('align1', W.DWORD),
                ('size', C.c_size_t), ('state', W.DWORD), ('protect', W.DWORD),
                ('kind', W.DWORD), ('align2', W.DWORD)]


class ReadOnlyProcess:
    def __init__(self, pid, image):
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.k.OpenProcess.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(Region), C.c_size_t]
        self.k.VirtualQueryEx.restype = C.c_size_t
        self.k.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ; no write/injection rights.
        self.h = self.k.OpenProcess(0x410, False, pid)
        if not self.h:
            raise OSError(C.get_last_error(), 'Cannot open selected Weixin process read-only')
        name = C.create_unicode_buffer(32768)
        size = W.DWORD(len(name))
        if (not self.k.QueryFullProcessImageNameW(self.h, 0, name, C.byref(size))
                or Path(name.value).resolve() != Path(image).resolve()
                or Path(name.value).name.lower() != 'weixin.exe'):
            self.close()
            raise SnapshotError('Weixin process identity verification failed')

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None

    def read(self, address, length):
        if not (0x10000 <= address < 0x800000000000 and 0 < length <= 2 * 1024 * 1024):
            return b''
        buf = C.create_string_buffer(length)
        count = C.c_size_t()
        self.k.ReadProcessMemory(self.h, C.c_void_p(address), buf, length, C.byref(count))
        return buf.raw[:count.value]

    def chunks(self):
        address = 0
        while address < 0x800000000000:
            r = Region()
            if not self.k.VirtualQueryEx(self.h, C.c_void_p(address), C.byref(r), C.sizeof(r)):
                break
            base = r.base or 0
            end = base + r.size
            if end <= address:
                break
            if (r.state == 0x1000 and not r.protect & 0x100
                    and (r.protect & 0xff) in (2, 4, 8, 0x20, 0x40, 0x80)):
                cursor = base
                while cursor < end:
                    size = min(1024 * 1024, end - cursor)
                    data = self.read(cursor, min(size + 256, end - cursor))
                    if data:
                        yield cursor, data
                    cursor += size
            address = end


def get_candidates(data):
    found = set()
    for match in HEX.finditer(data):
        run = match.group(1)
        starts = [0] if len(run) <= 96 else range(0, len(run) - 63, 2)
        for index in starts:
            key = bytes.fromhex(run[index:index + 64].decode('ascii'))
            if len(set(key)) >= 12:
                found.add(key)
    return found


def scan(pid, image, progress=print):
    proc = ReadOnlyProcess(pid, image)
    try:
        markers = set()
        candidates = set()
        scanned = 0
        for base, data in proc.chunks():
            scanned += len(data)
            offset = data.find(MARKER)
            while offset >= 0:
                markers.add(base + offset)
                offset = data.find(MARKER, offset + 1)
            candidates.update(get_candidates(data))
        _emit(progress, {'scan_pass': 1, 'pid': pid, 'MiB_read': round(scanned / 1048576),
                         'markers': len(markers), 'literal_candidates': len(candidates)})
        objects = 0
        if markers:
            pattern = re.compile(b'|'.join(re.escape(struct.pack('<QQ', a, len(MARKER))) for a in markers))
            for base, data in proc.chunks():
                for match in pattern.finditer(data):
                    node = proc.read(base + match.start() - 16, 64)
                    if len(node) != 64:
                        continue
                    pointer = struct.unpack_from('<Q', node, 0x28)[0]
                    obj = proc.read(pointer + 0x88, 40)
                    if len(obj) != 40:
                        continue
                    pointer, size = struct.unpack_from('<QQ', obj, 8)
                    if not 0 < size <= 1024:
                        continue
                    blob = proc.read(pointer, size)
                    if len(blob) != size:
                        continue
                    got = get_candidates(bytes(v ^ MASK[i % len(MASK)] for i, v in enumerate(blob)))
                    if got:
                        objects += 1
                        candidates.update(got)
        _emit(progress, {'scan_pass': 2, 'pid': pid, 'cipher_objects': objects, 'candidates': len(candidates)})
        return candidates
    finally:
        proc.close()


def mkey(key, salt):
    return hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, 32)


def authenticated(mac, page, number):
    if len(page) != PAGE:
        return False
    start = 16 if number == 1 else 0
    digest = hmac.new(mac, page[start:PAGE - 64] + struct.pack('<I', number), hashlib.sha512).digest()
    return hmac.compare_digest(digest, page[-64:])


def decrypt(key, page, number):
    start = 16 if number == 1 else 0
    dec = Cipher(algorithms.AES(key), modes.CBC(page[-80:-64])).decryptor()
    return ((b'SQLite format 3\0' if number == 1 else b'')
            + dec.update(page[start:-80]) + dec.finalize() + bytes(80))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp(path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino, value.st_dev)


def _optional_bytes(path, shm=False):
    try:
        # The SHM lock bytes 120..127 can be locked by Windows SQLite clients.
        # They are not database content and SQLite never reads/writes them.
        # Unbuffered I/O prevents Python from reading across a locked interval.
        with path.open('rb', buffering=0) as handle:
            if not shm:
                return handle.read()
            prefix = handle.read(120)
            if len(prefix) < 120:
                return prefix
            handle.seek(128)
            return prefix + bytes(8) + handle.read()
    except FileNotFoundError:
        return None


def _checksum(data):
    a = b = 0
    for x, y in struct.iter_unpack('<II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def parse_shm(shm):
    """Validate the Windows (little-endian) wal-index information copies."""
    if shm is None:
        return None
    if len(shm) < 136:
        raise SnapshotError('SHM header is incomplete')
    head = shm[:48]
    if head != shm[48:96]:
        raise SnapshotError('SHM duplicate headers differ')
    if _checksum(head[:40]) != struct.unpack_from('<II', head, 40):
        raise SnapshotError('SHM header checksum mismatch')
    version, unused, change = struct.unpack_from('<III', head)
    initialized, order, page_size = struct.unpack_from('<BBH', head, 12)
    maximum, pages = struct.unpack_from('<II', head, 16)
    backfill = struct.unpack_from('<I', shm, 96)[0]
    attempted = struct.unpack_from('<I', shm, 128)[0]
    if version != 3007000 or unused != 0 or initialized != 1 or order not in (0, 1):
        raise SnapshotError('Unsupported or uninitialized SHM header')
    page_size = 65536 if page_size == 1 else page_size
    # walIndexRecover assigns szPage/nPage only after finding a commit.
    # An initialized header with mxFrame=szPage=nPage=0 is valid.
    if page_size != PAGE and not (page_size == pages == maximum == 0):
        raise SnapshotError('SHM page size is unsupported')
    if backfill > maximum or attempted > maximum:
        raise SnapshotError('SHM checkpoint counters exceed committed frames')
    if maximum and not pages:
        raise SnapshotError('SHM committed database size is zero')
    return {'duplicate_headers_equal': True, 'checksum_valid': True,
            'change_counter': change, 'page_size': page_size, 'mxFrame': maximum,
            'nPage': pages, 'nBackfill': backfill, 'nBackfillAttempted': attempted,
            'big_endian_checksum': bool(order),
            'frame_checksum': struct.unpack_from('<II', head, 24),
            'salt': head[32:40]}


def _previous_generation(old_salt, current_salt):
    old1, old2 = struct.unpack('>II', old_salt)
    new1, new2 = struct.unpack('>II', current_salt)
    return (old1 + 1) & 0xffffffff == new1 and old2 != new2


def wal_plan(db, wal, shm):
    """Choose only published SHM commits; reject ambiguous/corrupt WALs.

    The caller must first establish byte stability of the three files. An
    inactive-generation suffix is ignored only after a checked current SHM
    header and the entire published commit prefix have been bound together.
    Same-generation corruption is rejected even beyond the published prefix.
    """
    if len(db) < PAGE or len(db) % PAGE:
        raise SnapshotError('Database is empty or has a non-integral page count')
    index = parse_shm(shm)
    public_index = None if index is None else {k: v for k, v in index.items() if k != 'salt'}
    base = {'wal_applied_frames': 0, 'wal_merged': False, 'shm': public_index,
            'shm_lock_bytes': '120..127 omitted and zero-filled in snapshot',
            'wal_unapplied_tail_bytes': 0}
    wal = wal or b''
    if not wal:
        if index and index['mxFrame']:
            raise SnapshotError('SHM references committed frames but WAL is missing or empty')
        if index and index['nPage'] not in (0, len(db) // PAGE):
            raise SnapshotError('SHM base database page count mismatch')
        return [], {**base, 'wal_state': 'empty_or_absent', 'wal_db_size_pages': len(db) // PAGE}
    if index is None:
        raise SnapshotError('Nonempty WAL requires a readable SHM snapshot')
    parsed, metadata = parse_committed_wal(wal, PAGE)
    if not metadata['header_valid']:
        raise SnapshotError('Invalid WAL header: ' + str(metadata['error']))
    salt = wal[16:24]
    magic = struct.unpack_from('>I', wal)[0]
    if index['big_endian_checksum'] != (magic == 0x377f0683):
        raise SnapshotError('WAL and SHM checksum byte orders differ')
    if index['salt'] != salt:
        # Reset may publish the new SHM header before rewriting the WAL file.
        if (index['mxFrame'] == index['nBackfill'] == index['nBackfillAttempted'] == 0
                and _previous_generation(salt, index['salt'])
                and index['nPage'] in (0, len(db) // PAGE)):
            return [], {**base, 'wal_state': 'checkpointed_previous_generation',
                        'wal_unapplied_tail_bytes': len(wal), 'wal_db_size_pages': len(db) // PAGE,
                        'wal_header_valid': True}
        raise SnapshotError('WAL and SHM generations do not match')

    maximum = index['mxFrame']
    limit = 32 + maximum * (24 + PAGE)
    if limit > len(wal):
        raise SnapshotError('WAL is shorter than the SHM committed prefix')
    frames, prefix = parse_committed_wal(wal[:limit], PAGE)
    if not prefix['entire_file_valid'] or len(frames) != maximum:
        raise SnapshotError('WAL committed prefix is invalid or incomplete')
    if maximum:
        if prefix['db_size_pages'] != index['nPage']:
            raise SnapshotError('WAL commit page count differs from SHM')
        last_offset = 32 + (maximum - 1) * (24 + PAGE)
        if struct.unpack_from('>II', wal, last_offset + 16) != index['frame_checksum']:
            raise SnapshotError('WAL final committed checksum differs from SHM')
    elif index['nPage'] not in (0, len(db) // PAGE):
        raise SnapshotError('SHM base database page count mismatch')

    tail_state = metadata['tail_status']
    if metadata['error'] is not None:
        offset = metadata.get('first_rejected_offset', 32)
        remaining = wal[offset:]
        # SQLite may reset a WAL without truncating it. After multiple shorter
        # generations, unoverwritten frames can be older than one reset. Only
        # equality with BOTH current salts identifies a current-generation
        # frame; no particular arithmetic distance between old salts is valid.
        # https://www.sqlite.org/fileformat2.html#wal_reset
        # The checks above still validate every frame through SHM.mxFrame and
        # bind its commit size and rolling checksum to the duplicate SHM header.
        inactive_tail = (offset >= limit and len(remaining) >= 24
                         and remaining[8:16] != salt
                         and tail_state in ('stale_or_mismatched_salt', 'incomplete_frame'))
        if not inactive_tail:
            raise SnapshotError('Invalid WAL tail: ' + str(metadata['error']))
        tail_state = 'inactive_generation_tail'
    # Complete extra frames, even a not-yet-published commit, are not replayed.
    # A raw WAL commit marker alone is not a published live SHM transaction.
    return frames, {**base, 'wal_state': 'committed_prefix' if maximum else 'shm_zero_commits',
                    'wal_header_valid': True, 'wal_committed_prefix_valid': bool(maximum),
                    'wal_index_verified': True,
                    'wal_selected_frames': len(frames),
                    'wal_db_size_pages': index['nPage'] if maximum else len(db) // PAGE,
                    'wal_unapplied_tail_bytes': len(wal) - limit,
                    'wal_tail_state': tail_state,
                    'wal_observed_current_valid_frames': metadata['valid_frame_count']}


def copy_stable(src, dst, attempts=5, delay=0.2):
    paths = [src, Path(str(src) + '-wal'), Path(str(src) + '-shm')]
    last_error = 'Source files changed while reading'
    for attempt in range(attempts):
        try:
            before = [stamp(path) for path in paths]
            guard = _optional_bytes(paths[2], shm=True)
            first = [_optional_bytes(path, shm=(i == 2)) for i, path in enumerate(paths)]
            middle = [stamp(path) for path in paths]
            second = [_optional_bytes(path, shm=(i == 2)) for i, path in enumerate(paths)]
            after = [stamp(path) for path in paths]
            # SHM is mmap'd, so timestamps alone never establish its stability.
            if before != middle or middle != after or first != second or guard != first[2]:
                raise SnapshotError('Source files changed while reading')
            if first[0] is None:
                raise SnapshotError('Source database disappeared')
            _, plan = wal_plan(*first)
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suffix, value in zip(('', '-wal', '-shm'), first):
                if value is not None:
                    Path(str(dst) + suffix).write_bytes(value)
            return {'captured_at': _now(), 'capture_attempts': attempt + 1,
                    'db_bytes': len(first[0]), 'wal_bytes': len(first[1] or b''),
                    'shm_bytes': len(first[2] or b''), 'db_sha256': sha(first[0]),
                    'wal_sha256': sha(first[1] or b''), 'shm_sha256': sha(first[2] or b''),
                    'wal_present': first[1] is not None, 'shm_present': first[2] is not None,
                    'stable_double_read': True, 'stable_shm_guard': True,
                    'consistency_method': 'optimistic_repeated_read_no_source_locks',
                    **plan}
        except (OSError, SnapshotError) as exc:
            last_error = _safe_error(exc)
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise SnapshotError('Stable snapshot unavailable: ' + last_error)


def inspect(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        checks = [row[0] for row in conn.execute('PRAGMA integrity_check')]
        if checks != ['ok']:
            raise SnapshotError('SQLite integrity_check failed')
        table_count = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    return {'integrity_check': 'ok', 'table_count': table_count, 'sha256': sha(path.read_bytes())}


def _decrypt_snapshot(snapshot, dest, key, info):
    encrypted = snapshot.read_bytes()
    wal = _optional_bytes(Path(str(snapshot) + '-wal'))
    shm = _optional_bytes(Path(str(snapshot) + '-shm'), shm=True)
    if (sha(encrypted) != info['db_sha256'] or sha(wal or b'') != info['wal_sha256']
            or sha(shm or b'') != info['shm_sha256']):
        raise SnapshotError('Saved encrypted snapshot hash changed')
    frames, plan = wal_plan(encrypted, wal, shm)
    info.update(plan)
    mac = mkey(key, encrypted[:16])
    plain = bytearray()
    for offset in range(0, len(encrypted), PAGE):
        number = offset // PAGE + 1
        page = encrypted[offset:offset + PAGE]
        if not authenticated(mac, page, number):
            raise SnapshotError('HMAC failed at base page ' + str(number))
        plain.extend(decrypt(key, page, number))
    info['base_pages_authenticated'] = len(plain) // PAGE
    for number, page in frames:
        if not authenticated(mac, page, number):
            raise SnapshotError('HMAC failed at WAL page ' + str(number))
    info['wal_pages_authenticated'] = len(frames)
    for number, page in frames:
        end = number * PAGE
        # A legitimate page number cannot require filling beyond all supplied
        # base pages plus WAL frames. Reject malformed huge allocations early.
        if number > len(encrypted) // PAGE + len(frames):
            raise SnapshotError('WAL page number exceeds available snapshot pages')
        if end > len(plain):
            plain.extend(bytes(end - len(plain)))
        plain[(number - 1) * PAGE:end] = decrypt(key, page, number)
    size = plan['wal_db_size_pages'] * PAGE
    if size > len(plain):
        raise SnapshotError('WAL commit size exceeds available pages')
    del plain[size:]
    # Mark the replay separately from final verification so a failed integrity
    # check never turns into a successful database entry or a publishable run.
    info['wal_applied_frames'] = len(frames)
    info['wal_merged'] = bool(frames)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + '.unverified')
    # Retain failed private artifacts for diagnosis; never recursively clean output.
    temporary.write_bytes(plain)
    verified = inspect(temporary)
    temporary.replace(dest)
    info.update(verified)
    info['status'] = 'verified'


def _files(source):
    files = sorted(path for path in source.rglob('*.db') if
                   (path.parent.name == 'message' and 'fts' not in path.name)
                   or path.relative_to(source).as_posix() in
                   (*REQUIRED_DATABASES, 'chatbot/chatbot_message.db', 'bizchat/bizchat.db'))
    relative = {path.relative_to(source).as_posix() for path in files}
    missing = [name for name in REQUIRED_DATABASES if name not in relative]
    if not any(re.fullmatch(r'message/message_\d+\.db', name) for name in relative):
        missing.append('message/message_<number>.db')
    if missing:
        raise SnapshotError('Required databases missing: ' + ', '.join(missing))
    # Resolve before the containment test: an unresolved account root (macOS
    # /var, Windows short names) is not a parent of its own resolved children.
    resolved_source = source.resolve()
    for path in files:
        if resolved_source not in path.resolve().parents:
            raise SnapshotError('Source database resolves outside the selected account')
    return files


def _write_report(out, report):
    temporary = out / 'capture_report.json.tmp'
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf8')
    temporary.replace(out / 'capture_report.json')


def capture_account(account: Path, output: Path, processes: list[dict], progress=print) -> dict:
    """Capture one explicit account. Raises CaptureError on any partial failure.

    ``processes`` must contain identity-checked Weixin main process candidates,
    each with ``pid`` and absolute ``image``. The opened image is checked again
    before reading. Each candidate key must authenticate the selected DB itself.
    The caller must not publish an export or update its latest link on failure.
    """
    account = Path(account).resolve()
    source = account / 'db_storage'
    out = Path(output).resolve()
    if out == account or account in out.parents:
        raise CaptureError('Output must be outside the source account', {})
    if out.exists():
        raise CaptureError('Use a new output directory', {})
    out.mkdir(parents=True)
    report = {'format_version': 2, 'source': str(source), 'started_at': _now(),
              'status': 'running', 'source_access': 'read_only',
              'cross_database_atomic': False, 'sqlite_read_transaction': False,
              'keys_persisted': False, 'process_attempts': [], 'databases': {},
              'limits': ['Per-database repeated-read validation is not an atomic SQLite transaction.',
                         'Databases are captured sequentially, so cross-database times can differ.',
                         'Local history may omit messages unavailable on this PC and deleted messages.']}
    keys = {}
    candidates = set()
    try:
        _write_report(out, report)
        if not source.is_dir():
            raise SnapshotError('Account db_storage directory does not exist')
        if not processes:
            raise SnapshotError('No verified running Weixin main process was provided')
        files = _files(source)
        report['total_databases'] = len(files)
        for path in files:
            relative = path.relative_to(source).as_posix()
            info = report['databases'][relative] = {'status': 'capturing'}
            try:
                info.update(copy_stable(path, out / 'encrypted_snapshot' / relative))
                info['status'] = 'captured'
            except Exception as exc:
                info.update(status='failed', error=_safe_error(exc))
            _write_report(out, report)
            _emit(progress, {'database': relative, 'status': info['status']})
        readable = {relative for relative, info in report['databases'].items() if info['status'] == 'captured'}
        for process in processes:
            if readable <= keys.keys():
                break
            pid = int(process['pid'])
            image = str(process['image'])
            attempt = {'pid': pid, 'image': image, 'status': 'scanning'}
            report['process_attempts'].append(attempt)
            try:
                candidates = scan(pid, image, progress=progress)
                matched = 0
                for relative in sorted(readable - keys.keys()):
                    with (out / 'encrypted_snapshot' / relative).open('rb') as handle:
                        first = handle.read(PAGE)
                    key = next((key for key in candidates if authenticated(mkey(key, first[:16]), first, 1)), None)
                    if key is not None:
                        keys[relative] = key
                        report['databases'][relative]['key_matched_process_id'] = pid
                        matched += 1
                attempt.update(status='completed', matched_databases=matched)
            except Exception as exc:
                attempt.update(status='failed', error=_safe_error(exc))
            finally:
                candidates.clear()
            _write_report(out, report)
        for relative in sorted(readable):
            info = report['databases'][relative]
            if relative not in keys:
                info.update(status='no_verified_key', error='No candidate authenticated this database')
            else:
                try:
                    _decrypt_snapshot(out / 'encrypted_snapshot' / relative,
                                      out / 'decrypted' / relative, keys[relative], info)
                except Exception as exc:
                    info.update(status='failed', error=_safe_error(exc))
            _write_report(out, report)
            _emit(progress, {'database': relative, 'status': info['status'],
                             'wal_applied_frames': info.get('wal_applied_frames', 0)})
        report['verified_databases'] = sum(info.get('status') == 'verified' for info in report['databases'].values())
        if report['verified_databases'] != len(files):
            raise SnapshotError('Capture incomplete; see per-database status in capture_report.json')
        report['status'] = 'verified'
    except Exception as exc:
        report.update(status='failed', error=_safe_error(exc))
    finally:
        keys.clear()
        candidates.clear()
        report['completed_at'] = _now()
        report['verified_databases'] = sum(info.get('status') == 'verified' for info in report['databases'].values())
        _write_report(out, report)
    _emit(progress, {'status': report['status'], 'verified_databases': report['verified_databases'],
                     'total': report.get('total_databases', 0), 'report': str(out / 'capture_report.json')})
    if report['status'] != 'verified':
        raise CaptureError(report['error'], report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Capture one local Weixin account read-only.')
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--account', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        capture_account(args.account, args.out, [{'pid': args.pid, 'image': args.image}])
    except CaptureError as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
