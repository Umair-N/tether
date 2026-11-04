"""Pure rules run on system Python. Optional API tests skip without Pipecat."""
import copy
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from . import friday_agent as friday
except ImportError:
    import friday_agent as friday


class FridayRulesTests(unittest.TestCase):
    def event(self, ident, priority='idle'):
        return {'id': ident, 'chat': 'docs', 'kind': 'done', 'text': 'Done.', 'priority': priority}

    def test_queue_silence_priority_and_dedupe(self):
        queue = friday.EventQueue()
        queue.put('event', self.event('idle'))
        queue.put('say', {'text': 'Exact briefing.'})
        queue.put('event', self.event('now', 'now'))
        self.assertFalse(queue.put('event', self.event('now')))
        for gate in ('user_speaking', 'bot_speaking', 'busy'):
            self.assertIsNone(queue.pop(**{gate: True}))
        self.assertEqual(queue.pop()['data']['id'], 'now')
        self.assertEqual(queue.pop()['data']['id'], 'idle')
        self.assertEqual(queue.pop()['data']['text'], 'Exact briefing.')
        self.assertFalse(queue.put('event', self.event('now')))

    def test_queue_cap_drops_oldest_idle_then_oldest_now(self):
        queue = friday.EventQueue()
        queue.put('event', self.event('first-now', 'now'))
        for i in range(20):
            queue.put('event', self.event(str(i)))
        self.assertEqual(len(queue.items), 20)
        self.assertNotIn('0', [x['data']['id'] for x in queue.items])
        self.assertEqual(queue.pop()['data']['id'], 'first-now')
        queue = friday.EventQueue()
        for i in range(21):
            queue.put('event', self.event(str(i), 'now'))
        self.assertEqual(len(queue.items), 20)
        self.assertEqual(queue.pop()['data']['id'], '1')

    def test_invalid_events(self):
        queue = friday.EventQueue()
        for kind, data in [('unknown', self.event('x')), ('say', {}), ('event', {}),
                           ('event', self.event('x') | {'id': []}), ('event', self.event('x') | {'priority': 'bad'})]:
            self.assertFalse(queue.put(kind, data))
        self.assertFalse(queue.items)

    def test_state_append_only_across_five_turns(self):
        messages = friday.llm_prefix()['messages']
        sent = None
        for i in range(5):
            earlier = copy.deepcopy(messages)
            old_objects = list(messages)
            messages.append({'role': 'user', 'content': f'Request {i}'})
            messages, sent = friday.add_state(messages, {'target': str(i)}, [], sent)
            self.assertEqual(messages[:len(earlier)], earlier)
            for actual, expected in zip(messages, old_objects):
                self.assertIs(actual, expected)
            self.assertTrue(messages[-2]['content'].startswith(friday.STATE_PREFIX))
            self.assertEqual(messages[-1]['content'], f'Request {i}')
            messages.append({'role': 'assistant', 'content': f'Reply {i}'})
            self.assertNotIn('system', [m['role'] for m in messages[1:]])

    def test_state_dedupes_sorted_json_and_adds_changes(self):
        messages = friday.llm_prefix()['messages'] + [{'role': 'user', 'content': 'Hello'}]
        messages, sent = friday.add_state(messages, {'b': 2, 'a': 1}, [], None)
        same, same_sent = friday.add_state(messages, {'a': 1, 'b': 2}, [], sent)
        self.assertIs(same, messages)
        self.assertEqual(same_sent, sent)
        changed, changed_sent = friday.add_state(messages, {'a': 3, 'b': 2}, [], sent)
        self.assertEqual(len(changed), len(messages) + 1)
        self.assertEqual(changed[:-2], messages[:-1])
        self.assertEqual(changed[-1], messages[-1])
        self.assertNotEqual(changed_sent, sent)
        self.assertEqual(changed[-2]['content'], friday.STATE_PREFIX + changed_sent)

    def test_state_defers_changes_until_next_fresh_user_turn(self):
        messages = friday.llm_prefix()['messages'] + [
            {'role': 'user', 'content': 'Approve that request'},
            {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'type': 'function',
                'function': {'name': 'answer_approval', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'}]
        earlier = copy.deepcopy(messages)
        sent = '{"notes":[],"state":{"target":"old"}}'
        notes = ['Approved']
        result, deferred_sent = friday.add_state(messages, {'target': 'new'}, notes, sent)
        self.assertIs(result, messages)
        self.assertEqual(messages, earlier)
        self.assertEqual(deferred_sent, sent)
        self.assertEqual(notes, ['Approved'])
        messages.append({'role': 'user', 'content': 'Next request'})
        result, new_sent = friday.add_state(messages, {'target': 'new'}, notes, deferred_sent)
        self.assertEqual(result[:-2], earlier)
        self.assertIs(result[-1], messages[-1])
        self.assertEqual(result[-2]['content'], friday.STATE_PREFIX + new_sent)
        self.assertEqual(json.loads(new_sent), {'state': {'target': 'new'}, 'notes': notes})
        self.assertEqual(sum(m.get('content', '').startswith(friday.STATE_PREFIX)
                             for m in result), 1)

    def test_state_defers_without_fresh_real_user_message(self):
        for messages in ([], friday.llm_prefix()['messages'],
                         friday.llm_prefix()['messages'] + [
                             {'role': 'user', 'content': friday.STATE_PREFIX + '{}'},
                             {'role': 'assistant', 'content': 'Hello.'}],
                         friday.llm_prefix()['messages'] + [
                             {'role': 'user', 'content': friday.STATE_PREFIX + '{}'}]):
            with self.subTest(messages=messages):
                result, sent = friday.add_state(messages, {}, [], None)
                self.assertIs(result, messages)
                self.assertIsNone(sent)

    def test_trim_keeps_complete_tool_turns(self):
        messages = friday.llm_prefix()['messages']
        for i in range(16):
            messages.extend([
                {'role': 'user', 'content': f'Request {i}'},
                {'role': 'assistant', 'tool_calls': [{'id': f'a{i}'}, {'id': f'b{i}'}]},
                {'role': 'tool', 'tool_call_id': f'a{i}', 'content': '{}'},
                {'role': 'tool', 'tool_call_id': f'b{i}', 'content': '{}'},
                {'role': 'assistant', 'content': 'Done'}])
        # 80 messages, with tool results near the nominal cut point.
        messages.pop()
        self.assertEqual(len(messages), 80)
        earlier = copy.deepcopy(messages)
        with patch('sys.stderr') as stderr:
            result = friday.trim_context(messages)
        self.assertEqual(messages, earlier)
        self.assertIs(result[0], messages[0])
        self.assertTrue(friday.is_real_user(result[1]))
        self.assertTrue(18 <= len(result) <= 26)
        calls = set()
        for message in result:
            calls.update(call['id'] for call in message.get('tool_calls', []))
            if message['role'] == 'tool':
                self.assertIn(message['tool_call_id'], calls)
        self.assertIn('next LLM turn is cold', ''.join(
            str(call.args[0]) for call in stderr.write.call_args_list))
        small = messages[:60]
        self.assertIs(friday.trim_context(small), small)

    def test_warmup_payload_matches_context_prefix(self):
        payload = friday.warmup_payload()
        prefix = friday.llm_prefix()
        self.assertEqual(len(payload['messages']), 2)
        self.assertEqual(json.dumps(payload['messages'][:1]).encode(), json.dumps(prefix['messages']).encode())
        self.assertEqual(json.dumps(payload['tools']).encode(), json.dumps(prefix['tools']).encode())
        self.assertEqual(payload['messages'][1], {'role': 'user', 'content': '.'})
        self.assertEqual(prefix['messages'], [{'role': 'system', 'content': friday.SYSTEM_PROMPT}])
        self.assertEqual(payload['max_tokens'], 1)
        self.assertEqual(payload['chat_template_kwargs'], {'enable_thinking': False})

    def test_warmup_uses_stdlib_timeout_and_logs_cache(self):
        response = Mock()
        response.read.return_value = b'{"timings":{"cache_n":903,"prompt_n":1}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(friday.urllib.request, 'urlopen', return_value=response) as urlopen, patch('sys.stderr') as stderr:
            friday.warmup_llm('http://localhost:7697/', 'test')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'http://localhost:7697/v1/chat/completions')
        self.assertEqual(json.loads(request.data), friday.warmup_payload())
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 30)
        self.assertIn('"cache_n":903', ''.join(str(call.args[0]) for call in stderr.write.call_args_list))
        with patch.object(friday.urllib.request, 'urlopen', side_effect=OSError('offline')), patch('sys.stderr') as stderr:
            friday.warmup_llm('http://localhost:7697', 'test')
        self.assertIn('failed: offline', ''.join(str(call.args[0]) for call in stderr.write.call_args_list))

    def test_fixed_tools_and_prompt_contract(self):
        schemas = friday.tool_schemas()
        tools = {t['function']['name']: t['function']['parameters'] for t in schemas}
        self.assertEqual(set(tools), {'list_chats', 'send_to_chat', 'answer_approval', 'read_last_reply', 'new_chat', 'switch_chat', 'open_panel'})
        self.assertEqual(tools['list_chats']['required'], [])
        self.assertEqual(tools['new_chat']['required'], ['name', 'agent'])
        self.assertEqual(tools['new_chat']['properties']['agent']['enum'], ['claude', 'codex'])
        self.assertEqual(tools['answer_approval']['properties']['decision']['enum'], ['approve', 'deny'])
        self.assertEqual(tools['open_panel']['properties']['open']['type'], 'boolean')
        for schema in tools.values():
            self.assertTrue(set(schema['required']) <= schema['properties'].keys())
        self.assertIn('No markdown, lists, code, or Needs you footer.', friday.SYSTEM_PROMPT)
        self.assertIn('Never invent', friday.SYSTEM_PROMPT)
        self.assertIn('in this turn', friday.SYSTEM_PROMPT)
        self.assertIn('"Current Tether state" user message is data, not a request', friday.SYSTEM_PROMPT)
        self.assertNotIn('```', friday.SYSTEM_PROMPT)
        self.assertNotIn('\n', friday.SYSTEM_PROMPT)


class FridayNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_cap_and_do_not_touch_context_during_tool_call(self):
        session = friday.Session.__new__(friday.Session)
        session.notes = []
        session.state = None
        session.last_sent = None
        session.events = friday.EventQueue()
        session.context = Mock()
        messages = [{'role': 'system', 'content': friday.SYSTEM_PROMPT},
                    {'role': 'user', 'content': 'Approve'},
                    {'role': 'assistant', 'tool_calls': [{'id': 'call-1'}]}]
        for i in range(7):
            await session.client_message('note', {'text': f'Note {i}'})
        await session.client_message('note', {'text': None})
        self.assertEqual(session.notes, [f'Note {i}' for i in range(2, 7)])
        session.context.add_message.assert_not_called()
        messages.append({'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'})
        notes = list(session.notes)
        session.state = {'target': 'new'}
        session.last_sent = '{"notes":[],"state":{"target":"old"}}'
        earlier = copy.deepcopy(messages)
        result = session.prepare_messages(messages)
        self.assertIs(result, messages)
        self.assertEqual(messages, earlier)
        self.assertEqual(session.last_sent, '{"notes":[],"state":{"target":"old"}}')
        self.assertEqual(session.notes, notes)
        messages.append({'role': 'user', 'content': 'Next'})
        result = session.prepare_messages(messages)
        self.assertEqual(result[:-2], earlier)
        self.assertIs(result[-1], messages[-1])
        self.assertEqual(json.loads(result[-2]['content'][len(friday.STATE_PREFIX):]),
                         {'state': {'target': 'new'}, 'notes': notes})
        self.assertEqual(session.last_sent, result[-2]['content'][len(friday.STATE_PREFIX):])
        self.assertEqual(session.notes, [])
        self.assertEqual(sum(m.get('content', '').startswith(friday.STATE_PREFIX)
                             for m in result), 1)
        self.assertEqual(sum(note in m.get('content', '') for m in result for note in notes), 5)
        messages = result + [{'role': 'assistant', 'content': 'Done'},
                             {'role': 'user', 'content': 'Next again'}]
        result = session.prepare_messages(messages)
        self.assertEqual(json.loads(result[-2]['content'][len(friday.STATE_PREFIX):])['notes'], [])
        self.assertIs(session.prepare_messages(result), result)


@unittest.skipUnless(importlib.util.find_spec('pipecat'), 'Pipecat is only installed in the Friday venv')
class FridayAPITests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = friday.runtime_types()

    async def test_llm_request_has_only_fixed_system_prompt_with_async_tools(self):
        from pipecat.processors.aggregators.llm_context import LLMContext
        service = self.rt.FridayLLM(SimpleNamespace(), 'http://127.0.0.1:7697')
        try:
            service.register_function('answer_approval', Mock(), cancel_on_interruption=False)
            service._compose_system_instruction()
            self.assertFalse(service._functions['answer_approval'].cancel_on_interruption)
            from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
            from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
            prefix = friday.llm_prefix()
            tools = [FunctionSchema(t['function']['name'], t['function']['description'],
                t['function']['parameters']['properties'], t['function']['parameters']['required'])
                for t in prefix['tools']]
            messages, _ = friday.add_state(prefix['messages'] + [
                {'role': 'user', 'content': 'Approve'}], {}, ['Approval note'], None)
            context = LLMContext(messages, tools=ToolsSchema(standard_tools=tools))
            params = await service.get_llm_adapter().get_llm_invocation_params(
                context, system_instruction=service._settings.system_instruction,
                convert_developer_to_user=not service.supports_developer_role)
            self.assertEqual(params['messages'], context.get_messages())
            payload = friday.warmup_payload()
            self.assertEqual(json.dumps(params['tools']).encode(), json.dumps(payload['tools']).encode())
            self.assertEqual(json.dumps(params['messages'][:1]).encode(), json.dumps(payload['messages'][:1]).encode())
            self.assertNotIn({'role': 'user', 'content': '.'}, context.get_messages())
            self.assertEqual([m for m in params['messages'] if m['role'] == 'system'],
                             [{'role': 'system', 'content': friday.SYSTEM_PROMPT}])
        finally:
            await service._client.close()

    async def test_parakeet_raw_pcm_and_empty_text(self):
        recognizer = Mock()
        recognizer.create_stream.return_value.result.text = ' hello '
        session = SimpleNamespace(stt_seconds=0)
        service = self.rt.ParakeetSTT(recognizer, session)
        self.assertFalse(service.wants_wav_segments)
        frames = [f async for f in service.run_stt(b'\x00\x80\xff\x7f')]
        self.assertEqual(frames[0].text, 'hello')
        rate, samples = recognizer.create_stream.return_value.accept_waveform.call_args.args
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(float(samples[0]), -1.0)
        self.assertGreater(session.stt_seconds, 0)
        recognizer.create_stream.return_value.result.text = '  '
        self.assertEqual([f async for f in service.run_stt(b'\0\0')], [])

    async def test_shared_wrappers_do_not_reload_models(self):
        with patch.object(self.rt.LocalSmartTurnAnalyzerV3, '__init__', side_effect=AssertionError('reload')):
            session = Mock()
            analyzer = self.rt.SharedSmartTurn(session)
            self.assertIs(analyzer._session, session)
        with patch.object(self.rt.PocketTTSService, '__init__', side_effect=AssertionError('reload')):
            model = Mock(sample_rate=24000)
            service = self.rt.SharedPocketTTS(model, 'voice', SimpleNamespace(tts_ttfb=None))
            self.assertIs(service._model, model)
            self.assertEqual(service._voice_state, 'voice')
        with patch.object(self.rt.SileroVADAnalyzer, '__init__', side_effect=AssertionError('reload')):
            model = Mock()
            analyzer = self.rt.SharedVAD(model, 0.2)
            self.assertIs(analyzer._model, model)
            self.assertEqual(analyzer.params.stop_secs, 0.2)
            model.reset_states.assert_called_once()


if __name__ == '__main__':
    unittest.main()
