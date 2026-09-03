#!/usr/bin/env python3
"""Tests for the canonical cp.py. No router required.

    python3 -m unittest discover -s tests -v
    python3 tests/test_cp.py

Two mock backends stand in for a router: an `AF_UNIX` listener speaking the
Config Store wire protocol (docs/cs-sock-protocol.md), and an HTTP server
speaking the REST API. Between them they cover both transports without hardware.

Coverage is deliberately weighted towards failure paths. A code review on
2026-08-17 found nine defects in this module, six of them only visible when the
backend misbehaves rather than when it answers correctly -- a hung socket
recorded as a healthy exchange, a probe that never retried, a caller error
blamed on the router. Every one of those has a regression test here, marked with
`regression:` in its docstring.
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cp  # noqa: E402


# ---------------------------------------------------------------------------
# Mock Config Store socket
# ---------------------------------------------------------------------------

def framed(payload, status='ok', content_length=None, body_override=None):
    """Build a response in the wire format: LF-separated headers, CRLFCRLF end."""
    if body_override is not None:
        body = body_override
    else:
        body = json.dumps(payload).encode()
    length = len(body) if content_length is None else content_length
    header = f'status: {status}\ncontent-length: {length}\n'.encode()
    return header + b'\r\n\r\n' + body


class MockConfigStore:
    """An AF_UNIX server that records commands and replies from a handler.

    handler(command_bytes) returns the bytes to send, or None to send nothing
    and hold the connection open (simulating a hung Config Store).
    """

    def __init__(self, handler, hold_seconds=2.0):
        self.handler = handler
        self.hold_seconds = hold_seconds
        self.commands = []
        self.directory = tempfile.mkdtemp(prefix='cp-test-')
        self.path = os.path.join(self.directory, 'cs.sock')
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                conn.settimeout(1.0)
                try:
                    command = conn.recv(65536)
                except socket.timeout:
                    command = b''
                self.commands.append(command.decode('utf-8', 'replace'))
                reply = self.handler(command)
                if reply is None:
                    # Accept, then never answer. This is what a wedged Config
                    # Store looks like from a client's point of view.
                    self._stop.wait(self.hold_seconds)
                else:
                    conn.sendall(reply)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        shutil.rmtree(self.directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Mock REST API
# ---------------------------------------------------------------------------

class MockRouterHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass                                    # keep the test output readable

    def _record_and_reply(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw_body = self.rfile.read(length) if length else b''
        request = {
            'method': self.command,
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'content_type': self.headers.get('Content-Type'),
            'body': raw_body.decode('utf-8', 'replace'),
        }
        self.server.requests.append(request)

        status, payload = self.server.responder(request)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_POST = do_DELETE = _record_and_reply


class MockRouter:
    def __init__(self, responder):
        self._server = HTTPServer(('127.0.0.1', 0), MockRouterHandler)
        self._server.responder = responder
        self._server.requests = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self):
        return f'127.0.0.1:{self._server.server_port}'

    @property
    def requests(self):
        return self._server.requests

    def close(self):
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# Base case
# ---------------------------------------------------------------------------

class CpTestCase(unittest.TestCase):
    """Resets cp's process-wide state, since it is a module not an object."""

    def setUp(self):
        self._saved = {
            'SOCKET_PATH': cp.SOCKET_PATH,
            'DOTENV_PATH': cp.DOTENV_PATH,
            '_RECV_TIMEOUT': cp._RECV_TIMEOUT,
            '_PROBE_COOLDOWN': cp._PROBE_COOLDOWN,
            '_MAX_RESPONSE_BYTES': cp._MAX_RESPONSE_BYTES,
            'APP_NAME': cp.APP_NAME,
        }
        self._logged = []
        self._original_log = cp.log
        cp.log = self._logged.append
        self.addCleanup(self._restore)
        self._reset_state()

    def _restore(self):
        # State first, log last: _reset_state is silent, so nothing leaks into
        # the test output on the way out.
        self._reset_state()
        for name, value in self._saved.items():
            setattr(cp, name, value)
        cp.log = self._original_log

    def _reset_state(self):
        with cp._lock:
            cp._transport.update(mode='socket', rest=None, ok=None, error=None,
                                 failures=0, successes=0, consecutive_failures=0,
                                 last_probe=0.0, last_failure_log=0.0)
            cp._warned.clear()

    @property
    def logs(self):
        return '\n'.join(str(line) for line in self._logged)

    def serve(self, handler, hold_seconds=2.0):
        store = MockConfigStore(handler, hold_seconds)
        self.addCleanup(store.close)
        cp.SOCKET_PATH = store.path
        return store


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------

class TestCommandFormat(CpTestCase):
    """The exact bytes each verb sends. Field counts are strict: a missing field
    hangs the Config Store rather than erroring."""

    def setUp(self):
        super().setUp()
        self.store = self.serve(lambda command: framed(None))

    def test_get_sends_four_fields(self):
        cp.get('status/gps/fix')
        self.assertEqual(self.store.commands[0], 'get\nstatus/gps/fix\n\n0\n')

    def test_get_passes_query_and_tree(self):
        cp.get('status/system', query='cpu', tree=1)
        self.assertEqual(self.store.commands[0], 'get\nstatus/system\ncpu\n1\n')

    def test_decrypt_matches_get_shape(self):
        cp.decrypt('config/certmgmt/certs/0/key')
        self.assertEqual(self.store.commands[0],
                         'decrypt\nconfig/certmgmt/certs/0/key\n\n0\n')

    def test_put_sends_json_encoded_value(self):
        cp.put('config/system/gps/enabled', True)
        self.assertEqual(self.store.commands[0],
                         'put\nconfig/system/gps/enabled\n\n0\ntrue\n')

    def test_put_quotes_a_bare_string(self):
        cp.put('config/system/system_id', 'router-1')
        self.assertEqual(self.store.commands[0],
                         'put\nconfig/system/system_id\n\n0\n"router-1"\n')

    def test_post_has_three_fields_and_no_tree(self):
        cp.post('config/system/sdk/appdata', {'name': 'a', 'value': '1'})
        self.assertEqual(self.store.commands[0],
                         'post\nconfig/system/sdk/appdata\n\n'
                         '{"name": "a", "value": "1"}\n')

    def test_delete_has_two_fields(self):
        cp.delete('config/wan/rules2/abc123')
        self.assertEqual(self.store.commands[0],
                         'delete\nconfig/wan/rules2/abc123\n\n')

    def test_alert_has_three_fields(self):
        cp.APP_NAME = 'test_app'
        cp.alert('tank level critical')
        self.assertEqual(self.store.commands[0],
                         'alert\ntest_app\ntank level critical\n')

    def test_non_ascii_value_is_escaped_by_json_not_rejected(self):
        cp.put('config/system/desc', 'caf\u00e9')
        self.assertEqual(self.store.commands[0],
                         'put\nconfig/system/desc\n\n0\n"caf\\u00e9"\n')


