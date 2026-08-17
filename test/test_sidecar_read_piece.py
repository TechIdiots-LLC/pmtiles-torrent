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
import tempfile
import threading
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


class Problems(unittest.TestCase):
    """Reporting what libtorrent said, instead of discarding it.

    The read loop drains the session's alert queue and used to keep only the
    read_piece_alert.  Everything else went in the bin -- including the alerts
    that say a piece could not be written or a file could not be opened, which
    the session subscribes to precisely so they arrive.  A full disk, an
    unwritable save path and a torrent that cannot verify its pieces all became
    the same silent timeout.
    """

    def test_the_bindings_expose_the_alerts_worth_keeping(self):
        names = [a.__name__ for a in sidecar._PROBLEM_ALERTS]
        self.assertIn("torrent_error_alert", names)
        self.assertIn("file_error_alert", names)

    def test_anything_that_is_not_a_fault_is_not_reported(self):
        self.assertIsNone(sidecar._problem(object()))
        self.assertIsNone(sidecar._problem(None))

    def test_nothing_is_appended_when_nothing_went_wrong(self):
        self.assertEqual(sidecar._joined([]), "")

    def test_faults_are_appended_to_the_message_that_carries_them(self):
        joined = sidecar._joined(["file_error_alert: disk full", "x: y"])
        self.assertIn("libtorrent also reported", joined)
        self.assertIn("disk full", joined)
        self.assertIn("x: y", joined)

    def test_a_description_never_becomes_the_failure(self):
        # Attached to an error path, so an alert whose message() raises must not
        # replace the fault being reported with one about reporting it.
        class Hostile:
            def message(self):
                raise RuntimeError("this alert cannot describe itself")

        original = sidecar._PROBLEM_ALERTS
        sidecar._PROBLEM_ALERTS = (Hostile,)
        try:
            self.assertEqual(sidecar._problem(Hostile()), "Hostile")
        finally:
            sidecar._PROBLEM_ALERTS = original

    def test_a_recognised_alert_is_described_by_kind_and_message(self):
        class Friendly:
            def message(self):
                return "disk full"

        original = sidecar._PROBLEM_ALERTS
        sidecar._PROBLEM_ALERTS = (Friendly,)
        try:
            self.assertEqual(sidecar._problem(Friendly()), "Friendly: disk full")
        finally:
            sidecar._PROBLEM_ALERTS = original


class Reachability(unittest.TestCase):
    """Whether peers can open a connection to this node, or only the reverse.

    A node nothing can reach still downloads and still uploads -- it dials out
    and works -- so none of its own traffic reveals that half the swarm can
    never start a conversation with it. The cost is invisible and permanent.
    """

    class FakeSession:
        def __init__(self, listening=True, port=6881, stats=None):
            self._listening = listening
            self._port = port
            self._stats = stats or {}

        def is_listening(self):
            return self._listening

        def listen_port(self):
            return self._port

    def instance(self, session, stats):
        obj = sidecar.Sidecar.__new__(sidecar.Sidecar)
        obj._session = session
        obj._session_stats = lambda: stats
        return obj

    def test_open_once_something_has_connected_inward(self):
        s = self.FakeSession()
        got = sidecar.Sidecar.op_reachability(
            self.instance(s, {"net.has_incoming_connections": 1,
                              "peer.incoming_connections": 4}),
            {},
        )
        self.assertEqual(got["state"], "open")
        self.assertEqual(got["incomingConnections"], 4)
        self.assertEqual(got["port"], 6881)

    def test_unproven_while_listening_with_nothing_inbound(self):
        # Not "firewalled": on a node no peer has tried, blocked and untried
        # are the same observation.
        got = sidecar.Sidecar.op_reachability(
            self.instance(self.FakeSession(), {"net.has_incoming_connections": 0}),
            {},
        )
        self.assertEqual(got["state"], "unproven")
        self.assertTrue(got["listening"])

    def test_offline_when_not_listening(self):
        got = sidecar.Sidecar.op_reachability(
            self.instance(self.FakeSession(listening=False),
                          {"net.has_incoming_connections": 1}),
            {},
        )
        # Not listening outranks a stale gauge: there is no socket to reach.
        self.assertEqual(got["state"], "offline")
        self.assertIsNone(got["port"])

    def test_missing_counters_read_as_nothing_inbound(self):
        got = sidecar.Sidecar.op_reachability(
            self.instance(self.FakeSession(), {}), {}
        )
        self.assertEqual(got["state"], "unproven")
        self.assertEqual(got["incomingConnections"], 0)

    def test_the_counters_it_reads_exist_in_this_libtorrent(self):
        # Named strings against a real library: a rename upstream would
        # otherwise read as "nothing has ever connected" for ever.
        names = {m.name for m in lt.session_stats_metrics()}
        for wanted in ("net.has_incoming_connections",
                       "peer.incoming_connections",
                       "peer.num_peers_connected"):
            self.assertIn(wanted, names)

    def test_the_session_answers_listen_state_directly(self):
        ses = lt.session({"listen_interfaces": "127.0.0.1:0",
                          "enable_dht": False, "enable_lsd": False})
        self.assertTrue(hasattr(ses, "is_listening"))
        self.assertTrue(hasattr(ses, "listen_port"))


