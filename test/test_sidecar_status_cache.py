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

import os
import shutil
import sys
import tempfile
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
