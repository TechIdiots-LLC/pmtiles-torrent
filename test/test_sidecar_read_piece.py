"""
Waiting for a torrent that cannot answer a read yet, against the bindings
actually installed.

The bug this exists for: a torrent that has just been added -- or re-added
after its files were deleted, which is how a resync starts -- has no metadata
and then spends time checking. Asking libtorrent for piece 0 in either state
was answered "invalid piece index in slot list", because the piece count is
still zero and every index is out of range. That reads as a corrupt torrent, is
in fact "ask again in a moment", and cost a consumer its full retry backoff for
a condition that clears in seconds.

It matters more than one piece sounds. The PMTiles v3 spec requires the root
directory to lie within the first 16,384 bytes, so a 16 KiB read at offset 0
carries both the header and the root directory -- one piece, and the archive
goes from unservable to servable.

Run with `python test/test_sidecar_read_piece.py` (stdlib unittest, no pytest).
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sidecar"))

import libtorrent as lt  # noqa: E402
import libtorrent_sidecar as sidecar  # noqa: E402


class FakeStatus:
    def __init__(self, has_metadata=True, state=None, num_pieces=3, num_peers=2):
        self.has_metadata = has_metadata
        self.state = state if state is not None else lt.torrent_status.downloading
        self.num_pieces = num_pieces
        self.num_peers = num_peers


class FakeInfo:
    def __init__(self, pieces):
        self._pieces = pieces

    def num_pieces(self):
        return self._pieces


class FakeHandle:
    """A handle whose readiness can be scripted pass by pass."""

    def __init__(self, statuses, pieces=10):
        self._statuses = list(statuses)
        self._info = FakeInfo(pieces)
        self.polls = 0

    def status(self):
        self.polls += 1
        # The last entry stands in for "and it stays that way".
        return self._statuses[min(self.polls - 1, len(self._statuses) - 1)]

    def torrent_file(self):
        return self._info


def instance():
    return sidecar.Sidecar.__new__(sidecar.Sidecar)


class AwaitReadable(unittest.TestCase):
    def test_a_ready_torrent_is_not_waited_for(self):
        handle = FakeHandle([FakeStatus()])
        sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() + 5)
        self.assertEqual(handle.polls, 1)

    def test_waits_for_metadata_rather_than_refusing_the_read(self):
        # The case from the field: two passes without metadata, then it lands.
        handle = FakeHandle(
            [
                FakeStatus(has_metadata=False),
                FakeStatus(has_metadata=False),
                FakeStatus(has_metadata=True),
            ]
        )
        sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() + 5)
        self.assertGreaterEqual(handle.polls, 3)

    def test_waits_while_the_torrent_is_checking(self):
        # A resync re-checks what is on disk before it can read anything.
        handle = FakeHandle(
            [
                FakeStatus(state=lt.torrent_status.checking_files),
                FakeStatus(state=lt.torrent_status.downloading),
            ]
        )
        sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() + 5)
        self.assertGreaterEqual(handle.polls, 2)

    def test_says_metadata_has_not_arrived_when_it_never_does(self):
        # The exact wording op_info uses, so both entry points describe the
        # same condition the same way and a consumer can treat it as a wait.
        handle = FakeHandle([FakeStatus(has_metadata=False)])
        with self.assertRaises(RuntimeError) as caught:
            sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() - 1)
        self.assertIn("metadata has not arrived", str(caught.exception))

    def test_names_checking_rather_than_blaming_the_index(self):
        handle = FakeHandle([FakeStatus(state=lt.torrent_status.checking_files)])
        with self.assertRaises(RuntimeError) as caught:
            sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() - 1)
        self.assertIn("checking", str(caught.exception))

    def test_an_out_of_range_index_says_so_and_says_the_count(self):
        # Only checkable once metadata is in hand; before that every index is
        # out of range, which is what produced the misleading original error.
        handle = FakeHandle([FakeStatus()], pieces=10)
        with self.assertRaises(RuntimeError) as caught:
            sidecar.Sidecar._await_readable(instance(), handle, 99, time.time() + 5)
        message = str(caught.exception)
        self.assertIn("out of range", message)
        self.assertIn("10 pieces", message)

    def test_piece_zero_of_a_real_torrent_is_in_range(self):
        handle = FakeHandle([FakeStatus()], pieces=1)
        sidecar.Sidecar._await_readable(instance(), handle, 0, time.time() + 5)


class Describe(unittest.TestCase):
    def test_reports_state_pieces_and_peers(self):
        handle = FakeHandle([FakeStatus(num_pieces=4, num_peers=7)], pieces=20)
        described = sidecar.Sidecar._describe(instance(), handle)
        self.assertIn("4/20 pieces", described)
        self.assertIn("7 peers", described)

    def test_never_becomes_the_failure_itself(self):
        # Attached to an error message, so it must not raise over one of its own.
        class Broken:
            def status(self):
                raise RuntimeError("no")

        self.assertEqual(
            sidecar.Sidecar._describe(instance(), Broken()), "state unavailable"
        )


class UnreadyStates(unittest.TestCase):
    def test_the_bindings_expose_the_states_the_wait_turns_on(self):
        # Named through getattr, so a binding missing one must not silently
        # leave that state readable -- this pins that they are all present.
        self.assertEqual(len(sidecar.UNREADY_STATES), 3)
        self.assertIn(lt.torrent_status.checking_files, sidecar.UNREADY_STATES)
        self.assertIn(lt.torrent_status.downloading_metadata, sidecar.UNREADY_STATES)


if __name__ == "__main__":
    unittest.main()
