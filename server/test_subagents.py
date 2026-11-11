"""Subagent reads use isolated transcripts and never write to real terminals."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server
import subagents
import tool_details


class SubagentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.main = self.root / 'session.jsonl'
        self.folder = self.root / 'session' / 'subagents'
        self.folder.mkdir(parents=True)
        self.config = {'agent': 'claude', 'transcript': str(self.main)}
        self.now = 1800000000
        self.clock = patch.object(subagents.time, 'time', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        subagents._cache.clear()
        self.addCleanup(subagents._cache.clear)
        self.write(self.main, [self.record('assistant', 'main', model='main-model', usage={
            'input_tokens': 10, 'cache_read_input_tokens': 20,
            'cache_creation_input_tokens': 30, 'output_tokens': 4})])

    def stamp(self, seconds):
        return datetime.fromtimestamp(self.now + seconds, timezone.utc).isoformat().replace('+00:00', 'Z')

    def record(self, role, content, seconds=-120, **message):
        return {'type': role, 'isSidechain': True, 'agentId': 'abc', 'timestamp': self.stamp(seconds),
                'message': dict(content=content, **message)}

    def write(self, path, records):
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def agent(self, identifier='abc', *, meta=None, records=None, age=30):
        path = self.folder / f'agent-{identifier}.jsonl'
        if records is None:
            records = [
                self.record('user', 'Check the files'),
                self.record('assistant', [
                    {'type': 'text', 'text': 'Work starts'},
                    {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': '/work/a.py'}},
                    {'type': 'tool_use', 'id': 'bash-1', 'name': 'Bash', 'input': {'command': 'false'}},
                    {'type': 'tool_use', 'id': 'pending-1', 'name': 'Grep', 'input': {'pattern': 'TODO'}}],
                    seconds=-110, model='first-model', usage={'input_tokens': 7, 'output_tokens': 3}),
                self.record('user', [
                    {'type': 'tool_result', 'tool_use_id': 'read-1', 'content': 'file contents'},
                    {'type': 'tool_result', 'tool_use_id': 'bash-1', 'content': 'Process exited with code 2'}], seconds=-100),
                self.record('assistant', [{'type': 'text', 'text': 'Report complete'}], seconds=-90,
                            model='last-model', usage={'input_tokens': 100, 'cache_read_input_tokens': 20,
                                                       'cache_creation_input_tokens': 4, 'output_tokens': 6})]
        self.write(path, records)
        os.utime(path, (self.now - age, self.now - age))
        if meta is not None:
            (self.folder / f'agent-{identifier}.meta.json').write_text(json.dumps(meta))
        return path

    def test_running_finished_stopped_and_elapsed_time(self):
        self.agent('running', age=900)
        self.agent('stopped', age=901)
        self.agent('finished', meta={'finished': True}, age=1000)
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertEqual(entries['running']['state'], 'running')
        self.assertIsNone(entries['running']['ended'])
        self.assertEqual(entries['running']['seconds'], 120)
        for identifier, state in (('stopped', 'stopped'), ('finished', 'finished')):
            self.assertEqual(entries[identifier]['state'], state)
            self.assertEqual(entries[identifier]['ended'], self.stamp(-90))
            self.assertEqual(entries[identifier]['seconds'], 30)
        self.assertIsNone(subagents.detail(self.config, 'running')['report'])
        self.assertIsNone(subagents.detail(self.config, 'stopped')['report'])

    def notification(self, identifier, status, summary='Task result'):
        return (f'<task-notification>\n<task-id>{identifier}</task-id>\n'
                f'<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>')

    def test_states_from_meta_and_notifications_with_precedence(self):
        cases = (
            ('completed', {}, 'completed', 'finished'),
            ('failed', {'finished': True}, 'failed', 'failed'),
            ('killed', {'finished': True}, 'killed', 'stopped'),
            ('user-stop', {'finished': True, 'stoppedByUser': True}, 'completed', 'stopped'),
            ('meta-stop', {'stoppedByUser': True}, None, 'stopped'),
        )
        records = []
        for identifier, meta, status, _ in cases:
            self.agent(identifier, meta=meta)
            if status:
                records.append(self.record('user', self.notification(identifier, status)))
        self.write(self.main, records)
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        for identifier, _, _, expected in cases:
            with self.subTest(identifier=identifier):
                self.assertEqual(entries[identifier]['state'], expected)
                self.assertIsNotNone(entries[identifier]['ended'])
                self.assertEqual(subagents.detail(self.config, identifier)['state'], expected)
        self.assertIsNone(entries['completed']['summary'])
        self.assertEqual(entries['failed']['summary'], 'Task result')
        self.assertIsNone(entries['meta-stop']['summary'])

    def test_notifications_decode_json_strings_and_text_parts_and_bound_summary(self):
        self.agent('string')
        self.agent('parts')
        self.write(self.main, [
            self.record('user', json.dumps(self.notification('string', 'failed', 'a' * 300))),
            self.record('user', [{'type': 'text', 'text': self.notification('parts', 'killed', 'Stopped &amp; done')}]),
            self.record('assistant', self.notification('parts', 'completed')),
        ])
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertEqual(entries['string']['state'], 'failed')
        self.assertEqual(entries['string']['summary'], 'a' * 240)
        self.assertEqual(entries['parts']['state'], 'stopped')
        self.assertEqual(entries['parts']['summary'], 'Stopped & done')

    def test_parent_detection_includes_tool_calls_before_retained_steps(self):
        self.agent('parent', meta={'spawnDepth': 1, 'toolUseId': 'main-spawn'}, records=[
            self.record('assistant', [{'type': 'tool_use', 'id': 'child-spawn', 'name': 'Agent'}] +
                        [{'type': 'text', 'text': 'step'} for _ in range(405)])])
        self.agent('child', meta={'spawnDepth': 2, 'toolUseId': 'child-spawn'})
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertIsNone(entries['parent']['parent'])
        self.assertEqual(entries['child']['parent'], 'parent')
        self.assertEqual(entries['child']['depth'], 2)
        self.assertEqual(subagents.detail(self.config, 'child')['parent'], 'parent')

    def test_newest_nonempty_session_title_from_both_record_types(self):
        self.agent()
        self.write(self.main, [
            {'type': 'custom-title', 'customTitle': 'Original'},
            {'type': 'agent-name', 'agentName': 'New name'},
            {'type': 'custom-title', 'customTitle': '   '},
            {'type': 'agent-name', 'agentName': None},
        ])
        self.assertEqual(subagents.listing(self.config)['main']['title'], 'New name')
        with self.main.open('a') as stream:
            stream.write(json.dumps({'type': 'custom-title', 'customTitle': ' Latest '}) + '\n')
        self.assertEqual(subagents.listing(self.config)['main']['title'], 'Latest')

    def test_notification_cache_resumes_after_append_and_handles_partial_lines_and_rewrite(self):
        self.agent()
        self.write(self.main, [self.record('user', self.notification('abc', 'failed'))])
        # Put the notification and title well before the usage tail.
        with self.main.open('a') as stream:
            stream.write(json.dumps({'type': 'agent-name', 'agentName': 'Early title'}) + '\n')
            stream.write((json.dumps({'type': 'system', 'text': 'x' * 1024}) + '\n') * 300)
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'failed')
        size = self.main.stat().st_size
        notice = json.dumps(self.record('user', self.notification('abc', 'killed', 'Stopped now')))
        with self.main.open('a') as stream:
            stream.write(notice[:30])
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'failed')
        original = Path.open
        seeks = []

        def read(path, *args, **kwargs):
            opened = original(path, *args, **kwargs)
            if path != self.main:
                return opened
            wrapped = Mock(wraps=opened)
            wrapped.__enter__ = Mock(return_value=wrapped)
            wrapped.__exit__ = Mock(side_effect=lambda *args: opened.close())
            wrapped.seek.side_effect = lambda *args: (seeks.append(args), opened.seek(*args))[1]
            return wrapped

        with self.main.open('a') as stream:
            stream.write(notice[30:] + '\n')
        with patch.object(Path, 'open', read):
            result = subagents.listing(self.config)
        self.assertEqual(result['agents'][0]['state'], 'stopped')
        self.assertEqual(result['agents'][0]['summary'], 'Stopped now')
        self.assertEqual(result['main']['title'], 'Early title')
        self.assertEqual(seeks[-1], (size,))
        self.write(self.main, [self.record('user', self.notification('abc', 'completed'))])
        result = subagents.listing(self.config)
        self.assertEqual(result['agents'][0]['state'], 'finished')
        self.assertIsNone(result['main']['title'])

    def test_five_changes_to_one_file_leave_one_cache_entry(self):
        path = self.agent()
        for index in range(5):
            with path.open('a') as stream:
                stream.write(json.dumps(self.record('assistant', str(index))) + '\n')
            subagents._cached(path, subagents._parse)
        self.assertEqual(len(subagents._cache), 1)

    def test_listing_model_tokens_metadata_and_shape(self):
        self.agent(meta={'description': 'Check files', 'agentType': 'general-purpose', 'model': 'fallback',
                         'pickedEffortModel': 'other', 'effort': 'medium', 'spawnDepth': 2,
                         'requestShape': 'background'})
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'main-model', 'context': 64, 'title': None})
        self.assertEqual(listing['agents'], [{
            'id': 'abc', 'description': 'Check files', 'type': 'general-purpose', 'model': 'last-model',
            'effort': 'medium', 'background': True, 'state': 'running', 'started': self.stamp(-120),
            'ended': None, 'seconds': 120, 'tokens': 130, 'tools': 3, 'depth': 2, 'parent': None, 'summary': None}])

    def test_main_and_agent_use_last_usage_record(self):
        records = [self.record('assistant', 'first', model='with-usage', usage={'input_tokens': 8}),
                   self.record('assistant', 'last', model='without-usage')]
        self.write(self.main, records)
        self.agent(records=records)
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'with-usage', 'context': 8, 'title': None})
        self.assertEqual(listing['agents'][0]['model'], 'without-usage')
        self.assertEqual(listing['agents'][0]['tokens'], 8)

    def test_model_fallback_and_missing_usage_metadata(self):
        for meta, expected in (({'model': 'meta-model', 'pickedEffortModel': 'picked'}, 'meta-model'),
                               ({'pickedEffortModel': 'picked'}, 'picked'), ({}, None)):
            with self.subTest(meta=meta):
                self.agent(meta=meta, records=[self.record('user', 'prompt')])
                entry = subagents.listing(self.config)['agents'][0]
                self.assertEqual(entry['model'], expected)
                self.assertIsNone(entry['tokens'])
                self.assertEqual(entry['depth'], 1)
                self.assertFalse(entry['background'])

    def test_detail_prompt_steps_report_and_tool_status(self):
        self.agent(meta={'finished': True})
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(detail['prompt'], 'Check the files')
        self.assertEqual(detail['report'], 'Report complete')
        self.assertEqual(detail['steps'][0], {'kind': 'text', 'text': 'Work starts'})
        self.assertEqual(detail['steps'][1], {'kind': 'tool', 'id': 'read-1', 'name': 'Read',
                                             'title': '/work/a.py', 'status': 'complete', 'is_error': False})
        self.assertEqual(detail['steps'][2]['status'], 'complete')
        self.assertTrue(detail['steps'][2]['is_error'])
        self.assertEqual(detail['steps'][3]['status'], 'pending')
        self.assertEqual(detail['steps'][-1], {'kind': 'text', 'text': 'Report complete'})
        self.assertNotIn('steps_cut', detail)
        for key, value in subagents.listing(self.config)['agents'][0].items():
            self.assertEqual(detail[key], value)

    def test_text_parts_and_byte_bounds(self):
        long_text = '🙂' * 10000
        self.agent(meta={'finished': True}, records=[
            self.record('user', [{'type': 'text', 'text': 'First'}, {'type': 'image'},
                                 {'type': 'text', 'text': long_text}]),
            self.record('assistant', long_text)])
        detail = subagents.detail(self.config, 'abc')
        self.assertTrue(detail['prompt'].startswith('First\n'))
        for value, limit in ((detail['prompt'], 16384), (detail['report'], 16384),
                             (detail['steps'][0]['text'], 4000)):
            self.assertLessEqual(len(value.encode()), limit)
            self.assertIn('[truncated]', value)
            self.assertNotIn('�', value)

    def test_steps_keep_last_400_and_tools_count_all_calls(self):
        self.agent(meta={'finished': True}, records=[self.record('assistant', [
            {'type': 'tool_use', 'id': f'call-{index}', 'name': 'Read'} for index in range(405)]),
            self.record('user', [{'type': 'tool_result', 'tool_use_id': 'call-404', 'is_error': True}])])
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(len(detail['steps']), 400)
        self.assertEqual(detail['steps'][0]['id'], 'call-5')
        self.assertTrue(detail['steps'][-1]['is_error'])
        self.assertEqual(detail['tools'], 405)
        self.assertTrue(detail['steps_cut'])

    def test_tool_detail_reads_sidechain_and_keeps_main_lookup_isolated(self):
        path = self.agent()
        result = subagents.tool(self.config, 'abc', 'bash-1')
        self.assertEqual(result['id'], 'bash-1')
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['is_error'])
        self.assertIn('false', result['detail'])
        self.assertIn('Process exited with code 2', result['detail'])
        self.assertEqual(subagents.tool(self.config, 'abc', 'pending-1')['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Tool call is no longer available'):
            tool_details.lookup(dict(self.config, transcript=path), 'bash-1')
        with self.assertRaises(ValueError):
            subagents.tool(self.config, 'abc', 'unknown')

    def test_invalid_and_missing_ids_are_rejected_for_detail_and_tool(self):
        self.agent()
        for identifier in ('../x', '', 'x/y', '/abc', 'x\\y', 'a' * 65, 'abc\n', None, 'missing'):
            for reader, args in ((subagents.detail, (identifier,)), (subagents.tool, (identifier, 'read-1'))):
                with self.subTest(identifier=identifier, reader=reader.__name__):
                    with self.assertRaisesRegex(ValueError, '^That agent is no longer available\\.$'):
                        reader(self.config, *args)

    def test_path_escape_via_symlink_is_rejected(self):
        self.write(self.root / 'outside.jsonl', [self.record('user', 'private')])
        (self.folder / 'agent-escape.jsonl').symlink_to(self.root / 'outside.jsonl')
        self.assertEqual(subagents.listing(self.config)['agents'], [])
        for reader, args in ((subagents.detail, ('escape',)), (subagents.tool, ('escape', 'read-1'))):
            with self.assertRaisesRegex(ValueError, '^That agent is no longer available\\.$'):
                reader(self.config, *args)

    def test_metadata_cannot_escape_folder(self):
        self.agent()
        outside = self.root / 'outside.json'
        outside.write_text('{"description":"private","finished":true}')
        (self.folder / 'agent-abc.meta.json').symlink_to(outside)
        entry = subagents.listing(self.config)['agents'][0]
        self.assertIsNone(entry['description'])
        self.assertEqual(entry['state'], 'running')

    def test_non_claude_and_missing_transcript_or_folder_return_empty(self):
        self.agent()
        for config in (dict(self.config, agent='codex'), dict(self.config, agent='antigravity'),
                       {'agent': 'claude'}, {'agent': 'claude', 'transcript': ''},
                       dict(self.config, transcript=str(self.root / 'missing.jsonl'))):
            with self.subTest(config=config):
                self.assertEqual(subagents.listing(config), {'main': {'model': None, 'context': None, 'title': None}, 'agents': []})
                with self.assertRaises(ValueError):
                    subagents.detail(config, 'abc')

    def test_malformed_lines_and_metadata_are_skipped(self):
        path = self.agent()
        original = path.read_bytes()
        path.write_bytes(b'{bad\nnull\n[]\n\xff\n{"type":"assistant","message":null}\n' + original + b'{unfinished')
        (self.folder / 'agent-abc.meta.json').write_text('{broken')
        (self.folder / 'agent-broken.jsonl').write_text('null\n[]\n{broken')
        listing = subagents.listing(self.config)
        self.assertEqual([entry['id'] for entry in listing['agents']], ['abc'])
        self.assertEqual(listing['agents'][0]['tokens'], 130)
        self.assertEqual(listing['agents'][0]['started'], self.stamp(-120))
        self.assertEqual(subagents.detail(self.config, 'abc')['prompt'], 'Check the files')
        self.assertEqual(subagents.tool(self.config, 'abc', 'read-1')['status'], 'complete')

    def test_unreadable_files_are_skipped(self):
        path = self.agent()
        original = Path.open

        def read(path_, *args, **kwargs):
            if path_ == path:
                raise PermissionError('unreadable')
            return original(path_, *args, **kwargs)

        with patch.object(Path, 'open', read):
            self.assertEqual(subagents.listing(self.config)['agents'], [])
            with self.assertRaises(ValueError):
                subagents.detail(self.config, 'abc')

    def test_missing_timestamps_and_usage_return_nulls(self):
        self.agent(records=[{'type': 'assistant', 'message': {'content': 'hello'}}], age=1000)
        entry = subagents.listing(self.config)['agents'][0]
        for key in ('started', 'ended', 'seconds', 'tokens', 'model'):
            self.assertIsNone(entry[key])

    def test_newest_40_sorted_by_started_time(self):
        for index in reversed(range(43)):
            self.agent(f'agent_{index}', records=[self.record('user', 'prompt', seconds=-1000 + index)])
        entries = subagents.listing(self.config)['agents']
        self.assertEqual([entry['id'] for entry in entries], [f'agent_{index}' for index in range(3, 43)])

    def test_main_reads_only_tail_and_ignores_later_non_usage_records(self):
        self.agent()
        self.main.write_bytes(b'x' * (subagents.TAIL_BYTES + 100) + b'\n' + json.dumps(
            self.record('assistant', 'recent', model='tail-model', usage={'input_tokens': 9})).encode() + b'\n' +
            json.dumps(self.record('assistant', 'no usage', model='ignore')).encode() + b'\n')
        original = Path.open
        stream = None

        def read(path, *args, **kwargs):
            nonlocal stream
            opened = original(path, *args, **kwargs)
            if path == self.main:
                stream = Mock(wraps=opened)
                stream.__enter__ = Mock(return_value=stream)
                stream.__exit__ = Mock(side_effect=lambda *args: opened.close())
                return stream
            return opened

        with patch.object(Path, 'open', read):
            self.assertEqual(subagents.listing(self.config)['main'], {'model': 'tail-model', 'context': 9, 'title': None})
        stream.read.assert_called_once_with(subagents.TAIL_BYTES)
        self.assertGreater(stream.seek.call_args_list[1].args[0], 0)

    def test_cache_reuses_reads_and_invalidates_on_transcript_and_meta_changes(self):
        path = self.agent(meta={})
        first = subagents.listing(self.config)
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged files must stay cached')):
            self.assertEqual(subagents.listing(self.config), first)
            subagents.detail(self.config, 'abc')
        with path.open('a') as stream:
            stream.write(json.dumps(self.record('assistant', 'New report', model='new-model',
                                                usage={'input_tokens': 99})) + '\n')
        (self.folder / 'agent-abc.meta.json').write_text('{"finished":true}')
        self.write(self.main, [self.record('assistant', 'main', model='new-main', usage={'output_tokens': 3})])
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'new-main', 'context': 3, 'title': None})
        self.assertEqual(listing['agents'][0]['state'], 'finished')
        self.assertEqual(listing['agents'][0]['tokens'], 99)
        self.assertEqual(subagents.detail(self.config, 'abc')['report'], 'New report')

    def test_cache_observes_mtime_with_same_size_and_size_with_same_mtime(self):
        path = self.agent(records=[self.record('assistant', 'one')])
        subagents.detail(self.config, 'abc')
        info = path.stat()
        path.write_text(path.read_text().replace('one', 'two'))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][0]['text'], 'two')
        info = path.stat()
        with path.open('a') as stream:
            stream.write(json.dumps(self.record('assistant', 'three')) + '\n')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][-1]['text'], 'three')

    def test_cached_state_and_duration_age_without_file_changes(self):
        self.agent(age=899)
        self.assertEqual(subagents.detail(self.config, 'abc')['state'], 'running')
        with patch.object(subagents.time, 'time', return_value=self.now + 2):
            self.assertEqual(subagents.detail(self.config, 'abc')['state'], 'stopped')

    def test_cache_is_bounded_and_response_mutation_does_not_change_it(self):
        self.agent()
        detail = subagents.detail(self.config, 'abc')
        detail['steps'][0]['text'] = 'changed'
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][0]['text'], 'Work starts')
        with patch.object(subagents, 'CACHE_LIMIT', 3):
            for index in range(10):
                self.agent(str(index))
            subagents.listing(self.config)
            self.assertLessEqual(len(subagents._cache), 3)

    def test_routes_select_session_and_require_auth(self):
        self.agent()
        for route in ('subagents', 'subagent?id=abc', 'subagent-tool?agent=abc&id=read-1'):
            with self.subTest(route=route):
                separator = '&' if '?' in route else '?'
                handler = object.__new__(server.Handler)
                handler.path = '/mobile/api/' + route + separator + 'session=%2512'
                handler.deny = Mock(return_value=False)
                handler.respond = Mock()
                with patch.object(server, 'choose', return_value=self.config) as choose:
                    handler.do_GET()
                    choose.assert_called_once_with('%12')
                self.assertEqual(handler.respond.call_args.args[0], 200)
                handler.deny.return_value = True
                with patch.object(server, 'choose') as choose:
                    handler.do_GET()
                    choose.assert_not_called()

    def test_routes_handle_value_errors_like_tool_route(self):
        for route in ('tool?id=missing', 'subagent?id=../x', 'subagent-tool?agent=../x&id=call'):
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/' + route
            handler.deny = Mock(return_value=False)
            handler.respond = Mock()
            with patch.object(server, 'choose', return_value=self.config):
                handler.do_GET()
            handler.respond.assert_called_once_with(503, {'error': 'The conversation is currently unavailable.'})


if __name__ == '__main__':
    unittest.main()