class Recheck(unittest.TestCase):
    """Hashing what is on disk when the record and the disk disagree.

    Every other answer about how much of an archive is present is derived from
    something written down earlier. This is the one that goes and looks.
    """

    class FakeHandle:
        def __init__(self, paused=False, raises=None):
            self.rechecked = False
            self.resumed = False
            self._paused = paused
            self._raises = raises

        def force_recheck(self):
            if self._raises:
                raise self._raises
            self.rechecked = True

        def flags(self):
            return lt.torrent_flags.paused if self._paused else 0

        def resume(self):
            self.resumed = True

    def instance(self, handle):
        obj = sidecar.Sidecar.__new__(sidecar.Sidecar)
        obj._handle = lambda _hash: handle
        return obj

    def test_rechecks_the_named_torrent(self):
        handle = self.FakeHandle()
        got = sidecar.Sidecar.op_recheck(
            self.instance(handle), {"infoHash": "a" * 40}
        )
        self.assertTrue(handle.rechecked)
        self.assertTrue(got["rechecking"])

    def test_resumes_a_paused_torrent_so_the_check_actually_runs(self):
        # A paused torrent does not check. Reporting success for a check that
        # never ran is the one outcome worse than refusing.
        handle = self.FakeHandle(paused=True)
        got = sidecar.Sidecar.op_recheck(
            self.instance(handle), {"infoHash": "a" * 40}
        )
        self.assertTrue(handle.resumed)
        self.assertTrue(got["wasPaused"])

    def test_leaves_a_running_torrent_alone(self):
        handle = self.FakeHandle(paused=False)
        got = sidecar.Sidecar.op_recheck(
            self.instance(handle), {"infoHash": "a" * 40}
        )
        self.assertFalse(handle.resumed)
        self.assertFalse(got["wasPaused"])

    def test_a_failure_is_reported_rather_than_swallowed(self):
        handle = self.FakeHandle(raises=RuntimeError("no such torrent"))
        with self.assertRaises(RuntimeError):
            sidecar.Sidecar.op_recheck(
                self.instance(handle), {"infoHash": "a" * 40}
            )

    def test_the_handle_really_has_this_method(self):
        # Against the installed bindings: a fake handle proves the branch, not
        # that libtorrent offers the operation the branch calls.
        ses = lt.session({"listen_interfaces": "127.0.0.1:0",
                          "enable_dht": False, "enable_lsd": False})
        self.assertTrue(hasattr(lt.torrent_handle, "force_recheck"))
        self.assertTrue(hasattr(lt.torrent_handle, "resume"))
        del ses

    def test_checking_is_a_state_the_read_path_already_waits_on(self):
        # The recheck returns immediately and the caller watches for this
        # state. If it were not in UNREADY_STATES a read during a recheck
        # would be answered from an index that is momentarily empty.
        self.assertIn(lt.torrent_status.checking_files, sidecar.UNREADY_STATES)
        self.assertEqual(
            sidecar.STATE_MAP[lt.torrent_status.checking_files], "checking"
        )


