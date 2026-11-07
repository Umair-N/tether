"""Validated screen patterns, with per-file last-good fallback and fail-closed access."""
import json
import os
import re
import sys
from pathlib import Path

import paths

VERSION = 1
DIRECTORY = Path(__file__).with_name('detection')
FILES = ('common', 'claude', 'codex', 'antigravity')
NEVER = re.compile(r'(?!)')
FLAGS = {'IGNORECASE': re.IGNORECASE, 'MULTILINE': re.MULTILINE, 'DOTALL': re.DOTALL}


class RulesError(ValueError):
    """A rules file or pattern is invalid."""


class _Pairs(list):
    """Preserve object keys until their file and pattern context is known."""


def _unique(value, path, trail=()):
    if isinstance(value, _Pairs):
        result = {}
        for key, item in value:
            if key in result:
                context = f' pattern {trail[1]!r}' if len(trail) >= 2 and trail[0] == 'patterns' else ''
                raise RulesError(f'{path}{context}: duplicate JSON key {key!r}')
            result[key] = _unique(item, path, (*trail, key))
        return result
    if isinstance(value, list):
        return [_unique(item, path, trail) for item in value]
    return value


def _load_file(name, directory):
    path = Path(directory) / f'{name}.json'
    try:
        raw = path.read_bytes()
        data = _unique(json.loads(raw.decode('utf-8'), object_pairs_hook=_Pairs), path)
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError) as error:
        raise RulesError(f'{path}: {error}') from error
    if not isinstance(data, dict):
        raise RulesError(f'{path}: expected an object')
    if type(data.get('version')) is not int or data['version'] != VERSION:
        raise RulesError(f'{path}: version must be {VERSION}')
    entries = data.get('patterns')
    if not isinstance(entries, dict) or not entries:
        raise RulesError(f'{path}: patterns must be a non-empty object')
    compiled = {}
    for name, entry in entries.items():
        context = f'{path} pattern {name!r}'
        if not isinstance(entry, dict):
            raise RulesError(f'{context}: expected an object')
        if entry.keys() - {'regex', 'flags', 'comment'}:
            raise RulesError(f'{context}: unknown entry keys')
        if not isinstance(entry.get('regex'), str):
            raise RulesError(f'{context}: regex must be a string')
        if not isinstance(entry.get('comment'), str) or not entry['comment'].strip():
            raise RulesError(f'{context}: comment must be a non-blank string')
        flags = entry.get('flags', [])
        if not isinstance(flags, list) or any(not isinstance(flag, str) or flag not in FLAGS for flag in flags):
            raise RulesError(f'{context}: flags must be a list of known names')
        combined = 0
        for flag in flags:
            combined |= FLAGS[flag]
        try:
            compiled[name] = re.compile(entry['regex'], combined)
        except (re.error, RecursionError, OverflowError) as error:
            raise RulesError(f'{context}: {error}') from error
    return compiled, raw


def load_file(name, directory=DIRECTORY) -> dict:
    """Load and compile one strict rules file."""
    return _load_file(name, directory)[0]


def load_all(directory=DIRECTORY) -> dict:
    return {name: load_file(name, directory) for name in FILES}


class Rules:
    def __init__(self, directory=DIRECTORY, snapshot=None, log=None):
        self.directory = Path(directory)
        self.snapshot = Path(snapshot) if snapshot is not None else None
        self.log = log if log is not None else lambda message: print(message, file=sys.stderr, flush=True)
        self.errors = []
        self._files = {}
        self._raw = {}
        self._snapshots = {}
        self._bad = set()
        self._unsafe = set()
        for name in FILES:
            try:
                self._files[name], self._raw[name] = _load_file(name, self.directory)
            except RulesError as error:
                self._error(str(error))
                self._unsafe.add(name)
                self._files[name] = self._snapshot(name)
                if self._files[name]:
                    self._log(f'{self.directory / (name + ".json")}: last good rules are in use')
                else:
                    self._bad.add(name)

    def _log(self, message):
        # Errors can contain terminal rows or newlines from invalid data.
        try:
            self.log(' '.join(message.splitlines()))
        except Exception:
            pass  # Logging must not break prompt guards.

    def _error(self, message):
        self.errors.append(message)
        self._log(message)

    def _snapshot(self, file):
        if file not in self._snapshots:
            self._snapshots[file] = {}
            if self.snapshot is not None:
                try:
                    self._snapshots[file] = load_file(file, self.snapshot)
                except RulesError as error:
                    self._error(str(error))
        return self._snapshots[file]

    def pattern(self, file, name, groups=0, names=()):
        def valid(pattern):
            return pattern is not None and pattern.groups >= groups and all(key in pattern.groupindex for key in names)

        found = self._files.get(file, {}).get(name)
        if valid(found):
            return found
        self._unsafe.add(file)
        self._error(f'{self.directory / (file + ".json")} pattern {name!r}: missing pattern or required groups')
        found = self._snapshot(file).get(name)
        if valid(found):
            self._log(f'{self.directory / (file + ".json")} pattern {name!r}: last good rules are in use')
            return found
        self._bad.add(file)
        return NEVER

    def available(self, agent) -> bool:
        return 'common' not in self._bad and agent not in self._bad

    def save_last_good(self):
        if self.snapshot is None:
            return
        for name, raw in self._raw.items():
            if name in self._unsafe:
                continue
            target = self.snapshot / f'{name}.json'
            temporary = None
            try:
                self.snapshot.mkdir(parents=True, exist_ok=True)
                try:
                    if target.read_bytes() == raw:
                        continue
                except FileNotFoundError:
                    pass
                temporary = self.snapshot / f'.{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp'
                with temporary.open('xb') as stream:
                    stream.write(raw)
                os.replace(temporary, target)
            except OSError as error:
                self._log(f'{target}: {error}')
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError as error:
                        self._log(f'{temporary}: {error}')


_RULES = Rules(snapshot=paths.CACHE / 'detection-rules')
pattern = _RULES.pattern
available = _RULES.available
save_last_good = _RULES.save_last_good
