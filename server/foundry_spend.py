"""Azure Foundry spend this month, for the Codex provider switch.

Two sources:
- estimate: tokens from this machine's Codex logs (~/.codex/sessions) for runs on the Foundry provider, priced with Microsoft's public
  Retail Prices API (Global Standard). It is live, but it counts runs on this machine only.
- bill: the real month-to-date cost from Azure Cost Management, when ~/.config/tether/azure-cost.json gives a read-only
  service principal or Azure CLI sign-in. Azure updates it a few times a day. The file must be mode 600;
  Tether never sends or prints the secret.
"""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paths

PROVIDER = 'azure'
BUDGET = 4000.0
LONG_CONTEXT = 272_000  # Input tokens per request above which the long-context price applies (assumed, see notes).
PRICES_URL = 'https://prices.azure.com/api/retail/prices'
PRICES_FILE = paths.CACHE / 'foundry_prices.json'
BILL_CONFIG = paths.HOME / '.config/tether/azure-cost.json'
COST_API = '2026-06-01'
_lock = threading.Lock()
_files = {}  # path -> (mtime, size, events)
_bill = {}   # cached bill answer and its time


def _month_start(now):
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _events(path):
    """[(time, long, input, cached, cache_write, output, model)] for a Foundry log, or [] for other providers.

    Model is None before the first turn_context.
    """
    events, previous, model = [], None, None
    with open(path, 'rb') as handle:
        first = handle.readline()
        try:
            meta = json.loads(first).get('payload') or {}
        except ValueError:
            return []
        if meta.get('model_provider') != PROVIDER:
            return []
        for line in handle:
            if b'"token_count"' not in line and b'"turn_context"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get('type') == 'turn_context':
                model = (entry.get('payload') or {}).get('model')
                continue
            info = (entry.get('payload') or {}).get('info') or {}
            total, last = info.get('total_token_usage'), info.get('last_token_usage') or {}
            if not isinstance(total, dict):
                continue
            keys = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
            now = [int(total.get(key) or 0) for key in keys]
            # Codex can repeat a token_count; only growth of the running total is new use.
            delta = now if previous is None else [a - b for a, b in zip(now, previous)]
            previous = now
            if not any(value > 0 for value in delta):
                continue
            events.append((entry.get('timestamp') or '', int(last.get('input_tokens') or 0) > LONG_CONTEXT, *delta, model))
    return events


def _logs(now, root):
    """Session logs that may hold use this month: this month's folder, and last month's files that changed this month."""
    start = _month_start(now)
    before = start - dt.timedelta(days=1)
    found = list((root / f'{start:%Y/%m}').rglob('*.jsonl'))
    for path in (root / f'{before:%Y/%m}').rglob('*.jsonl'):
        try:
            if path.stat().st_mtime >= start.timestamp():
                found.append(path)
        except OSError:
            pass
    return found


def usage(now=None, root=None):
    """Foundry tokens this month and today, split by price tier and model."""
    now = now or dt.datetime.now(dt.timezone.utc)
    root = Path(root or paths.CODEX / 'sessions')
    month, today = f'{now:%Y-%m}', f'{now:%Y-%m-%d}'
    blank = lambda: {'input': 0, 'cached': 0, 'cache_write': 0, 'output': 0, 'requests': 0}
    windows = lambda: {'month': {'short': blank(), 'long': blank()}, 'today': {'short': blank(), 'long': blank()}}
    out = {**windows(), 'sessions': 0, 'by_model': {}}
    with _lock:
        for path in _logs(now, root):
            try:
                stat = path.stat()
                key = str(path)
                if key not in _files or _files[key][:2] != (stat.st_mtime, stat.st_size):
                    _files[key] = (stat.st_mtime, stat.st_size, _events(path))
                events = _files[key][2]
            except OSError:
                continue
            counted = False
            for stamp, long, input_tokens, cached, cache_write, output, model in events:
                if not stamp.startswith(month):
                    continue
                counted = True
                if model not in out['by_model']:
                    out['by_model'][model] = windows()
                for window in ('month', 'today') if stamp.startswith(today) else ('month',):
                    for totals in (out, out['by_model'][model]):
                        tier = totals[window]['long' if long else 'short']
                        # input_tokens includes cached and cache-write tokens; each is priced on its own.
                        tier['input'] += max(0, input_tokens - cached - cache_write)
                        tier['cached'] += cached
                        tier['cache_write'] += cache_write
                        tier['output'] += output
                        tier['requests'] += 1
            out['sessions'] += counted
    return out