class TestResponseParsing(CpTestCase):
    def test_json_object_body_is_unwrapped(self):
        self.serve(lambda command: framed({'product_name': 'R980'}))
        self.assertEqual(cp.get('status/product_info'), {'product_name': 'R980'})

    def test_scalar_body(self):
        self.serve(lambda command: framed('connected'))
        self.assertEqual(cp.get('status/wan/connection_state'), 'connected')

    def test_null_body_is_none(self):
        self.serve(lambda command: framed(None))
        self.assertIsNone(cp.get('status/nothing/here'))

    def test_plain_string_body_falls_back_to_text(self):
        """alert replies and some put errors are not JSON."""
        self.serve(lambda command: framed(None, body_override=b"Alert added('hi')"))
        response = cp.put('config/x', 1)
        self.assertEqual(response['data'], "Alert added('hi')")

    def test_headers_may_be_in_any_order(self):
        def handler(command):
            body = b'{"ok": 1}'
            return (f'content-length: {len(body)}\nstatus: ok\n'.encode()
                    + b'\r\n\r\n' + body)

        self.serve(handler)
        self.assertEqual(cp.get('status/x'), {'ok': 1})

    def test_multi_word_status_is_read_whole(self):
        """regression: the status header was matched with \\w*, which truncated
        any value containing a space."""
        self.serve(lambda command: framed('nope', status='not found'))
        response = cp.put('config/x', 1)
        self.assertEqual(response['status'], 'not found')

    def test_body_split_across_packets_is_reassembled(self):
        payload = {'blob': 'x' * 40000}
        self.serve(lambda command: framed(payload))
        self.assertEqual(cp.get('status/big'), payload)

    def test_extra_bytes_past_content_length_are_ignored(self):
        def handler(command):
            return framed({'a': 1}) + b'trailing garbage'

        self.serve(handler)
        self.assertEqual(cp.get('status/x'), {'a': 1})


# ---------------------------------------------------------------------------
# Transport health: the three induced failure states
# ---------------------------------------------------------------------------

class TestSocketAbsent(CpTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, self.directory, True)
        cp.SOCKET_PATH = os.path.join(self.directory, 'absent.sock')

    def test_reads_return_none(self):
        self.assertIsNone(cp.get('status/product_info'))

    def test_reported_unavailable(self):
        self.assertFalse(cp.config_store_available())
        status = cp.config_store_status()
        self.assertFalse(status['available'])
        self.assertFalse(status['socket_exists'])
        self.assertIsNotNone(status['last_error'])

    def test_names_the_missing_volume(self):
        cp.get('status/product_info')
        self.assertIn('$CONFIG_STORE', self.logs)

    def test_repeated_failures_are_logged_once_then_throttled(self):
        for _ in range(25):
            cp.get('status/product_info')
        unreachable = [line for line in self._logged
                       if 'unreachable' in str(line)]
        self.assertEqual(len(unreachable), 1,
                         'a missing volume must not log once per poll')


class TestHungConfigStore(CpTestCase):
    """A backend that accepts the connection and never answers."""

    def setUp(self):
        super().setUp()
        cp._RECV_TIMEOUT = 0.3
        self.store = self.serve(lambda command: None, hold_seconds=1.0)

    def test_read_returns_none(self):
        self.assertIsNone(cp.get('status/product_info'))

    def test_recorded_as_a_failure_not_a_success(self):
        """regression: _receive returned a synthetic 'timeout' status as an
        ordinary value, so a hung Config Store was counted as healthy -- the one
        failure these counters exist to catch."""
        cp.get('status/product_info')
        status = cp.config_store_status()
        self.assertFalse(status['available'])
        self.assertEqual(status['successes'], 0)
        self.assertGreaterEqual(status['failures'], 1)
        self.assertIn('timeout', str(status['last_error']))

    def test_write_returns_none_rather_than_a_synthetic_response(self):
        """A caller must not be handed {'status': 'timeout'} as if the router
        had replied."""
        self.assertIsNone(cp.put('config/x', 1))

    def test_bounded_by_the_timeout(self):
        start = time.monotonic()
        cp.get('status/product_info')
        self.assertLess(time.monotonic() - start, 2.0)


class TestMalformedResponses(CpTestCase):
    def setUp(self):
        super().setUp()
        cp._RECV_TIMEOUT = 0.3

    def _assert_failed(self):
        status = cp.config_store_status()
        self.assertFalse(status['available'])
        self.assertEqual(status['successes'], 0)

    def test_closed_before_header_terminator(self):
        self.serve(lambda command: b'status: ok\ncontent-length: 2\n')
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()
        self.assertIn('malformed', self.logs)

    def test_body_shorter_than_content_length(self):
        """regression: a timeout while reading the body was not caught at all,
        so it took a different path from a timeout reading the header."""
        self.serve(lambda command: framed(None, content_length=500,
                                          body_override=b'{"a": 1}'),
                   hold_seconds=1.0)
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()

    def test_missing_status_header(self):
        def handler(command):
            body = b'{"a": 1}'
            return f'content-length: {len(body)}\n'.encode() + b'\r\n\r\n' + body

        self.serve(handler)
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()
        self.assertIn('no status header', self.logs)

    def test_missing_content_length_header(self):
        self.serve(lambda command: b'status: ok\n' + b'\r\n\r\n' + b'{"a": 1}')
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()
        self.assertIn('no content-length header', self.logs)

    def test_non_numeric_content_length(self):
        def handler(command):
            return b'status: ok\ncontent-length: banana\n' + b'\r\n\r\n' + b'{}'

        self.serve(handler)
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()

    def test_content_length_over_the_cap_is_refused(self):
        cp._MAX_RESPONSE_BYTES = 1024
        self.serve(lambda command: framed(None, content_length=99999,
                                          body_override=b'{}'))
        self.assertIsNone(cp.get('status/x'))
        self._assert_failed()
        self.assertIn('cap', self.logs)