class RemoveClearsResume(unittest.TestCase):
    """Resume data must not outlive the data it describes.

    Deleting an archive and re-fetching it left the old resume file in place,
    so the re-add handed libtorrent a record of a complete 698 GiB file against
    a path holding a fresh partial one. libtorrent answered with
    fastresume_rejected ("mismatching file size") and rechecked, and until that
    settled nothing was verified -- bytes arriving at full speed against a
    verified-piece count stuck at 1, and every read told the piece was not in
    the slot list.
    """

    class FakeSession:
        def __init__(self):
            self.removed = []

        def remove_torrent(self, handle, flags):
            self.removed.append((handle, flags))

    def instance(self, resume_dir):
        obj = sidecar.Sidecar.__new__(sidecar.Sidecar)
        obj._session = self.FakeSession()
        obj._resume_dir = resume_dir
        obj._handles = {"a" * 40: object()}
        obj._lock = threading.Lock()
        obj._statuses = {}
        obj._status_lock = threading.Lock()
        obj._handle = lambda _hash: obj._handles["a" * 40]
        return obj

    def resume_file(self, directory):
        path = os.path.join(directory, f"{'a' * 40}.resume")
        with open(path, "wb") as handle:
            handle.write(b"stale")
        return path

    def test_deleting_the_data_deletes_the_record_of_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.resume_file(directory)
            got = sidecar.Sidecar.op_remove(
                self.instance(directory), {"infoHash": "a" * 40, "deleteData": True}
            )
            self.assertFalse(os.path.exists(path))
            self.assertTrue(got["resumeRemoved"])

    def test_keeping_the_data_keeps_the_record(self):
        # A removal that keeps the files is how a pause is expressed for an
        # engine with no pause of its own. Discarding resume data there turns
        # every pause into a full re-hash.
        with tempfile.TemporaryDirectory() as directory:
            path = self.resume_file(directory)
            got = sidecar.Sidecar.op_remove(
                self.instance(directory), {"infoHash": "a" * 40, "deleteData": False}
            )
            self.assertTrue(os.path.exists(path))
            self.assertFalse(got["resumeRemoved"])

    def test_a_missing_resume_file_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            got = sidecar.Sidecar.op_remove(
                self.instance(directory), {"infoHash": "a" * 40, "deleteData": True}
            )
            self.assertTrue(got["removed"])
            self.assertFalse(got["resumeRemoved"])

    def test_the_torrent_still_goes_when_there_is_no_resume_dir(self):
        obj = self.instance(None)
        got = sidecar.Sidecar.op_remove(
            obj, {"infoHash": "a" * 40, "deleteData": True}
        )
        self.assertTrue(got["removed"])
        self.assertEqual(len(obj._session.removed), 1)


