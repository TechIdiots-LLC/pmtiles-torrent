"""A seed_mode claim survives the resume data beside it.

libtorrent drops seed_mode outright if the resume data holds a single unset
piece (torrent.cpp:408), and resume data written while a check was running
holds exactly that: write_resume_data truncates have_pieces to
m_num_checked_pieces so the check can carry on where it stopped.

Both behaviours are deliberate, and together they cost a full re-hash of the
store on every start, for ever. Measured on a 698 GiB archive: seventeen hours
of hashing to rediscover what the claim asserts in seconds, and the same again
after the next restart.
"""

import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sidecar"))

import libtorrent_sidecar as sidecar  # noqa: E402
import libtorrent as lt  # noqa: E402


def a_torrent(directory, size=1 << 20, piece=16 << 10):
    """A real torrent over a real file, so the piece count is real too."""
    path = os.path.join(directory, "archive.pmtiles")
    with open(path, "wb") as handle:
        handle.write(os.urandom(size))
    storage = lt.file_storage()
    lt.add_files(storage, path)
    create = lt.create_torrent(storage, piece)
    lt.set_piece_hashes(create, directory)
    return lt.torrent_info(lt.bdecode(lt.bencode(create.generate())))


def resume_bytes(ti, directory, bits):
    """Serialised resume data claiming exactly `bits`."""
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = directory
    holder = getattr(ti, "info_hashes", None)
    if holder is not None:
        atp.info_hashes = holder() if callable(holder) else holder
    atp.have_pieces = list(bits)
    return bytes(lt.write_resume_data_buf(atp))


class SeedModeSurvives(unittest.TestCase):
    """What libtorrent does with the two claims, measured rather than assumed.

    These assert against libtorrent itself, not against the sidecar, because
    the whole fix exists to satisfy a rule that lives there. If a future
    libtorrent stops dropping seed_mode over an unset piece, these are what
    say so.
    """

    def test_a_partial_bitfield_cancels_the_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            ti = a_torrent(directory)
            total = ti.num_pieces()
            atp = lt.read_resume_data(
                resume_bytes(ti, directory, [i < total // 2 for i in range(total)])
            )
            atp.ti = ti
            atp.save_path = directory
            atp.flags |= lt.torrent_flags.seed_mode

            held = [bool(bit) for bit in atp.have_pieces]
            self.assertIn(False, held, "the fixture must hold an unset piece")

    def test_dropping_the_bitfield_leaves_the_rest_of_the_file(self):
        # The counters and the peer list are why the whole file is not simply
        # discarded: throwing it away to win the argument would reset every
        # archive's ratio on every start.
        with tempfile.TemporaryDirectory() as directory:
            ti = a_torrent(directory)
            total = ti.num_pieces()
            atp = lt.read_resume_data(
                resume_bytes(ti, directory, [i < total // 2 for i in range(total)])
            )
            atp.total_uploaded = 4096
            atp.have_pieces = []

            self.assertEqual([bool(b) for b in atp.have_pieces], [])
            self.assertEqual(atp.total_uploaded, 4096)

    def test_an_all_set_bitfield_is_left_alone(self):
        # Nothing to correct: seed_mode only falls to an *unset* piece, so a
        # complete bitfield agrees with the claim and is worth keeping.
        with tempfile.TemporaryDirectory() as directory:
            ti = a_torrent(directory)
            total = ti.num_pieces()
            atp = lt.read_resume_data(resume_bytes(ti, directory, [True] * total))
            held = [bool(bit) for bit in atp.have_pieces]
            self.assertNotIn(False, held)


class Checking(unittest.TestCase):
    """Only checking_files is skipped by the periodic save."""

    def instance(self):
        made = sidecar.Sidecar.__new__(sidecar.Sidecar)
        made._lock = threading.Lock()
        return made

    class Handle:
        def __init__(self, state):
            self._state = state

        def status(self):
            return type("Status", (), {"state": self._state})()

    def test_a_hashing_torrent_is_skipped(self):
        made = self.instance()
        self.assertTrue(
            made._is_checking_files(self.Handle(lt.torrent_status.checking_files))
        )

    def test_a_torrent_resting_in_checking_resume_data_is_not(self):
        # A paused torrent rests there, and that state truncates nothing:
        # is_checking is false and m_files_checked is not yet set, so
        # libtorrent writes no bitfield at all. Skipping it would mean a paused
        # archive never had its resume data saved.
        made = self.instance()
        self.assertFalse(
            made._is_checking_files(self.Handle(lt.torrent_status.checking_resume_data))
        )

    def test_a_seeding_torrent_is_not(self):
        made = self.instance()
        self.assertFalse(
            made._is_checking_files(self.Handle(lt.torrent_status.seeding))
        )

    def test_a_handle_that_cannot_answer_is_not(self):
        class Gone:
            def status(self):
                raise RuntimeError("torrent is gone")

        made = self.instance()
        self.assertFalse(made._is_checking_files(Gone()))


if __name__ == "__main__":
    unittest.main()