class TestReprobing(CpTestCase):
    def test_reprobes_a_failed_backend_after_the_cooldown(self):
        """regression: the probe only ran while transport state was None, so a
        loop shaped `if not config_store_available(): report; continue` latched
        for the life of the process once the socket was missing at startup."""
        cp._PROBE_COOLDOWN = 0.0
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'late.sock')

        self.assertFalse(cp.config_store_available())

        store = MockConfigStore(lambda command: framed({'product_name': 'R980'}))
        self.addCleanup(store.close)
        cp.SOCKET_PATH = store.path

        self.assertTrue(cp.config_store_available(),
                        'a socket that appears later must be picked up')

    def test_cooldown_suppresses_probing_on_every_call(self):
        cp._PROBE_COOLDOWN = 3600.0
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')

        self.assertFalse(cp.config_store_available())
        failures = cp.config_store_status()['failures']
        for _ in range(5):
            self.assertFalse(cp.config_store_available())
        self.assertEqual(cp.config_store_status()['failures'], failures,
                         'within the cooldown the cached answer is reused')

    def test_success_clears_a_previous_failure(self):
        replies = [None, framed({'product_name': 'R980'})]
        cp._RECV_TIMEOUT = 0.3
        self.serve(lambda command: replies.pop(0) if replies else framed(None),
                   hold_seconds=0.5)
        self.assertIsNone(cp.get('status/product_info'))
        self.assertEqual(cp.get('status/product_info'), {'product_name': 'R980'})
        self.assertIsNone(cp.last_transport_error())
        self.assertTrue(cp.config_store_status()['available'])


# ---------------------------------------------------------------------------
# Caller errors are not transport failures
# ---------------------------------------------------------------------------

