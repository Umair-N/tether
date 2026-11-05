"""Linux process information and the raw readers used by injectable process scans."""
import os
from pathlib import Path
import time

ROOT = '/proc'


def read_text(path):
    return Path(path).read_text()


def read_bytes(path):
    return Path(path).read_bytes()


def list_dir(path):
    return os.listdir(path)


def read_link(path):
    return os.readlink(path)


def uid_of(path):
    return os.stat(path).st_uid


def pids(proc=Path('/proc')):
    return [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]


def stat_fields(pid, proc=Path('/proc')):
    return (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()


def stat_exists(pid, proc=Path('/proc')):
    return (proc / str(pid) / 'stat').is_file()


def parent(pid, proc=Path('/proc')):
    return int(stat_fields(pid, proc)[1])


def start_ticks(pid, proc=Path('/proc')):
    return stat_fields(pid, proc)[19]


def uptime(proc=Path('/proc')):
    return float((proc / 'uptime').read_text().split()[0])


def start_ms(ticks, proc=Path('/proc'), now=None):
    now = time.time() if now is None else now
    boot = now - uptime(proc)
    return int((boot + int(ticks) / os.sysconf('SC_CLK_TCK')) * 1000)


def comm(pid, proc=Path('/proc')):
    return (proc / str(pid) / 'comm').read_text().strip()


def argv(pid, proc=Path('/proc')):
    return (proc / str(pid) / 'cmdline').read_bytes().split(b'\0')


def environ(pid, proc=Path('/proc')):
    return (proc / str(pid) / 'environ').read_bytes().split(b'\0')


def uid(pid, proc=Path('/proc')):
    return uid_of(proc / str(pid))


def fd_names(pid, proc=Path('/proc')):
    return [entry.name for entry in (proc / str(pid) / 'fd').iterdir()]


def fd_targets(pid, proc=Path('/proc')):
    targets = []
    for name in fd_names(pid, proc):
        try:
            targets.append((name, fd_target(pid, name, proc)))
        except OSError:
            continue
    return targets


def resolved_fds(pid, proc=Path('/proc')):
    # Resolve the fd path itself: relative, broken and disappearing links keep
    # Path.resolve's semantics. Yield so callers can retain partial matches.
    for entry in (proc / str(pid) / 'fd').iterdir():
        yield entry.resolve()


def fd_target(pid, fd, proc=Path('/proc'), *, readlink=None):
    return (os.readlink if readlink is None else readlink)(proc / str(pid) / 'fd' / str(fd))


def cwd(pid, proc=Path('/proc'), *, readlink=None):
    return (os.readlink if readlink is None else readlink)(proc / str(pid) / 'cwd')


def tcp_socket_inode(local_port, remote_port, uid, proc=Path('/proc')):
    for table in ('tcp', 'tcp6'):
        try:
            rows = (proc / 'net' / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == '01':
                local = int(fields[1].rsplit(':', 1)[1], 16)
                remote = int(fields[2].rsplit(':', 1)[1], 16)
                if local == local_port and remote == remote_port and int(fields[7]) == uid:
                    return fields[9]
    return None


def pid_with_socket(inode, uid, proc=Path('/proc')):
    for pid in pids(proc):
        try:
            if uid_of(proc / str(pid)) != uid:
                continue
            for _, target in fd_targets(pid, proc):
                if target == 'socket:[%s]' % inode:
                    return pid
        except OSError:
            continue
    return None


def foreground_differs(pid, proc=Path('/proc')):
    fields = stat_fields(pid, proc)
    return fields[2] != fields[5]
