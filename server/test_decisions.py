from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import autonomy
import decisions
import paths


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.object(paths, 'CONFIG', Path(temp.name) / 'config')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = paths.CONFIG / 'decisions.md'
        self.now = datetime(2026, 11, 11, 9, 30)

    def record(self, question='Choose?', answer='Default'):
        decisions.record(question, answer, now=self.now)

    def test_format_header_and_append(self):
        self.record()
        self.record('Proceed?', 'Yes')
        self.assertEqual(self.path.read_text(), decisions.HEADER
                         + '- 2026-11-11: Q: Choose? -> A: Default\n'
                         + '- 2026-11-11: Q: Proceed? -> A: Yes\n')

    def test_control_characters_are_removed_and_whitespace_collapses(self):
        self.record('  A\n\tB\x00\x1b\x7f\x85 C\u200b?  ', '  Y\r\nes\t please\x00\u202e  ')
        self.assertEqual(self.path.read_text(), decisions.HEADER + '- 2026-11-11: Q: A B C? -> A: Y es please\n')

    def test_question_and_answer_are_capped_separately(self):
        self.record('q' * 250, 'a' * 350)
        self.assertEqual(self.path.read_text(), decisions.HEADER
                         + '- 2026-11-11: Q: ' + 'q' * 200 + ' -> A: ' + 'a' * 300 + '\n')

    def test_oldest_entries_drop_after_two_hundred(self):
        for index in range(205):
            self.record(f'Question {index}', f'Answer {index}')
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 202)
        self.assertEqual(lines[2], '- 2026-11-11: Q: Question 5 -> A: Answer 5')
        self.assertEqual(lines[-1], '- 2026-11-11: Q: Question 204 -> A: Answer 204')

    def test_atomic_replace_uses_complete_temp_file_in_the_same_folder(self):
        self.record()
        old = self.path.read_text()
        replace = decisions.os.replace
        def check(temp, destination):
            self.assertEqual(Path(temp).parent, self.path.parent)
            self.assertEqual(destination, self.path)
            self.assertEqual(self.path.read_text(), old)
            self.assertEqual(Path(temp).read_text(), old + '- 2026-11-11: Q: Again? -> A: Yes\n')
            replace(temp, destination)
        with mock.patch.object(decisions.os, 'replace', side_effect=check) as replaced:
            self.record('Again?', 'Yes')
        replaced.assert_called_once()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_replace_preserves_the_old_file_and_removes_temp_file(self):
        self.record()
        old = self.path.read_text()
        with mock.patch.object(decisions.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.record('Again?', 'Yes')
        self.assertEqual(self.path.read_text(), old)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_answers_do_not_overwrite_each_other(self):
        errors = []
        def write(index):
            try:
                self.record(f'Question {index}')
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=write, args=(index,)) for index in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        lines = self.path.read_text().splitlines()[2:]
        self.assertEqual(set(lines), {f'- 2026-11-11: Q: Question {index} -> A: Default' for index in range(12)})

    def test_autonomy_reads_the_same_decisions_path(self):
        self.assertNotIn('Before you ask the user, read', autonomy.rules({'level': 4, 'source': 'global'}))
        self.record()
        self.assertIn(autonomy.DECISIONS.format(path=self.path.resolve()), autonomy.rules({'level': 4, 'source': 'global'}))


if __name__ == '__main__':
    unittest.main()
