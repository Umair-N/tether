"""On-demand host and cgroup v2 samples. No privileged container API."""
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import threading
import time
from urllib.request import ProxyHandler, HTTPRedirectHandler, build_opener

import procinfo

GLANCES_URL = 'http://127.0.0.1:61208'


def enabled():
    return os.environ.get('TETHER_RESOURCES') == '1'


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def number(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def fields(path):
    return {key.rstrip(':'): value for line in read(path).splitlines() if len(line.split(None, 1)) == 2
            for key, value in [line.split(None, 1)]}


def _vm_stat_used(text):
    page_size = re.search(r'^Mach Virtual Memory Statistics: \(page size of (\d+) bytes\)', text)
    if not page_size:
        return None
    pages = dict(re.findall(r'^Pages (.+?):\s+(\d+)\.$', text, re.M))
    names = ('active', 'wired down', 'occupied by compressor')
    if not all(name in pages for name in names):
        return None
    return sum(int(pages[name]) for name in names) * int(page_size.group(1))


def _command_text(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=2)
        return result.stdout if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _darwin_host():
    try:
        load = list(os.getloadavg())
    except OSError:
        load = []
    try:
        uptime = procinfo.uptime()
    except (OSError, ValueError, subprocess.SubprocessError):
        uptime = None
    return dict(cpu_pct=None, mem_total=number(_command_text(['sysctl', '-n', 'hw.memsize'])),
                mem_bytes=_vm_stat_used(_command_text(['vm_stat'])), load=load, uptime=uptime)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def working_set(path):
    """Memory the group really holds: memory.current minus the inactive file cache the kernel frees on demand.
    docker stats and the kubelet count it the same way. Without that, cached files make a group look larger than the host's used memory."""
    current = number(read(path / 'memory.current'))
    if current is None:
        return None
    return max(0, current - (number(fields(path / 'memory.stat').get('inactive_file')) or 0))


def glances():
    # An environment override cannot redirect requests to another host, port or service.
    if os.environ.get('TETHER_GLANCES_URL', GLANCES_URL) != GLANCES_URL:
        return {}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def get(endpoint):
        with opener.open(GLANCES_URL + '/api/4/' + endpoint, timeout=1) as response:
            return json.load(response)
    try:
        get('status')
    except (OSError, ValueError):
        return {}
    result = {'glances': True, 'processes': []}
    try:
        processes = get('processlist')
        if isinstance(processes, list):
            valid = [p for p in processes if isinstance(p, dict) and isinstance(p.get('cpu_percent'), (int, float))]
            result['processes'] = [dict(pid=p.get('pid'), name=str(p.get('name', '')), cpu_pct=p['cpu_percent'])
                                   for p in sorted(valid, key=lambda p: p['cpu_percent'], reverse=True)[:5]]
    except (OSError, ValueError, TypeError):
        pass
    return result


class Sampler:
    def __init__(self, root=None, proc='/proc', clock=time.monotonic):
        self.root = Path(root or os.environ.get('TETHER_CGROUP_ROOT', '/sys/fs/cgroup'))
        self.proc = Path(proc)
        self.clock = clock
        self.previous = {}
        self.host_previous = None
        self.cached = None
        self.cached_at = 0
        self.lock = threading.Lock()

    def payload(self):
        if not enabled():
            return None
        with self.lock:
            now = self.clock()
            if self.cached is not None and now - self.cached_at < 5:
                return self.cached
            self.cached = dict(host=self.host(), groups=self.groups(), btop=bool(shutil.which('btop')), **glances())
            self.cached_at = now
            return self.cached

    def host(self):
        metrics = _darwin_host() if procinfo.DARWIN else self._linux_host()
        disk_used = disk_total = None
        try:
            disk = os.statvfs('/')
            disk_total = disk.f_blocks * disk.f_frsize
            disk_used = (disk.f_blocks - disk.f_bfree) * disk.f_frsize
        except OSError:
            pass
        return dict(cpus=os.cpu_count() or 1, disk_bytes=disk_used, disk_total=disk_total, **metrics)

    def _linux_host(self):
        cpu = None
        line = read(self.proc / 'stat').splitlines()
        sampled_at = self.clock()
        if line and line[0].startswith('cpu '):
            values = [number(v) for v in line[0].split()[1:9]]  # guest time already counts in user/nice
            if len(values) >= 4 and all(v is not None for v in values):
                total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
                if self.host_previous:
                    _, old_total, old_idle = self.host_previous
                    if total > old_total and idle >= old_idle:
                        cpu = round(max(0, min(100, 100 * (1 - (idle - old_idle) / (total - old_total)))), 1)
                self.host_previous = (sampled_at, total, idle)
        memory = fields(self.proc / 'meminfo')
        total = number(memory.get('MemTotal', '').split(' ')[0])
        available = number(memory.get('MemAvailable', '').split(' ')[0])
        try:
            load = [float(v) for v in read(self.proc / 'loadavg').split()[:3]]
            uptime = float(read(self.proc / 'uptime').split()[0])
        except (ValueError, IndexError):
            load, uptime = [], None
        return dict(cpu_pct=cpu,
                    mem_bytes=(max(0, total - available) * 1024 if total is not None and available is not None else None),
                    mem_total=total * 1024 if total is not None else None, load=load,
                    uptime=uptime)

    def groups(self):
        if not (self.root / 'cgroup.controllers').exists():
            self.previous = {}
            return []
        candidates = [(p, 'lxc', p.name.removeprefix('lxc.payload.')) for p in self.root.glob('lxc.payload.*') if p.is_dir()]
        docker = [(p, 'docker', m.group(1)) for p in (self.root / 'system.slice').glob('docker-*.scope')
                  if p.is_dir() and (m := re.fullmatch(r'docker-([a-f0-9]+)\.scope', p.name))]
        names = {}
        if docker:
            try:
                result = subprocess.run(['docker', 'ps', '--format', '{{.ID}}\t{{.Names}}'], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    names = dict(line.split('\t', 1) for line in result.stdout.splitlines() if '\t' in line)
            except (OSError, subprocess.TimeoutExpired):
                pass
        candidates += [(p, kind, next((name for short, name in names.items() if ident.startswith(short)), ident[:12])) for p, kind, ident in docker]
        for p in (self.root / 'user.slice').glob('user-*.slice'):
            match = re.fullmatch(r'user-(\d+)\.slice', p.name)
            if not p.is_dir() or not match:
                continue
            uid = int(match.group(1))
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            candidates.append((p, 'user', name))
        samples, groups = {}, []
        for path, kind, name in candidates:
            usage = number(fields(path / 'cpu.stat').get('usage_usec'))
            now = self.clock()
            cpu = None
            old = self.previous.get(str(path))
            if usage is not None:
                if old and now > old[0] and usage >= old[1]:
                    cpu = round((usage - old[1]) / ((now - old[0]) * 10000), 1)
                samples[str(path)] = (now, usage)
            limit = read(path / 'cpu.max').split()
            cpu_limit = None
            if len(limit) == 2 and number(limit[0]) is not None and number(limit[1]) and int(limit[1]) > 0:
                cpu_limit = int(limit[0]) / int(limit[1])
            io = {'rbytes': 0, 'wbytes': 0}
            for line in read(path / 'io.stat').splitlines():
                for part in line.split()[1:]:
                    key, _, value = part.partition('=')
                    if key in io and number(value) is not None:
                        io[key] += int(value)
            groups.append(dict(kind=kind, name=name, cpu_pct=cpu, cpu_limit=cpu_limit,
                               mem_bytes=working_set(path), mem_limit=number(read(path / 'memory.max')),
                               io_read=io['rbytes'], io_write=io['wbytes'], pids=number(read(path / 'pids.current'))))
        self.previous = samples
        return groups


SAMPLER = Sampler()


def payload():
    return SAMPLER.payload()
