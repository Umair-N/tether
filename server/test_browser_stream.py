"""Capture freshness and stream pacing. All CDP traffic uses FakeChrome."""
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser
import browser_api
from test_browser import make


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.browser = make()
        self.addCleanup(self.fake.close)
        self.addCleanup(self.browser.stop)
        self.browser.list_tabs()
        self.tab = self.browser.watch('T1', 2560, 1600, (1280, 800))

    def test_ack_on_receipt_even_without_viewer_consumption(self):
        self.browser.watch('T1', 2560, 1600, (1280, 800))
        self.assertEqual(self.tab.viewers, 2)
        with patch.object(self.browser.conn, 'send') as send:
            for seq in range(1, 5):
                self.browser._event('Page.screencastFrame', {
                    'data': str(seq), 'metadata': {}, 'sessionId': 7}, self.tab.session)
            self.assertEqual(send.call_count, 4)
            self.assertEqual(self.tab.frame[0], 4)
            self.assertEqual(self.browser.wait_frame(self.tab, 1, 0)[0], 4)
            for _ in range(2):
                self.browser.ack(self.tab, self.tab.frame)
            self.assertEqual(send.call_count, 4)

    def test_cap_is_thirty(self):
        self.assertAlmostEqual(browser_api.FRAME_INTERVAL, 1 / 30)

    def test_optional_scroll_metadata(self):
        meta = {'deviceWidth': 1280, 'deviceHeight': 800,
                'scrollOffsetX': 0, 'scrollOffsetY': 123.5, 'pageScaleFactor': 1.25}
        data = json.loads(browser.frame_event((1, meta, 'jpeg', 7)).split(b'data: ')[1])
        for field in ('scrollOffsetX', 'scrollOffsetY', 'pageScaleFactor'):
            self.assertEqual(data.get(field), meta[field])
        data = json.loads(browser.frame_event((2, {}, 'jpeg', 0)).split(b'data: ')[1])
        self.assertNotIn('scrollOffsetY', data)

    def test_click_and_typing_keep_the_sharp_size(self):
        with patch.object(browser.threading, 'Timer'):
            self.browser.input('T1', [{'type': 'down', 'x': 5, 'y': 5}, {'type': 'up', 'x': 5, 'y': 5},
                                      {'type': 'keydown', 'key': 'a'}, {'type': 'text', 'text': 'hello'}])
        self.assertEqual(self.tab.casting, (2560, 1600))
        self.assertEqual(self.tab.motion_until, 0.0)

    def test_input_records_browser_clock_once_before_dispatch(self):
        self.assertEqual(self.tab.last_input, 0.0)
        self.browser.clock = Mock(return_value=123.0)
        with patch.object(self.browser, 'ensure'), patch.object(self.browser.conn, 'call') as call:
            call.side_effect = lambda *args, **kwargs: self.assertEqual(self.tab.last_input, 123.0)
            self.browser.input('T1', [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}])
        self.browser.clock.assert_called_once_with()
        self.assertEqual(call.call_count, 2)

    def test_motion_restores_sharp_static_frame_without_blank(self):
        now = [10.0]
        self.browser.clock = lambda: now[0]
        retained = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'old', 7)
        self.tab.seq, self.tab.frame = 1, retained
        answer = self.fake.answer
        def screenshot(message):
            if message['method'] == 'Page.captureScreenshot':
                self.assertEqual(message['params']['quality'], 80)
                return {'data': 'sharp'}
            return answer(message)
        with patch.object(browser.threading, 'Timer') as timer, patch.object(self.fake, 'answer', screenshot):
            self.browser.input('T1', [{'type': 'wheel', 'x': 5, 'y': 5, 'dx': 0, 'dy': 10}])
            self.assertEqual(self.tab.casting, (1280, 800))
            self.assertIs(self.tab.frame, retained)
            starts = len(self.fake.sent('Page.startScreencast'))
            now[0] += 0.2
            self.browser.input('T1', [{'type': 'keydown', 'key': 'ArrowDown'}])
            self.assertEqual(len(self.fake.sent('Page.startScreencast')), starts)
            self.browser._settle_cast(self.tab)
            self.assertEqual(self.tab.casting, (1280, 800))
            now[0] += 0.301
            self.browser._settle_cast(self.tab)
            self.assertEqual(self.tab.casting, (2560, 1600))
            self.assertEqual(self.tab.frame[2:], ('sharp', 0))
            self.assertGreater(self.tab.frame[0], retained[0])
            self.assertTrue(timer.return_value.daemon)
            self.assertTrue(all(c['params']['quality'] == 80 for c in self.fake.sent('Page.startScreencast')))

    def test_motion_floor_viewers_and_timer_cleanup(self):
        with patch.object(browser.threading, 'Timer') as timer:
            self.browser.watch('T1', 640, 480, (320, 240))
            self.browser.input('T1', [{'type': 'wheel', 'x': 5, 'y': 5, 'dx': 0, 'dy': 10}])
            self.assertEqual(self.tab.casting, (1280, 800))  # larger viewer still sets the floor
            self.browser.unwatch(self.tab, (2560, 1600), (1280, 800))
            self.assertEqual(self.tab.casting, (320, 240))
            self.browser.unwatch(self.tab, (640, 480), (320, 240))
            timer.return_value.cancel.assert_called_once()
            starts = len(self.fake.sent('Page.startScreencast'))
            self.browser._settle_cast(self.tab)
            self.assertEqual(len(self.fake.sent('Page.startScreencast')), starts)

    def test_phone_touch_marks_motion_and_stop_cancels_timer(self):
        self.browser.set_device('T1', 'pixel')
        with patch.object(browser.threading, 'Timer') as timer:
            self.browser.input('T1', [{'type': 'down', 'x': 5, 'y': 5}])
            self.assertEqual(self.tab.casting, (1280, 800))  # capture bounds, not a new page viewport
            self.assertEqual(self.fake.sent('Input.dispatchTouchEvent')[-1]['params']['type'], 'touchStart')
            self.browser.stop()
            timer.return_value.cancel.assert_called_once()

    def test_canceled_callback_cannot_replace_new_viewer_timer(self):
        with patch.object(browser.threading, 'Timer'):
            self.browser._moving(self.tab)
            generation = self.tab.motion_generation
            self.browser.unwatch(self.tab)
            self.browser.watch('T1', 2560, 1600, (1280, 800))
            self.browser._moving(self.tab)
            timer = self.tab.motion_timer
            self.browser._settle_cast(self.tab, generation)
            self.assertIs(self.tab.motion_timer, timer)
            self.assertEqual(self.tab.casting, (1280, 800))

    def test_unknown_css_size_preserves_full_resolution(self):
        self.browser.unwatch(self.tab)
        self.browser.watch('T1', 2560, 1600)
        with patch.object(browser.threading, 'Timer'):
            self.browser._moving(self.tab)
            self.assertEqual(self.tab.casting, (2560, 1600))

    def test_upscaled_viewer_and_multiple_viewer_css_floors(self):
        self.browser.unwatch(self.tab)
        self.browser.watch('T1', 1600, 1600, (1000, 1000))
        with patch.object(browser.threading, 'Timer'):
            self.browser._moving(self.tab)
            self.assertEqual(self.tab.casting, (1000, 1000))
            self.browser.watch('T1', 1600, 1600)  # unknown-DPR viewer forbids reduction
            self.assertEqual(self.tab.casting, (1600, 1600))
            self.browser.unwatch(self.tab, (1600, 1600))
            self.assertEqual(self.tab.casting, (1000, 1000))

    def test_css_query_validation_and_dispatch(self):
        from test_browser import FakeHandler
        handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&w=2560&h=1600&cssw=1280&cssh=800')
        with patch.object(browser_api, 'stream') as stream:
            browser_api.get(handler, self.browser)
        stream.assert_called_once_with(handler, self.browser, 'T1', 2560, 1600, 'test', css_size=(1280, 800))
        for query in ('cssw=1', 'cssw=-1&cssh=800', 'cssw=nan&cssh=800', 'cssw=99999&cssh=800'):
            handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&' + query)
            browser_api.get(handler, self.browser)
            self.assertEqual(handler.replies[0][0], 400)

    def test_idle_fallback_capture_does_not_hold_input_lock(self):
        self.tab.casting = (1280, 800)
        with patch.object(self.browser, 'wait_frame', return_value=None), \
                patch.object(self.browser, '_snapshot') as snapshot:
            def capture(tab):
                # Another input thread must acquire the lock while capture waits on CDP.
                import threading
                acquired = []
                def input_lock():
                    with self.browser.lock:
                        acquired.append(True)
                worker = threading.Thread(target=input_lock)
                worker.start()
                worker.join(1)
                self.assertEqual(acquired, [True])
            snapshot.side_effect = capture
            self.browser._settle_cast(self.tab)
            snapshot.assert_called_once()

    def test_idle_skips_screenshot_when_sharp_cast_paints(self):
        self.tab.casting = (1280, 800)
        with patch.object(self.browser, 'wait_frame', return_value=(1, {}, 'sharp', 7)), \
                patch.object(self.browser, '_snapshot') as snapshot:
            self.browser._settle_cast(self.tab)
            snapshot.assert_not_called()

    def test_idle_wait_excludes_frames_received_during_restart(self):
        self.tab.casting = (1280, 800)
        def restart(tab, size):
            tab.casting = size
            tab.seq += 1  # a late frame from the stopped motion cast
        with patch.object(self.browser, '_start_cast', side_effect=restart), \
                patch.object(self.browser, 'wait_frame', return_value=None) as wait, \
                patch.object(self.browser, '_snapshot'):
            self.browser._settle_cast(self.tab)
            wait.assert_called_once_with(self.tab, 1, 0.1)