class TestCallerErrors(CpTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.serve(lambda command: framed({'ok': 1}))

    def test_newline_in_a_path_is_refused(self):
        """regression: paths were interpolated straight into a newline-delimited
        protocol, so a newline injected extra protocol fields. Refused rather
        than stripped -- a stripped path addresses a different node."""
        self.assertIsNone(cp.get('config/system/users/\nput\nconfig/admin'))
        self.assertEqual(self.store.commands, [], 'nothing may reach the router')
        self.assertIn('newline', self.logs)

    def test_carriage_return_in_a_path_is_refused(self):
        self.assertIsNone(cp.get('status/system\rmore'))
        self.assertEqual(self.store.commands, [])

    def test_newline_in_a_query_is_refused(self):
        self.assertIsNone(cp.get('status/system', query='a\nb'))
        self.assertEqual(self.store.commands, [])

    def test_empty_path_is_refused(self):
        self.assertIsNone(cp.get(''))
        self.assertEqual(self.store.commands, [])
        self.assertIn('path is empty', self.logs)

    def test_non_ascii_path_is_refused(self):
        self.assertIsNone(cp.get('status/caf\u00e9'))
        self.assertEqual(self.store.commands, [])
        self.assertIn('non-ASCII', self.logs)

    def test_non_serialisable_value_does_not_raise(self):
        """regression: json.dumps ran in the caller's frame, outside the error
        handling, so a bad value raised straight past the module's documented
        'accessors do not raise' contract."""
        self.assertIsNone(cp.put('config/x', object()))
        self.assertEqual(self.store.commands, [])
        self.assertIn('not JSON-serialisable', self.logs)

    def test_caller_errors_leave_transport_health_alone(self):
        """regression: an unencodable path was logged as 'config store
        unreachable' and counted as a transport failure, which then latched the
        availability flag against a perfectly healthy router."""
        self.assertEqual(cp.get('status/product_info'), {'ok': 1})
        self.assertTrue(cp.config_store_status()['available'])

        cp.get('status/caf\u00e9')
        cp.get('bad\npath')
        cp.put('config/x', object())

        status = cp.config_store_status()
        self.assertTrue(status['available'], 'the router is still reachable')
        self.assertEqual(status['failures'], 0)
        self.assertIsNone(status['last_error'])
        self.assertNotIn('unreachable', self.logs)


# ---------------------------------------------------------------------------
# Appdata
# ---------------------------------------------------------------------------

class AppdataStore:
    """A tiny appdata model, enough to exercise create/update/delete for real."""

    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.next_id = 100

    def handle(self, command):
        text = command.decode()
        lines = text.split('\n')
        verb, path = lines[0], lines[1]
        if verb == 'get':
            if path == cp._APPDATA_PATH:
                return framed(self.entries)
            return framed(None)
        if verb == 'post':
            value = json.loads(lines[3])
            self.next_id += 1
            self.entries.append({'_id_': f'id{self.next_id}',
                                 'name': value['name'],
                                 'value': value['value']})
            return framed('ok')
        if verb == 'put':
            # config/system/sdk/appdata/<id>/value
            parts = path.split('/')
            entry_id, field = parts[-2], parts[-1]
            for entry in self.entries:
                if entry['_id_'] == entry_id:
                    entry[field] = json.loads(lines[4])
            return framed('ok')
        if verb == 'delete':
            entry_id = path.split('/')[-1]
            self.entries = [e for e in self.entries if e['_id_'] != entry_id]
            return framed('ok')
        return framed(None)


class TestAppdata(CpTestCase):
    def setUp(self):
        super().setUp()
        self.model = AppdataStore([
            {'_id_': 'id1', 'name': 'poll_interval', 'value': '10'},
            {'_id_': 'id2', 'name': 'debug', 'value': 'false'},
        ])
        self.store = self.serve(self.model.handle)

    def test_get_by_name(self):
        self.assertEqual(cp.get_appdata('poll_interval'), '10')

    def test_get_is_case_insensitive(self):
        self.assertEqual(cp.get_appdata('POLL_Interval'), '10')

    def test_get_unset_name(self):
        self.assertIsNone(cp.get_appdata('missing'))

    def test_get_all_entries(self):
        self.assertEqual(len(cp.get_appdata()), 2)

    def test_put_updates_in_place(self):
        self.assertTrue(cp.put_appdata('poll_interval', '5'))
        self.assertEqual(cp.get_appdata('poll_interval'), '5')
        self.assertEqual(len(self.model.entries), 2, 'no new entry')

    def test_put_creates_when_absent(self):
        self.assertTrue(cp.put_appdata('new_setting', 'x'))
        self.assertEqual(cp.get_appdata('new_setting'), 'x')
        self.assertEqual(len(self.model.entries), 3)

    def test_put_coerces_to_string(self):
        self.assertTrue(cp.put_appdata('poll_interval', 2.5))
        self.assertEqual(cp.get_appdata('poll_interval'), '2.5')

    def test_put_matches_case_insensitively(self):
        """regression: put matched case-sensitively while get did not, so this
        created a duplicate entry and then reported False because its own
        read-back found the older one."""
        self.assertTrue(cp.put_appdata('Poll_Interval', '5'))
        self.assertEqual(len(self.model.entries), 2, 'must not duplicate')
        self.assertEqual(cp.get_appdata('poll_interval'), '5')

    def test_put_reports_false_when_the_write_does_not_land(self):
        """The write status is not trusted; only a read-back is."""
        model = AppdataStore([{'_id_': 'id1', 'name': 'a', 'value': 'old'}])
        model.handle = lambda command: (
            framed(model.entries) if command.startswith(b'get') else framed('ok')
        )
        self.serve(model.handle)
        self.assertFalse(cp.put_appdata('a', 'new'))

    def test_put_survives_an_entry_with_no_id(self):
        """regression: _id_ was indexed directly, so a malformed entry raised
        KeyError out of a function documented never to raise."""
        self.model.entries.append({'name': 'broken', 'value': '1'})
        self.assertFalse(cp.put_appdata('broken', '2'))
        self.assertIn('_id_', self.logs)

    def test_post_creates(self):
        self.assertTrue(cp.post_appdata('fresh', '1'))
        self.assertEqual(cp.get_appdata('fresh'), '1')

    def test_post_refuses_a_duplicate(self):
        """regression: post created unconditionally, so NCM ended up with two
        rows for one setting and every read returned only the first."""
        self.assertFalse(cp.post_appdata('poll_interval', '99'))
        self.assertEqual(len(self.model.entries), 2)
        self.assertEqual(cp.get_appdata('poll_interval'), '10')
        self.assertIn('duplicate', self.logs)

    def test_delete(self):
        self.assertTrue(cp.delete_appdata('poll_interval'))
        self.assertIsNone(cp.get_appdata('poll_interval'))

    def test_delete_is_case_insensitive(self):
        self.assertTrue(cp.delete_appdata('DEBUG'))
        self.assertIsNone(cp.get_appdata('debug'))

    def test_delete_absent_name_succeeds(self):
        self.assertTrue(cp.delete_appdata('never_existed'))

    def test_delete_removes_pre_existing_duplicates(self):
        self.model.entries.append({'_id_': 'id9', 'name': 'Poll_Interval',
                                   'value': '20'})
        self.assertTrue(cp.delete_appdata('poll_interval'))
        self.assertIsNone(cp.get_appdata('poll_interval'))

    def test_helpers_report_false_when_unreachable(self):
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')
        self.assertFalse(cp.put_appdata('a', '1'))
        self.assertFalse(cp.post_appdata('a', '1'))
        self.assertFalse(cp.delete_appdata('a'))
        self.assertIsNone(cp.get_appdata('a'))


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

class TestIdentity(CpTestCase):
    def _serve_paths(self, mapping):
        def handler(command):
            path = command.decode().split('\n')[1]
            return framed(mapping.get(path))

        return self.serve(handler)

    def test_product_and_serial(self):
        self._serve_paths({'status/product_info': {
            'product_name': 'R1900', 'mac0': '00:11:22:33:44:55',
            'manufacturing': {'serial_num': 'ABC123'}}})
        self.assertEqual(cp.get_product_name(), 'R1900')
        self.assertEqual(cp.get_router_model(), 'R1900')
        self.assertEqual(cp.get_mac(), '00:11:22:33:44:55')
        self.assertEqual(cp.get_serial_number(), 'ABC123')

    def test_identity_on_unexpected_payload(self):
        self._serve_paths({'status/product_info': 'not a dict'})
        self.assertIsNone(cp.get_product_name())
        self.assertIsNone(cp.get_mac())
        self.assertIsNone(cp.get_serial_number())

    def test_firmware_version(self):
        self._serve_paths({'status/fw_info': {
            'major_version': 7, 'minor_version': 25, 'patch_version': 20,
            'build_date': '2025-01-01', 'build_version': 'abc'}})
        self.assertEqual(cp.get_firmware_version(), '7.25.20')
        self.assertEqual(cp.get_firmware_version(True), '7.25.20 (2025-01-01 abc)')

    def test_firmware_version_returns_none_on_missing_fields(self):
        """regression: this interpolated the three fields unchecked and returned
        the string 'None.None.None', which looks like data downstream."""
        self._serve_paths({'status/fw_info': {'unexpected': 1}})
        self.assertIsNone(cp.get_firmware_version())
        self.assertIsNone(cp.get_firmware_version(True))

    def test_firmware_version_without_build_fields(self):
        self._serve_paths({'status/fw_info': {
            'major_version': 7, 'minor_version': 26, 'patch_version': 21}})
        self.assertEqual(cp.get_firmware_version(True), '7.26.21')

    def test_uptime(self):
        self._serve_paths({'status/system/uptime': 1234.5})
        self.assertEqual(cp.get_uptime(), 1234.5)

    def test_uptime_as_string(self):
        self._serve_paths({'status/system/uptime': '99'})
        self.assertEqual(cp.get_uptime(), 99.0)

    def test_uptime_garbage(self):
        self._serve_paths({'status/system/uptime': 'soon'})
        self.assertIsNone(cp.get_uptime())


class TestWanHelpers(CpTestCase):
    def _serve(self, payload):
        return self.serve(lambda command: framed(payload))

    def test_connected_wans(self):
        self._serve({
            'mdm-1': {'status': {'connection_state': 'connected'}},
            'ethernet-2': {'status': {'connection_state': 'disconnected'}},
            'mdm-3': {'status': {'connection_state': 'connected'}},
        })
        self.assertEqual(sorted(cp.get_connected_wans()), ['mdm-1', 'mdm-3'])

    def test_connected_wans_on_bad_payload(self):
        self._serve(None)
        self.assertEqual(cp.get_connected_wans(), [])

    def test_sims_excludes_nosim(self):
        self._serve({
            'mdm-a': {'status': {}},
            'mdm-b': {'status': {'error_text': 'NOSIM'}},
            'ethernet-1': {'status': {}},
        })
        self.assertEqual(cp.get_sims(), ['mdm-a'])

    def test_sims_tolerates_null_error_text(self):
        self._serve({'mdm-a': {'status': {'error_text': None}}})
        self.assertEqual(cp.get_sims(), ['mdm-a'])

    def test_wan_profiles_sorted_ascending(self):
        self._serve([{'_id_': 'a', 'priority': 2.1},
                     {'_id_': 'b', 'priority': 1.2},
                     {'_id_': 'c', 'priority': 3.0}])
        self.assertEqual([r['_id_'] for r in cp.get_wan_profiles()],
                         ['b', 'a', 'c'])

    def test_wan_profiles_tolerates_mixed_priority_types(self):
        """regression: a non-numeric priority made the whole sort raise
        TypeError, taking out a caller that only wanted to list profiles."""
        self._serve([{'_id_': 'a', 'priority': 'high'},
                     {'_id_': 'b', 'priority': 1.0},
                     {'_id_': 'c'}])
        order = [r['_id_'] for r in cp.get_wan_profiles()]
        self.assertEqual(order[0], 'b')
        self.assertEqual(len(order), 3)


class TestGpio(CpTestCase):
    def _serve(self, model, pins):
        def handler(command):
            path = command.decode().split('\n')[1]
            if path == 'status/product_info':
                return framed({'product_name': model})
            if path == 'status/gpio':
                return framed(pins)
            return framed(None)

        return self.serve(handler)

    def test_named_pin(self):
        self._serve('R1900', {'CONNECTOR_GPIO_2': 1})
        self.assertEqual(cp.get_gpio('power_input'), 1)

    def test_all_mapped_pins(self):
        self._serve('R920', {'CONNECTOR_GPIO_1': 0, 'CONNECTOR_GPIO_2': 1})
        self.assertEqual(cp.get_gpio(), {'power_input': 0, 'power_output': 1})

    def test_unknown_model(self):
        self._serve('XYZ999', {'SOMETHING': 1})
        self.assertIsNone(cp.get_gpio('power_input'))
        self.assertEqual(cp.get_gpio(), {})
        self.assertIn('no logical pin map', self.logs)

    def test_unmapped_name(self):
        self._serve('R920', {'CONNECTOR_GPIO_1': 0})
        self.assertIsNone(cp.get_gpio('sata_1'))
        self.assertIn('not mapped', self.logs)

    def test_explicit_model_avoids_a_lookup(self):
        self._serve('R920', {'CONNECTOR_GPIO_1': 7})
        self.assertEqual(cp.get_gpio('power_input', router_model='r920'), 7)


class TestGps(CpTestCase):
    def test_dec_positive(self):
        self.assertEqual(cp.dec(43, 40, 23.2175), 43.673116)

    def test_dec_negative_degree_carries_the_sign(self):
        self.assertEqual(cp.dec(-116, 30, 0), -116.5)

    def test_dec_negative_zero_float(self):
        self.assertEqual(cp.dec(-0.0, 30, 0), -0.5)

    def test_dec_integer_zero_cannot_carry_a_sign(self):
        """Documented limitation, not a bug in this function: the router reports
        degree as an integer, and integer zero has no sign, so a position just
        south of the equator is indistinguishable from its northern mirror."""
        self.assertEqual(cp.dec(0, 30, 0), 0.5)

    def test_dec_string_input(self):
        self.assertEqual(cp.dec('-43', '30', '0'), -43.5)

    def test_dec_garbage(self):
        self.assertIsNone(cp.dec('north'))
        self.assertIsNone(cp.dec(None))

    def test_lat_long(self):
        self.serve(lambda command: framed({
            'lock': True,
            'latitude': {'degree': 43, 'minute': 40, 'second': 23.2175},
            'longitude': {'degree': -116, 'minute': 12, 'second': 3.0},
        }))
        latitude, longitude = cp.get_lat_long()
        self.assertEqual(latitude, 43.673116)
        self.assertEqual(longitude, -116.200833)

    def test_lat_long_without_lock(self):
        self.serve(lambda command: framed({'lock': False, 'latitude': {},
                                           'longitude': {}}))
        self.assertEqual(cp.get_lat_long(), (None, None))

    def test_lat_long_with_unexpected_shape(self):
        self.serve(lambda command: framed({'lock': True, 'latitude': 43.6,
                                           'longitude': -116.2}))
        self.assertEqual(cp.get_lat_long(), (None, None))

    def test_lat_long_when_unreachable(self):
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')
        self.assertEqual(cp.get_lat_long(), (None, None))


class TestValidatePassword(CpTestCase):
    @staticmethod
    def ncos_hash(password, salt='abcdef0123456789', iterations=1000):
        """Build a real $3$ hash the way NCOS does: PBKDF2-HMAC-SHA256 with the
        salt used as raw ASCII bytes, not base64 decoded."""
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                      salt.encode(), iterations, dklen=32)
        key = base64.b64encode(derived).decode()
        return f'$3${iterations}${salt}${key}'

    def _serve_users(self, users):
        return self.serve(lambda command: framed(users))

    def test_correct_password(self):
        self._serve_users([{'username': 'admin',
                            'password': self.ncos_hash('s3cret')}])
        self.assertEqual(cp.validate_password('admin', 's3cret'), {'valid': True})

    def test_wrong_password(self):
        self._serve_users([{'username': 'admin',
                            'password': self.ncos_hash('s3cret')}])
        self.assertEqual(cp.validate_password('admin', 'nope'), {'valid': False})

    def test_masked_hash_is_reported_as_such(self):
        self._serve_users([{'username': 'admin', 'password': '$0$'}])
        result = cp.validate_password('admin', 'anything')
        self.assertFalse(result['valid'])
        self.assertIn('masked', result['error'])

    def test_unknown_user(self):
        self._serve_users([{'username': 'admin', 'password': '$3$1$a$b'}])
        result = cp.validate_password('nobody', 'x')
        self.assertIn('no such user', result['error'])

    def test_unsupported_scheme(self):
        self._serve_users([{'username': 'admin', 'password': '$9$1$a$b'}])
        self.assertIn('unsupported hash scheme',
                      cp.validate_password('admin', 'x')['error'])

    def test_unreadable_users(self):
        self.serve(lambda command: framed(None))
        self.assertIn('could not read',
                      cp.validate_password('admin', 'x')['error'])