def _fetch_json(url, data=None, headers=None, timeout=20):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def prices(model, fetch=_fetch_json, now=None):
    """USD per 1M tokens for `model` at Global Standard: {'short': {...}, 'long': {...}}, cached for a day. None if unknown."""
    now = time.time() if now is None else now
    name = (model or '').removeprefix('gpt-')
    try:
        saved = json.loads(PRICES_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    models = saved.get('models') or {}
    if saved.get('model') and saved.get('prices'):
        models[f"gpt-{saved['model']}"] = {'at': saved.get('at', 0), 'prices': saved['prices']}
    cached = models.get(model) or {}
    if now - cached.get('at', 0) < 86400 and cached.get('prices'):
        return cached['prices']
    try:
        query = urllib.parse.urlencode({'$filter': f"serviceName eq 'Foundry Models' and armRegionName eq 'eastus2' and contains(skuName, '{name} ')"})
        items = fetch(f'{PRICES_URL}?{query}').get('Items') or []
    except (OSError, ValueError):
        return cached.get('prices')
    meters = {'Inp': 'input', 'Cd Inp': 'cached', 'Cd Wr': 'cache_write', 'Opt': 'output'}
    table = {'short': {}, 'long': {}}
    for item in items:
        sku = item.get('skuName') or ''
        if not sku.startswith(f'{name} ') or not sku.endswith(' Std Gl') or item.get('unitOfMeasure') != '1M':
            continue
        middle = sku[len(name) + 1:-len(' Std Gl')]
        tier, _, meter = middle.partition(' ')
        if tier in ('ShortCo', 'LongCo') and meter in meters:
            table['short' if tier == 'ShortCo' else 'long'][meters[meter]] = float(item['retailPrice'])
    if any(len(table[tier]) < 4 for tier in table):
        return cached.get('prices')
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    models[model] = {'at': now, 'prices': table}
    PRICES_FILE.write_text(json.dumps({'models': models}))
    return table


def cost(tokens, table):
    """USD for one usage window."""
    return sum(tokens[tier][kind] * table[tier][kind] / 1e6 for tier in ('short', 'long') for kind in ('input', 'cached', 'cache_write', 'output'))


def _bill_config():
    path = BILL_CONFIG
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, None
    if info.st_mode & 0o077:
        return None, f'{path.name} must be readable by you only. Run: chmod 600 {path}'
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, f'{path.name} is not valid JSON.'
    if config.get('auth') == 'az':
        if not isinstance(config.get('scope'), str) or not config['scope']:
            return None, f'{path.name} needs scope.'
        return config, None
    need = ('tenant_id', 'client_id', 'client_secret', 'scope')
    if not all(isinstance(config.get(key), str) and config[key] for key in need):
        return None, f'{path.name} needs tenant_id, client_id, client_secret and scope.'
    return config, None


class _NoToken(Exception):
    """The az CLI gave no token; the message is the hint for the user."""


def bill(fetch=_fetch_json, now=None, run=subprocess.run):
    """The real month-to-date cost from Azure Cost Management, cached (Azure limits query calls)."""
    now = now or time.time()
    config, problem = _bill_config()
    if problem:
        return {'state': 'error', 'hint': problem}
    if not config:
        return {'state': 'not-set-up'}
    auth = 'az' if config.get('auth') == 'az' else 'service-principal'
    with _lock:
        # Keep a good answer for an hour; retry an error after 5 minutes, so a fresh az login shows soon.
        ttl = 3600 if _bill.get('answer', {}).get('state') == 'ok' else 300
        if (_bill.get('at', 0) > now - ttl and _bill.get('scope') == config['scope']
                and _bill.get('auth') == auth):
            return _bill['answer']
    try:
        if auth == 'az':
            az = shutil.which('az')
            if not az:
                fallback = paths.HOME / '.local/bin/az'
                az = str(fallback) if fallback.is_file() else None
            if not az:
                raise _NoToken('Install the Azure CLI on this server, or use a service principal.')
            expired = 'The Azure CLI sign-in has expired. Run az login on this server.'
            try:
                result = run([az, 'account', 'get-access-token', '--resource', 'https://management.azure.com/', '--output', 'json'],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
                token = None if result.returncode else json.loads(result.stdout).get('accessToken')
            except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
                token = None
            if not token:
                raise _NoToken(expired)
        else:
            token = fetch(f"https://login.microsoftonline.com/{urllib.parse.quote(config['tenant_id'])}/oauth2/v2.0/token",
                          data=urllib.parse.urlencode({'grant_type': 'client_credentials', 'client_id': config['client_id'],
                                                       'client_secret': config['client_secret'], 'scope': 'https://management.azure.com/.default'}).encode())['access_token']
        body = {'type': 'ActualCost', 'timeframe': 'MonthToDate',
                'dataset': {'granularity': 'None', 'aggregation': {'totalCost': {'name': 'Cost', 'function': 'Sum'}},
                            'filter': {'dimensions': {'name': 'ServiceName', 'operator': 'In', 'values': ['Foundry Models']}}}}
        scope = config['scope'].strip('/')
        result = fetch(f'https://management.azure.com/{scope}/providers/Microsoft.CostManagement/query?api-version={COST_API}',
                       data=json.dumps(body).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        columns = [column.get('name') for column in result['properties']['columns']]
        rows = result['properties']['rows']
        total = sum(float(row[columns.index('Cost')]) for row in rows)
        currency = rows[0][columns.index('Currency')] if rows and 'Currency' in columns else 'USD'
        answer = {'state': 'ok', 'usd': round(total, 2), 'currency': currency, 'checked': int(now)}
    except _NoToken as failure:
        answer = {'state': 'error', 'hint': str(failure)}
    except urllib.error.HTTPError as failure:
        hint = ('Check the signed-in account can read costs on the scope.' if auth == 'az'
                else 'Check the service principal has Cost Management Reader on the scope.')
        answer = {'state': 'error', 'hint': f'Azure answered {failure.code}. {hint}'}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        answer = {'state': 'error', 'hint': 'Could not read the Azure bill. Try again later.'}
    with _lock:
        _bill.update(at=now, scope=config['scope'], auth=auth, answer=answer)
    return answer


def report(model):
    now = dt.datetime.now(dt.timezone.utc)
    tokens = usage(now)
    table = prices(model)
    by_model = {}
    for name, windows in tokens['by_model'].items():
        name = model if name is None else name
        if name not in by_model:
            by_model[name] = {window: {tier: dict(bucket) for tier, bucket in tiers.items()}
                              for window, tiers in windows.items()}
        else:
            for window, tiers in windows.items():
                for tier, bucket in tiers.items():
                    for kind, value in bucket.items():
                        by_model[name][window][tier][kind] += value
    tables = {model: table}
    models, month_usd, today_usd, known = [], 0.0, 0.0, True
    for name, windows in by_model.items():
        if name not in tables:
            tables[name] = prices(name)
        model_table = tables[name]
        usd = cost(windows['month'], model_table) if model_table else None
        if usd is None:
            known = False
        else:
            month_usd += usd
            today_usd += cost(windows['today'], model_table)
        models.append({'model': name, 'usd': round(usd, 2) if usd is not None else None, 'tokens': windows['month']})
    models.sort(key=lambda row: (row['usd'] is not None, row['usd'] or 0), reverse=True)
    estimate = {'tokens': tokens['month'], 'today_tokens': tokens['today'], 'sessions': tokens['sessions'], 'prices': table,
                'usd': round(month_usd, 2) if known else None,
                'today_usd': round(today_usd, 2) if known else None, 'models': models}
    return {'month': f'{now:%B %Y}', 'budget': BUDGET, 'estimate': estimate, 'bill': bill()}
