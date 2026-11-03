"""Tool summary and lazy detail contracts for both transcript formats."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import activity
import agents
import server
import tool_details as tools

FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


class ToolImageTests(unittest.TestCase):
    PNG = '89504e470d0a1a0a'

    def test_read_image_is_counted_and_served_from_the_transcript(self):
        import base64
        data = base64.b64encode(bytes.fromhex(self.PNG)).decode()
        records = [
            {'type': 'assistant', 'timestamp': 't', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': '/work/shot.png'}}]}},
            {'type': 'user', 'timestamp': 't', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'read-1', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/svg+xml', 'data': data}}]}]}}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 't.jsonl'
            path.write_text('\n'.join(json.dumps(record) for record in records))
            messages = []
            for index, record in enumerate(records):
                agents.claude_consume(messages, index, record, lambda value: value)
            self.assertEqual(activity.group_actions(messages)[0]['tools'][0]['images'], 1)  # SVG is not served
            config = {'agent': 'claude', 'transcript': path}
            self.assertEqual(tools.lookup(config, 'read-1')['images'], 1)
            self.assertEqual(tools.image(config, 'read-1', 0), ('image/png', bytes.fromhex(self.PNG)))
            with self.assertRaises(ValueError):
                tools.image(config, 'read-1', 1)
            with self.assertRaises(ValueError):
                tools.image(config, 'missing', 0)


class ToolDetailsTests(unittest.TestCase):
    def test_claude_fixture_summary_result_pairing_and_diff(self):
        messages = []
        for index, line in enumerate((FIXTURES / 'tools-claude.jsonl').read_text().splitlines()):
            agents.claude_consume(messages, index, json.loads(line), lambda value: value)
        group = activity.group_actions(messages)[0]
        self.assertEqual(group['count'], 2)
        self.assertEqual([tool['id'] for tool in group['tools']], ['edit-1', 'bash-1'])
        self.assertTrue(group['tools'][1]['is_error'])
        self.assertNotIn('detail', group['tools'][0])
        config = {'agent': 'claude', 'transcript': FIXTURES / 'tools-claude.jsonl'}
        detail = tools.lookup(config, 'edit-1')
        self.assertEqual(detail['format'], 'diff')
        self.assertIn('--- a//work/a.py\n+++ b//work/a.py', detail['detail'])
        self.assertIn('-old\n+new', detail['detail'])
        self.assertIn('keep', detail['detail'])
        detail = tools.lookup(config, 'bash-1')
        self.assertTrue(detail['is_error'])
        self.assertIn('printf hello', detail['detail'])
        self.assertIn('hello', detail['detail'])

    def test_codex_fixture_function_output_and_patch(self):
        state = server.codex_state()
        for index, line in enumerate((FIXTURES / 'tools-codex.jsonl').read_text().splitlines()):
            server.codex_consume(state, index, json.loads(line))
        group = activity.group_actions(state['messages'])[0]
        self.assertEqual(group['count'], 2)
        self.assertTrue(group['tools'][0]['is_error'])
        self.assertEqual(group['tools'][0]['title'], 'git status')
        self.assertEqual(group['tools'][1]['title'], 'a.py')
        config = {'agent': 'codex', 'transcript': FIXTURES / 'tools-codex.jsonl'}
        self.assertIn('failed', tools.lookup(config, 'exec-1')['detail'])
        detail = tools.lookup(config, 'patch-1')
        self.assertEqual(detail['format'], 'diff')
        self.assertIn('-old\n+new', detail['detail'])

    def test_multiedit_write_search_and_other_input(self):
        result = tools.build({'name': 'MultiEdit', 'input': {'file_path': 'a', 'edits': [
            {'old_string': 'one', 'new_string': 'two'}, {'old_string': '', 'new_string': 'three'}]}})
        self.assertIn('-one\n+two', result['detail'])
        self.assertIn('+three', result['detail'])
        result = tools.build({'name': 'Write', 'input': {'file_path': 'a', 'content': '\n'.join(map(str, range(300)))}})
        self.assertTrue(result['truncated'])
        self.assertTrue(result['detail'].startswith('0\n1'))
        self.assertNotIn('\n299\n', result['detail'])
        for name, data, title in [('Read', {'file_path': '/full/path'}, '/full/path'),
                                  ('Grep', {'pattern': 'needle'}, 'needle'), ('Glob', {'pattern': '*.py'}, '*.py')]:
            self.assertEqual(tools.summary({'name': name, 'input': data})['title'], title)
        result = tools.build({'name': 'Other', 'input': {'key': 'value'}}, {'content': [{'type': 'text', 'text': 'answer'}]})
        self.assertIn('value', result['detail'])
        self.assertIn('answer', result['detail'])

    def test_bounds_utf8_and_bash_tail(self):
        detail = tools.build({'name': 'Bash', 'input': {'command': 'run'}}, {'content': '\n'.join(str(n) + '🙂' * 100 for n in range(400))})
        self.assertTrue(detail['truncated'])
        self.assertLess(len(detail['detail'].encode()), tools.MAX_BYTES)
        self.assertLessEqual(len(detail['detail'].splitlines()), tools.MAX_LINES)
        self.assertIn('399🙂', detail['detail'])
        self.assertTrue(detail['detail'].startswith('run\n'))
        self.assertNotIn('�', detail['detail'])
        value, cut = tools.bounded('🙂' * 10000)
        self.assertTrue(cut)
        self.assertLessEqual(len(value.encode()), tools.MAX_BYTES)

    def test_pending_error_and_unknown_id(self):
        detail = tools.build({'name': 'Read', 'input': {'file_path': 'a'}})
        self.assertIn('pending', detail['detail'])
        detail = tools.build({'name': 'Edit', 'input': {'old_string': 'a', 'new_string': 'b'}}, {'is_error': True, 'content': 'not found'})
        self.assertTrue(detail['is_error'])
        self.assertIn('not found', detail['detail'])
        with self.assertRaises(ValueError):
            tools.lookup({'agent': 'codex', 'transcript': FIXTURES / 'tools-codex.jsonl'}, 'missing')

    def test_route_selects_session_and_requires_auth(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/tool?session=%2512&id=patch-1'
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        config = {'agent': 'codex', 'transcript': FIXTURES / 'tools-codex.jsonl'}
        with patch.object(server, 'choose', return_value=config) as choose:
            handler.do_GET()
            choose.assert_called_once_with('%12')
        self.assertEqual(handler.respond.call_args.args[0], 200)
        self.assertEqual(handler.respond.call_args.args[1]['id'], 'patch-1')
        handler.deny.return_value = True
        with patch.object(server, 'choose') as choose:
            handler.do_GET()
            choose.assert_not_called()

    def test_result_appended_after_first_read(self):
        import history
        records = (FIXTURES / 'tools-claude.jsonl').read_text().splitlines()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'chat.jsonl'
            path.write_text(records[0] + '\n')
            def consume(state, index, record):
                agents.claude_consume(state['messages'], index, record, lambda text: text)
            fresh = lambda: {'messages': [], 'dropped': 0}
            state = history.read(path, fresh, consume)
            before = activity.group_actions(state['messages'])
            self.assertFalse(before[0]['tools'][1]['is_error'])
            with path.open('a') as stream:
                stream.write(records[1] + '\n')
            state = history.read(path, fresh, consume)
            self.assertTrue(activity.group_actions(state['messages'])[0]['tools'][1]['is_error'])
            self.assertFalse(before[0]['tools'][1]['is_error'])
