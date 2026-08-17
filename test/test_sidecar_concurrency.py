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

import base64
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



class ReadsDoNotStarveOrStealFromEachOther(unittest.TestCase):
    """
    The second instance of the same fault, and the one that mattered in normal
    running: every tile served from a cache-mode archive goes through
    `read_piece`, which waits up to 60s for a piece to arrive from the swarm. A
    serial loop spent most of its life inside one.

    It could not simply be threaded like `create`. Both reads drained the
    session's single alert queue, so two at once would each have swallowed the
    other's `read_piece_alert` and both would have timed out. Alert delivery is
    a pump with subscribers now, which is what makes the threading safe -- so
    both halves are worth holding to.
    """

    def setUp(self):
        self.sidecar = SidecarProcess()
        self.assertTrue(self.sidecar.ready.wait(60), "the sidecar never announced itself")
        self.workspace = tempfile.mkdtemp(prefix="sidecar-reads-")

    def tearDown(self):
        self.sidecar.close()

    def _seeded_archive(self, size=8 * 1024 * 1024, piece_length=1 << 20):
        """A real torrent of real bytes, added complete so reads can answer."""
        path = os.path.join(self.workspace, "planet.pmtiles")
        payload = os.urandom(size)
        with open(path, "wb") as handle:
            handle.write(payload)

        created = self.sidecar.call(
            "create", {"path": path, "pieceLength": piece_length}, timeout=120
        )
        self.assertIsNotNone(created)
        self.assertTrue(created["ok"], created.get("error"))

        added = self.sidecar.call(
            "add",
            {
                "torrentFile": created["result"]["torrentFile"],
                "savePath": self.workspace,
                "seedOnly": True,
            },
            timeout=60,
        )
        self.assertIsNotNone(added)
        self.assertTrue(added["ok"], added.get("error"))
        return added["result"]["infoHash"], payload, piece_length

    def test_two_reads_at_once_each_get_their_own_piece(self):
        # Draining a shared queue, whichever read popped first took both
        # alerts: one returned somebody else's piece or the wrong one, and the
        # other waited out its full timeout for an alert already consumed.
        info_hash, payload, piece_length = self._seeded_archive()

        wanted = [0, 1, 2, 3]
        sent = {
            piece: self.sidecar.send(
                "read_piece",
                {"infoHash": info_hash, "piece": piece, "timeoutMs": 30000},
            )
            for piece in wanted
        }

        for piece, request_id in sent.items():
            reply = self.sidecar.wait(request_id, timeout=60)
            self.assertIsNotNone(reply, f"piece {piece} was never answered")
            self.assertTrue(reply["ok"], reply.get("error"))
            self.assertEqual(reply["result"]["piece"], piece, "answered with another piece")

            expected = payload[piece * piece_length : (piece + 1) * piece_length]
            self.assertEqual(
                base64.b64decode(reply["result"]["data"]),
                expected,
                f"piece {piece} came back with the wrong bytes",
            )

    def test_list_answers_while_a_read_is_waiting(self):
        # A magnet has no metadata, so the read waits out its whole timeout
        # without ever being able to answer — which is exactly the shape of a
        # piece that has not arrived from the swarm yet, and what a serving node
        # spends its time doing.
        magnet = f"magnet:?xt=urn:btih:{'a' * 40}&dn=planet.pmtiles"
        added = self.sidecar.call(
            "add", {"magnet": magnet, "savePath": self.workspace}, timeout=60
        )
        self.assertIsNotNone(added)
        self.assertTrue(added["ok"], added.get("error"))

        reading = self.sidecar.send(
            "read_piece",
            {"infoHash": added["result"]["infoHash"], "piece": 0, "timeoutMs": 15000},
        )
        listing = self.sidecar.send("list")

        reply = self.sidecar.wait(listing, timeout=10)
        self.assertIsNotNone(reply, "list went unanswered while a read was waiting")
        self.assertTrue(reply["ok"], reply.get("error"))

        with self.sidecar.lock:
            arrivals = list(self.sidecar.arrivals)
        self.assertNotIn(
            reading,
            arrivals[: arrivals.index(listing)],
            "the read answered first, so the loop is still serial",
        )



class AlertsAreNeverHeldPastThePump(unittest.TestCase):
    """
    libtorrent owns its alerts and frees them on the next pop_alerts(), so an
    alert object handed to another thread is a pointer into memory the session
    is about to reuse. Reading one later is a use-after-free, and it does not
    raise -- it takes the process out with SIGSEGV.

    Confirmed against the installed bindings: capture alerts, pop five more
    times, then read one of the captured ones, and the interpreter dies with a
    memory-corruption code rather than an exception.

    Asserted as a type invariant rather than by reproducing it, because a test
    that segfaults takes the runner with it and reports nothing. Nothing a
    subscriber can receive may be an alert.
    """

    def test_a_subscription_queues_snapshots_not_alerts(self):
        import libtorrent as lt

        sidecar_module = _sidecar_module()
        sidecar = sidecar_module.Sidecar.__new__(sidecar_module.Sidecar)
        sidecar._subscriber_lock = threading.Lock()
        sidecar._subscribers = []
        sidecar._reported = set()

        seen = []
        with sidecar_module.Sidecar._subscribe(
            sidecar,
            lambda alert: True,
            lambda alert: {"kind": type(alert).__name__},
        ) as subscription:
            # Stand in for the pump, which is what calls offer().
            session = lt.session({"listen_interfaces": "0.0.0.0:0"})
            session.post_session_stats()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not seen:
                for alert in session.pop_alerts():
                    subscription.offer(alert)
                    seen.append(alert)
                time.sleep(0.02)

        self.assertTrue(seen, "no alerts were produced to offer")
        queued = []
        while not subscription.queue.empty():
            queued.append(subscription.queue.get_nowait())
        self.assertTrue(queued, "nothing reached the queue")
        for item in queued:
            self.assertNotIsInstance(
                item,
                lt.alert,
                "an alert object reached a queue; it will be freed under the reader",
            )
            self.assertIsInstance(item, dict)


def _sidecar_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("libtorrent_sidecar", SIDECAR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
if __name__ == "__main__":
    unittest.main()
