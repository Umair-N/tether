"""Share persistence and recovery, entirely under temporary folders."""
import io
import os
from pathlib import Path
import shutil
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import files
import paths
import share_store as share


class Reader:
    def __init__(self, size, block=None, release=None):
        self.remaining = size
        self.reads = []
        self.block, self.release = block, release

    def read(self, size):
        self.reads.append(size)
        if self.block:
            self.block.set()
            if not self.release.wait(5):
                raise TimeoutError('test reader timeout')
        size = min(size, self.remaining)
        self.remaining -= size
        return b'x' * size


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'share'
        self.env = patch.dict(os.environ, {'TETHER_HOME': self.temp.name, 'TETHER_SHARE': str(self.root),
                                           'TETHER_SHARE_QUOTA_BYTES': str(50 * 1024**3),
                                           'TETHER_SHARE_MAX_BYTES': str(2 * 1024**3)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.disk = patch.object(share.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10**12, 0, 10**12))
        self.disk.start()
        self.addCleanup(self.disk.stop)
        self.store = share.Store(self.root)

    def put(self, data=b'hello', name='note.txt', **kwargs):
        return self.store.put(io.BytesIO(data), len(data), name, **kwargs)

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(share.ShareError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def object(self, file):
        with self.store.connection() as db:
            return self.store.object_path(self.store.row(db, file['id'], trash=True))

    def test_old_database_adds_source_and_preserves_files(self):
        file = self.put()
        with self.store.connection(write=True) as db:
            db.execute('''CREATE TABLE old_files AS SELECT
                          id,owner_id,sha,size,name,mime,kind,inline_mime,created,modified,deleted_at,
                          width,height,duration,thumb_mime FROM files''')
            db.execute('DROP TABLE files')
            db.execute('ALTER TABLE old_files RENAME TO files')
        for _ in range(2):
            store = share.Store(self.root)
            with store.connection() as db:
                self.assertIn('source', {row['name'] for row in db.execute('PRAGMA table_info(files)')})
                self.assertIsNone(db.execute('SELECT source FROM files').fetchone()[0])
            self.assertEqual(store.list()['files'], [dict(file, source='you')])

    def test_sources_are_validated_stored_and_filtered(self):
        browser = self.put()
        agent = self.put(source='agent:codex')
        self.assertEqual(browser['source'], 'you')
        self.assertEqual(agent['source'], 'agent:codex')
        self.assertEqual(self.store.stat(agent['id']), agent)
        self.assertEqual(self.store.list(source='you')['files'], [browser])
        self.assertEqual(self.store.list(source='agent')['files'], [agent])
        self.assertEqual(len(self.store.list(source='all')['files']), 2)
        for source in ('agent:A_z.0-9', 'agent:' + 'a' * 64):
            self.assertEqual(self.put(source=source)['source'], source)
        for source in ('', 'you', 'agent:', 'agent:' + 'a' * 65, 'agent:a/b', 'agent:é', 'agent:x\n', 1, False, []):
            with self.subTest(source=source):
                with self.assertRaises(share.ShareError) as caught:
                    self.put(source=source)
                self.assertEqual((caught.exception.status, str(caught.exception)), (400, 'Choose a valid source.'))
        for source in ('bad', '', None):
            with self.assertRaises(share.ShareError) as caught:
                self.store.list(source=source)
            self.assertEqual((caught.exception.status, str(caught.exception)), (400, 'Choose a valid source.'))

    def test_stream_exact_length_short_body_and_cleanup(self):
        source = Reader(share.CHUNK * 2 + 19)
        file = self.store.put(source, source.remaining, 'large.bin')
        self.assertEqual(file['size'], share.CHUNK * 2 + 19)
        self.assertEqual(source.reads, [share.CHUNK, share.CHUNK, 19])
        self.error(400, self.store.put, io.BytesIO(b'short'), 6, 'short.bin')
        self.assertEqual(self.store.list()['usage']['count'], 1)
        self.assertEqual(list((self.root / 'incoming').iterdir()), [])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 0)
        source = io.BytesIO(b'extra')
        self.store.put(source, 2, 'two')
        self.assertEqual(source.read(), b'tra')

    def test_private_modes_and_safe_names(self):
        file = self.put(name='../../outside/..\\résumé.txt')
        self.assertEqual(file['name'], 'résumé.txt')
        self.assertEqual(share.safe_name('..'), 'file')
        self.assertEqual(share.safe_name('a\r\nb.txt'), 'ab.txt')
        self.assertLessEqual(len(share.safe_name('é' * 300).encode()), 240)
        self.store.export(file['id'])
        self.store.thumbnail(file['id'], b'\xff\xd8\xffthumb', 'image/jpeg')
        with self.store.connection(write=True) as db:
            db.execute('UPDATE files SET modified=?', ('changed',))
            for journal in self.root.glob('catalog.sqlite3*'):
                self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        for parent, dirs, names in os.walk(self.root):
            self.assertEqual(Path(parent).stat().st_mode & 0o777, 0o700)
            for name in names:
                self.assertEqual((Path(parent) / name).stat().st_mode & 0o777, 0o600)

    def test_dedupe_trash_restore_and_purge_reference_count(self):
        first, second = self.put(), self.put(name='second.txt')
        obj = self.object(first)
        self.assertEqual(obj, self.object(second))
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.store.list()['usage']['used'], 10)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM objects').fetchone()[0], 1)
        self.error(409, self.store.change, 'purge', [first['id']])
        self.store.change('delete', [first['id']])
        self.assertEqual(len(self.store.list(trash=True)['files']), 1)
        self.assertEqual(self.store.list()['usage']['used'], 10)
        self.store.change('restore', [first['id']])
        self.assertEqual(len(self.store.list()['files']), 2)
        for file in (first, second):
            self.store.change('delete', [file['id']])
            self.store.change('purge', [file['id']])
            self.assertEqual(obj.exists(), file is first)
        self.assertEqual(self.store.list()['usage'], {'used': 0, 'quota': self.store.quota, 'count': 0})

    def test_quota_limit_disk_and_owner_authorization(self):
        with patch.object(self.store, 'quota', 4):
            self.error(507, self.put)
        self.error(413, self.store.put, io.BytesIO(), 2 * 1024**3 + 1, 'huge')
        self.assertEqual(self.store.max_bytes, 2 * 1024**3)
        with patch.object(share.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10**12, 0, share.MIN_FREE)):
            self.error(507, self.put)
        file = self.put()
        for function, args in ((self.store.stat, (file['id'],)), (self.store.list, ()),
                               (self.store.export, (file['id'],)), (self.store.rename, (file['id'], 'x')),
                               (self.store.change, ('delete', [file['id']]))):
            self.error(403, function, *args, actor='someone')

    def test_concurrent_reservations_and_two_upload_limit(self):
        releases, starts, threads, results = [], [], [], []
        def upload(start, release):
            try:
                results.append(self.store.put(Reader(5, start, release), 5, 'parallel'))
            except BaseException as exc:
                results.append(exc)
        try:
            for _ in range(2):
                start, release = threading.Event(), threading.Event()
                starts.append(start)
                releases.append(release)
                thread = threading.Thread(target=upload, args=(start, release))
                threads.append(thread)
                thread.start()
                self.assertTrue(start.wait(5))
            self.error(409, self.put)
            # Another process's startup must not discard live upload reservations.
            share.Store(self.root)
            with self.store.connection() as db:
                self.assertEqual(db.execute('SELECT SUM(size) FROM uploads').fetchone()[0], 10)
        finally:
            for release in releases:
                release.set()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(item, dict) for item in results), results)
        self.assertEqual(self.store.list()['usage']['used'], 10)

    def test_reservation_counts_against_quota_and_is_released_on_failure(self):
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO uploads VALUES (?,?,?,?)', ('reserved', 'local', 6, time.time() + 3600))
        with patch.object(self.store, 'quota', 10):
            self.error(507, self.put)
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM uploads')
        with patch.object(share.os, 'fsync', side_effect=OSError('full')):
            self.error(507, self.put)
        self.assertEqual(self.store.list()['usage']['count'], 0)
        self.assertEqual(list((self.root / 'incoming').iterdir()), [])

    def test_reconcile_expires_trash_uploads_orphans_and_missing_objects(self):
        old, missing, live = self.put(name='old'), self.put(b'missing'), self.put(b'live')
        export = Path(self.store.export(old['id']))
        self.store.thumbnail(old['id'], b'\xff\xd8\xff', 'image/jpeg')
        self.object(missing).unlink()
        self.store.change('delete', [old['id']])
        with self.store.connection(write=True) as db:
            db.execute("UPDATE files SET deleted_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
            db.execute('INSERT INTO uploads VALUES (?,?,?,?)', ('stale', 'local', 5, 0))
        temp = self.root / 'incoming' / 'stale.part'
        temp.write_bytes(b'partial')
        os.utime(temp, (0, 0))
        orphan = self.root / 'objects' / 'orphan'
        orphan.write_bytes(b'unreferenced')
        self.store.reconcile()
        self.assertEqual([f['id'] for f in self.store.list()['files']], [live['id']])
        self.assertFalse(export.exists())
        self.assertFalse(temp.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse((self.root / 'thumbs' / old['id']).exists())

    def test_list_filters_pagination_and_usage(self):
        for name, data in [('one.txt', b'text'), ('several.dots.DOCX', b'office'), ('other.bin', b'bin'), ('shot.png', b'\x89PNG\r\n\x1a\n')]:
            self.put(data, name)
        self.assertEqual(len(self.store.list(kind='doc')['files']), 2)
        self.assertEqual(len(self.store.list(kind='other')['files']), 1)
        self.assertEqual(len(self.store.list(kind='image')['files']), 1)
        self.assertEqual(len(self.store.list(q='DOTS')['files']), 1)
        page = self.store.list(limit=2)
        next_page = self.store.list(limit=2, cursor=page['next'])
        self.assertIsNone(next_page['next'])
        self.assertEqual(len({f['id'] for f in page['files'] + next_page['files']}), 4)
        self.error(400, self.store.list, cursor='junk')
        self.error(400, self.store.list, limit=0)

    def test_export_is_named_private_copy_and_purge_removes_it(self):
        file = self.put(name='../../résumé.txt')
        export = Path(self.store.export(file['id']))
        self.assertEqual(export.name, 'résumé.txt')
        self.assertEqual(export.parent, self.root / 'exports' / file['id'])
        export.write_bytes(b'modified export')
        self.assertEqual(self.object(file).read_bytes(), b'hello')
        self.store.change('delete', [file['id']])
        self.assertTrue(export.exists())
        self.error(404, self.store.export, file['id'])
        self.store.change('restore', [file['id']])
        self.store.change('delete', [file['id']])
        self.store.change('purge', [file['id']])
        self.assertFalse(export.exists())

    def test_symlink_original_cannot_be_read_or_exported(self):
        file = self.put()
        obj = self.object(file)
        obj.unlink()
        obj.symlink_to(Path(self.temp.name) / 'secret')
        with self.assertRaises(OSError):
            self.store.export(file['id'])

    def test_explorer_cannot_mutate_share_or_its_ancestor(self):
        file = self.put()
        with patch.object(paths, 'SHARE', self.root):
            for function, args in ((files.write_folder, (str(self.root), Path(self.temp.name))),
                                   (files.write_item, (str(self.object(file)), Path(self.temp.name))),
                                   (files.write_item, (str(self.root), Path(self.temp.name))),
                                   (files.refuse_share_write, (Path(self.temp.name), True)),
                                   (files.make_folder, (self.temp.name, 'share'))):
                with self.assertRaises(files.FilesError) as caught:
                    function(*args)
                self.assertEqual(caught.exception.status, 403)

    def test_private_files_override_restrictive_umask_and_bad_names_are_rejected(self):
        previous = os.umask(0o777)
        try:
            file = self.put()
            export = Path(self.store.export(file['id']))
            self.store.thumbnail(file['id'], b'\xff\xd8\xff', 'image/jpeg')
        finally:
            os.umask(previous)
        for path in (self.object(file), export, self.root / 'thumbs' / file['id']):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.error(400, self.put, name='\ud800')
        self.error(400, self.store.change, 'delete', [{}])
