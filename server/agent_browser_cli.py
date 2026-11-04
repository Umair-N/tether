#!/usr/bin/env python3
"""Agent access to Tether's shared browser, using only the standard library."""
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


NOTE = '''## Tether shared browser
Tether runs one shared browser that the user can watch from desktop or phone.
- Playwright MCP already uses it. Do not pass --isolated or launch another browser.
- In scripts, connect instead of launching: Node `chromium.connectOverCDP(process.env.TETHER_CDP_URL)`, Python `p.chromium.connect_over_cdp(os.environ["TETHER_CDP_URL"])`. Use `browser.contexts[0]`. You get a private context; the user's logins are not in it.
- Limit: 3 tabs. Close tabs you no longer need.
- If you need the user in the browser (2FA code, captcha, a password you must not see), run `tether-browser ask "<what you need>"`, then wait for the user to reply in chat.'''


def endpoint(cdp, action):
    url = urlsplit(cdp)
    if (url.scheme not in ('ws', 'wss', 'http', 'https') or not url.hostname
            or not url.port or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r'/cdp/[A-Za-z0-9_-]+', url.path)):
        raise ValueError('Invalid shared browser endpoint.')
    scheme = 'https' if url.scheme in ('wss', 'https') else 'http'
    return urlunsplit((scheme, url.netloc, url.path.replace('/cdp/', f'/{action}/', 1), '', ''))


def main(args=None):
    args = sys.argv[1:] if args is None else args
    cdp = os.environ.get('TETHER_CDP_URL')
    if not cdp:
        print('Tether shared browser is not available in this shell.')
        return 1
    status = args == ['status']
    if not status and (len(args) != 2 or args[0] != 'ask' or not args[1].strip()):
        print('Usage: tether-browser ask "<text>" | status')
        return 1
    try:
        data = None
        headers = {}
        if not status:
            body = {'text': args[1]}
            pane = os.environ.get('TMUX_PANE', '')
            if re.fullmatch(r'%[0-9]+', pane):
                body['pane'] = pane
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(endpoint(cdp, 'health' if status else 'ask'), data=data, headers=headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            if not status or exc.code != 503:
                raise
            response = exc
        with response:
            result = json.load(response)
        if status:
            if result.get('ok') is True:
                print('on')
                return 0
            reason = ' '.join(str(result.get('reason') or 'unavailable').split())
            print('off: ' + reason)
            return 1
        if not isinstance(result.get('id'), str) or not result['id']:
            raise ValueError('Invalid shared browser response.')
        print('Asked the user. Wait for their reply in chat.')
        return 0
    except Exception as exc:
        # Do not expose the endpoint's bearer token in exception messages.
        reason = f'HTTP {exc.code}' if isinstance(exc, HTTPError) else 'shared browser request failed'
        print(('off: ' if status else 'Tether browser error: ') + reason)
        return 1


if __name__ == '__main__':
    sys.exit(main())
