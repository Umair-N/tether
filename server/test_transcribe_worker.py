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
        with mock.patch.object(worker, 'accurate', return_value=([NS(words=words)], None)) as accurate:
            worker.commit(item, samples)
        accurate.assert_called_once()
        self.assertEqual(accurate.call_args.kwargs, {'words': True, 'stop': worker.final_waits})
        self.assertEqual(item.done[1], 'hello there friend')
        self.assertAlmostEqual(item.done[0], 2.0 + 3.5)  # middle of the 1.0 s to 6.0 s pause
        self.assertFalse(item.lock.locked())



def segment(text, end):
    return NS(text=text, end=end, words=None)


class FinalPassTest(unittest.TestCase):
    """The 175 s hang (3 Nov 2026): a final pass must not retry forever or wait behind background passes."""

    def setUp(self):
        # The stub returns one shared mock for both models; give each its own.
        for name in ('MODEL', 'LIVE_MODEL'):
            patcher = mock.patch.object(worker, name, mock.MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_accurate_pass_uses_one_temperature_and_a_token_cap(self):
        worker.MODEL.transcribe.return_value = (iter([segment(' hi', 1.0)]), None)
        found, cut = worker.accurate([0.0] * 16000)
        self.assertEqual(([item.text for item in found], cut), ([' hi'], None))
        options = worker.MODEL.transcribe.call_args.kwargs
        self.assertEqual((options['temperature'], options['condition_on_previous_text'], options['max_new_tokens']), (0.0, False, worker.MAX_TOKENS))

    def test_final_over_budget_lets_the_fast_model_finish(self):
        clock = iter([0.0, 0.0, 5.0, 25.0, 26.0, 27.0])
        worker.MODEL.transcribe.return_value = (iter([segment(' first part', 10.0), segment(' second part', 20.0), segment(' never', 30.0)]), None)
        worker.LIVE_MODEL.transcribe.return_value = (iter([segment(' rough rest', 0.0)]), None)
        samples = [0.0] * (40 * worker.RATE)
        with mock.patch.object(worker.time, 'monotonic', side_effect=lambda: next(clock, 99.0)), mock.patch.object(worker, 'FINAL_BUDGET', 20.0):
            self.assertEqual(worker.final(samples), 'first part second part rough rest')
        rest = worker.LIVE_MODEL.transcribe.call_args.args[0]
        self.assertEqual(len(rest), 20 * worker.RATE)  # the fast model starts where the accurate one stopped
        self.assertEqual(worker.WAITING[0], 0)

    def test_final_gives_up_on_a_busy_lock_and_uses_the_fast_model(self):
        worker.LIVE_MODEL.transcribe.return_value = (iter([segment(' all rough', 0.0)]), None)
        worker.FINAL_LOCK.acquire()
        try:
            with mock.patch.object(worker, 'FINAL_BUDGET', 0.05):
                self.assertEqual(worker.final([0.0] * 16000), 'all rough')
        finally:
            worker.FINAL_LOCK.release()
        worker.MODEL.transcribe.assert_not_called()

    def test_background_pass_stops_when_a_final_waits(self):
        item = worker.Recording()
        worker.MODEL.transcribe.return_value = (iter([segment(' a', 5.0), segment(' b', 10.0)]), None)
        worker.WAITING[0] += 1
        try:
            worker.commit(item, [0.0] * (20 * worker.RATE))
        finally:
            worker.WAITING[0] -= 1
        worker.MODEL.transcribe.assert_not_called()  # it does not even start
        self.assertEqual(item.done, (0.0, ''))

    def test_background_pass_stops_mid_way_and_keeps_what_settled(self):
        item = worker.Recording()
        words = [word(' one', 0.0, 1.0), word(' two', 1.5, 2.0)]
        segments = [NS(text=' one two', end=2.0, words=words), NS(text=' later', end=12.0, words=[word(' later', 11.0, 12.0)])]
        worker.MODEL.transcribe.return_value = (iter(segments), None)
        with mock.patch.object(worker, 'final_waits', side_effect=[False] + [True] * 5):
            worker.commit(item, [0.0] * (20 * worker.RATE))
        self.assertEqual(item.done[1], 'one')  # cut in the pause before "two"; "later" was never reached
        self.assertAlmostEqual(item.done[0], 1.25)


if __name__ == '__main__':
    unittest.main()
