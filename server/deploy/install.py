#!/usr/bin/env python3
"""Compatibility entry point for the root Tether installer."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    # The old installer applied immediately when no mode was supplied.
    if '--plan' not in sys.argv and '--apply' not in sys.argv:
        sys.argv.insert(1, '--apply')
    runpy.run_path(str(Path(__file__).resolve().parents[2] / 'install.py'), run_name='__main__')
