"""Start tabs and manage review links from an agent's own Tether pane."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import auth
from ask_bridge import install_settings

PATH = '/mobile/api/agent-tab'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def arguments(argv):
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    start = actions.add_parser('start')
    start.add_argument('--agent', choices=('claude', 'codex'))
    review = start.add_mutually_exclusive_group()
    review.add_argument('--review-it', action='store_true')
    review.add_argument('--reviews-me', action='store_true')
    start.add_argument('--context', action='store_true')
    start.add_argument('--name')
    start.add_argument('--cwd')
    start.add_argument('task', nargs='?', default='')
    stop = actions.add_parser('stop')
    stop.add_argument('--worker', required=True)
    stop.add_argument('--reviewer')
    actions.add_parser('list')
    args = parser.parse_args(argv)
    body = {'action': args.action}
    if args.action == 'start':
        message = sys.stdin.read() if args.task == '-' else args.task
        if not args.reviews_me and not message.strip():
            parser.error('a task is required unless --reviews-me is set')
        body.update(message=message, context=args.context,
                    review='new' if args.reviews_me else 'source' if args.review_it else False)
        for key in ('agent', 'name', 'cwd'):
            value = getattr(args, key)
            if value is not None:
                body[key] = os.path.abspath(value) if key == 'cwd' else value
    elif args.action == 'stop':
        body['worker'] = args.worker
        if args.reviewer is not None:
            body['reviewer'] = args.reviewer
    return body


def main(argv=None):
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        print('Run this inside a Tether tab.', file=sys.stderr)
        return 1
    try:
        body = arguments(argv)
    except ValueError as exc:
        print(f'Tether tab error: {exc}', file=sys.stderr)
        return 1
    body['session'] = pane
    try:
        port = int(os.environ.get('MOBILE_TMUX_PORT') or install_settings()['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid port.')
        secret = auth.TOKEN_FILE.read_text().strip()
        if not secret:
            raise ValueError('Local hook authentication required.')
        credential = auth.session_value(secret)
        url = f'http://127.0.0.1:{port}'
        request = Request(url + PATH, json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Origin': url, 'X-Codex-Mobile': '1',
            'X-Tether-Hook': credential, 'Cookie': auth.COOKIE + '=' + credential})
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            result = json.loads(response.read(262144))
        print(json.dumps(result))
        return 0
    except HTTPError as exc:
        error = str(exc.code)
        try:
            reply = json.loads(exc.read(262144))
            if isinstance(reply, dict) and isinstance(reply.get('error'), str):
                error = reply['error']
        except (ValueError, OSError):
            pass
        print(f'Tether tab error: {error}', file=sys.stderr)
    except (OSError, URLError, ValueError, KeyError):
        print('Tether tab error: Could not contact the local Tether server.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
