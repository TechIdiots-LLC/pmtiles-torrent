"""Resume data that has not changed is not rewritten.

A hybrid torrent's resume data carries a merkle tree of 32 bytes per 16 KiB
block: a few hundred megabytes for a 128 GiB archive, several gigabytes for a
planet build. Rewriting that every five minutes, staged and fsynced and
renamed, for every torrent at once, to record that nothing had moved, is most
of the disk a seeding node does.
"""

import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sidecar"))

import libtorrent_sidecar as sidecar  # noqa: E402


class HasResume(unittest.TestCase):
    def instance(self, directory):
        made = sidecar.Sidecar.__new__(sidecar.Sidecar)
        made._resume_dir = directory
        made._lock = threading.Lock()
        return made

    def test_is_false_before_anything_is_written(self):
        # The question need_save_resume_data cannot answer: a torrent that has
        # never been saved has nothing to have changed since.
        with tempfile.TemporaryDirectory() as directory:
            made = self.instance(directory)
            self.assertFalse(made._has_resume("a" * 40))

    def test_is_true_once_something_is(self):
        with tempfile.TemporaryDirectory() as directory:
            made = self.instance(directory)
            info_hash = "b" * 40
            with open(made._resume_path(info_hash), "wb") as handle:
                handle.write(b"resume")
            self.assertTrue(made._has_resume(info_hash))

    def test_is_false_when_there_is_nowhere_to_look(self):
        # A node configured without a resume directory keeps nothing, so every
        # torrent is asked every time and none of it is written.
        made = sidecar.Sidecar.__new__(sidecar.Sidecar)
        made._resume_dir = None
        made._lock = threading.Lock()
        self.assertFalse(made._has_resume("c" * 40))


if __name__ == "__main__":
    unittest.main()