class StreamTests(unittest.TestCase):
    def run_idle_stream(self, last_input, lifetime, frames=None):
        now = [100.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        clock.sleep = lambda delay: now.__setitem__(0, now[0] + delay)
        tab = SimpleNamespace(target='T1', last_input=last_input)
        fake = Mock(tabs={'T1': tab}, version=0, clock=lambda: now[0] + 1000)
        fake.watch.return_value = tab
        fake.first_frame.return_value = None
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        waits, writes = [], []
        def wait_frame(tab, seen, timeout):
            waits.append(timeout)
            delay, frame = (frames or {}).get(len(waits), (timeout + 1e-9, None))
            now[0] += delay
            return frame
        fake.wait_frame.side_effect = wait_frame
        class Sink(io.BytesIO):
            def write(self, data):
                writes.append((now[0], data))
                return super().write(data)
        with patch.object(browser_api, 'time', clock), patch.object(browser_api, 'STREAM_LIFETIME', lifetime):
            browser_api.stream(Mock(wfile=Sink()), fake, 'T1', 1280, 800)
        fake.unwatch.assert_called_once_with(tab, (1280, 800), None)
        return waits, writes

    def test_without_recent_input_preserves_idle_events(self):
        for last_input in (0.0, 1085.0):  # never, or exactly 15 seconds ago
            with self.subTest(last_input=last_input):
                waits, writes = self.run_idle_stream(last_input, 16)
                self.assertTrue(waits)
                self.assertTrue(all(timeout == 1.0 for timeout in waits))
                self.assertFalse(any(data == b':\n\n' for _, data in writes))
                idle = [when for when, data in writes if data.startswith(b'event: idle')]
                self.assertEqual(len(idle), 1)
                self.assertAlmostEqual(idle[0], 115.0)

    def test_recent_input_writes_comments_every_eighty_ms(self):
        waits, writes = self.run_idle_stream(1099.0, 0.3)
        self.assertTrue(waits)
        self.assertTrue(all(timeout == browser_api.KEEP_WARM for timeout in waits))
        comments = [when for when, data in writes if data == b':\n\n']
        self.assertGreaterEqual(len(comments), 3)
        for before, after in zip([100.0] + comments, comments):
            self.assertAlmostEqual(after - before, 0.08)
        self.assertFalse(any(data.startswith(b'event: idle') for _, data in writes))

    def test_frame_write_resets_comment_timer(self):
        _, writes = self.run_idle_stream(1099.0, 0.25, {1: (0.04, (1, {}, 'jpeg', 7))})
        frames = [when for when, data in writes if data.startswith(b'event: frame')]
        self.assertEqual(frames, [100.04])
        comments = [when for when, data in writes if data == b':\n\n']
        self.assertTrue(comments)
        self.assertAlmostEqual(comments[0] - frames[0], browser_api.FRAME_INTERVAL + 0.08)

    def test_warm_window_expires(self):
        waits, writes = self.run_idle_stream(1085.1, 1.2)
        self.assertEqual(waits[:2], [browser_api.KEEP_WARM] * 2)
        self.assertTrue(all(timeout == 1.0 for timeout in waits[2:]))
        self.assertEqual(sum(data == b':\n\n' for _, data in writes), 2)

    def test_pacing_takes_newest_after_sleep_and_slow_write(self):
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        clock.sleep = lambda delay: now.__setitem__(0, now[0] + delay)
        tab = SimpleNamespace(target='T1', last_input=0.0)
        emitted = []
        def newest(*args):
            seq = int(round(now[0] * 120)) + 1
            return (seq, {}, str(seq), 7)
        fake = Mock(tabs={'T1': tab}, version=0, clock=lambda: now[0] + 1000)
        fake.watch.return_value = tab
        fake.first_frame.side_effect = newest
        fake.wait_frame.side_effect = newest
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        class Sink(io.BytesIO):
            def write(self, data):
                if data.startswith(b'event: frame'):
                    seq = json.loads(data.split(b'data: ')[1])['seq']
                    emitted.append((now[0], seq))
                    if len(emitted) == 2:
                        now[0] += 0.2  # capture continues while this socket stalls
                    if len(emitted) == 4:
                        raise BrokenPipeError()
                return super().write(data)
        handler = Mock(wfile=Sink())
        with patch.object(browser_api, 'time', clock):
            browser_api.stream(handler, fake, 'T1', 2560, 1600)
        self.assertEqual([seq for _, seq in emitted], [1, 5, 29, 33])
        self.assertTrue(all(b[0] - a[0] >= 1 / 30 - 1e-9 for a, b in zip(emitted, emitted[1:])))
        fake.ack.assert_not_called()  # CDP receipt owns acknowledgments
        fake.unwatch.assert_called_once_with(tab, (2560, 1600), None)


class FlowTests(unittest.TestCase):
    """A panel that sends `sid` gets at most ACK_WINDOW unconfirmed frames, so frames cannot queue in the network."""

    def start(self, sid='abcdef123456'):
        tab = SimpleNamespace(target='T1', last_input=0.0)
        counter = [0]
        def newest(*args):
            time.sleep(0.005)
            counter[0] += 1
            return (counter[0], {}, 'x', 7)
        fake = Mock(tabs={'T1': tab}, version=0, clock=time.monotonic)
        fake.watch.return_value = tab
        fake.first_frame.side_effect = newest
        fake.wait_frame.side_effect = newest
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        sent, stop = [], threading.Event()
        class Sink(io.BytesIO):
            def write(self, data):
                if stop.is_set():
                    raise BrokenPipeError()
                if data.startswith(b'event: frame'):
                    sent.append(json.loads(data.split(b'data: ')[1])['seq'])
                return super().write(data)
        thread = threading.Thread(target=browser_api.stream, args=(Mock(wfile=Sink()), fake, 'T1', 800, 600),
                                  kwargs={'sid': sid}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(3)))
        return sent

    def test_unconfirmed_frames_wait_and_ack_releases_the_next(self):
        with patch.object(browser_api, 'ACK_WAIT', 5):
            sent = self.start()
            time.sleep(0.4)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW, 'Only the window may be in flight without acks.')
            browser_api.ack('abcdef123456', sent[0])
            time.sleep(0.2)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW + 1)
            browser_api.ack('abcdef123456', sent[-1])
            time.sleep(0.2)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW + 3)

    def test_lost_acks_slow_the_stream_but_never_stop_it(self):
        with patch.object(browser_api, 'ACK_WAIT', 0.15):
            sent = self.start()
            time.sleep(0.7)
            self.assertGreaterEqual(len(sent), browser_api.ACK_WINDOW + 2)
            self.assertLess(len(sent), 10)

    def test_ack_route_and_validation(self):
        from test_browser import FakeHandler
        handler = FakeHandler(browser_api.PREFIX + 'ack', {'sid': 'unknown-stream', 'seq': 3})
        browser_api.post(handler, Mock())
        self.assertEqual(handler.replies[0], (200, {'ok': True}))
        handler = FakeHandler(browser_api.PREFIX + 'ack', {'sid': 'unknown-stream', 'seq': '3'})
        browser_api.post(handler, Mock())
        self.assertEqual(handler.replies[0][0], 400)
        handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&w=800&h=600&sid=bad!')
        fake = Mock()
        browser_api.get(handler, fake)
        self.assertEqual(handler.replies[0][0], 400)
        fake.watch.assert_not_called()

    def test_stream_end_drops_its_flow(self):
        sent = self.start('flow-to-drop')
        time.sleep(0.1)
        self.assertIn('flow-to-drop', browser_api.FLOWS)
        self.doCleanups()
        self.assertNotIn('flow-to-drop', browser_api.FLOWS)