class TestAlert(CpTestCase):
    def test_accepted(self):
        self.serve(lambda command: framed(None,
                                          body_override=b"Alert added('hi')"))
        self.assertTrue(cp.alert('hi'))

    def test_empty_value_is_refused_without_sending(self):
        store = self.serve(lambda command: framed('ok'))
        self.assertFalse(cp.alert(''))
        self.assertEqual(store.commands, [])
        self.assertIn('refusing to send an empty alert', self.logs)

    def test_newlines_are_collapsed_not_refused(self):
        """Unlike a path, alert text is prose: altering it is better than
        refusing to report the condition at all."""
        store = self.serve(lambda command: framed('ok'))
        cp.APP_NAME = 'app'
        cp.alert('line one\nline two\ttabbed')
        self.assertEqual(store.commands[0],
                         'alert\napp\nline one line two tabbed\n')

    def test_non_ascii_is_replaced(self):
        store = self.serve(lambda command: framed('ok'))
        cp.APP_NAME = 'app'
        cp.alert('caf\u00e9')
        self.assertEqual(store.commands[0], 'alert\napp\ncaf?\n')

    def test_long_text_is_truncated(self):
        store = self.serve(lambda command: framed('ok'))
        cp.alert('x' * 5000)
        sent = store.commands[0].split('\n')[2]
        self.assertEqual(len(sent), cp._ALERT_MAX_CHARS)
        self.assertTrue(sent.endswith('...'))

    def test_rejection_returns_false(self):
        self.serve(lambda command: framed('denied', status='error'))
        self.assertFalse(cp.alert('hi'))
        self.assertIn('did not accept', self.logs)

    def test_unreachable_returns_false(self):
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')
        self.assertFalse(cp.alert('hi'))


class TestUnimplementedStubs(CpTestCase):
    def test_register_logs_and_returns_none(self):
        self.assertIsNone(cp.register('put', 'config/system', lambda: None))
        self.assertIn('register()', self.logs)

    def test_stub_message_does_not_state_the_reason_as_fact(self):
        """The event-socket explanation has never been tested from a container,
        and this repo has already been wrong once about exactly this kind of
        claim. A stub that states a confident reason is read as evidence."""
        cp.register('put', 'config/system', lambda: None)
        self.assertIn('UNVERIFIED', self.logs)

    def test_on_is_an_alias(self):
        self.assertIsNone(cp.on('put', 'config/system', lambda: None))

    def test_unregister(self):
        self.assertIsNone(cp.unregister())
        self.assertIn('unregister()', self.logs)


