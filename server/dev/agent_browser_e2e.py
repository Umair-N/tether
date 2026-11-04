#!/usr/bin/env python3
"""Exercise real Playwright over Tether's CDP proxy, without changing the running service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser
import browser_cdp

CLIENT = r'''
const { chromium } = require('playwright-core');
(async () => {
  const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL);
  const context = connection.contexts()[0];
  if (!context) throw new Error('No default agent context');
  if (context.pages().length !== 0) throw new Error('Agent can see startup or user tabs');
  const page = await context.newPage();
  await page.goto('data:text/html,<h1>hi</h1>');
  const heading = await page.locator('h1').textContent();
  if (heading !== 'hi') throw new Error('Unexpected heading: ' + heading);
  const shot = await page.screenshot();
  if (!shot.length) throw new Error('Empty screenshot');
  if (context.pages().some(p => p.url() === process.env.E2E_USER_URL))
    throw new Error('User tab leaked into agent context');
  console.log('Playwright: heading=hi; screenshot=' + shot.length + ' bytes; user/startup tabs isolated');
  console.log('AGENT_READY');
  process.stdin.once('data', async () => {
    await connection.close();
    console.log('Playwright: disconnected');
  });
})().catch(error => { console.error(error); process.exit(1); });
'''


def chromium_pss(pid):
    """Total PSS of the Chromium process group, in KiB (not double-counted RSS)."""
    total = 0
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[2]) != pid:
                continue
            for line in (path / 'smaps_rollup').read_text().splitlines():
                if line.startswith('Pss:'):
                    total += int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    if not total:
        raise RuntimeError('Could not read Chromium PSS')
    return total


def main():
    chrome = browser.find_chrome()
    if not chrome:
        raise RuntimeError('Chromium: find_chrome() returned no binary')
    modules = sorted((Path.home() / '.npm/_npx').glob('*/node_modules'))
    # The first npx cache can belong to another tool; use an existing Playwright cache without installing anything.
    node_path = os.environ.get('NODE_PATH') or next((str(path) for path in modules if (path / 'playwright-core').is_dir()), '')
    if not node_path:
        raise RuntimeError('Playwright: no ~/.npm/_npx/*/node_modules directory')
    node = None
    proxy = real = None
    with tempfile.TemporaryDirectory(prefix='tether-agent-e2e-') as folder:
        root = Path(folder)
        real = browser.Browser(profile=root / 'browser/profile')
        proxy = browser_cdp.Proxy(real, root / 'config/agent-browser.json',
                                  env_runner=lambda *a, **k: None, pane_finder=lambda *a: '%99')
        try:
            print('Chromium: start real browser with a temporary profile', flush=True)
            user = real.new_tab('about:blank', 'saved')
            user_url = 'data:text/html,<h1>User tab</h1>'
            real.conn.call('Page.navigate', {'url': user_url}, real.tabs[user['id']].session)
            print('Proxy: bind loopback port', flush=True)
            proxy.start()
            env = dict(os.environ, NODE_PATH=node_path, E2E_CDP_URL=proxy.url, E2E_USER_URL=user_url)
            node = subprocess.Popen(['node', '-e', CLIENT], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            # Read stdout on a daemon; the queue wait limits the complete client handshake to 60 seconds.
            import queue
            import threading
            lines = queue.Queue()
            def read_lines():
                for line in node.stdout:
                    lines.put(line)
                lines.put(None)
            reader = threading.Thread(target=read_lines, daemon=True, name='e2e-node-output')
            reader.start()
            deadline = time.monotonic() + 60
            while True:
                line = lines.get(timeout=max(.01, deadline - time.monotonic()))
                if line is None:
                    raise RuntimeError('Playwright exited before the agent page loaded')
                print(line, end='', flush=True)
                if line.strip() == 'AGENT_READY':
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Playwright did not load the agent page within 60 seconds')
            deadline = time.monotonic() + 5
            while not any(tab['mode'] == 'agent' for tab in real.state()['tabs']) and time.monotonic() < deadline:
                time.sleep(.05)
            state = real.state()
            agents = [tab for tab in state['tabs'] if tab['mode'] == 'agent']
            if len(agents) != 1 or agents[0].get('owner') != {'pane': '%99'}:
                raise RuntimeError('Panel did not adopt the agent tab: ' + json.dumps(state))
            print('Panel: one agent tab; mode=agent; owner=%99', flush=True)
            print('Chromium total PSS: %d KiB' % chromium_pss(real.pid), flush=True)
            node.stdin.write('done\n')
            node.stdin.flush()
            node.stdin.close()  # EOF releases Node's stdin handle after the disconnect callback.
            status = node.wait(timeout=20)
            reader.join(timeout=2)
            while not lines.empty():
                line = lines.get_nowait()
                if line:
                    print(line, end='')
            if status:
                raise RuntimeError('Playwright exit status %d' % status)
            deadline = time.monotonic() + 5
            while proxy.busy() and time.monotonic() < deadline:
                time.sleep(.05)
            if proxy.busy():
                raise RuntimeError('Agent connection did not clean up')
            if user['id'] not in real.tabs:
                raise RuntimeError('Disconnect closed the user tab')
            print('PASS: real CDP connection, isolation, panel adoption, screenshot, PSS, cleanup', flush=True)
        finally:
            if node:
                if node.poll() is None:
                    node.kill()
                node.wait(timeout=5)
                for stream in (node.stdin, node.stdout):
                    stream.close()
            proxy.stop()
            real.stop()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('%s: %s' % (type(exc).__name__, exc), file=sys.stderr, flush=True)
        sys.exit(1)
