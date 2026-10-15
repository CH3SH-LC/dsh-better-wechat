"""Validate an immutable SQLite/SQLCipher WAL byte snapshot, without file I/O.

Sources reviewed on 2026-10-12:
https://www.sqlite.org/fileformat.html#wal_file_format
https://www.sqlite.org/fileformat.html#checksum_algorithm
https://github.com/sqlcipher/sqlcipher/blob/master/src/wal.c

In SQLCipher's walWriteOneFrame(), sqlcipherPagerCodec() runs BEFORE
walEncodeFrame(), and the same encoded bytes are then written to disk. The WAL
checksum therefore covers the on-disk ENCRYPTED page, including its reserve
area. This module never decrypts pages or tries a plaintext-checksum fallback.

Returned frames are ordered, may repeat page numbers, and include every valid
frame through the last commit. Replay them in order, then truncate the output
to metadata['db_size_pages'] * page_size. Do not filter pages by their first
byte: overflow, freelist, and pointer-map pages are valid too.

This validates a WAL prefix only. It cannot establish that a separately copied
main DB belongs to the same snapshot, check SQLCipher page HMACs, or establish
SQLite database integrity. The caller owns those separate checks.
"""

from __future__ import annotations

import struct
from typing import Any

_MASK32 = 0xFFFFFFFF
_WAL_VERSION = 3007000
_MAX_PAGE_NUMBER = 0xFFFFFFFE
_HEADER_SIZE = 32
_FRAME_HEADER_SIZE = 24