# ---------------------------------------------------------------------------
# Readiness helpers
# ---------------------------------------------------------------------------

class TestReadiness(CpTestCase):
    def test_uptime_ready_immediately(self):
        self.serve(lambda command: framed(500))
        self.assertTrue(cp.wait_for_uptime(60, timeout=1))

    def test_uptime_times_out_without_overshooting(self):
        """regression: the internal sleep was not clamped to the deadline, so
        timeout=3 could take 10 seconds and then log 'timed out after 3.0s'."""
        self.serve(lambda command: framed(1))
        start = time.monotonic()
        self.assertFalse(cp.wait_for_uptime(60, timeout=1.0))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, f'overshot the timeout ({elapsed:.1f}s)')

    def test_stop_event_returns_immediately(self):
        self.serve(lambda command: framed(1))
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        start = time.monotonic()
        self.assertFalse(cp.wait_for_uptime(60, timeout=30, stop=stop))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, f'ignored the stop event ({elapsed:.1f}s)')
        self.assertIn('shutdown requested', self.logs)

    def test_stop_already_set_does_not_poll(self):
        store = self.serve(lambda command: framed(1))
        stop = threading.Event()
        stop.set()
        self.assertFalse(cp.wait_for_uptime(60, timeout=30, stop=stop))
        self.assertEqual(store.commands, [])

    def test_ntp_ready(self):
        self.serve(lambda command: framed(12.0))
        self.assertTrue(cp.wait_for_ntp(timeout=1))

    def test_ntp_times_out(self):
        self.serve(lambda command: framed(None))
        start = time.monotonic()
        self.assertFalse(cp.wait_for_ntp(timeout=0.5, check_interval=0.1))
        self.assertLess(time.monotonic() - start, 2.0)

    def test_wan_ready(self):
        self.serve(lambda command: framed('connected'))
        self.assertTrue(cp.wait_for_wan_connection(timeout=1))

    def test_wan_times_out(self):
        self.serve(lambda command: framed('disconnected'))
        self.assertFalse(cp.wait_for_wan_connection(timeout=0.5,
                                                    check_interval=0.1))

    def test_wan_stop_event(self):
        self.serve(lambda command: framed('disconnected'))
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        start = time.monotonic()
        self.assertFalse(cp.wait_for_wan_connection(timeout=30, stop=stop))
        self.assertLess(time.monotonic() - start, 3.0)


# ---------------------------------------------------------------------------
# REST transport
# ---------------------------------------------------------------------------

