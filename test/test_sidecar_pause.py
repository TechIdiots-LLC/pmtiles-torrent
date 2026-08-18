"""
Pausing a torrent, so that it is actually stopped.

The report this exists for: an archive was paused in the console, the row read
`paused`, and it went on downloading at 8.4 MiB/s on one node and 9.1 MiB/s on
another. Two faults, one behind the other.

The first was that nothing here could pause at all -- there was no pause
operation, so the request reached the catalog and stopped there.

The second is the one these tests are about, and it would have reproduced the
same symptom once the first was fixed. `handle.pause()` on its own is not a
stop: libtorrent's auto-manager owns the paused flag of every torrent carrying
auto_managed, and it clears it again within about a second. Measured against
2.0.13: paused at 0.2s, running again at 1.0s, still running at six. Since the
flag is what a status reports, that produces exactly the thing complained
about -- a torrent that says "paused" while it transfers.

Run with `python test/test_sidecar_pause.py` (stdlib unittest, no pytest).
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sidecar"))

import libtorrent as lt  # noqa: E402
import libtorrent_sidecar as sidecar  # noqa: E402

# Long enough to outlast the auto-manager, which took about a second to undo a
# plain pause. Three seconds is not a guess at a race; it is several times the
# interval the thing being guarded against was measured at.
SETTLE = 3.0


class PausingATorrent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="pause-")
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
        except Exception:  # noqa: BLE001 - the session may already be gone
            instance._stop.set()

    def seed(self, instance, **extra):
        created = instance.op_create(
            {"path": self.data, "pieceLength": 256 * 1024, "format": "v1"}
        )
        added = instance.op_add({
            "torrentFile": created["torrentFile"],
            "savePath": self.work,
            "seedOnly": True,
            **extra,
        })
        return added["infoHash"]

    def flags_of(self, instance, info_hash):
        handle = instance._handle(info_hash)
        value = handle.flags()
        return {
            "paused": bool(value & lt.torrent_flags.paused),
            "auto": bool(value & lt.torrent_flags.auto_managed),
        }

    def test_a_paused_torrent_stays_paused(self):
        # The whole defect in one assertion. A plain handle.pause() passes at
        # 0.2s and fails here, because the auto-manager has since started it.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)

        self.assertEqual(instance.op_pause({"infoHash": info_hash}),
                         {"paused": True})
        time.sleep(SETTLE)

        self.assertTrue(
            self.flags_of(instance, info_hash)["paused"],
            "the auto-manager started the torrent again",
        )

    def test_the_listing_agrees_that_it_is_paused(self):
        # What the console reads. The status cache is fed by the pump, so a
        # pause that libtorrent honoured but nothing reported would leave the
        # row saying "seeding" -- the same lie the other way round.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)
        instance.op_pause({"infoHash": info_hash})
        time.sleep(SETTLE)

        listed = {row["infoHash"]: row for row in instance.op_list({})}
        self.assertEqual(listed[info_hash]["state"], "paused")

    def test_resuming_gives_it_back_to_the_auto_manager(self):
        # Pausing takes auto_managed away. Leaving it off would mean a resumed
        # torrent runs outside the queue, so the limits on how many run at once
        # stop applying to it -- and every pause would quietly erode them.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance)

        instance.op_pause({"infoHash": info_hash})
        time.sleep(SETTLE)
        self.assertEqual(instance.op_resume({"infoHash": info_hash}),
                         {"paused": False})
        time.sleep(SETTLE)

        after = self.flags_of(instance, info_hash)
        self.assertFalse(after["paused"], "it did not start again")
        self.assertTrue(after["auto"], "it came back outside the auto-manager")

    def test_a_cache_torrent_is_not_handed_to_the_auto_manager(self):
        # Cache mode is kept out of the auto-manager on purpose: it wants no
        # bytes, so the manager reads it as idle and pauses it, and a paused
        # torrent stops seeding. Resuming one must not undo that, or "resume"
        # becomes a slower pause.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance, mode="cache")

        instance.op_pause({"infoHash": info_hash})
        time.sleep(SETTLE)
        instance.op_resume({"infoHash": info_hash})
        time.sleep(SETTLE)

        after = self.flags_of(instance, info_hash)
        self.assertFalse(after["paused"], "the cache torrent stayed stopped")
        self.assertFalse(
            after["auto"],
            "resume handed a cache torrent to the auto-manager, which pauses it",
        )

    def test_adding_paused_actually_stays_paused(self):
        # The same defect on the path a restart takes. The catalog remembers
        # `paused`, and every archive is re-added with it on startup -- so
        # without this a paused archive came back up transferring.
        instance = self.sidecar_instance()
        info_hash = self.seed(instance, paused=True)
        time.sleep(SETTLE)

        self.assertTrue(
            self.flags_of(instance, info_hash)["paused"],
            "an archive added paused was started by the auto-manager",
        )


if __name__ == "__main__":
    unittest.main()
