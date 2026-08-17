"""
The sidecar answers ordinary calls while it is hashing an archive.

The report behind this: adding a 698 GiB local archive made the console
unreachable for as long as the hash ran. `list` timed out after 60s, the header
sat at "connecting…", and clicking an archive never loaded its details. Nothing
was wrong with any of those calls -- they were queued behind one `create` in a
strictly serial reader loop.

Driven over the real stdin/stdout protocol rather than by calling the handlers,
because the loop is the thing under test.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.join(HERE, "..", "sidecar", "libtorrent_sidecar.py")

# Big enough that hashing takes long enough to interleave against, small enough
# that the test stays quick. Random rather than zeroes: a file of zeroes
# compresses in the page cache in ways that make the timing meaningless.
ARCHIVE_BYTES = 192 * 1024 * 1024


class SidecarProcess:
    """A running sidecar, spoken to the way pmtiles-swarm speaks to it."""

    def __init__(self):
        self.replies = {}
        # The order replies actually arrived in, which is the whole question
        # here: a serial loop can only answer in the order it was asked.
        self.arrivals = []
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.next_id = 1
        self.child = subprocess.Popen(
            [sys.executable, SIDECAR],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, "SIDECAR_SETTINGS": json.dumps({"listenPort": 0})},
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.child.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("event") == "ready":
                self.ready.set()
            elif message.get("id") is not None:
                with self.lock:
                    self.replies[message["id"]] = message
                    self.arrivals.append(message["id"])

    def send(self, op, params=None):
        """Sends one request and hands back its id."""
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        self.child.stdin.write(
            json.dumps({"id": request_id, "op": op, "params": params or {}}) + "\n"
        )
        self.child.stdin.flush()
        return request_id

    def wait(self, request_id, timeout):
        """Waits for one reply, or None if it never came."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if request_id in self.replies:
                    return self.replies[request_id]
            time.sleep(0.01)
        return None

    def call(self, op, params=None, timeout=30):
        return self.wait(self.send(op, params), timeout)

    def close(self):
        try:
            self.child.stdin.close()
        except OSError:
            pass
        self.child.kill()
        self.child.wait(timeout=10)


class HashingDoesNotStarveTheLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.mkdtemp(prefix="sidecar-concurrency-")
        cls.archive = os.path.join(cls.workspace, "planet.pmtiles")
        with open(cls.archive, "wb") as handle:
            handle.write(os.urandom(1024 * 1024) * (ARCHIVE_BYTES // (1024 * 1024)))

    def setUp(self):
        self.sidecar = SidecarProcess()
        self.assertTrue(self.sidecar.ready.wait(60), "the sidecar never announced itself")

    def tearDown(self):
        self.sidecar.close()

    def test_list_answers_before_the_hash_it_was_queued_behind(self):
        # Asserted as an ordering rather than a latency, so it means the same
        # thing on any machine. A serial loop can only answer in the order it
        # was asked: `list` sent second cannot come back first, however quick it
        # is and however small the archive. That it does is the entire fix, and
        # a timing bound would instead have measured the test file against the
        # disk under it -- 192 MiB hashes in under a second on an NVMe, which is
        # well inside any tolerance a serial loop would also have met.
        hashing = self.sidecar.send(
            "create", {"path": self.archive, "pieceLength": 1 << 20}
        )
        listing = self.sidecar.send("list")

        reply = self.sidecar.wait(listing, timeout=60)
        self.assertIsNotNone(reply, "list went unanswered during a hash")
        self.assertTrue(reply["ok"], reply.get("error"))

        with self.sidecar.lock:
            arrivals = list(self.sidecar.arrivals)
        self.assertIn(listing, arrivals)
        self.assertNotIn(
            hashing,
            arrivals[: arrivals.index(listing)],
            "the hash answered first, so the loop is still serial",
        )

        result = self.sidecar.wait(hashing, timeout=180)
        self.assertIsNotNone(result, "the hash itself never came back")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["result"]["size"], ARCHIVE_BYTES)

    def test_concurrent_replies_do_not_corrupt_each_other(self):
        # Two threads writing one line-delimited protocol is how a reply gets
        # spliced into the middle of another and both are lost. Every one of
        # these has to come back whole, parseable, and under its own id.
        hashing = self.sidecar.send(
            "create", {"path": self.archive, "pieceLength": 1 << 20}
        )
        sent = [self.sidecar.send("version") for _ in range(50)]

        for request_id in sent:
            reply = self.sidecar.wait(request_id, timeout=60)
            self.assertIsNotNone(reply, f"reply {request_id} was lost")
            self.assertTrue(reply["ok"], reply.get("error"))
            self.assertEqual(reply["id"], request_id)

        result = self.sidecar.wait(hashing, timeout=180)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"], result.get("error"))


if __name__ == "__main__":
    unittest.main()
