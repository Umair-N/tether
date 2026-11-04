#!/usr/bin/env python3
"""Prepare a verified Tether release. Does not install services or change agent settings."""
import argparse
import fcntl
import filecmp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen


def version(value):
    if not re.fullmatch(r'v?\d+\.\d+\.\d+(?:-rc\.?\d+)?', str(value)):
        raise ValueError('Invalid release version')
    return str(value).removeprefix('v')


def switch(link, target):
    if link.exists() and not link.is_symlink():
        raise ValueError('Refusing to replace a non-symlink current path')
    temp = link.with_name(link.name + '.new')
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    temp.replace(link)


def extract(archive, destination):
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe tar member: ' + member.name)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)


def download(url, destination):
    with urlopen(url, timeout=30) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)


def verify(archive, sums, name):
    expected = [line.split()[0] for line in sums.read_text().splitlines()
                if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for chunk in iter(lambda: source.read(262144), b''):
            digest.update(chunk)
    if expected != [digest.hexdigest()]:
        raise ValueError('Checksum mismatch; install refused')


def release_files(release, directory):
    ver = version(release['tag_name'])
    name = f'tether-{ver}.tar.gz'
    assets = {item['name']: item['browser_download_url'] for item in release.get('assets', [])}
    if name not in assets or 'SHA256SUMS' not in assets:
        raise ValueError('Release lacks the tarball or SHA256SUMS')
    archive, sums = directory / name, directory / 'SHA256SUMS'
    download(assets[name], archive)
    download(assets['SHA256SUMS'], sums)
    verify(archive, sums, name)
    return archive, ver


def fetch_release(tag=None):
    repo = os.environ.get('TETHER_REPO', 'incmak/tether')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid TETHER_REPO')
    endpoint = f'https://api.github.com/repos/{repo}/releases/' + ('tags/' + tag if tag else 'latest')
    with urlopen(Request(endpoint, headers={'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'Tether-updater'}), timeout=10) as response:
        release = json.load(response)
    version(release['tag_name'])
    return release


def stage(args, root, reuse=False):
    if bool(args.archive) != bool(args.sums):
        raise ValueError('--archive and --sums are required together')
    tag = 'v' + version(args.to) if args.to else None
    with tempfile.TemporaryDirectory(prefix='tether-install-') as temp:
        directory = Path(temp)
        if args.archive:
            archive = Path(args.archive)
            match = re.fullmatch(r'tether-(.+)\.tar\.gz', archive.name)
            ver = version(match[1]) if match else version('invalid')
            if tag and ver != tag[1:]:
                raise ValueError('Archive version differs from --to')
            verify(archive, Path(args.sums), f'tether-{ver}.tar.gz')
        else:
            archive, ver = release_files(fetch_release(tag), directory)
            if tag and ver != tag[1:]:
                raise ValueError('Release version differs from --to')
        tree = directory / 'tree'
        tree.mkdir()
        extract(archive, tree)
        if ((tree / 'VERSION').read_text().strip() != ver or
                not all((tree / item).is_file() for item in ('install.py', 'tether', 'ui/dist/index.html'))):
            raise ValueError('Invalid release contents')
        releases = root / 'releases'
        releases.mkdir(parents=True, exist_ok=True)
        target = releases / ver
        partial = releases / f'.{ver}.partial'
        if target.exists() or target.is_symlink():
            # Migration may use the exact archive that bootstrap just prepared.
            expected = {p.relative_to(tree) for p in tree.rglob('*')}
            actual = {p.relative_to(target) for p in target.rglob('*') if '__pycache__' not in p.relative_to(target).parts}
            if (not reuse or target.is_symlink() or expected != actual or
                    any(p.is_symlink() for p in target.rglob('*')) or
                    any(not filecmp.cmp(tree / p, target / p, shallow=False) or
                        (tree / p).stat().st_mode & 0o777 != (target / p).stat().st_mode & 0o777
                        for p in expected if (tree / p).is_file())):
                raise ValueError('Release directory already exists or differs from the verified archive')
            return target
        if partial.exists() or partial.is_symlink():
            raise ValueError('Partial release directory already exists')
        try:
            shutil.copytree(tree, partial)
            partial.rename(target)
        finally:
            if partial.is_dir():
                shutil.rmtree(partial)
        return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sums', type=Path)
    parser.add_argument('--to')
    args = parser.parse_args(argv)
    root = Path.home() / '.local/share/tether'
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'update.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (root / 'current').exists() or (root / 'current').is_symlink():
                raise ValueError('current already exists; use current/tether update or migrate')
            switch(root / 'current', stage(args, root))
        print('Release ready. Review current/install.py --plan --json, then apply with --no-build-ui.')
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
