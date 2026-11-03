import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import prefs_sync


class PrefsSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(prefs_sync, 'FILE', Path(self.folder.name) / 'prefs.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def test_empty_store_is_version_zero(self):
        self.assertEqual(prefs_sync.load(), {'prefs': {}, 'version': 0})

    def test_changed_keys_merge_and_bump_version(self):
        prefs_sync.save({'pinned': ['$1'], 'theme': 'dark'})
        stored = prefs_sync.save({'paneOrder': {'$1': ['%2', '%1']}})
        self.assertEqual(stored['version'], 2)
        self.assertEqual(stored['prefs'], {'pinned': ['$1'], 'theme': 'dark', 'paneOrder': {'$1': ['%2', '%1']}})
        self.assertEqual(prefs_sync.load(), stored)

    def test_color_scheme_syncs(self):
        # The page syncs 'scheme' with every first save; refusing it failed every prefs save from that device.
        self.assertEqual(prefs_sync.save({'scheme': 'claude', 'theme': 'dark'})['prefs'], {'scheme': 'claude', 'theme': 'dark'})

    def test_seen_keeps_the_latest_mark_per_pane(self):
        prefs_sync.save({'seen': {'%1': 200, '%2': 100}})
        stored = prefs_sync.save({'seen': {'%1': 150, '%3': 300}})
        self.assertEqual(stored['prefs']['seen'], {'%2': 100, '%1': 200, '%3': 300})

    def test_drafts_keep_the_newest_edit_and_old_clears_expire(self):
        now = time.time()
        prefs_sync.save({'drafts': {'%1': {'text': 'phone', 'at': now - 10}, '%2': {'text': 'keep', 'at': now - 30}}})
        stored = prefs_sync.save({'drafts': {'%1': {'text': 'older desktop copy', 'at': now - 20}, '%2': {'text': '', 'at': now}}})
        self.assertEqual(stored['prefs']['drafts'], {'%1': {'text': 'phone', 'at': now - 10}, '%2': {'text': '', 'at': now}})
        merged = prefs_sync.merge_drafts(stored['prefs']['drafts'], {}, now=now + prefs_sync.CLEARED_KEEP + 1)
        self.assertEqual(merged, {'%1': {'text': 'phone', 'at': now - 10}})
        with self.assertRaises(ValueError):
            prefs_sync.save({'drafts': {'%1': {'text': 5, 'at': 1}}})

    def test_archived_sessions_and_desktop_layout_sync(self):
        layout = {'desktop': {'count': 2, 'panes': ['%1', '%4'], 'focus': 0, 'cols': {'2': [0.6]}, 'row': 0.5, 'inbox': True, 'sidebar': True}}
        stored = prefs_sync.save({'archived': {'$3': 'old-work'}, 'layout': layout})
        self.assertEqual(stored['prefs'], {'archived': {'$3': 'old-work'}, 'layout': layout})
        with self.assertRaises(ValueError):
            prefs_sync.save({'archived': ['$3']})

    def test_rejects_unknown_or_badly_typed_keys(self):
        for changes in ({}, {'selected': '%1'}, {'pinned': 'x'}, {'showPaths': 1}, []):
            with self.assertRaises(ValueError):
                prefs_sync.save(changes)
        self.assertEqual(prefs_sync.load()['version'], 0)


if __name__ == '__main__':
    unittest.main()