class RestTestCase(CpTestCase):
    def setUp(self):
        super().setUp()
        # use_rest() refuses when the Config Store socket exists, so point
        # SOCKET_PATH somewhere guaranteed absent rather than depending on
        # whether this host happens to have /var/tmp/cs.sock.
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')

        # Credentials come from .env only. Point DOTENV_PATH at this temporary
        # directory so the suite never reads the developer's real .env -- without
        # this, these tests pass or fail depending on whose machine runs them.
        self._dotenv = os.path.join(directory, '.env')
        cp.DOTENV_PATH = self._dotenv

        # Router credentials are no longer environment variables. Clear the names
        # anyway, so a stale export in the shell running the suite cannot make a
        # test that asserts they are ignored pass for the wrong reason.
        self._saved_env = {}
        for names in cp._REST_ENV_NAMES.values():
            for name in names:
                self._saved_env[name] = os.environ.pop(name, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_dotenv(self, **values):
        """Write a .env for use_rest() to read. Keys are given without prefix."""
        with open(self._dotenv, 'w', encoding='utf-8') as handle:
            handle.write('# test fixture\n')
            for key, value in values.items():
                handle.write(f'NCOS_DEV_{key.upper()}={value}\n')

    def make_router(self, responder):
        router = MockRouter(responder)
        self.addCleanup(router.close)
        return router

    def connect(self, router, **kwargs):
        options = dict(host=router.host, username='admin', password='secret',
                       scheme='http', timeout=5.0)
        options.update(kwargs)
        return cp.use_rest(**options)


class TestRestConfiguration(RestTestCase):
    def test_unconfigured_raises_and_names_the_variables(self):
        """An absent configuration must never masquerade as an unreachable
        router, and must never default to an address."""
        with self.assertRaises(ValueError) as caught:
            cp.use_rest()
        message = str(caught.exception)
        self.assertIn('NCOS_DEV_HOST', message)
        self.assertIn('NCOS_DEV_PASSWORD', message)
        self.assertEqual(cp.transport(), 'socket', 'must not switch on failure')

    def test_missing_password_alone_raises(self):
        with self.assertRaises(ValueError):
            cp.use_rest(host='192.168.0.1')

    def test_reads_dotenv(self):
        self.write_dotenv(host='10.0.0.1', password='pw')
        target = cp.use_rest()
        self.assertEqual(target.host, '10.0.0.1')
        self.assertEqual(target.username, 'admin')
        self.assertEqual(target.sources['host'], '.env:NCOS_DEV_HOST')

    def test_dotenv_supplies_every_field(self):
        self.write_dotenv(host='10.0.0.2', password='pw', username='operator')
        target = cp.use_rest()
        self.assertEqual(target.host, '10.0.0.2')
        self.assertEqual(target.username, 'operator')

    def test_environment_variables_are_ignored_for_credentials(self):
        """`.env` is the only source. A stale export must not aim this
        anywhere, because a second source is exactly how a corrected address
        became invisible."""
        self.write_dotenv(host='10.0.0.5', password='pw')
        os.environ['NCOS_DEV_HOST'] = '10.9.9.9'
        os.environ['CP_ROUTER_HOST'] = '10.9.9.9'
        self.assertEqual(cp.use_rest().host, '10.0.0.5')

    def test_environment_alone_does_not_configure_rest(self):
        os.environ['NCOS_DEV_HOST'] = '10.9.9.9'
        os.environ['NCOS_DEV_PASSWORD'] = 'pw'
        with self.assertRaises(ValueError):
            cp.use_rest()

    def test_explicit_arguments_win_over_dotenv(self):
        self.write_dotenv(host='10.0.0.6', password='pw')
        self.assertEqual(cp.use_rest(host='10.0.0.7', password='pw').host,
                         '10.0.0.7')

    def test_absent_dotenv_raises_rather_than_defaulting(self):
        self.assertFalse(os.path.exists(self._dotenv))
        with self.assertRaises(ValueError):
            cp.use_rest()

    def test_dotenv_comments_and_quotes(self):
        with open(self._dotenv, 'w', encoding='utf-8') as handle:
            handle.write('# a comment\n\n')
            handle.write('NCOS_DEV_HOST="10.0.0.8"\n')
            handle.write("NCOS_DEV_PASSWORD=pa#ss\n")
        target = cp.use_rest()
        self.assertEqual(target.host, '10.0.0.8')
        self.assertEqual(target.password, 'pa#ss',
                         "'#' is an ordinary password character mid-line")

    def test_scheme_pasted_into_host_is_tolerated(self):
        target = cp.use_rest(host='https://10.0.0.3/', password='pw')
        self.assertEqual(target.host, '10.0.0.3')
        self.assertEqual(target.scheme, 'https')

    def test_invalid_scheme_raises(self):
        with self.assertRaises(ValueError):
            cp.use_rest(host='h', password='p', scheme='ftp')

    def test_invalid_timeout_raises(self):
        with self.assertRaises(ValueError):
            cp.use_rest(host='h', password='p', timeout=0)

    def test_tls_off_by_default_and_warned_once(self):
        cp.use_rest(host='h', password='p')
        self.assertIn('verification is OFF', self.logs)

    def test_transport_reports_rest_then_socket(self):
        cp.use_rest(host='h', password='p')
        self.assertEqual(cp.transport(), 'rest')
        cp.use_socket()
        self.assertEqual(cp.transport(), 'socket')


class TestRestCredentialSafety(RestTestCase):
    def test_repr_is_redacted(self):
        target = cp.use_rest(host='h', password='hunter2')
        self.assertNotIn('hunter2', repr(target))
        self.assertIn('redacted', repr(target))

    def test_describe_reports_only_whether_set(self):
        target = cp.use_rest(host='h', password='hunter2')
        self.assertEqual(target.describe()['password'], 'set')
        self.assertNotIn('hunter2', json.dumps(target.describe()))

    def test_status_never_carries_the_password(self):
        router = self.make_router(lambda request: (200, {'success': True, 'data': {}}))
        self.connect(router, password='hunter2')
        self.assertNotIn('hunter2', json.dumps(cp.config_store_status()))

    def test_password_never_reaches_the_log(self):
        router = self.make_router(lambda request: (401, {'error': 'no'}))
        self.connect(router, password='hunter2')
        cp.get('status/product_info')
        self.assertNotIn('hunter2', self.logs)


class TestRestRequests(RestTestCase):
    def setUp(self):
        super().setUp()
        self.router = self.make_router(self._respond)
        self.connect(self.router)

    @staticmethod
    def _respond(request):
        if request['method'] == 'GET' and 'product_info' in request['path']:
            return 200, {'success': True, 'data': {'product_name': 'R980'}}
        return 200, {'success': True, 'data': 'written'}

    def test_get_unwraps_the_success_envelope(self):
        """REST wraps replies as {'success': ..., 'data': ...}; the socket does
        not. Unwrapping here is what lets one accessor serve both."""
        self.assertEqual(cp.get('status/product_info'),
                         {'product_name': 'R980'})

    def test_url_is_prefixed_with_api(self):
        cp.get('status/product_info')
        self.assertEqual(self.router.requests[0]['path'], '/api/status/product_info')

    def test_leading_and_trailing_slashes_are_normalised(self):
        cp.get('/status/product_info/')
        self.assertEqual(self.router.requests[0]['path'], '/api/status/product_info')

    def test_basic_auth_header_is_sent_upfront(self):
        cp.get('status/product_info')
        expected = 'Basic ' + base64.b64encode(b'admin:secret').decode()
        self.assertEqual(self.router.requests[0]['authorization'], expected)

    def test_put_uses_form_encoded_data_field(self):
        cp.put('config/system/gps/enabled', True)
        request = self.router.requests[0]
        self.assertEqual(request['method'], 'PUT')
        self.assertEqual(request['content_type'],
                         'application/x-www-form-urlencoded')
        self.assertEqual(request['body'], 'data=true')

    def test_post_sends_json_in_the_data_field(self):
        cp.post('config/system/sdk/appdata', {'name': 'a', 'value': '1'})
        self.assertIn('data=', self.router.requests[0]['body'])
        self.assertEqual(self.router.requests[0]['method'], 'POST')

    def test_delete_maps_to_the_delete_method(self):
        cp.delete('config/wan/rules2/abc')
        self.assertEqual(self.router.requests[0]['method'], 'DELETE')

    def test_identity_accessors_work_unchanged(self):
        self.assertEqual(cp.get_product_name(), 'R980')

    def test_available_and_status_report_the_transport(self):
        self.assertTrue(cp.config_store_available())
        status = cp.config_store_status()
        self.assertEqual(status['transport'], 'rest')
        self.assertEqual(status['target']['host'], self.router.host)


class TestRestFailures(RestTestCase):
    def test_success_false_is_surfaced_not_silently_none(self):
        router = self.make_router(lambda request: (
            200, {'success': False, 'data': None, 'reason': 'no such path'}))
        self.connect(router)
        self.assertIsNone(cp.get('config/nonsense'))
        self.assertIn('router rejected', self.logs)

    def test_rejection_does_not_mark_the_router_unreachable(self):
        """The router answered, so the transport is healthy. Only the request
        failed."""
        router = self.make_router(lambda request: (200, {'success': False}))
        self.connect(router)
        cp.get('config/nonsense')
        self.assertTrue(cp.config_store_status()['available'])

    def test_401_is_reported_clearly(self):
        router = self.make_router(lambda request: (401, {'error': 'unauthorized'}))
        self.connect(router)
        self.assertIsNone(cp.get('status/product_info'))
        self.assertFalse(cp.config_store_status()['available'])
        self.assertIn('401 unauthorized', self.logs)
        self.assertIn('NCOS_DEV_PASSWORD', self.logs)

    def test_http_error_is_a_transport_failure(self):
        router = self.make_router(lambda request: (500, {'error': 'boom'}))
        self.connect(router)
        self.assertIsNone(cp.get('status/x'))
        self.assertFalse(cp.config_store_status()['available'])

    def test_html_login_page_is_reported_as_non_json(self):
        router = self.make_router(lambda request: (200, b'<html>login</html>'))
        self.connect(router)
        self.assertIsNone(cp.get('status/x'))
        self.assertIn('non-JSON', self.logs)

    def test_unreachable_host(self):
        cp.use_rest(host='127.0.0.1:1', password='pw', scheme='http',
                    timeout=1.0)
        self.assertIsNone(cp.get('status/x'))
        self.assertFalse(cp.config_store_available())

    def test_failures_are_throttled_like_the_socket_transport(self):
        cp.use_rest(host='127.0.0.1:1', password='pw', scheme='http',
                    timeout=0.5)
        for _ in range(5):
            cp.get('status/x')
        unreachable = [line for line in self._logged
                       if 'unreachable' in str(line)]
        self.assertEqual(len(unreachable), 1)


class TestRestUnsupportedVerbs(RestTestCase):
    def setUp(self):
        super().setUp()
        router = self.make_router(lambda request: (200, {'success': True, 'data': 1}))
        self.connect(router)
        self.router_mock = router

    def test_alert_is_refused_with_an_explanation(self):
        self.assertFalse(cp.alert('hi'))
        self.assertEqual(self.router_mock.requests, [])
        self.assertIn('not available over the REST transport', self.logs)

    def test_decrypt_is_refused(self):
        self.assertIsNone(cp.decrypt('config/certmgmt/certs/0/key'))
        self.assertEqual(self.router_mock.requests, [])
        self.assertIn('not available over the REST transport', self.logs)

    def test_query_and_tree_are_reported_as_ignored(self):
        cp.get('status/system', query='cpu', tree=1)
        self.assertIn('ignores query', self.logs)
        self.assertIn('ignores tree', self.logs)

    def test_ignored_argument_warnings_are_not_repeated(self):
        for _ in range(4):
            cp.get('status/system', tree=1)
        warnings = [line for line in self._logged if 'ignores tree' in str(line)]
        self.assertEqual(len(warnings), 1)


class TestRestRefusedOnTheRouter(RestTestCase):
    """On the router the socket is available, so REST is refused outright.

    This is what makes "on the router, always the socket" an enforced invariant
    rather than a convention documented in a README. Together with
    TestNoAutomaticFallback it covers both directions: the socket transport never
    becomes REST by itself, and REST cannot be selected where the socket works.
    """

    def _serve_socket(self):
        store = MockConfigStore(lambda command: framed({'product_name': 'R980'}))
        self.addCleanup(store.close)
        cp.SOCKET_PATH = store.path
        return store

    def test_use_rest_raises_when_the_socket_exists(self):
        self._serve_socket()
        with self.assertRaises(RuntimeError) as caught:
            cp.use_rest(host='10.0.0.9', password='pw')
        self.assertIn(cp.SOCKET_PATH, str(caught.exception))
        self.assertIn('force=True', str(caught.exception))

    def test_refusal_leaves_the_transport_on_the_socket(self):
        store = self._serve_socket()
        with self.assertRaises(RuntimeError):
            cp.use_rest(host='10.0.0.9', password='pw')
        self.assertEqual(cp.transport(), 'socket')
        self.assertEqual(cp.get('status/product_info'), {'product_name': 'R980'})
        self.assertTrue(store.commands)

    def test_refusal_happens_before_any_credential_is_resolved(self):
        """A refused call must not read, hold or report a password. The password
        goes in .env, the real source, or this asserts nothing."""
        self._serve_socket()
        self.write_dotenv(host='10.0.0.9', password='hunter2')
        with self.assertRaises(RuntimeError) as caught:
            cp.use_rest(host='10.0.0.9')
        self.assertNotIn('hunter2', str(caught.exception))
        self.assertNotIn('hunter2', json.dumps(cp.config_store_status()))
        self.assertNotIn('hunter2', self.logs)

    def test_refusal_does_not_depend_on_being_configured(self):
        """The guard fires even when the target is fully configured. Configure it
        through .env, so the refusal is what raises rather than a missing host."""
        self._serve_socket()
        self.write_dotenv(host='10.0.0.9', password='pw')
        with self.assertRaises(RuntimeError):
            cp.use_rest()
        self.assertEqual(cp.transport(), 'socket')

    def test_force_overrides_for_a_deliberate_cross_router_call(self):
        self._serve_socket()
        target = cp.use_rest(host='10.0.0.9', password='pw', force=True)
        self.assertEqual(cp.transport(), 'rest')
        self.assertEqual(target.host, '10.0.0.9')

    def test_no_socket_means_no_refusal(self):
        """A development host has no cs.sock, so the normal path is unaffected."""
        target = cp.use_rest(host='10.0.0.9', password='pw')
        self.assertEqual(cp.transport(), 'rest')
        self.assertEqual(target.host, '10.0.0.9')

    def test_cli_rest_flag_is_refused_cleanly(self):
        """`python3 cp.py --rest` on the router exits 2 with the explanation,
        rather than raising through to a traceback."""
        self._serve_socket()
        self.write_dotenv(host='10.0.0.9', password='pw')
        self.assertEqual(cp._main(['--rest']), 2)
        self.assertIn('refusing to enable the REST transport', self.logs)
        self.assertEqual(cp.transport(), 'socket')


class TestCli(CpTestCase):
    def test_reports_a_path_and_exits_zero(self):
        self.serve(lambda command: framed({'product_name': 'R980'}))
        self.assertEqual(cp._main(['status/product_info']), 0)
        self.assertIn('R980', self.logs)

    def test_defaults_to_product_info(self):
        store = self.serve(lambda command: framed({'product_name': 'R980'}))
        self.assertEqual(cp._main([]), 0)
        self.assertIn('status/product_info', store.commands[0])

    def test_exits_one_without_a_config_store(self):
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')
        self.assertEqual(cp._main([]), 1)
        self.assertIn('$CONFIG_STORE', self.logs)

    def test_exits_two_when_rest_is_unconfigured(self):
        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')
        # Point .env at an absent path. Without this the test reads the real
        # repo-root .env and passes or fails per developer machine.
        cp.DOTENV_PATH = os.path.join(directory, 'absent.env')
        self.assertEqual(cp._main(['--rest']), 2)
        self.assertIn('not configured', self.logs)


class TestNoAutomaticFallback(RestTestCase):
    def test_a_missing_socket_never_silently_uses_rest(self):
        """The safety property that makes this transport acceptable to ship in
        container code: a container whose $CONFIG_STORE volume is missing must
        fail visibly, not start reconfiguring whatever router a leftover .env
        points at. Fully configured on purpose — an unconfigured target would
        pass this test without exercising the property."""
        router = self.make_router(lambda request: (200, {'success': True, 'data': 'X'}))
        self.write_dotenv(host=router.host, password='secret', scheme='http')

        directory = tempfile.mkdtemp(prefix='cp-test-')
        self.addCleanup(shutil.rmtree, directory, True)
        cp.SOCKET_PATH = os.path.join(directory, 'absent.sock')

        self.assertIsNone(cp.get('status/product_info'))
        self.assertEqual(cp.transport(), 'socket')
        self.assertEqual(router.requests, [],
                         'the remote router must not have been contacted')

    def test_use_socket_discards_rest_credentials(self):
        cp.use_rest(host='h', password='pw')
        cp.use_socket()
        with cp._lock:
            self.assertIsNone(cp._transport['rest'])
        self.assertEqual(cp.config_store_status()['target'],
                         f'unix:{cp.SOCKET_PATH}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
