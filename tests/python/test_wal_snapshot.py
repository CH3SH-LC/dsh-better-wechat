"""Generated WAL/SHM bytes only; no accounts, messages or process access."""
from pathlib import Path
import struct
import sys
import unittest

COLLECTOR = Path(__file__).resolve().parents[2] / "collector"
sys.path.insert(0, str(COLLECTOR))
try:
    import wal_snapshot as wal
    import extract_local as capture
finally:
    sys.path.pop(0)

PAGE = 4096
FRAME = PAGE + 24
SALT = (100, 200)


def generated_wal(records=((1, 2), (2, 2)), salt=SALT, big=False):
    order = ">" if big else "<"
    header = struct.pack(">6I", 0x377F0683 if big else 0x377F0682,
                         3007000, PAGE, 0, *salt)
    checksum = wal._checksum(header, order)
    output = header + struct.pack(">2I", *checksum)
    for number, (page, database_pages) in enumerate(records, 1):
        payload = bytes([number % 256]) * PAGE
        fields = struct.pack(">2I", page, database_pages)
        checksum = wal._checksum(payload, order,
                                 wal._checksum(fields, order, checksum))
        output += fields + struct.pack(">4I", *salt, *checksum) + payload
    return output


def generated_shm(raw, maximum=2, pages=2, checksum=None, salt=None, big=False):
    if checksum is None:
        checksum = ((0, 0) if maximum == 0 else
                    struct.unpack_from(">2I", raw, 32 + (maximum - 1) * FRAME + 16))
    head = struct.pack("<3IBBH4I", 3007000, 0, 1, 1, int(big), PAGE,
                       maximum, pages, *checksum)
    head += raw[16:24] if salt is None else struct.pack(">2I", *salt)
    head += struct.pack("<2I", *capture._checksum(head))
    return head + head + bytes(40)


def inactive_tail(age=2, salt2=300, size=FRAME):
    salt = ((SALT[0] - age) & 0xFFFFFFFF, salt2)
    return (struct.pack(">6I", 1, 2, *salt, 0, 0) + bytes(PAGE))[:size]


