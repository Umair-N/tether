"""Local owner access to Share. No HTTP credentials or terminal writes."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import stat
import sys

from share_store import ShareError, Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    put = commands.add_parser('put')
    put.add_argument('path')
    put.add_argument('--name')
    listing = commands.add_parser('list')
    listing.add_argument('--kind', default='all')
    listing.add_argument('--limit', type=int, default=60)
    for command in ('stat', 'path'):
        commands.add_parser(command).add_argument('id')
    args = parser.parse_args(argv)
    try:
        store = Store()
        if args.command == 'put':
            path = Path(args.path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ShareError(400, 'Choose a regular file.')
                result = {'file': store.put(source, info.st_size, args.name or path.name, mimetypes.guess_type(path.name)[0] or '')}
        elif args.command == 'list':
            result = store.list(kind=args.kind, limit=args.limit)
        elif args.command == 'stat':
            result = {'file': store.stat(args.id)}
        else:
            result = {'path': store.export(args.id)}
    except (ShareError, OSError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, ShareError) else 'Share could not access that file.'
        print(json.dumps({'error': message}, separators=(',', ':')))
        return 1
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
