"""Autonomy settings use temporary paths and mocked processes, never live tmux."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import autonomy
import codex_level
import paths
import server

PID = 'abcdef012345'


class Handler:
    def __init__(self, path=autonomy.PATH, body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


def reviewers(**changes):
    return {tier: dict(autonomy.TIER_DEFAULT, **changes.get(tier, {})) for tier in autonomy.TIERS}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('CONFIG', 'TETHER', 'CODEX'):
            patch = mock.patch.object(paths, name, self.root / name.lower())
            patch.start()
            self.addCleanup(patch.stop)
        models = mock.patch.object(codex_level, 'models', return_value=[])
        models.start()
        self.addCleanup(models.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


class Resolve(Base):
    def test_missing_and_bad_global_files_mean_standard(self):
        self.assertEqual(autonomy.resolve(PID), {'level': 2, 'source': 'global', 'global': 2, 'project': None})
        for text in ('7', 'two', '', '-1', '2 3', 'true'):
            with self.subTest(text=text):
                self.write(autonomy.global_path(), text)
                self.assertEqual(autonomy.read_global(), 2)

    def test_override_on_beats_global_and_can_lower_it(self):
        autonomy.set_global(4)
        autonomy.set_project(PID, True, 1)
        self.assertEqual(autonomy.global_path().read_text(), '4\n')
        self.assertEqual(autonomy.resolve(PID), {'level': 1, 'source': 'project', 'global': 4,
                                               'project': {'override': True, 'level': 1}})

    def test_override_off_retains_project_level(self):
        autonomy.set_global(1)
        autonomy.set_project(PID, False, 5)
        self.assertEqual(autonomy.resolve(PID), {'level': 1, 'source': 'global', 'global': 1,
                                               'project': {'override': False, 'level': 5}})

    def test_bad_project_files_and_ids_are_ignored(self):
        for text in ('bad json', '[]', '{"override": true, "level": 9}', '{"override": "yes", "level": 3}',
                     '{"override": true, "level": true}', '{"level": 3}'):
            self.write(autonomy.project_path(PID), text)
            self.assertIsNone(autonomy.read_project(PID), text)
        self.assertEqual(autonomy.resolve('../../etc')['level'], 2)

    def test_invalid_settings_do_not_write(self):
        for level in (6, -1, '2', True, None, 2.0, [], {}):
            with self.subTest(level=level), self.assertRaises(autonomy.LevelError):
                autonomy.set_global(level)
        for pid, override, level in ((PID, 'on', 2), ('../x', True, 2), (PID, True, False)):
            with self.assertRaises(autonomy.LevelError):
                autonomy.set_project(pid, override, level)
        self.assertFalse(paths.CONFIG.exists())
        self.assertFalse(paths.TETHER.exists())


class Rules(Base):
    def test_each_level_has_its_rules_and_safety_and_last_line(self):
        safety = ('At every level, ask before you delete data, force-push, run a destructive migration, spend money, '
                  'make a public release, or send a message to another person.')
        for level, (name, _) in enumerate(autonomy.LEVELS):
            with self.subTest(level=level):
                text = autonomy.rules({'level': level, 'source': 'global'})
                self.assertEqual(text.splitlines()[0],
                                 f'Autonomy level {level} of 5 ({name}), set by the user for all projects.')
                self.assertIn(autonomy.RULES[level], text)
                self.assertIn(safety, text.splitlines())
                self.assertEqual('Needs you: yes' in text, level >= 4)
                self.assertEqual('Urgent: <the question in one sentence>' in text, level >= 4)
                self.assertNotIn('Before you ask the user, read', text)
                self.assertEqual(text.splitlines()[-1], 'Do not change the level. The user sets it in Tether.')

    def test_decisions_only_exist_at_levels_three_to_five_with_a_file(self):
        path = paths.CONFIG / 'decisions.md'
        self.write(path, 'Past decisions.')
        expected = (f'Before you ask the user, read {path.resolve()}. If a past decision answers your question, '
                    'use it and say so.')
        for level in range(6):
            text = autonomy.rules({'level': level, 'source': 'project'})
            self.assertIn('set by the user for this project.', text)
            self.assertEqual(expected in text, level >= 3)
        path.unlink()
        for level in range(3, 6):
            self.assertNotIn('Before you ask the user, read', autonomy.rules({'level': level, 'source': 'global'}))


class Reviewers(Base):
    def test_missing_or_bad_file_means_all_auto(self):
        self.assertEqual(autonomy.read_reviewers(), reviewers())
        for text in ('not json', '[]', '{}', json.dumps(reviewers(normal={'agent': 'bad'}))):
            self.write(autonomy.reviewers_path(), text)
            self.assertEqual(autonomy.read_reviewers(), reviewers())
        first = autonomy.read_reviewers()
        first['normal']['effort'] = 'high'
        self.assertEqual(first['light']['effort'], 'auto')
        self.assertEqual(autonomy.read_reviewers(), reviewers())

    def test_valid_choices_round_trip(self):
        for agent, models in (('auto', ('auto',)), ('none', ('auto',)),
                              ('claude', ('auto', *autonomy.CLAUDE_REVIEW_MODELS)),
                              ('codex', ('auto', 'gpt-6.1-sol', 'new_slug.1'))):
            for model in models:
                for effort in (('auto',) if agent == 'none' else ('auto', 'low', 'medium', 'high', 'xhigh', 'max')):
                    with self.subTest(agent=agent, model=model, effort=effort):
                        value = reviewers(normal={'agent': agent, 'model': model, 'effort': effort})
                        autonomy.set_reviewers(value)
                        self.assertEqual(autonomy.read_reviewers(), value)

    def test_invalid_shapes_and_choices_are_rejected(self):
        invalid_tiers = [None, [], {}, {'agent': 'auto'}, dict(autonomy.TIER_DEFAULT, extra=True)]
        invalid_tiers += [dict(autonomy.TIER_DEFAULT, **change) for change in (
            {'agent': 'other'}, {'agent': []}, {'agent': True}, {'model': None}, {'model': []},
            {'model': '-bad'}, {'model': 'x' * 65}, {'model': 'x; command'}, {'effort': 'ultra'},
            {'effort': 'auto '}, {'effort': []}, {'effort': True}, {'agent': 'auto', 'model': 'opus'},
            {'agent': 'none', 'model': 'opus'}, {'agent': 'none', 'effort': 'low'},
            {'agent': 'claude', 'model': 'gpt-6.1-sol'}, {'agent': 'codex', 'model': '-bad'})]
        for tier in invalid_tiers:
            value = reviewers()
            value['normal'] = tier
            with self.subTest(tier=tier), self.assertRaises(autonomy.LevelError) as raised:
                autonomy.set_reviewers(value)
            self.assertEqual(raised.exception.status, 400)
        for value in (None, [], {}, {'critical': dict(autonomy.TIER_DEFAULT)}, dict(reviewers(), extra={})):
            with self.subTest(value=value), self.assertRaises(autonomy.LevelError):
                autonomy.set_reviewers(value)

    def test_codex_model_must_be_in_nonempty_cache(self):
        value = reviewers(critical={'agent': 'codex', 'model': 'listed'})
        autonomy.set_reviewers(value)  # no cache accepts any slug
        with mock.patch.object(codex_level, 'models', return_value=[{'slug': 'listed'}]):
            autonomy.set_reviewers(value)
            with self.assertRaises(autonomy.LevelError):
                autonomy.set_reviewers(reviewers(critical={'agent': 'codex', 'model': 'unlisted'}))
        with mock.patch.object(codex_level, 'models', return_value=[{'slug': 'different'}]):
            self.assertEqual(autonomy.read_reviewers(), reviewers())

    def test_default_algorithm_and_override_lines(self):
        default = autonomy.reviewer_rules(reviewers())
        self.assertTrue(default.startswith('Choose a reviewer by the risk of the task:\n- Critical:'))
        self.assertIn('with the strongest model and high effort.', default)
        self.assertIn('with its default model and medium effort.', default)
        self.assertIn('Do not start a reviewer tab. Review the change yourself.', default)
        self.assertIn(f'`python3 {Path(autonomy.__file__).parent / "agent_tab_cli.py"} start --reviews-me '
                      '--agent AGENT [--model MODEL] [--effort EFFORT]`.', default)
        self.assertNotIn('The user set', default)
        value = reviewers(critical={'agent': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'},
                          normal={'effort': 'high'}, light={'agent': 'none'})
        self.assertEqual(autonomy.reviewer_rules(value), default + '\n' + '\n'.join((
            'The user set the Critical reviewer: Codex, model gpt-6.1-sol, effort xhigh. This overrides the default.',
            'The user set the Normal reviewer effort: high.',
            'The user set the Light reviewer: none. Do not start a reviewer for light tasks.')))

    def test_override_omits_auto_parts(self):
        for choice, expected in (
                ({'agent': 'claude'}, 'Claude'),
                ({'agent': 'claude', 'model': 'opus'}, 'Claude, model opus'),
                ({'agent': 'codex', 'effort': 'max'}, 'Codex, effort max')):
            text = autonomy.reviewer_rules(reviewers(normal=choice))
            self.assertEqual(text.splitlines()[-1],
                             f'The user set the Normal reviewer: {expected}. This overrides the default.')


class Hook(Base):
    def test_hook_shape_project_and_both_context_parts(self):
        autonomy.set_global(1)
        autonomy.set_project(PID, True, 3)
        tmux = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=PID + '\n'))
        out = json.loads(autonomy.hook_output({'cwd': '/work'}, {'TMUX': '/tmux', 'TMUX_PANE': '%7'}, tmux))
        self.assertEqual(out, {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext':
                         autonomy.rules(autonomy.resolve(PID)) + '\n\n' + autonomy.reviewer_rules(reviewers())}})
        tmux.assert_called_once_with('display-message', '-p', '-t', '%7', '#{@project}')

    def test_tmux_failure_uses_global(self):
        for error in (OSError('missing tmux'), subprocess.TimeoutExpired('tmux', 2)):
            out = json.loads(autonomy.hook_output({'cwd': '/work'}, {}, mock.Mock(side_effect=error)))
            self.assertIn('Autonomy level 2 of 5', out['hookSpecificOutput']['additionalContext'])

    def test_script_hook_fails_open_and_show_prints_both_parts(self):
        env = dict(os.environ, MOBILE_TMUX_CONFIG=str(paths.CONFIG), TETHER_HOME=str(paths.TETHER),
                   MOBILE_TMUX_TMUX='/bin/false')
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        for raw in ('{"cwd": "/nowhere-at-all"}', '', '[]'):
            done = subprocess.run([sys.executable, autonomy.__file__, 'hook'], input=raw,
                                  capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(done.returncode, 0)
            self.assertIn('Autonomy level 2 of 5', json.loads(done.stdout)['hookSpecificOutput']['additionalContext'])
        bad = subprocess.run([sys.executable, autonomy.__file__, 'hook'], input='{not json',
                             capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((bad.returncode, bad.stdout), (0, ''))
        shown = subprocess.run([sys.executable, autonomy.__file__, 'show'],
                               capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(shown.stdout, autonomy.rules(autonomy.resolve()) + '\n\n' +
                         autonomy.reviewer_rules(reviewers()) + '\n')


class Http(Base):
    def test_get_and_post_round_trips(self):
        handler = Handler()
        autonomy.get(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual((handler.body['global'], handler.body['level'], len(handler.body['levels'])), (2, 2, 6))
        self.assertEqual(handler.body['reviewers'], reviewers())
        self.assertEqual(handler.body['tiers'], ['critical', 'normal', 'light'])
        self.assertEqual(handler.body['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(handler.body['models'], {'claude': [{'slug': s, 'name': n} for s, n in autonomy.delegate.CLAUDE_MODELS], 'codex': []})
        handler = Handler(body={'level': 4})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['global']), (200, 4))
        handler = Handler(body={'project': PID, 'override': True, 'level': 1})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['level'], handler.body['source']), (200, 1, 'project'))
        handler = Handler(f'{autonomy.PATH}?project={PID}')
        autonomy.get(handler)
        self.assertEqual(handler.body['project'], {'override': True, 'level': 1})
        value = reviewers(light={'agent': 'claude', 'model': 'haiku'})
        handler = Handler(body={'reviewers': value})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['reviewers'], handler.body['global']), (200, value, 4))
        handler = Handler()
        autonomy.get(handler)
        self.assertEqual(handler.body['reviewers'], value)

    def test_codex_model_state_shape(self):
        with mock.patch.object(codex_level, 'models', return_value=[
                {'slug': 'gpt-test', 'name': 'Test', 'efforts': ['low', 'high', 'ultra'], 'fast': True}]):
            self.assertEqual(autonomy.state()['models']['codex'],
                             [{'slug': 'gpt-test', 'name': 'Test', 'efforts': ['low', 'high']}])

    def test_bad_requests(self):
        for body in ({'level': 9}, {'level': '2'}, {'level': True}, {'project': 'x', 'override': True, 'level': 1},
                     {'project': PID, 'override': 'yes', 'level': 1}, {'reviewers': {}}, [1]):
            handler = Handler(body=body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 400, body)
        handler = Handler(autonomy.PATH + '?project=../x')
        autonomy.get(handler)
        self.assertEqual(handler.status, 400)
        self.assertTrue(autonomy.handles(autonomy.PATH + '?project=abc'))
        self.assertFalse(autonomy.handles(autonomy.PATH + '-other'))

    def test_bad_json_sizes_and_save_failure(self):
        for size in ('0', '-1', '4097'):
            handler = Handler(body={'level': 1})
            handler.headers['Content-Length'] = size
            autonomy.post(handler)
            self.assertEqual(handler.status, 413)
        for raw in (b'{bad json', b'\xff'):
            handler = Handler()
            handler.rfile = io.BytesIO(raw)
            handler.headers['Content-Length'] = str(len(raw))
            autonomy.post(handler)
            self.assertEqual(handler.status, 400)
        handler = Handler(body={'level': 1})
        with mock.patch.object(autonomy, '_write', side_effect=OSError('disk full')):
            autonomy.post(handler)
        self.assertEqual(handler.status, 500)

    def test_server_post_dispatch_preserves_auth_and_origin_guards(self):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = autonomy.PATH, 'POST'
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'}
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        with mock.patch.object(autonomy, 'post') as post:
            handler.do_POST()
            post.assert_called_once_with(handler)
            post.reset_mock()
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                handler.headers.update(headers)
                handler.do_POST()
                handler.respond.assert_called_with(403, {'error': 'Reload the page and try again.'})
            post.assert_not_called()
            handler.deny.return_value = True
            handler.do_POST()
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
