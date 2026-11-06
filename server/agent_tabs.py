"""Local agent commands share the browser's guarded tab launcher."""
import hmac
import os
import re

import auth
import delegate
import paths
import review_links

PATH = '/mobile/api/agent-tab'


def review_pair(body):
    worker, reviewer = body.get('worker'), body.get('reviewer')
    if (not isinstance(worker, str) or not re.fullmatch(r'%[0-9]+', worker)
            or ('reviewer' in body and (not isinstance(reviewer, str)
                                      or not re.fullmatch(r'%[0-9]+', reviewer)))):
        raise ValueError('Select a tab to stop reviewing.')
    return worker, reviewer


def serve(handler, body, *, sessions, folder, snapshot, run, lock):
    credential = handler.headers.get('X-Tether-Hook', '')
    if (not auth.is_loopback(handler.client_address[0]) or not credential
            or not hmac.compare_digest(credential.encode(), auth.session_value().encode())):
        handler.respond(403, {'error': 'Local hook authentication required.'})
        return
    with lock:
        entries = sessions()
        caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
        if caller is None:
            raise ValueError('That session has ended. Select another tab.')
        action = body.get('action')
        if action == 'start':
            request = dict(body, thread=caller.get('thread'))
            request.setdefault('agent', caller['agent'] if caller.get('agent') in ('claude', 'codex') else 'claude')
            delegate.validate(request, entries)
            if 'cwd' in body and body['cwd'] is None:
                raise ValueError('Choose an absolute path to an existing directory.')
            directory = folder(caller)
            pane, name = delegate.start(caller, request,
                                        directory if directory and os.path.isdir(directory) else str(paths.HOME),
                                        run=run, recent=lambda: snapshot(caller).get('messages') or [],
                                        cwd=body.get('cwd'))
            result = 201, {'pane': pane, 'name': name}
        elif action == 'stop':
            worker, reviewer = review_pair(body)
            if caller['id'] != worker:
                if reviewer is None:
                    if caller['id'] not in review_links.reviewed_by().get(worker, []):
                        raise ValueError('That review link is not on this tab.')
                    reviewer = caller['id']
                elif reviewer != caller['id']:
                    raise ValueError('That review link is not on this tab.')
            result = 200, {'stopped': review_links.remove(worker, reviewer)}
        elif action == 'list':
            links = review_links.reviewed_by()
            by_pane = {entry['id']: entry for entry in entries}

            def row(pane):
                entry = by_pane.get(pane, {})
                return {'pane': pane, 'name': entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or ''}

            result = 200, {
                'reviewing': [row(worker) for worker, reviewers in links.items() if caller['id'] in reviewers],
                'reviewers': [row(reviewer) for reviewer in links.get(caller['id'], [])]}
        else:
            raise ValueError('Unknown action.')
    handler.respond(*result)
