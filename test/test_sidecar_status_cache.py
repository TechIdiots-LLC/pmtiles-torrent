"""
Listing torrents does not wait on libtorrent's session thread.

The report behind this: `libtorrent list timed out after 60000ms`, over and
over, on a node that was otherwise working -- seeding, downloading, answering
tile reads. Every 15s poll from the console failed, so the header sat at
"connecting…" and clicking an archive never loaded its details.

The cause was in how a listing was assembled. Reading one torrent's state cost
a blocking round-trip to libtorrent's session thread, and the listing did three
of them per torrent: status(), flags() and torrent_file(). Twenty archives was
sixty round-trips, each queued behind whatever that one thread was doing, so a
session busy hashing a large archive turned a listing into a minute of waiting.
Nothing was wrong with the call; the cost was multiplied by sixty before the
session was even slow.

libtorrent offers the other direction -- post_torrent_updates() queues a message
and returns, and the session answers with one alert describing everything -- so
the pump keeps every torrent's state current and a listing reads a dictionary.
"""

import base64
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sidecar"))

import libtorrent_sidecar as sidecar  # noqa: E402


class RefusingSession:
    """A session thread that answers nothing, and says who asked.

    Standing in for the real fault, which was not a session that refused but one
    too busy to reply for longer than the caller would wait. Refusing outright
    is the same thing to a listing, and it names the caller.
    """

    def __getattr__(self, name):
        raise AssertionError(f"listing asked libtorrent for {name}()")


class _Checking:
    """A torrent_status carrying only what the state decision reads."""

    def __init__(self, state, flags):
        self.state = state
        self.flags = flags
        self.has_metadata = True
        self.total_wanted = 1 << 30
        self.progress = 0.5
        self.name = "planet.pmtiles"
        self.save_path = "/store"
        self.info_hash = "c" * 40
        self.torrent_file = None

    def __getattr__(self, _name):
        return 0


class ListingIsAnsweredFromCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="status-cache-")
        cls.data = os.path.join(cls.work, "archive.bin")
        with open(cls.data, "wb") as handle:
            handle.write(os.urandom(2 * 1024 * 1024))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def sidecar_instance(self):
        instance = sidecar.Sidecar({
            "listen": "127.0.0.1:0",
            "dht": False,
            "lsd": False,
            "upnp": False,
            "natpmp": False,
        })
        self.addCleanup(self._shutdown, instance)
        return instance

    @staticmethod
    def _shutdown(instance):
        try:
            instance.op_shutdown({})
        except Exception:  # noqa: BLE001 - the session may already be a stub
            instance._stop.set()

    def seed(self, instance):
        """Adds the sample archive and hands back its infohash."""
        created = instance.op_create(
            {"path": self.data, "pieceLength": 1024 * 1024, "format": "v1"}
        )
        added = instance.op_add({
            "torrentFile": created["torrentFile"],
            "savePath": self.work,
            "seedOnly": True,
        })
        return added["infoHash"]

    def test_a_listing_asks_libtorrent_nothing(self):
        # The assertion the fix is really about. Whatever the session thread is
        # doing -- hashing 698 GiB, or in this case refusing to speak at all --
        # the console still gets its library.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        self.assertEqual(len(instance.op_list({})), 1, "nothing to list from")

        # The pump goes down with it: it is the session it waits on. So this is
        # the cache alone, with no way to refresh and nobody to ask.
        instance._session = RefusingSession()
        time.sleep(0.2)

        listed = instance.op_list({})
        self.assertEqual([entry["infoHash"] for entry in listed], [info_hash])
        self.assertEqual(listed[0]["size"], os.path.getsize(self.data))

    def test_an_archive_added_moments_ago_is_in_the_next_listing(self):
        # The cache is fed by an alert, so there is a window where a torrent
        # exists and has not been described. A caller that adds and then lists
        # to confirm must not be told its add did nothing.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        listed = instance.op_list({})
        self.assertIn(info_hash, [entry["infoHash"] for entry in listed])

    def test_a_removed_archive_stops_being_listed(self):
        # The other half of a cache: it has to forget. Reporting a removed
        # archive as still seeding is the failure that comes with keeping one.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        instance.op_remove({"infoHash": info_hash})
        self.assertEqual(instance.op_list({}), [])

    def quiet(self):
        """Stops the periodic update tick for the length of a test.

        Without this a torrent added moments ago is still settling, so the next
        tick re-describes it and hides whatever the test is trying to show. The
        archives this matters for in the field are the opposite: complete, idle
        and never changing, so never re-described.
        """
        was = sidecar.STATUS_INTERVAL
        sidecar.STATUS_INTERVAL = 3600.0
        self.addCleanup(setattr, sidecar, "STATUS_INTERVAL", was)

    def test_a_removal_alert_does_not_drop_a_torrent_added_back(self):
        # torrent_removed_alert arrives well after the removal that caused it,
        # and a re-add in between is ordinary: it is how the library is
        # restored and how a mode change is applied. Dropping the handle on the
        # alert therefore deletes a registration made after it.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        self.quiet()

        # The alert, arriving late, for a torrent that is registered again.
        instance._forget_status(info_hash)

        self.assertIn(info_hash, instance._handles, "the live handle was dropped")
        self.assertEqual(
            [entry["infoHash"] for entry in instance.op_list({})],
            [info_hash],
        )

    def test_a_status_dropped_from_the_cache_comes_back(self):
        # The safety net. post_torrent_updates() reports only torrents that
        # have *changed*, so a complete archive sitting there seeding is
        # described once and never again -- and a cache that lost that entry,
        # by any means, would omit the archive from every listing for the life
        # of the process while it seeded perfectly well. Whatever loses an
        # entry, a listing must not report a smaller library than is held.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        self.assertEqual(len(instance.op_list({})), 1)
        self.quiet()

        with instance._status_lock:
            instance._statuses.clear()

        self.assertEqual(
            [entry["infoHash"] for entry in instance.op_list({})],
            [info_hash],
            "a listing reported an archive the session is still holding as gone",
        )

    def test_a_truncated_resume_file_costs_a_recheck_not_the_archive(self):
        # The one that took a production node's library out. Resume data was
        # written in place, so a reboot mid-write left a truncated file under
        # the real name; read_resume_data then raised straight out of the add,
        # and restoring the library skipped that archive entirely. It was not
        # in the session at all -- 0% and no state in the console, "no such
        # torrent" from a recheck -- while its data sat on the disk in full.
        work = tempfile.mkdtemp(prefix="resume-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        instance = sidecar.Sidecar({
            "listen": "127.0.0.1:0",
            "dht": False,
            "lsd": False,
            "upnp": False,
            "natpmp": False,
            "resumeDir": work,
        })
        self.addCleanup(self._shutdown, instance)

        created = instance.op_create(
            {"path": self.data, "pieceLength": 1024 * 1024, "format": "v1"}
        )
        info_hash = created["infoHash"]

        # What a reboot leaves behind: the front of a real file, and nothing
        # else. Bencode reads far enough to know it is incomplete and raises.
        with open(os.path.join(work, f"{info_hash}.resume"), "wb") as handle:
            handle.write(b"d8:added_timei1700000000e9:file-form")

        added = instance.op_add({
            "torrentFile": created["torrentFile"],
            "savePath": self.work,
            "seedOnly": True,
        })
        self.assertEqual(added["infoHash"], info_hash)
        self.assertIn(
            info_hash,
            [entry["infoHash"] for entry in instance.op_list({})],
            "the archive was skipped instead of rechecked",
        )
        self.assertIsNotNone(instance._handle(info_hash))

    def test_resume_data_for_another_torrent_costs_a_recheck(self):
        # What actually took the node's library out. Resume data was written by
        # a loop that popped the session's alert queue while the pump popped it
        # too, so the pump's next pop freed the batch that loop was reading:
        # alert.handle and alert.params became reads of reclaimed memory, and
        # what reached the disk was resume data under another torrent's name.
        #
        # read_resume_data parses such a file perfectly well. add_torrent is
        # what rejects it -- "mismatching info-hash" -- which failed the add,
        # so the archive was never in the session: 0% and no state in the
        # console, "no such torrent" from a recheck, data untouched on disk.
        # Eighteen archives on the reporting node, a few more each restart.
        work = tempfile.mkdtemp(prefix="resume-wrong-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        instance = sidecar.Sidecar({
            "listen": "127.0.0.1:0",
            "dht": False,
            "lsd": False,
            "upnp": False,
            "natpmp": False,
            "resumeDir": work,
        })
        self.addCleanup(self._shutdown, instance)

        mine = instance.op_create(
            {"path": self.data, "pieceLength": 1024 * 1024, "format": "v1"}
        )
        # A second, genuinely different torrent, whose resume data is filed
        # under the first one's name. That is exactly what the race produced.
        other = os.path.join(work, "other.bin")
        with open(other, "wb") as handle:
            handle.write(os.urandom(1024 * 1024))
        theirs = instance.op_create(
            {"path": other, "pieceLength": 1024 * 1024, "format": "v1"}
        )

        # Genuine resume data, saved the way the timer saves it -- it carries
        # its own infohash, which is what libtorrent then refuses. A blob built
        # by hand does not: its identity is zero and the add is accepted.
        instance.op_add({
            "torrentFile": theirs["torrentFile"],
            "savePath": work,
            "seedOnly": True,
        })
        instance.op_save_resume({})
        instance.op_remove({"infoHash": theirs["infoHash"]})
        shutil.copyfile(
            os.path.join(work, f"{theirs['infoHash']}.resume"),
            os.path.join(work, f"{mine['infoHash']}.resume"),
        )

        added = instance.op_add({
            "torrentFile": mine["torrentFile"],
            "savePath": self.work,
            "seedOnly": True,
        })

        self.assertEqual(added["infoHash"], mine["infoHash"])
        self.assertIn(
            mine["infoHash"],
            [entry["infoHash"] for entry in instance.op_list({})],
            "the archive was skipped instead of rechecked",
        )
        self.assertFalse(
            os.path.exists(os.path.join(work, f"{mine['infoHash']}.resume")),
            "the unusable resume file was kept, to fail the add again next start",
        )

    def test_saving_resume_data_never_pops_the_alert_queue(self):
        # The root cause, stated directly. Two threads popping one destructive
        # queue is not a race that can be narrowed -- the pump's pop frees what
        # the other is reading. Everything else already goes through the pump.
        instance = self.sidecar_instance()
        self.seed(instance)

        popped = []
        real_pop = instance._session.pop_alerts
        caller = threading.current_thread()

        def watched():
            if threading.current_thread() is caller:
                popped.append(True)
            return real_pop()

        instance._session.pop_alerts = watched
        try:
            saved = instance.op_save_resume({})
        finally:
            instance._session.pop_alerts = real_pop

        self.assertEqual(popped, [], "op_save_resume popped the session's alerts")
        self.assertEqual(saved["written"], 1, "no resume data was written")

    def test_resume_data_is_never_left_half_written(self):
        # The other half: no truncated file should exist to be read. Written
        # beside the target and renamed over it, so an interrupted write leaves
        # the previous good copy rather than a partial new one.
        work = tempfile.mkdtemp(prefix="resume-atomic-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        instance = sidecar.Sidecar.__new__(sidecar.Sidecar)
        instance._resume_dir = work

        info_hash = "b" * 40
        target = os.path.join(work, f"{info_hash}.resume")
        with open(target, "wb") as handle:
            handle.write(b"the previous good copy")

        opened = []
        real_open = open

        def watched(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        import builtins

        builtins.open = watched
        try:
            instance._write_resume(info_hash, b"the new copy")
        finally:
            builtins.open = real_open

        self.assertNotIn(
            target, opened, "the live resume file was opened for writing"
        )
        self.assertTrue(os.path.exists(target))
        self.assertNotEqual(
            open(target, "rb").read(), b"the previous good copy", "nothing was written"
        )
        self.assertEqual(
            [name for name in os.listdir(work) if name.endswith(".new")],
            [],
            "the staging file was left behind",
        )

    def test_an_archive_being_checked_says_so_rather_than_paused(self):
        # libtorrent hashes one store at a time -- active_checking defaults to
        # 1 -- and every torrent in that queue carries the paused flag,
        # including the one actually being hashed. Testing that flag first made
        # "checking" unreachable: on the restart that recovered eighteen
        # archives the whole library read as paused, with no way to tell work
        # in progress from an archive somebody had stopped.
        work = tempfile.mkdtemp(prefix="checking-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        instance = self.sidecar_instance()

        # Asserted on the rendering rather than by racing a real hash: which
        # torrent libtorrent picks first, and whether it carries the flag at
        # the instant of a poll, is timing. The invariant is not. A store being
        # hashed is being hashed whether or not it is also queued.
        rendered = instance._render(
            _Checking(
                state=sidecar.lt.torrent_status.checking_files,
                flags=int(sidecar.lt.torrent_flags.paused)
                | int(sidecar.lt.torrent_flags.auto_managed),
            )
        )
        self.assertEqual(rendered["state"], "checking")

        # And the other direction, which is what keeps this honest: a torrent
        # genuinely held back rests in checking_resume_data, and calling that
        # work in progress would be the same confusion reversed.
        held = instance._render(
            _Checking(
                state=sidecar.lt.torrent_status.checking_resume_data,
                flags=int(sidecar.lt.torrent_flags.paused),
            )
        )
        self.assertEqual(held["state"], "paused")

    def test_paused_is_read_from_the_status_flags(self):
        # status() and flags() used to be two separate round-trips, and the
        # paused bit came from the second. It is on the status all along, and
        # this is what proves the one that stayed reports the same thing.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        instance._handle(info_hash).pause()

        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            listed = instance.op_list({})
            state = listed[0]["state"] if listed else None
            if state == "paused":
                break
            time.sleep(0.1)
        self.assertEqual(state, "paused")


if __name__ == "__main__":
    unittest.main()