class WalSnapshotContract(unittest.TestCase):
    def setUp(self):
        self.db = bytes(PAGE * 2)
        self.raw = generated_wal()
        self.shm = generated_shm(self.raw)

    def plan(self, raw=None, shm=None):
        return capture.wal_plan(self.db, self.raw if raw is None else raw,
                                self.shm if shm is None else shm)

    def rejected(self, raw=None, shm=None):
        with self.assertRaises(capture.SnapshotError):
            self.plan(raw, shm)

    def test_committed_prefix_is_selected_in_both_checksum_orders(self):
        for big in (False, True):
            with self.subTest(big=big):
                raw = generated_wal(big=big)
                frames, info = self.plan(raw, generated_shm(raw, big=big))
                self.assertEqual([page for page, _ in frames], [1, 2])
                self.assertEqual(info["wal_db_size_pages"], 2)

    def test_multiple_old_generations_beyond_published_prefix_are_inactive(self):
        for age in (1, 2, 7, 100, 101, 0xFFFFFFFF):
            with self.subTest(age=age):
                frames, info = self.plan(self.raw + inactive_tail(age))
                self.assertEqual(len(frames), 2)
                self.assertEqual(info["wal_unapplied_tail_bytes"], FRAME)
                self.assertEqual(info["wal_tail_state"], "inactive_generation_tail")

    def test_only_one_changed_salt_word_also_makes_tail_inactive(self):
        for tail in (inactive_tail(0), inactive_tail(2, SALT[1])):
            frames, info = self.plan(self.raw + tail)
            self.assertEqual(len(frames), 2)
            self.assertEqual(info["wal_tail_state"], "inactive_generation_tail")

    def test_inactive_partial_frame_requires_complete_frame_header(self):
        for size in (24, 25, FRAME - 1):
            with self.subTest(size=size):
                frames, _ = self.plan(self.raw + inactive_tail(4, size=size))
                self.assertEqual(len(frames), 2)
        for size in (1, 8, 16, 23):
            with self.subTest(size=size):
                self.rejected(self.raw + inactive_tail(4, size=size))

    def test_published_zero_frames_can_ignore_inactive_tail(self):
        raw = generated_wal(()) + inactive_tail(5)
        frames, info = self.plan(raw, generated_shm(raw, maximum=0))
        self.assertEqual(frames, [])
        self.assertEqual(info["wal_state"], "shm_zero_commits")

    def test_parser_stops_and_reports_inactive_tail_without_promoting_it(self):
        raw = self.raw + inactive_tail(5) + generated_wal()[32:]
        frames, info = wal.parse_committed_wal(raw, PAGE)
        self.assertEqual(len(frames), 2)
        self.assertFalse(info["entire_file_valid"])
        self.assertEqual(info["error"], "stale_or_mismatched_salt")
        self.assertEqual(info["first_rejected_offset"], len(self.raw))

    def test_same_generation_tail_checksum_damage_is_rejected(self):
        tail = struct.pack(">6I", 1, 2, *SALT, 0, 0) + bytes(PAGE)
        self.rejected(self.raw + tail)

    def test_same_generation_incomplete_tail_is_rejected(self):
        tail = struct.pack(">6I", 1, 2, *SALT, 0, 0) + bytes(PAGE)
        for size in (1, 16, 24, FRAME - 1):
            with self.subTest(size=size):
                self.rejected(self.raw + tail[:size])

    def test_prefix_checksum_damage_cannot_be_hidden_by_inactive_tail(self):
        raw = bytearray(self.raw)
        raw[32 + 24 + 19] ^= 1
        self.rejected(bytes(raw) + inactive_tail())

    def test_prefix_salt_damage_cannot_be_hidden_by_inactive_tail(self):
        raw = bytearray(self.raw)
        raw[32 + 8] ^= 1
        self.rejected(bytes(raw) + inactive_tail())

    def test_truncated_published_prefix_is_rejected(self):
        self.rejected(self.raw[:-1])

    def test_published_boundary_must_be_a_commit(self):
        raw = generated_wal(((1, 2), (2, 0)))
        self.rejected(raw + inactive_tail(), generated_shm(raw))

    def test_shm_page_count_must_match_commit(self):
        self.rejected(self.raw + inactive_tail(), generated_shm(self.raw, pages=3))

    def test_shm_frame_checksum_must_match_committed_boundary(self):
        self.rejected(self.raw + inactive_tail(),
                      generated_shm(self.raw, checksum=(11, 22)))

    def test_shm_and_wal_generation_must_match(self):
        self.rejected(self.raw + inactive_tail(),
                      generated_shm(self.raw, salt=(99, 300)))

    def test_shm_duplicate_headers_and_checksum_remain_mandatory(self):
        changed = bytearray(self.shm)
        changed[8] ^= 1
        self.rejected(self.raw + inactive_tail(), bytes(changed))
        changed[56] ^= 1
        self.rejected(self.raw + inactive_tail(), bytes(changed))

    def test_wal_header_checksum_remains_mandatory(self):
        changed = bytearray(self.raw)
        changed[12] ^= 1
        self.rejected(bytes(changed) + inactive_tail())

    def test_extra_valid_unpublished_frames_are_never_replayed(self):
        for commit in (0, 2):
            with self.subTest(commit=commit):
                raw = generated_wal(((1, 2), (2, 2), (1, commit)))
                frames, info = self.plan(raw + inactive_tail(7))
                self.assertEqual(len(frames), 2)
                self.assertEqual(info["wal_observed_current_valid_frames"], 3)
                self.assertEqual(info["wal_unapplied_tail_bytes"], 2 * FRAME)


if __name__ == "__main__":
    unittest.main()
