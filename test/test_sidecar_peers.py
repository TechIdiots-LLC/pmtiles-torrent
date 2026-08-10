"""
Peer reporting, against the bindings actually installed.

The bug this exists for: `peer_info.utp_socket` is in the C++ header but not in
the 2.x Python bindings, and reading it raised on the first peer. The caller
caught that and returned an empty list, so an archive downloading at 10 MiB/s
reported zero peers — and looked exactly like a swarm nobody was in.

Run with `python test/test_sidecar_peers.py` (stdlib unittest, no pytest).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sidecar"))

import libtorrent_sidecar as sidecar  # noqa: E402


class FakePeer:
    """A peer exposing only what a given build exposes."""

    def __init__(self, flags=0, connection_type=None, client=b"qBittorrent/5.0"):
        self.ip = ("203.0.113.7", 51413)
        self.client = client
        self.progress = 0.5
        self.down_speed = 10_000_000
        self.up_speed = 280_000
        self.flags = flags
        self.connection_type = connection_type


class Unreadable(FakePeer):
    """A peer whose `flags` raises, standing in for any absent attribute."""

    @property
    def flags(self):
        raise AttributeError("this build does not expose flags")

    @flags.setter
    def flags(self, value):
        pass


def peers_from(peer_list):
    """Runs op_peers over a handle returning the given peers."""

    class Handle:
        def get_peer_info(self):
            return peer_list

    instance = sidecar.Sidecar.__new__(sidecar.Sidecar)
    instance._handle = lambda info_hash: Handle()
    return sidecar.Sidecar.op_peers(instance, {"infoHash": "a" * 40})


class PeerReporting(unittest.TestCase):
    def test_reports_a_peer(self):
        [peer] = peers_from([FakePeer()])
        self.assertEqual(peer["address"], "203.0.113.7:51413")
        self.assertEqual(peer["client"], "qBittorrent/5.0")
        self.assertEqual(peer["downloadSpeed"], 10_000_000)

    def test_a_missing_attribute_costs_only_that_attribute(self):
        # The whole point. Before, one unreadable field emptied the list.
        [peer] = peers_from([Unreadable()])
        self.assertEqual(peer["address"], "203.0.113.7:51413")
        self.assertEqual(peer["downloadSpeed"], 10_000_000)

    def test_a_peer_is_still_a_peer_without_the_utp_flag(self):
        # 2.0.13 has no utp_socket. That must not stop the peer being listed.
        self.assertIsNone(getattr(sidecar.lt.peer_info, "utp_socket", None))
        [peer] = peers_from([FakePeer()])
        self.assertEqual(peer["connection"], "unknown")

    def test_names_the_transport_where_the_build_exposes_it(self):
        bit = getattr(sidecar.lt.peer_info, "seed", None)
        self.assertIsNotNone(bit, "this build should expose the seed flag")
        self.assertTrue(sidecar._has_flag(FakePeer(flags=bit), "seed"))
        self.assertFalse(sidecar._has_flag(FakePeer(flags=0), "seed"))
        self.assertIsNone(sidecar._has_flag(FakePeer(), "not_a_real_flag"))

    def test_distinguishes_a_web_seed_from_a_peer(self):
        # An archive pulling at full speed from one web seed and one pulling
        # from the swarm look identical in the totals; only the first stops
        # dead when that single server goes away.
        web = getattr(sidecar.lt.peer_info, "web_seed", None)
        self.assertIsNotNone(web)
        [peer] = peers_from([FakePeer(connection_type=web)])
        self.assertEqual(peer["kind"], "web seed")

        standard = sidecar.lt.peer_info.standard_bittorrent
        [peer] = peers_from([FakePeer(connection_type=standard)])
        self.assertEqual(peer["kind"], "peer")

    def test_every_peer_survives_a_bad_one(self):
        found = peers_from([FakePeer(), Unreadable(), FakePeer()])
        self.assertEqual(len(found), 3)

    def test_decodes_a_bytes_client_name(self):
        [peer] = peers_from([FakePeer(client=b"\xff\xfeodd")])
        self.assertIsInstance(peer["client"], str)



class Bucketising(unittest.TestCase):
    """Reducing a per-piece map to a drawable width."""

    def test_covers_every_piece(self):
        # A rounding gap at the end shows up as a bar that never reaches the
        # right edge — and as an archive that looks unfinished when it is not.
        for total, buckets in ((1000, 10), (178000, 1000), (7, 3), (999, 1000)):
            values = list(range(total))
            seen = set()

            def collect(chunk):
                seen.update(chunk)
                return 0

            sidecar._bucketise(values, buckets, collect)
            self.assertIn(total - 1, seen, f"{total}/{buckets} dropped the last piece")
            self.assertIn(0, seen, f"{total}/{buckets} dropped the first piece")

    def test_every_bucket_gets_a_piece(self):
        # Fewer pieces than columns must widen the pieces, not leave gaps.
        out = sidecar._bucketise([1, 1, 1, 1], 16, lambda v: 1 if all(v) else 0)
        self.assertEqual(len(out), 16)
        self.assertTrue(all(out))

    def test_all_versus_any(self):
        pieces = [1] * 100
        pieces[55] = 0
        self.assertEqual(sidecar._bucketise(pieces, 10, lambda v: 1 if all(v) else 0)[5], 0)
        holes = [0] * 100
        holes[55] = 1
        self.assertEqual(sidecar._bucketise(holes, 10, lambda v: 1 if any(v) else 0)[5], 1)

    def test_availability_takes_the_rarest(self):
        # An average would hide the one piece nobody has.
        values = [9, 9, 9, 1, 9, 9, 9, 9, 9, 9]
        out = sidecar._bucketise(values, 2, lambda v: min(min(v), 255))
        self.assertEqual(out, [1, 9])

    def test_empty(self):
        self.assertEqual(sidecar._bucketise([], 10, lambda v: 0), [])


class PieceFields(unittest.TestCase):
    """The libtorrent fields the piece maps are built from."""

    def test_the_bindings_expose_what_op_pieces_reads(self):
        # The peers bug was exactly this: a field assumed present and absent in
        # the 2.x bindings. Check rather than assume, against this build.
        for name in ("pieces", "num_pieces", "distributed_copies"):
            self.assertTrue(
                hasattr(sidecar.lt.torrent_status, name), f"torrent_status.{name} is missing"
            )
        for name in ("piece_availability", "status"):
            self.assertTrue(
                hasattr(sidecar.lt.torrent_handle, name), f"torrent_handle.{name} is missing"
            )
        self.assertTrue(hasattr(sidecar.lt.peer_info, "pieces"))
class IdentifyingATorrent(unittest.TestCase):
    """Naming resume files, which is what makes a restart skip re-hashing."""

    @staticmethod
    def _torrent(work):
        data = os.path.join(work, "demo.bin")
        with open(data, "wb") as fh:
            fh.write(os.urandom(2 * 1024 * 1024))
        storage = sidecar.lt.file_storage()
        sidecar.lt.add_files(storage, data)
        creator = sidecar.lt.create_torrent(storage, 1 << 20)
        sidecar.lt.set_piece_hashes(creator, os.path.dirname(data))
        return sidecar.lt.torrent_info(
            sidecar.lt.bdecode(sidecar.lt.bencode(creator.generate()))
        )

    def test_reads_the_hash_from_metadata(self):
        # The bug: `info_hashes` is only filled in for params parsed from a
        # magnet. With a .torrent it reads as forty zeros — a perfectly good
        # name for a file that will never exist, so the resume lookup missed
        # every time and every restart re-hashed the whole store.
        import tempfile

        work = tempfile.mkdtemp()
        info = self._torrent(work)
        params = sidecar.lt.add_torrent_params()
        params.ti = info

        self.assertEqual(sidecar._identify(params), str(info.info_hash()))
        self.assertNotEqual(sidecar._identify(params), sidecar.ZERO_HASH)

    def test_reads_the_hash_from_a_magnet(self):
        params = sidecar.lt.parse_magnet_uri("magnet:?xt=urn:btih:" + "b" * 40)
        self.assertEqual(sidecar._identify(params), "b" * 40)

    def test_answers_nothing_when_there_is_nothing_to_read(self):
        # Better than a name made of zeros, which every torrent would share.
        self.assertIsNone(sidecar._identify(sidecar.lt.add_torrent_params()))

    def test_a_resume_path_needs_a_hash(self):
        instance = sidecar.Sidecar.__new__(sidecar.Sidecar)
        instance._resume_dir = "/tmp/resume"
        self.assertIsNone(instance._resume_path(None))
        self.assertTrue(instance._resume_path("a" * 40).endswith("a" * 40 + ".resume"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
