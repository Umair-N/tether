import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalog


def listing(names, content, initial=True):
    return json.dumps({'type': 'attachment', 'attachment': {'type': 'skill_listing', 'names': names, 'content': content, 'isInitial': initial}}) + '\n'


class TranscriptSkillTests(unittest.TestCase):
    def setUp(self):
        catalog._listings.clear()

    def test_reads_bundled_claude_ai_and_plugin_skills(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / 't.jsonl'
            transcript.write_text('{"type":"user"}\n' + listing(
                ['simplify', 'anthropic-skills:docx', 'atlassian:triage-issue'],
                '- simplify: Review the changed code\n- anthropic-skills:docx: Word files: read and write\n- atlassian:triage-issue: Triage bugs'))
            found = catalog.transcript_skills(str(transcript))
            self.assertEqual(found, {'simplify': 'Review the changed code', 'anthropic-skills:docx': 'Word files: read and write',
                                     'atlassian:triage-issue': 'Triage bugs'})
            # A later listing adds to the set; a half-written line waits for the next call.
            with transcript.open('a') as handle:
                handle.write(listing(['loop'], '- loop: Run on an interval', initial=False) + '{"type":"skill_li')
            self.assertIn('loop', catalog.transcript_skills(str(transcript)))
            # A new initial listing (resume, /clear) replaces the old set.
            with transcript.open('a') as handle:
                handle.write('stin"}\n' + listing(['pr'], '- pr: Write a PR body'))
            self.assertEqual(catalog.transcript_skills(str(transcript)), {'pr': 'Write a PR body'})

    def test_missing_transcript_gives_nothing(self):
        self.assertEqual(catalog.transcript_skills(None), {})
        self.assertEqual(catalog.transcript_skills('/nonexistent/t.jsonl'), {})


class ClaudeCatalogTests(unittest.TestCase):
    def test_plugin_prefix_and_synced_plugins_and_transcript(self):
        catalog._listings.clear()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            claude, home = root / '.claude', root / 'home'
            plugin = claude / 'plugins' / 'cache' / 'market' / 'atlassian-mcp' / 'v1'
            (plugin / '.claude-plugin').mkdir(parents=True)
            (plugin / '.claude-plugin' / 'plugin.json').write_text('{"name": "atlassian"}')
            (plugin / 'skills' / 'triage').mkdir(parents=True)
            (plugin / 'skills' / 'triage' / 'SKILL.md').write_text('---\nname: triage-issue\ndescription: Triage bugs\n---\n')
            (claude / 'settings.json').write_text('{"enabledPlugins": {"atlassian-mcp@market": true}}')
            synced = claude / 'plugins' / 'synced' / 'org_acct' / 'engineering~g2'
            (synced / '.claude-plugin').mkdir(parents=True)
            (synced / '.claude-plugin' / 'plugin.json').write_text('{"name": "engineering"}')
            (synced / 'skills' / 'standup').mkdir(parents=True)
            (synced / 'skills' / 'standup' / 'SKILL.md').write_text('---\nname: standup\ndescription: Write a standup\n---\n')
            home.mkdir()
            (home / '.claude.json').write_text('{"oauthAccount": {"organizationUuid": "org", "accountUuid": "acct"}}')
            transcript = root / 't.jsonl'
            transcript.write_text(listing(['atlassian:triage-issue', 'simplify'], '- atlassian:triage-issue: Triage bugs\n- simplify: Clean up'))
            with mock.patch.object(catalog.paths, 'CLAUDE', claude), mock.patch.object(catalog, 'HOME', home), \
                    mock.patch.object(catalog.claude_probe, 'commands', return_value=None):
                result = catalog.catalog('claude', None, str(transcript))
            names = [item['name'] for item in result['commands']]
            self.assertIn('atlassian:triage-issue', names)
            self.assertNotIn('atlassian-mcp:triage-issue', names)
            self.assertIn('engineering:standup', names)
            self.assertIn('simplify', names)
            self.assertIn('fast', names)
            status = next(item for item in result['commands'] if item['name'] == 'clear')
            self.assertIn('new', status['aliases'])
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(result['plugins'][0]['name'], 'atlassian')


class DocsParserTests(unittest.TestCase):
    def test_rows_aliases_and_skills(self):
        import update_claude_commands as update
        page = (
            '| Command | Purpose |\n| :-- | :-- |\n'
            '| `/clear [name]` | Start a new conversation with empty context. Pass a name. Aliases: `/reset`, `/new` |\n'
            '| `/loop [interval] [prompt]` | **[Skill](https://x/skills.md#bundled-skills).** Run a prompt repeatedly. Alias: `/proactive` |\n'
            '| `/status` | Open the [Settings](https://x) interface, showing version, model, account, and connectivity. More text |\n')
        self.assertEqual(update.parse(page), [
            {'name': 'clear', 'description': 'Start a new conversation with empty context.', 'aliases': ['reset', 'new'], 'kind': 'builtin'},
            {'name': 'loop', 'description': 'Run a prompt repeatedly.', 'aliases': ['proactive'], 'kind': 'skill'},
            {'name': 'status', 'description': 'Open the Settings interface, showing version, model, account, and connectivity.', 'aliases': [], 'kind': 'builtin'},
        ])


if __name__ == '__main__':
    unittest.main()
