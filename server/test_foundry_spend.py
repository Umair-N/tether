"""Foundry spend: token totals from Codex logs, Retail Prices parsing, and the Azure bill (fake network)."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import foundry_spend

NOW = dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc)


def log(path, provider, totals, last_input=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{'type': 'session_meta', 'payload': {'model_provider': provider}}]
    for stamp, (inp, cached, write, out) in totals:
        usage = {'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': write, 'output_tokens': out}
        lines.append({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': usage, 'last_token_usage': {'input_tokens': last_input}}}})
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')


PRICES = {'Items': [{'skuName': f'6.1-sol {tier} {meter} Std Gl', 'unitOfMeasure': '1M', 'retailPrice': price}
                    for tier, base in (('ShortCo', 1), ('LongCo', 2))
                    for meter, price in (('Inp', 2.0 * base), ('Cd Inp', 0.1 * base), ('Cd Wr', 2.5 * base), ('Opt', 10.0 * base))]
          + [{'skuName': '6.1-sol ShortCo Inp Std DZ', 'unitOfMeasure': '1M', 'retailPrice': 99.0}]}


class Spend(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        foundry_spend._files.clear()
        foundry_spend._bill.clear()
        patcher = mock.patch.multiple(foundry_spend, PRICES_FILE=self.root / 'prices.json', BILL_CONFIG=self.root / 'azure-cost.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_counts_only_foundry_growth_this_month(self):
        log(self.root / '2026/11/02/a.jsonl', 'azure', [('2026-11-01T10:00:00Z', (1000, 0, 900, 10)), ('2026-11-01T10:00:01Z', (1000, 0, 900, 10)),
                                                       ('2026-11-02T09:00:00Z', (3000, 800, 900, 30))])
        log(self.root / '2026/11/02/b.jsonl', 'openai', [('2026-11-02T09:00:00Z', (5000, 0, 0, 50))])
        out = foundry_spend.usage(NOW, self.root)
        self.assertEqual(out['month']['short'], {'input': 1300, 'cached': 800, 'cache_write': 900, 'output': 30, 'requests': 2})
        self.assertEqual(out['today']['short']['requests'], 1)
        self.assertEqual(out['sessions'], 1)

    def test_long_requests_and_last_month_file(self):
        old = self.root / '2026/10/31/c.jsonl'
        log(old, 'azure', [('2026-10-31T23:00:00Z', (100, 0, 0, 1)), ('2026-11-01T01:00:00Z', (400_100, 0, 0, 11))], last_input=400_000)
        os.utime(old, (NOW.timestamp(), NOW.timestamp()))
        out = foundry_spend.usage(NOW, self.root)
        self.assertEqual(out['month']['long'], {'input': 400_000, 'cached': 0, 'cache_write': 0, 'output': 10, 'requests': 1})

    def test_prices_use_global_standard_and_cost_adds_up(self):
        table = foundry_spend.prices('gpt-6.1-sol', fetch=lambda url: PRICES, now=0)
        self.assertEqual(table['short'], {'input': 2.0, 'cached': 0.1, 'cache_write': 2.5, 'output': 10.0})
        tokens = {'short': {'input': 1e6, 'cached': 1e6, 'cache_write': 0, 'output': 1e6}, 'long': {'input': 0, 'cached': 0, 'cache_write': 0, 'output': 1e6}}
        self.assertAlmostEqual(foundry_spend.cost(tokens, table), 2.0 + 0.1 + 10.0 + 20.0)
        # Cached for a day: a failing fetch still returns the saved table.
        self.assertEqual(foundry_spend.prices('gpt-6.1-sol', fetch=mock.Mock(side_effect=OSError), now=10), table)

    def test_bill_states(self):
        self.assertEqual(foundry_spend.bill()['state'], 'not-set-up')
        config = foundry_spend.BILL_CONFIG
        config.write_text(json.dumps({'tenant_id': 't', 'client_id': 'c', 'client_secret': 's', 'scope': '/subscriptions/x/resourceGroups/rg'}))
        os.chmod(config, 0o644)
        self.assertIn('chmod 600', foundry_spend.bill()['hint'])
        os.chmod(config, 0o600)
        calls = []

        def fetch(url, data=None, headers=None):
            calls.append(url)
            if 'oauth2' in url:
                return {'access_token': 'tok'}
            self.assertEqual(headers['Authorization'], 'Bearer tok')
            self.assertIn('/subscriptions/x/resourceGroups/rg/providers/Microsoft.CostManagement/query', url)
            return {'properties': {'columns': [{'name': 'Cost'}, {'name': 'Currency'}], 'rows': [[12.345, 'USD']]}}
        self.assertEqual(foundry_spend.bill(fetch, now=100)['usd'], 12.35)
        foundry_spend.bill(fetch, now=200)
        self.assertEqual(len(calls), 2, 'second call within the hour uses the cache')


if __name__ == '__main__':
    unittest.main()