def _checksum(data: bytes | memoryview, order: str,
              seed: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """SQLite's two-word rolling checksum, modulo 2**32."""
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of 8 bytes")
    s0, s1 = seed
    for x0, x1 in struct.iter_unpack(order + "II", data):
        s0 = (s0 + x0 + s1) & _MASK32
        s1 = (s1 + x1 + s0) & _MASK32
    return s0, s1


def parse_committed_wal(
    wal_bytes: bytes, page_size: int = 4096,
) -> tuple[list[tuple[int, bytes]], dict[str, Any]]:
    """Return checksum-verified frames through the last complete commit.

    Invalid headers return no frames and ``status='invalid_wal'``. Parsing
    stops at the FIRST invalid/incomplete frame; it never skips a bad frame
    to resume at a later one. A prior committed prefix can still be returned,
    with the rejected tail described explicitly. Valid but uncommitted tail
    frames are excluded. ``entire_file_valid`` means only WAL structural and
    rolling-checksum validity, not cryptographic or database integrity.

    A salt-mismatched tail can be ordinary retained content after WAL reuse,
    including frames from more than one reset ago. It is still reported as an
    invalid continuation here. A caller with a separately verified WAL-index
    may classify it as inactive only beyond its fully checked commit boundary.

    A zero-byte WAL returns ``status='empty'``. Wrong argument types or an
    impossible requested page size raise TypeError/ValueError. Input data is
    never modified and no local files/processes are accessed.
    """
    if not isinstance(wal_bytes, bytes):
        raise TypeError("wal_bytes must be immutable bytes")
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise TypeError("page_size must be an integer")
    if not (512 <= page_size <= 65536) or page_size & (page_size - 1):
        raise ValueError("page_size must be a power of two from 512 to 65536")

    length = len(wal_bytes)
    meta: dict[str, Any] = {
        "status": "empty" if not length else "invalid_wal",
        "page_size": page_size,
        "input_bytes": length,
        "checksum_input": "on_disk_page_bytes",
        "validation_scope": "wal_structure_and_rolling_checksums_only",
        "header_valid": False,
        "entire_file_valid": False,
        "committed_prefix_valid": False,
        "valid_frame_count": 0,
        "committed_frame_count": 0,
        "last_commit_frame": 0,
        "commit_count": 0,
        "db_size_pages": None,
        "uncommitted_frame_count": 0,
        "rejected_tail_bytes": 0,
        "ignored_tail_bytes": 0,
        "tail_status": "empty" if not length else "unexamined",
        "error": None,
        "snapshot_pair_verified": False,
        "page_hmac_verified": False,
        "database_integrity_verified": False,
    }

    def bad_header(reason: str) -> tuple[list[tuple[int, bytes]], dict[str, Any]]:
        meta.update(error=reason, tail_status="invalid_header",
                    rejected_tail_bytes=length, ignored_tail_bytes=length)
        return [], meta

    if not length:
        return [], meta
    if length < _HEADER_SIZE:
        return bad_header("incomplete_wal_header")

    magic, version, stored_page_size, sequence, salt1, salt2, c0, c1 = (
        struct.unpack_from(">8I", wal_bytes)
    )
    if magic not in (0x377F0682, 0x377F0683):
        return bad_header("unsupported_wal_magic")
    if version != _WAL_VERSION:
        return bad_header("unsupported_wal_version")
    # Unlike the database header's two-byte field, the WAL stores 65536
    # directly; a WAL page-size value of 1 is not the 65536 shorthand.
    if stored_page_size != page_size:
        return bad_header("wal_page_size_mismatch")
    order = "<" if magic == 0x377F0682 else ">"
    state = _checksum(memoryview(wal_bytes)[:24], order)
    if state != (c0, c1):
        return bad_header("wal_header_checksum_mismatch")

    meta.update(header_valid=True, wal_version=version,
                checkpoint_sequence=sequence,
                checksum_byte_order="little" if order == "<" else "big")
    frame_size = _FRAME_HEADER_SIZE + page_size
    valid_frames: list[tuple[int, bytes]] = []
    last_commit = 0
    offset = _HEADER_SIZE
    stopped = "none"
    view = memoryview(wal_bytes)

    while offset < length:
        remaining = length - offset
        frame_number = len(valid_frames) + 1
        if remaining < frame_size:
            stopped = "incomplete_frame"
            break
        pgno, dbsize, fsalt1, fsalt2, f0, f1 = struct.unpack_from(
            ">6I", wal_bytes, offset
        )
        if (fsalt1, fsalt2) != (salt1, salt2):
            stopped = "stale_or_mismatched_salt"
            break
        if not (1 <= pgno <= _MAX_PAGE_NUMBER):
            stopped = "invalid_page_number"
            break
        if dbsize > _MAX_PAGE_NUMBER:
            stopped = "invalid_commit_db_size"
            break
        page_start = offset + _FRAME_HEADER_SIZE
        page_end = offset + frame_size
        next_state = _checksum(view[offset:offset + 8], order, state)
        next_state = _checksum(view[page_start:page_end], order, next_state)
        if next_state != (f0, f1):
            # No inference about whether this is corruption, concurrent
            # copying, an unfinished write, or an unsupported codec variant.
            stopped = "frame_checksum_mismatch"
            break
        valid_frames.append((pgno, wal_bytes[page_start:page_end]))
        state = next_state
        offset = page_end
        if dbsize:
            last_commit = frame_number
            meta["db_size_pages"] = dbsize
            meta["commit_count"] += 1

    valid_count = len(valid_frames)
    uncommitted = valid_count - last_commit
    committed_end = _HEADER_SIZE + last_commit * frame_size
    if stopped != "none":
        meta["error"] = stopped
        meta["first_rejected_frame"] = valid_count + 1
        meta["first_rejected_offset"] = offset
    meta.update(
        status="valid_committed_prefix" if last_commit else "no_committed_frames",
        entire_file_valid=(stopped == "none"),
        committed_prefix_valid=bool(last_commit),
        valid_frame_count=valid_count,
        committed_frame_count=last_commit,
        last_commit_frame=last_commit,
        uncommitted_frame_count=uncommitted,
        rejected_tail_bytes=length - offset,
        ignored_tail_bytes=length - committed_end,
        tail_status=stopped if stopped != "none" else (
            "uncommitted_frames" if uncommitted else "none"
        ),
    )
    meta['committed_db_pages'] = meta['db_size_pages']
    return valid_frames[:last_commit], meta
