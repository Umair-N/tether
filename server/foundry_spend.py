"""Azure Foundry spend this month, for the Codex provider switch.

Two sources:
- estimate: tokens from this machine's Codex logs (~/.codex/sessions) for runs on the Foundry provider, priced with Microsoft's public
  Retail Prices API (Global Standard). It is live, but it counts runs on this machine only.
- bill: the real month-to-date cost from Azure Cost Management, when ~/.config/tether/azure-cost.json gives a read-only
  service principal. Azure updates it a few times a day. The file must be mode 600; Tether never sends or prints the secret.
"""
import datetime as dt
import json
import os
from pathlib import Path
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
    """[(time, long, input, cached, cache_write, output)] for one Foundry session log, or [] for any other provider."""
    events, previous = [], None
    with open(path, 'rb') as handle:
        first = handle.readline()
        try:
            meta = json.loads(first).get('payload') or {}
        except ValueError:
            return []
        if meta.get('model_provider') != PROVIDER:
            return []
        for line in handle:
            if b'"token_count"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
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
            events.append((entry.get('timestamp') or '', int(last.get('input_tokens') or 0) > LONG_CONTEXT, *delta))
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
    """Foundry tokens this month and today, split by price tier."""
    now = now or dt.datetime.now(dt.timezone.utc)
    root = Path(root or paths.CODEX / 'sessions')
    month, today = f'{now:%Y-%m}', f'{now:%Y-%m-%d}'
    blank = lambda: {'input': 0, 'cached': 0, 'cache_write': 0, 'output': 0, 'requests': 0}
    out = {'month': {'short': blank(), 'long': blank()}, 'today': {'short': blank(), 'long': blank()}, 'sessions': 0}
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
            for stamp, long, input_tokens, cached, cache_write, output in events:
                if not stamp.startswith(month):
                    continue
                counted = True
                for window in ('month', 'today') if stamp.startswith(today) else ('month',):
                    tier = out[window]['long' if long else 'short']
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
    now = now or time.time()
    name = (model or '').removeprefix('gpt-')
    try:
        saved = json.loads(PRICES_FILE.read_text())
        if saved.get('model') == name and now - saved.get('at', 0) < 86400:
            return saved['prices']
    except (OSError, ValueError, KeyError):
        saved = None
    try:
        query = urllib.parse.urlencode({'$filter': f"serviceName eq 'Foundry Models' and armRegionName eq 'eastus2' and contains(skuName, '{name} ')"})
        items = fetch(f'{PRICES_URL}?{query}').get('Items') or []
    except (OSError, ValueError):
        return saved['prices'] if saved else None
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
        return saved['prices'] if saved else None
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICES_FILE.write_text(json.dumps({'model': name, 'at': now, 'prices': table}))
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
    need = ('tenant_id', 'client_id', 'client_secret', 'scope')
    if not all(isinstance(config.get(key), str) and config[key] for key in need):
        return None, f'{path.name} needs tenant_id, client_id, client_secret and scope.'
    return config, None


def bill(fetch=_fetch_json, now=None):
    """The real month-to-date cost from Azure Cost Management, cached for an hour (Azure limits query calls)."""
    now = now or time.time()
    config, problem = _bill_config()
    if problem:
        return {'state': 'error', 'hint': problem}
    if not config:
        return {'state': 'not-set-up'}
    with _lock:
        if _bill.get('at', 0) > now - 3600 and _bill.get('scope') == config['scope']:
            return _bill['answer']
    try:
        token = fetch(f"https://login.microsoftonline.com/{urllib.parse.quote(config['tenant_id'])}/oauth2/v2.0/token",
                      data=urllib.parse.urlencode({'grant_type': 'client_credentials', 'client_id': config['client_id'],
                                                   'client_secret': config['client_secret'], 'scope': 'https://management.azure.com/.default'}).encode())['access_token']
        body = {'type': 'ActualCost', 'timeframe': 'MonthToDate',
                'dataset': {'granularity': 'None', 'aggregation': {'totalCost': {'name': 'Cost', 'function': 'Sum'}}}}
        scope = config['scope'].strip('/')
        result = fetch(f'https://management.azure.com/{scope}/providers/Microsoft.CostManagement/query?api-version={COST_API}',
                       data=json.dumps(body).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        columns = [column.get('name') for column in result['properties']['columns']]
        rows = result['properties']['rows']
        total = sum(float(row[columns.index('Cost')]) for row in rows)
        currency = rows[0][columns.index('Currency')] if rows and 'Currency' in columns else 'USD'
        answer = {'state': 'ok', 'usd': round(total, 2), 'currency': currency, 'checked': int(now)}
    except urllib.error.HTTPError as failure:
        answer = {'state': 'error', 'hint': f'Azure answered {failure.code}. Check the service principal has Cost Management Reader on the scope.'}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        answer = {'state': 'error', 'hint': 'Could not read the Azure bill. Try again later.'}
    with _lock:
        _bill.update(at=now, scope=config['scope'], answer=answer)
    return answer


def report(model):
    now = dt.datetime.now(dt.timezone.utc)
    tokens = usage(now)
    table = prices(model)
    estimate = {'tokens': tokens['month'], 'today_tokens': tokens['today'], 'sessions': tokens['sessions'], 'prices': table,
                'usd': round(cost(tokens['month'], table), 2) if table else None,
                'today_usd': round(cost(tokens['today'], table), 2) if table else None}
    return {'month': f'{now:%B %Y}', 'budget': BUDGET, 'estimate': estimate, 'bill': bill()}