class HeadPriority(unittest.TestCase):
    """
    Asking for the header before anything asks for a tile.

    The bug this exists for: prioritising the head was reactive. read_piece
    raises whatever it is fetching, the header piece included, so an archive
    somebody reads gets its head hurried -- and an archive nobody reads does
    not. A consumer that wrongly concluded an archive was already summarised
    issued no read at all, and the archive sat unservable with nothing to
    indicate why. Asking at add time costs one piece and depends on no reader.
    """

    class Files:
        def __init__(self, offsets, sizes):
            self._offsets = offsets
            self._sizes = sizes

        def file_offset(self, index):
            return self._offsets[index]

        def file_size(self, index):
            return self._sizes[index]

    class Info:
        def __init__(self, piece_length, pieces, offsets, sizes):
            self._piece_length = piece_length
            self._pieces = pieces
            self._files = HeadPriority.Files(offsets, sizes)

        def piece_length(self):
            return self._piece_length

        def num_pieces(self):
            return self._pieces

        def num_files(self):
            return len(self._files._offsets)

        def files(self):
            return self._files

    class Handle:
        def __init__(self, info, has_metadata=True):
            self._info = info
            self._status = FakeStatus(has_metadata=has_metadata)
            self.priorities = {}
            self.deadlines = {}

        def status(self):
            return self._status

        def torrent_file(self):
            return self._info

        def piece_priority(self, index, priority):
            self.priorities[index] = priority

        def set_piece_deadline(self, index, deadline, flags=None):
            self.deadlines[index] = deadline

    def single(self, piece_length=16 * 1024 * 1024, length=698 * 1024**3):
        pieces = -(-length // piece_length)
        return self.Handle(self.Info(piece_length, pieces, [0], [length]))

    def test_one_sixteen_mib_piece_covers_the_whole_head(self):
        # The realistic case: piece 0 alone carries the header and the root
        # directory, and it is the only piece that has to be hurried.
        handle = self.single()
        got = sidecar.Sidecar._prioritise_head(sidecar.Sidecar.__new__(sidecar.Sidecar), handle)
        self.assertEqual(got["pieces"], 1)
        self.assertEqual(handle.priorities, {0: 7})

    def test_priority_alone_is_not_enough_so_a_deadline_goes_with_it(self):
        # Priority 7 promises the piece will not be skipped and says nothing
        # about when. set_piece_deadline is what reorders the picker, and its
        # absence is the difference between "eventually" and "first".
        handle = self.single()
        sidecar.Sidecar._prioritise_head(sidecar.Sidecar.__new__(sidecar.Sidecar), handle)
        self.assertEqual(handle.deadlines, {0: 0})

    def test_a_small_piece_length_takes_every_piece_the_head_spans(self):
        # 4 KiB pieces put the 16 KiB head across four of them. Hurrying only
        # the first would leave the root directory truncated and unreadable.
        handle = self.single(piece_length=4096, length=1024**3)
        got = sidecar.Sidecar._prioritise_head(sidecar.Sidecar.__new__(sidecar.Sidecar), handle)
        self.assertEqual(got["pieces"], 4)
        self.assertEqual(sorted(handle.priorities), [0, 1, 2, 3])

    def test_a_file_after_the_first_starts_at_its_own_offset(self):
        # A multi-file torrent's archive does not begin at piece 0, and asking
        # for piece 0 would hurry somebody else's bytes.
        info = self.Info(4096, 100, [0, 40960], [40960, 40960])
        handle = self.Handle(info)
        got = sidecar.Sidecar._prioritise_head(
            sidecar.Sidecar.__new__(sidecar.Sidecar), handle, 16384, 1
        )
        self.assertEqual(got["first"], 10)
        self.assertEqual(sorted(handle.priorities), [10, 11, 12, 13])

    def test_an_archive_smaller_than_the_head_window_is_clipped(self):
        handle = self.single(piece_length=4096, length=8192)
        got = sidecar.Sidecar._prioritise_head(sidecar.Sidecar.__new__(sidecar.Sidecar), handle)
        self.assertEqual(got["pieces"], 2)

    def test_a_torrent_without_metadata_yet_is_left_alone(self):
        # Piece numbers do not exist yet. The magnet path retries once the
        # metadata lands rather than guessing now.
        handle = self.Handle(self.Info(4096, 100, [0], [40960]), has_metadata=False)
        got = sidecar.Sidecar._prioritise_head(sidecar.Sidecar.__new__(sidecar.Sidecar), handle)
        self.assertEqual(got["pieces"], 0)
        self.assertEqual(handle.priorities, {})

    def test_a_handle_that_throws_does_not_fail_the_add(self):
        class Broken:
            def status(self):
                raise RuntimeError("torrent is gone")

        got = sidecar.Sidecar._prioritise_head(
            sidecar.Sidecar.__new__(sidecar.Sidecar), Broken()
        )
        self.assertEqual(got["pieces"], 0)


if __name__ == "__main__":
    unittest.main()
