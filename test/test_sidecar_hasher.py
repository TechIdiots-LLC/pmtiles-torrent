"""
Hashing an archive happens in a process of its own, so it can be cancelled.

The gap this exists for: libtorrent's hashing never checks for interruption, so
a hash started by mistake could not be stopped. The sidecar could not be ended
to stop one either -- it holds the session and every torrent seeding from it --
so a 698 GiB build begun by a misclick ran for its full six hours, saturating
the disk the rest of the library was being served from, and the console showed
`hashing 698 GiB · 3m` throughout with no way to tell a third of the way
through from stuck.

`--create` hashes one archive with no session and no port. Killing it costs the
hash and nothing else, and the callback that already existed to release the GIL
now reports where it has got to.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.join(HERE, "..", "sidecar", "libtorrent_sidecar.py")

# Big enough to hash for long enough to interrupt and to report more than once,
# small enough that the suite stays quick. Random rather than zeroes: zeroes
# compress in the page cache in ways that make the timing meaningless.
ARCHIVE_BYTES = 192 * 1024 * 1024


class OneShotHashing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="hasher-")
        cls.archive = os.path.join(cls.work, "planet.pmtiles")
        with open(cls.archive, "wb") as handle:
            handle.write(os.urandom(ARCHIVE_BYTES))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def hash(self, params, wait=300):
        """Runs one hash to completion and hands back everything it said."""
        child = subprocess.Popen(
            [sys.executable, SIDECAR, "--create"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        out, _ = child.communicate(json.dumps(params), timeout=wait)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_it_produces_the_same_torrent_the_sidecar_would(self):
        # The whole point is that this replaces the in-process hash, so what it
        # produces has to be indistinguishable from what that produced.
        params = {
            "path": self.archive,
            "pieceLength": 1024 * 1024,
            "format": "v1",
            "comment": "planet",
            "createdBy": "pmtiles-swarm",
        }
        messages = self.hash(params)
        result = messages[-1]
        self.assertTrue(result["ok"], result)

        sys.path.insert(0, os.path.join(HERE, "..", "sidecar"))
        import libtorrent_sidecar as sidecar  # noqa: PLC0415 - needs the path

        in_process = sidecar.build_torrent(params)
        self.assertEqual(result["result"]["infoHash"], in_process["infoHash"])
        self.assertEqual(result["result"]["torrentFile"], in_process["torrentFile"])
        self.assertEqual(result["result"]["pieceCount"], in_process["pieceCount"])

    def test_it_says_how_far_it_has_got(self):
        # "hashing 698 GiB · 3m" is not progress. The piece count is known
        # before a byte is read, so a real fraction is available throughout.
        messages = self.hash(
            {"path": self.archive, "pieceLength": 256 * 1024, "format": "v1"}
        )
        progress = [m for m in messages if m.get("event") == "progress"]

        self.assertTrue(progress, "the hash never reported progress")
        total = progress[0]["pieces"]
        self.assertEqual(total, ARCHIVE_BYTES // (256 * 1024))
        # Monotonic, and finishing at the end: the last piece always reports,
        # whatever the throttle, so a caller drawing a bar reaches 100%.
        pieces = [m["piece"] for m in progress]
        self.assertEqual(pieces, sorted(pieces))
        self.assertEqual(pieces[-1], total - 1)

    def test_a_hash_can_be_stopped(self):
        # The reason this exists. Nothing here can be interrupted from inside,
        # so cancelling means ending the process -- which is safe only because
        # the process holds nothing else.
        child = subprocess.Popen(
            [sys.executable, SIDECAR, "--create"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        child.stdin.write(
            json.dumps(
                {"path": self.archive, "pieceLength": 256 * 1024, "format": "v1"}
            )
        )
        child.stdin.close()

        # Wait until it is demonstrably working, so this kills a hash in
        # progress rather than racing its startup.
        started = time.time()
        while child.poll() is None and time.time() - started < 30:
            line = child.stdout.readline()
            if line and json.loads(line).get("event") == "progress":
                break
        else:  # pragma: no cover - only on a machine too slow to hash at all
            self.fail("the hash never started")

        child.kill()
        child.wait(timeout=30)
        self.assertIsNotNone(child.poll(), "the hash outlived being killed")

        # And the archive it was reading is untouched, which is what makes
        # cancelling free: hashing only ever reads.
        self.assertEqual(os.path.getsize(self.archive), ARCHIVE_BYTES)

    def test_a_bad_request_is_reported_rather_than_crashing(self):
        messages = self.hash({"path": os.path.join(self.work, "not-here.pmtiles")})
        self.assertFalse(messages[-1]["ok"])
        self.assertTrue(messages[-1]["error"])

    def test_it_holds_no_session(self):
        # No port is opened and no torrent is joined, which is what makes it
        # safe to start one of these per hash and kill it without warning.
        messages = self.hash(
            {"path": self.archive, "pieceLength": 4 * 1024 * 1024, "format": "v1"}
        )
        self.assertTrue(messages[-1]["ok"])
        self.assertNotIn(
            "ready",
            [m.get("event") for m in messages],
            "the one-shot announced itself as a sidecar",
        )


if __name__ == "__main__":
    unittest.main()
