"""One authenticated WebSocket for browser pictures, input, and acknowledgements."""
import base64
import hashlib
import json
import shutil
import socket
import struct
import threading
import time

import auth
import browser
import browser_cdp
import h264

STILL_AFTER = .25
MAX_MESSAGE = 256 * 1024


def _metadata(frame, browser_id):
    # Keep the wire fields identical to the established SSE contract.
    header = json.loads(browser.frame_event(frame, browser_id).split(b'data: ', 1)[1])
    del header['jpeg']
    return header


def serve(handler, the_browser, target, width, height, browser_id='test', css_size=None, codec='jpeg',
          finder=shutil.which, encoder_factory=h264.Encoder):
    # Imported here because browser_api dispatches to this module.
    import browser_api as api

    handler.close_connection = True
    origins = handler.headers.get_all('Origin', [])
    try:
        origin_ok = len(origins) == 1 and auth.same_origin(origins[0], handler.headers.get('Host', ''))
    except ValueError:
        origin_ok = False
    if not origin_ok:
        handler.respond(403, {'error': 'Invalid Origin.'})
        return
    key = handler.headers.get('Sec-WebSocket-Key', '')
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid_key = False
    if (not valid_key or handler.headers.get('Upgrade', '').lower() != 'websocket'
            or 'upgrade' not in [s.strip() for s in handler.headers.get('Connection', '').lower().split(',')]
            or handler.headers.get('Sec-WebSocket-Version') != '13'):
        handler.respond(400, {'error': 'WebSocket required.'})
        return
    tab = the_browser.watch(target, width, height, css_size) if css_size is not None else the_browser.watch(target, width, height)
    stopped = threading.Event()
    lock = threading.RLock()
    flow = api.Flow()
    encoder = reader = None
    active_codec = 'h264' if codec == 'h264' and finder('ffmpeg') else 'jpeg'
    latest = None
    last_video = None
    still_sent = False
    wrote = time.monotonic()

    def abort():
        stopped.set()
        with flow.changed:
            flow.changed.notify_all()
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def send(opcode, data=b''):
        nonlocal wrote
        with lock:
            if stopped.is_set():
                return
            try:
                handler.wfile.write(browser_cdp.frame(opcode, data))
                handler.wfile.flush()
                wrote = time.monotonic()
            except OSError:
                abort()

    def message(kind, header=None, payload=b''):
        head = json.dumps(dict(header or {}, type=kind), separators=(',', ':')).encode()
        send(2, struct.pack('!I', len(head)) + head + payload)

    def encoded(kind, header, payload):
        nonlocal active_codec, last_video, still_sent
        with lock:
            if stopped.is_set() or active_codec != 'h264':
                return
            if kind == 'failed':
                active_codec = 'jpeg'
                message('hello', {'codec': 'jpeg'})
                if latest:
                    message('frame', latest[0], latest[1])
            else:
                message(kind, header, payload)
                if kind == 'video':
                    last_video, still_sent = time.monotonic(), False

    def maybe_still():
        nonlocal still_sent
        with lock:
            if (active_codec == 'h264' and latest and last_video is not None and not still_sent
                    and time.monotonic() - last_video >= STILL_AFTER
                    and the_browser.clock() - tab.last_input >= STILL_AFTER):
                message('still', latest[0], latest[1])
                still_sent = True

    pending_input = []
    typed = threading.Condition()

    def merged(events):
        # Each event is one blocking CDP call, so input that queued while Chrome was busy is merged first:
        # a run of moves keeps the last one, a run of wheel turns becomes one turn with the summed distance.
        out = []
        for event in events:
            last = out[-1] if out else None
            kind = event.get('type') if isinstance(event, dict) else None
            if last is not None and kind == 'move' and last.get('type') == 'move':
                out[-1] = event
            elif last is not None and kind == 'wheel' and last.get('type') == 'wheel' and all(
                    last.get(k) == event.get(k) for k in ('x', 'y', 'modifiers')):
                out[-1] = dict(last, dx=float(last.get('dx') or 0) + float(event.get('dx') or 0),
                               dy=float(last.get('dy') or 0) + float(event.get('dy') or 0))
            else:
                out.append(event)
        return out

    def type_input():
        while True:
            with typed:
                while not pending_input and not stopped.is_set():
                    typed.wait(.5)
                if stopped.is_set():
                    return
                events = merged(pending_input)
                pending_input.clear()
            try:
                for start in range(0, len(events), 200):
                    the_browser.input(tab.target, events[start:start + 200])
            except (browser.BrowserError, ValueError, TypeError) as exc:
                message('error', {'error': str(exc)})

    def read():
        try:
            for raw in browser_cdp.websocket_messages(handler.rfile, send, limit=MAX_MESSAGE):
                try:
                    body = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(body, dict):
                    continue
                if body.get('type') == 'ack' and type(body.get('seq')) is int:
                    flow.ack(body['seq'])
                elif body.get('type') == 'input':
                    events = body.get('events')
                    if not isinstance(events, list) or len(events) > 200:
                        message('error', {'error': 'Invalid input.'})
                        continue
                    with typed:
                        pending_input.extend(events)
                        typed.notify()
        except browser_cdp.WebSocketError as exc:
            send(8, struct.pack('!H', exc.code))
        except (OSError, EOFError):
            pass
        finally:
            abort()

    try:
        handler.protocol_version = 'HTTP/1.1'
        handler.send_response(101)
        handler.send_header('Upgrade', 'websocket')
        handler.send_header('Connection', 'Upgrade')
        accept = hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()
        handler.send_header('Sec-WebSocket-Accept', base64.b64encode(accept).decode())
        handler.end_headers()
        handler.wfile.flush()
        handler.connection.settimeout(None)
        message('hello', {'codec': active_codec})
        if active_codec == 'h264':
            encoder = encoder_factory(encoded, finder=finder)
        reader = threading.Thread(target=read, daemon=True, name='browser-live-reader')
        reader.start()
        typist = threading.Thread(target=type_input, daemon=True, name='browser-live-input')
        typist.start()
        checked = next_frame = time.monotonic()
        seen, version, last_picture, first = 0, None, None, True
        while not stopped.is_set():
            if encoder is not None and active_codec == 'jpeg':
                encoder.close()
                encoder = None
            stopped.wait(max(0.0, next_frame - time.monotonic()))
            # Preserve Flow's ACK_WAIT deadline while servicing the still timer and disconnects.
            deadline = time.monotonic() + api.ACK_WAIT
            while not stopped.is_set():
                maybe_still()
                with flow.changed:
                    if len(flow.sent) < api.ACK_WINDOW:
                        break
                    left = deadline - time.monotonic()
                    if left <= 0:
                        flow.sent.pop(0)
                        break
                    flow.changed.wait(min(left, STILL_AFTER))
            if stopped.is_set():
                break
            if time.monotonic() - checked >= 1:
                checked = time.monotonic()
                the_browser.refresh(tab)
            if tab.target not in the_browser.tabs:
                message('closed', api._tag({}, browser_id))
                break
            if the_browser.version != version:
                version = the_browser.version
                state = the_browser.state()
                try:
                    state['nav'] = the_browser.nav_state(tab.target)
                except browser.BrowserError:
                    state['nav'] = {'back': False, 'forward': False}
                message('tabs', api._tag(state, browser_id))
            warm = the_browser.clock() - tab.last_input < api.WARM_AFTER_INPUT
            wait = api.KEEP_WARM if warm else 1.0
            with lock:
                if active_codec == 'h264':
                    # Output can arrive after this wait begins, including the first video.
                    wait = min(wait, STILL_AFTER)
                    if last_video is not None and not still_sent:
                        wait = min(wait, max(.001, STILL_AFTER - (time.monotonic() - last_video)))
            captured = the_browser.first_frame(tab) if first else the_browser.wait_frame(tab, seen, wait)
            first = False
            if captured is not None:
                seen = captured[0]
                if the_browser.heal(tab, captured):
                    metadata = captured[1] or {}
                    picture = (captured[2], tuple(metadata.get(k) for k in (
                        'deviceWidth', 'deviceHeight', 'offsetTop', 'scrollOffsetX', 'scrollOffsetY', 'pageScaleFactor')))
                    if picture != last_picture:
                        header = _metadata(captured, browser_id)
                        jpeg = base64.b64decode(captured[2])
                        with lock:
                            flow.sending(captured[0])
                            latest = (header, jpeg)
                            frame_encoder = encoder if active_codec == 'h264' else None
                        next_frame = time.monotonic() + api.FRAME_INTERVAL
                        if frame_encoder is not None:
                            frame_encoder.push(jpeg, header)
                        else:
                            message('frame', header, jpeg)
                        last_picture = picture
            maybe_still()
            if not warm and time.monotonic() - wrote >= api.KEEPALIVE:
                message('idle', api._tag({}, browser_id))
            if warm and time.monotonic() - wrote >= api.KEEP_WARM:
                send(9)
            the_browser.touch()
    except (OSError, ValueError, browser.BrowserError) as exc:
        message('error', {'error': str(exc)})
    finally:
        abort()
        if encoder is not None:
            encoder.close()
        if reader is not None:
            reader.join(timeout=2)
        with typed:
            typed.notify_all()
        the_browser.unwatch(tab, (width, height), css_size)
