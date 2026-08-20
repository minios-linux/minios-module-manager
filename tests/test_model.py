import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from minios_module_manager.model import LoadState, Snapshot


class SnapshotTests(unittest.TestCase):
    def test_loading_is_not_usable(self):
        self.assertFalse(Snapshot().usable)

    def test_ready_preserves_module_order(self):
        snapshot = Snapshot(LoadState.READY, ['00-core.sb', '50-user.sb'])
        self.assertTrue(snapshot.usable)
        self.assertEqual(snapshot.modules, ('00-core.sb', '50-user.sb'))

    def test_empty_is_authoritative(self):
        self.assertTrue(Snapshot(LoadState.EMPTY).usable)

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(ValueError):
            Snapshot('missing')


if __name__ == '__main__':
    unittest.main()
