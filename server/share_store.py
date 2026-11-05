"""Private personal file storage. SQLite transactions serialize quota and publication."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import uuid

import media_files
import paths

OWNER = 'local'
CHUNK = 1024 * 1024
MIN_FREE = 20 * 1024**3
UPLOAD_TTL = 24 * 3600
TRASH_TTL = 30 * 24 * 3600
THUMB_LIMIT = 512 * 1024
OFFICE = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.rtf'}


class ShareError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def authorize(actor, file):
    """The single owner policy, also used for owner-level operations."""
    if actor != OWNER or file['owner_id'] != actor:
        raise ShareError(403, 'You cannot access that file.')


def safe_name(value):
    if not isinstance(value, str):
        raise ShareError(400, 'Send a file name.')
    name = value.replace('\\', '/').split('/')[-1]
    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127).strip().strip('.')
    if not name:
        name = 'file'
    try:
        while len(name.encode('utf-8')) > 240:
            name = name[:-1]
    except UnicodeError as exc:
        raise ShareError(400, 'Send a UTF-8 file name.') from exc
    return name


def stamp():
    return datetime.now(timezone.utc).isoformat()


def raster_type(head):
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'image/webp'
    return None


def detected_type(head):
    raster = raster_type(head)
    if raster:
        return raster
    if head.startswith(b'%PDF-'):
        return 'application/pdf'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand == b'qt  ':
            return 'video/quicktime'
        if brand in (b'M4A ', b'M4B ', b'F4A '):
            return 'audio/mp4'
        if brand in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'M4V ', b'dash'):
            return 'video/mp4'
    if head.startswith(b'\x1aE\xdf\xa3') and b'webm' in head:
        return 'video/webm'
    if head.startswith(b'RIFF') and head[8:12] == b'WAVE':
        return 'audio/wav'
    if head.startswith(b'OggS') and any(s in head for s in (b'OpusHead', b'\x01vorbis', b'Speex')):
        return 'audio/ogg'
    if head.startswith(b'fLaC'):
        return 'audio/flac'
    if head.startswith(b'ID3') or (len(head) >= 2 and head[0] == 255 and head[1] & 0xe6 == 0xe2):
        return 'audio/mpeg'
    return None


def classification(name, claimed, head):
    detected = detected_type(head)
    # Active formats never acquire an inline media type through a misleading header.
    active = Path(name).suffix.lower() in {'.html', '.htm', '.svg', '.xml', '.js', '.mjs'}
    if detected and not active:
        kind = 'pdf' if detected == 'application/pdf' else detected.split('/')[0]
        return detected, kind, detected
    guessed = mimetypes.guess_type(name)[0] or ''
    claimed = claimed.split(';', 1)[0].lower().strip()
    if Path(name).suffix.lower() in media_files.TEXT_SUFFIXES or name in media_files.TEXT_NAMES or guessed.startswith('text/'):
        return 'text/plain', 'text', None
    mime = claimed if re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', claimed) else guessed
    return mime or 'application/octet-stream', 'other', None


def metadata(values):
    result = {}
    for key in ('width', 'height', 'duration'):
        value = values.get(key)
        if value is None:
            continue
        try:
            number = float(value)
            if not math.isfinite(number) or number < 0 or (key != 'duration' and (number != int(number) or number > 1000000)):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ShareError(400, 'Send valid media dimensions and duration.') from exc
        result[key] = number if key == 'duration' else int(number)
    return result


class Store:
    def __init__(self, root=None):
        self.root = Path(root or paths.SHARE).absolute()
        self.quota = int(os.environ.get('TETHER_SHARE_QUOTA_BYTES', 50 * 1024**3))
        self.max_bytes = int(os.environ.get('TETHER_SHARE_MAX_BYTES', 2 * 1024**3))
        self._directory(self.root)
        for name in ('objects', 'incoming', 'thumbs', 'exports'):
            self._directory(self.root / name)
        database = self.root / 'catalog.sqlite3'
        fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ShareError(409, 'The Share catalog is not a regular file.')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS objects (
                    owner_id TEXT NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL,
                    PRIMARY KEY(owner_id, sha, size));
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL,
                    name TEXT NOT NULL, mime TEXT NOT NULL, kind TEXT NOT NULL, inline_mime TEXT,
                    created TEXT NOT NULL, modified TEXT NOT NULL, deleted_at TEXT,
                    width INTEGER, height INTEGER, duration REAL, thumb_mime TEXT, source TEXT);
                CREATE INDEX IF NOT EXISTS file_listing ON files(owner_id, deleted_at, created, id);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, size INTEGER NOT NULL, expires REAL NOT NULL);
            ''')
            if 'source' not in {row['name'] for row in db.execute('PRAGMA table_info(files)')}:
                db.execute('ALTER TABLE files ADD COLUMN source TEXT')
        self.reconcile()

    @staticmethod
    def _directory(path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ShareError(409, 'The Share folder changed.')
        os.chmod(path, 0o700)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.root / 'catalog.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        # SQLite journals inherit the database's private mode.
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def object_path(self, row):
        return self.root / 'objects' / row['owner_id'] / row['sha'][:2] / row['sha']

    def open_private(self, path):
        """Open each managed component without following links, including the leaf."""
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.relative_to(self.root).parts
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
            leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            os.close(leaf)
            raise ShareError(404, 'That file is not available.')
        return os.fdopen(leaf, 'rb')

    def row(self, db, identifier, actor=OWNER, trash=False):
        if not isinstance(identifier, str):
            raise ShareError(400, 'Send a file ID.')
        row = db.execute('SELECT * FROM files WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ShareError(404, 'That file is not available.')
        authorize(actor, row)
        if row['deleted_at'] and not trash:
            raise ShareError(404, 'That file is in the trash.')
        return row

    @staticmethod
    def public(row):
        result = {k: row[k] for k in ('id', 'name', 'size', 'mime', 'kind', 'created', 'modified')}
        result['has_thumb'] = bool(row['thumb_mime'])
        result['source'] = row['source'] if row['source'] is not None else 'you'
        for k in ('width', 'height', 'duration', 'deleted_at'):
            if row[k] is not None:
                result[k] = row[k]
        return result

    def stat(self, identifier, actor=OWNER):
        with self.connection() as db:
            return self.public(self.row(db, identifier, actor, trash=True))

    def usage(self, db, actor):
        authorize(actor, {'owner_id': actor})
        row = db.execute('SELECT COALESCE(SUM(size),0), COUNT(*) FROM files WHERE owner_id=?', (actor,)).fetchone()
        return {'used': row[0], 'quota': self.quota, 'count': row[1]}

    def list(self, kind='all', q='', trash=False, cursor=None, limit=60, actor=OWNER, source='all'):
        authorize(actor, {'owner_id': actor})
        if kind not in ('all', 'image', 'video', 'audio', 'doc', 'other'):
            raise ShareError(400, 'Choose a valid file kind.')
        if source not in ('all', 'you', 'agent'):
            raise ShareError(400, 'Choose a valid source.')
        try:
            limit = int(limit)
            if not 1 <= limit <= 200:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ShareError(400, 'Choose a limit from 1 to 200.') from exc
        clauses = ['owner_id=?', 'deleted_at IS NOT NULL' if trash else 'deleted_at IS NULL', 'instr(lower(name), lower(?)) > 0']
        args = [actor, q]
        if source == 'you':
            clauses.append('source IS NULL')
        elif source == 'agent':
            clauses.append("source LIKE 'agent:%'")
        # Match the last extension, including names containing multiple dots.
        doc = "(kind IN ('pdf','text') OR " + ' OR '.join('lower(name) LIKE ?' for _ in OFFICE) + ')'
        if kind == 'doc':
            clauses.append(doc)
            args.extend('%' + ext for ext in sorted(OFFICE))
        elif kind == 'other':
            clauses.extend(["kind='other'", 'NOT ' + doc])
            args.extend('%' + ext for ext in sorted(OFFICE))
        elif kind != 'all':
            clauses.append('kind=?')
            args.append(kind)
        if cursor:
            try:
                created, identifier = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
                if not isinstance(created, str) or not isinstance(identifier, str):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ShareError(400, 'That cursor is not valid.') from exc
            clauses.append('(created < ? OR (created = ? AND id < ?))')
            args.extend((created, created, identifier))
        with self.connection() as db:
            rows = db.execute('SELECT * FROM files WHERE ' + ' AND '.join(clauses) + ' ORDER BY created DESC, id DESC LIMIT ?', (*args, limit + 1)).fetchall()
            for row in rows:
                authorize(actor, row)
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = base64.urlsafe_b64encode(json.dumps([last['created'], last['id']]).encode()).decode().rstrip('=')
            return {'files': [self.public(row) for row in rows[:limit]], 'next': next_cursor, 'usage': self.usage(db, actor)}

    def put(self, stream, size, name, mime='', actor=OWNER, source=None, **values):
        authorize(actor, {'owner_id': actor})
        if source is not None and (not isinstance(source, str) or not re.fullmatch(r'agent:[A-Za-z0-9._-]{1,64}', source)):
            raise ShareError(400, 'Choose a valid source.')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ShareError(400, 'Send the file size in Content-Length.')
        if size > self.max_bytes:
            raise ShareError(413, 'That file exceeds the Share file limit.')
        name, values = safe_name(name), metadata(values)
        identifier = uuid.uuid4().hex
        temp = self.root / 'incoming' / (identifier + '.part')
        with self.connection(write=True) as db:
            reserved = db.execute('SELECT COALESCE(SUM(size),0), COUNT(*) FROM uploads').fetchone()
            owner_reserved = db.execute('SELECT COALESCE(SUM(size),0) FROM uploads WHERE owner_id=?', (actor,)).fetchone()[0]
            if reserved[1] >= 2:
                raise ShareError(409, 'Two uploads are already in progress.')
            if self.usage(db, actor)['used'] + owner_reserved + size > self.quota:
                raise ShareError(507, 'The Share quota is full.')
            if shutil.disk_usage(self.root).free - reserved[0] - size < MIN_FREE:
                raise ShareError(507, 'The disk needs at least 20 GiB of free space.')
            db.execute('INSERT INTO uploads VALUES (?,?,?,?)', (identifier, actor, size, time.time() + UPLOAD_TTL))
        try:
            digest, head = hashlib.sha256(), bytearray()
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                left = size
                while left:
                    chunk = stream.read(min(CHUNK, left))
                    if not chunk or len(chunk) > left:
                        raise ShareError(400, 'The upload stopped before the declared length.')
                    target.write(chunk)
                    digest.update(chunk)
                    head.extend(chunk[:max(0, 4096 - len(head))])
                    left -= len(chunk)
                target.flush()
                os.fsync(target.fileno())
            sha = digest.hexdigest()
            mime, kind, inline = classification(name, mime, bytes(head))
            now = stamp()
            with self.connection(write=True) as db:
                if not db.execute('SELECT 1 FROM uploads WHERE id=?', (identifier,)).fetchone():
                    raise ShareError(409, 'That upload expired. Try again.')
                final = self.object_path({'owner_id': actor, 'sha': sha})
                self._directory(final.parent.parent)
                self._directory(final.parent)
                if not db.execute('SELECT 1 FROM objects WHERE owner_id=? AND sha=? AND size=?', (actor, sha, size)).fetchone():
                    os.replace(temp, final)
                    self._sync_dir(final.parent)
                    db.execute('INSERT INTO objects VALUES (?,?,?)', (actor, sha, size))
                db.execute('''INSERT INTO files (id,owner_id,sha,size,name,mime,kind,inline_mime,created,modified,width,height,duration,source)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (identifier, actor, sha, size, name, mime, kind, inline, now, now,
                            values.get('width'), values.get('height'), values.get('duration'), source))
                db.execute('DELETE FROM uploads WHERE id=?', (identifier,))
                return self.public(self.row(db, identifier, actor))
        except OSError as exc:
            raise ShareError(507, 'The file could not be stored on disk.') from exc
        finally:
            temp.unlink(missing_ok=True)
            with self.connection(write=True) as db:
                db.execute('DELETE FROM uploads WHERE id=?', (identifier,))

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def rename(self, identifier, name, actor=OWNER):
        name = safe_name(name)
        with self.connection(write=True) as db:
            row = self.row(db, identifier, actor)
            # Renaming cannot expand the original inline policy.
            inline = row['inline_mime'] if Path(name).suffix.lower() not in {'.html', '.htm', '.svg', '.xml', '.js', '.mjs'} else None
            db.execute('UPDATE files SET name=?, modified=?, inline_mime=? WHERE id=?', (name, stamp(), inline, identifier))
            return self.public(self.row(db, identifier, actor))

    def change(self, action, identifiers, actor=OWNER):
        authorize(actor, {'owner_id': actor})
        if action not in ('delete', 'restore', 'purge') or not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 1000:
            raise ShareError(400, 'Send a list of file IDs.')
        if any(not isinstance(identifier, str) for identifier in identifiers):
            raise ShareError(400, 'Send a list of file IDs.')
        with self.connection(write=True) as db:
            rows = [self.row(db, identifier, actor, trash=True) for identifier in dict.fromkeys(identifiers)]
            if action == 'purge' and any(not row['deleted_at'] for row in rows):
                raise ShareError(409, 'Move files to the trash before purge.')
            for row in rows:
                if action == 'purge':
                    self._purge(db, row)
                else:
                    db.execute('UPDATE files SET deleted_at=?, modified=? WHERE id=?',
                               (stamp() if action == 'delete' else None, stamp(), row['id']))
            return {'ok': True, 'files': [] if action == 'purge' else [self.public(self.row(db, row['id'], actor, trash=True)) for row in rows]}

    def _purge(self, db, row):
        db.execute('DELETE FROM files WHERE id=?', (row['id'],))
        if not db.execute('SELECT 1 FROM files WHERE owner_id=? AND sha=? AND size=?', (row['owner_id'], row['sha'], row['size'])).fetchone():
            db.execute('DELETE FROM objects WHERE owner_id=? AND sha=? AND size=?', (row['owner_id'], row['sha'], row['size']))
            self.object_path(row).unlink(missing_ok=True)
        (self.root / 'thumbs' / row['id']).unlink(missing_ok=True)
        export = self.root / 'exports' / row['id']
        if export.is_symlink():
            export.unlink()
        elif export.exists():
            shutil.rmtree(export)

    def thumbnail(self, identifier, body, mime, actor=OWNER):
        if len(body) > THUMB_LIMIT:
            raise ShareError(413, 'That thumbnail exceeds 512 KiB.')
        if mime not in ('image/jpeg', 'image/webp') or raster_type(body) != mime:
            raise ShareError(400, 'Send a JPEG or WebP thumbnail.')
        with self.connection(write=True) as db:
            self.row(db, identifier, actor)
            self._copy_bytes(self.root / 'thumbs' / identifier, body)
            db.execute('UPDATE files SET thumb_mime=? WHERE id=?', (mime, identifier))
            return self.public(self.row(db, identifier, actor))

    def _copy_bytes(self, final, body):
        temp = self.root / 'incoming' / (uuid.uuid4().hex + '.part')
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, final)
        finally:
            temp.unlink(missing_ok=True)

    def export(self, identifier, actor=OWNER):
        # Keep the catalog lock during the copy so purge cannot race the export.
        with self.connection(write=True) as db:
            row = self.row(db, identifier, actor)
            folder = self.root / 'exports' / row['id']
            self._directory(folder)
            final = folder / safe_name(row['name'])
            temp = self.root / 'incoming' / (uuid.uuid4().hex + '.part')
            try:
                with self.open_private(self.object_path(row)) as source:
                    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
                        os.fchmod(target.fileno(), 0o600)
                        shutil.copyfileobj(source, target, CHUNK)
                        target.flush()
                        os.fsync(target.fileno())
                os.replace(temp, final)
            finally:
                temp.unlink(missing_ok=True)
            return str(final)

    def reconcile(self):
        """Recover abandoned state. Active reservations survive another process's startup."""
        with self.connection(write=True) as db:
            cutoff = datetime.fromtimestamp(time.time() - TRASH_TTL, timezone.utc).isoformat()
            for row in db.execute('SELECT * FROM files WHERE deleted_at < ?', (cutoff,)).fetchall():
                self._purge(db, row)
            db.execute('DELETE FROM uploads WHERE expires < ?', (time.time(),))
            live_uploads = {row[0] + '.part' for row in db.execute('SELECT id FROM uploads')}
            for path in (self.root / 'incoming').iterdir():
                if path.name not in live_uploads and path.lstat().st_mtime < time.time() - UPLOAD_TTL:
                    path.unlink(missing_ok=True)
            for row in db.execute('SELECT * FROM files').fetchall():
                try:
                    with self.open_private(self.object_path(row)) as source:
                        present = os.fstat(source.fileno()).st_size == row['size']
                except (OSError, ShareError):
                    present = False
                if not present:
                    self._purge(db, row)
            db.execute('DELETE FROM objects WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.owner_id=objects.owner_id AND f.sha=objects.sha AND f.size=objects.size)')
            referenced = {str(self.object_path(row)) for row in db.execute('SELECT * FROM objects')}
            for parent, dirs, names in os.walk(self.root / 'objects', followlinks=False):
                for name in names:
                    path = Path(parent) / name
                    if str(path) not in referenced:
                        path.unlink(missing_ok=True)
            ids = {row[0] for row in db.execute('SELECT id FROM files')}
            for path in (self.root / 'thumbs').iterdir():
                if path.name not in ids:
                    path.unlink(missing_ok=True)
            for path in (self.root / 'exports').iterdir():
                if path.name not in ids:
                    if path.is_symlink() or not path.is_dir():
                        path.unlink(missing_ok=True)
                    else:
                        shutil.rmtree(path)
