import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest import mock

# The worker loads faster-whisper models at import; replace them with stubs.
fake = types.ModuleType('faster_whisper')
fake.WhisperModel = mock.MagicMock()
fake.decode_audio = mock.MagicMock()
with mock.patch.dict(sys.modules, {'faster_whisper': fake}):
    import transcribe_worker as worker


def word(text, start, end):
    return NS(word=text, start=start, end=end)


class SettledTest(unittest.TestCase):
    def test_cuts_inside_one_long_segment(self):
        # One segment runs to the end of the clip: the old code saved nothing here.
        words = [word(' one', 0.0, 0.5), word(' two', 0.55, 1.0), word(' three', 1.05, 1.5), word(' four', 1.55, 2.0)]
        self.assertEqual(worker.settled([NS(words=words)], 1.2), ('one two', 1.025))

    def test_prefers_a_recent_pause(self):
        words = [word(' a', 0.0, 0.4), word(' b', 0.8, 1.2), word(' c', 1.25, 1.6), word(' d', 1.65, 2.0)]
        text, end = worker.settled([NS(words=words)], 1.7)
        self.assertEqual(text, 'a')
        self.assertAlmostEqual(end, 0.6)

    def test_ignores_an_old_pause(self):
        words = [word(' a', 0.0, 0.4), word(' b', 1.0, 1.4)] + [word(' w', 1.45 + i * 0.5, 1.9 + i * 0.5) for i in range(10)]
        text, end = worker.settled([NS(words=words)], 6.5)
        self.assertEqual(text.split()[:2], ['a', 'b'])
        self.assertGreater(end, 5.0)

    def test_nothing_settled(self):
        self.assertIsNone(worker.settled([NS(words=[word(' late', 3.0, 3.5)])], 2.0))
        self.assertIsNone(worker.settled([NS(words=None)], 2.0))

    def test_commit_moves_done_forward(self):
        item = worker.Recording()
        item.done = (2.0, 'hello')
        words = [word(' there', 0.0, 0.5), word(' friend', 0.55, 1.0), word(' tail', 6.0, 6.5)]
        samples = [0.0] * int(9.0 * worker.RATE)
        with mock.patch.object(worker, 'accurate', return_value=[NS(words=words)]) as accurate:
            worker.commit(item, samples)
        accurate.assert_called_once()
        self.assertEqual(accurate.call_args.kwargs, {'words': True})
        self.assertEqual(item.done[1], 'hello there friend')
        self.assertAlmostEqual(item.done[0], 2.0 + 3.5)  # middle of the 1.0 s to 6.0 s pause
        self.assertFalse(item.lock.locked())


if __name__ == '__main__':
    unittest.main()
