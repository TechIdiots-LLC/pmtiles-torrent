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


if __name__ == "__main__":
    unittest.main(verbosity=2)
