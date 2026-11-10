"""Read the UI manifest and validate, save or generate Tether layouts."""
import argparse
import http.client
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import auth
from ask_bridge import install_settings
import custom_layouts as layouts
from layout_generate import built_ins


class Client:
    """Use agent_tab_cli's local port, owner cookie and same-origin write headers."""
    def __init__(self):
        port = int(os.environ.get('MOBILE_TMUX_PORT') or install_settings()['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid port.')
        secret = auth.TOKEN_FILE.read_text().strip()
        if not secret:
            raise ValueError('Local authentication required.')
        credential = auth.session_value(secret)
        self.url = f'http://127.0.0.1:{port}'
        self.headers = {'Content-Type': 'application/json', 'Origin': self.url,
                        'X-Codex-Mobile': '1', 'Cookie': auth.COOKIE + '=' + credential}
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path='', body=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = Request(self.url + layouts.PATH + path, data, headers=self.headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read(1048576))
        except HTTPError as error:
            try:
                message = json.loads(error.read(65536)).get('error', 'Layout request failed.')
            except (ValueError, AttributeError):
                message = 'Layout request failed.'
            raise ValueError(message) from None


def generate(client, prompt):
    job = client.request('/generate', {'prompt': prompt})['job']
    deadline = time.monotonic() + 420  # Two 180-second attempts, plus process overhead.
    while time.monotonic() < deadline:
        result = client.request('/generate?' + urlencode({'job': job}))
        if result['state'] == 'done':
            return result['layout']
        if result['state'] == 'failed':
            raise ValueError(result.get('error', 'Tether could not make the layout.'))
        time.sleep(1.5)
    raise ValueError('The layout job took too long.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('manifest', help='print the UI manifest JSON')
    commands.add_parser('list', help='list custom and built-in layouts')
    for command in ('validate', 'save'):
        sub = commands.add_parser(command)
        sub.add_argument('file')
        if command == 'save':
            sub.add_argument('--overwrite', action='store_true')
    sub = commands.add_parser('generate')
    sub.add_argument('prompt')
    sub.add_argument('--save', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'manifest':
            print(json.dumps(layouts.MANIFEST, indent=2))
        elif args.command in ('validate', 'save'):
            file = Path(args.file)
            layout = layouts.validate(layouts._read(file), file.stem)
            if args.command == 'validate':
                print('OK')
            else:
                print(json.dumps(Client().request('/save', {'layout': layout, 'overwrite': args.overwrite})))
        elif args.command == 'list':
            for layout in built_ins():
                print('%s\t%s\t%s' % (layout['id'], layout['name'], '(built-in)'))
            result = Client().request()
            for layout in result['layouts']:
                print('%s\t%s\t%s' % (layout['id'], layout['name'], layout['file']))
            for error in result.get('errors', []):
                print('%s: %s' % (error['file'], error['reason']), file=sys.stderr)
        else:
            client = Client()
            layout = generate(client, args.prompt)
            if args.save:
                layout = client.request('/save', {'layout': layout, 'overwrite': False})['layout']
            print(json.dumps(layout, indent=2))
    except (ValueError, OSError, URLError, http.client.HTTPException, KeyError) as error:
        print('Layout error: %s' % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Layout request stopped. A server job may still finish as a draft.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
