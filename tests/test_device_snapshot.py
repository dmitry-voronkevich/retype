"""Protocol tests use generated CML lines and injected transports only.

The checked-in cho fixture records the physical Two S3 identity and profile-A
keymap but not individual CML C1 replies, so the C1 examples below encode the
published/report format rather than claiming to be a hardware capture.
"""
import logging
import sys

import pytest

import retype.services.device_snapshot as device_snapshot
from retype.services.device_snapshot import (
    CmlSnapshotFailed, DeviceCancelled, DeviceCloseError, DeviceIndexMismatch, DeviceReadError,
    DeviceRejected,
    DeviceSnapshotReader, MalformedDeviceReply, MissingResponseFraming,
    TransportTimeout,
    UnsupportedDevice,
    decode_chord_hex, decode_phrase_hex, is_chara_chorder_port,
    parse_cml_count, parse_cml_entry, parse_keymap_entry, snapshot_to_chords)


class Port:
    device = '/dev/fake-charachorder'
    manufacturer = 'CharaChorder'


def _c1(index, input_codes, output_codes):
    number = 0
    for code in reversed(input_codes):
        number = (number << 10) | code
    input_hex = format(number, '032x')
    output_hex = ''.join(format(code, '02x') for code in output_codes) or '0'
    return 'CML C1 {} {} {}\r\n'.format(index, input_hex, output_hex).encode()


class FakeTransport:
    """A serialized fake: each exchange returns only its command's reply."""
    def __init__(self, replies=None, on_exchange=None, open_error=None,
                 exchange_error=None, delayed_output=None, close_error=None):
        self.replies = replies or {}
        self.on_exchange = on_exchange
        self.open_error = open_error
        self.exchange_error = exchange_error
        self.delayed_output = list(delayed_output or [])
        self.close_error = close_error
        self.commands = []
        self.drain_calls = []
        self.cancel_read_count = 0
        self.opened = False
        self.close_count = 0

    def open(self):
        self.opened = True
        if self.open_error:
            raise self.open_error

    def exchange(self, request, _timeout):
        command = request.decode().strip()
        self.commands.append(command)
        if self.on_exchange:
            self.on_exchange()
        if self.exchange_error:
            raise self.exchange_error
        reply = self.replies.get(command, b'')
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else b''
        return reply() if callable(reply) else reply

    def drain_until_quiet(self, timeout):
        self.drain_calls.append(timeout)
        return self.delayed_output.pop(0) if self.delayed_output else b''

    def cancel_pending_read(self):
        self.cancel_read_count += 1

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error


def _reader(transport, *, total_timeout=10):
    return DeviceSnapshotReader(
        list_ports=lambda: [Port()], transport_factory=lambda _path: transport,
        request_timeout=0.01, total_timeout=total_timeout)


def _complete_replies(chords=None):
    keymap = [0] * 90
    keymap[:6] = [606, ord('t'), 608, ord('h'), 607, ord('e')]
    replies = {
        'ID': b'ID CHARACHORDER TWO S3\r\n',
        'VERSION': b'VERSION 3.0.0\r\n',
        'CML C0': 'CML C0 {}\r\n'.format(len(chords or [])).encode(),
    }
    for index, code in enumerate(keymap):
        replies['VAR B3 A1 {}'.format(index)] = (
            'VAR B3 A1 {} {} 0\r\n'.format(index, code).encode())
    for index, chord in enumerate(chords or []):
        replies['CML C1 {}'.format(index)] = _c1(index, *chord)
    return replies


def test_discovery_accepts_pyserial_integer_espressif_vid():
    assert is_chara_chorder_port(type('Port', (), {'vid': 0x303A, 'manufacturer': ''})())


def test_cml_parsing_strictly_decodes_ccos_three_replies():
    assert parse_cml_count('CML C0 2') == 2
    input_codes, output_codes = parse_cml_entry(
        _c1(0, [116, 104, 101], [116, 104, 101]).decode().strip(), 0)
    assert input_codes[:3] == (116, 104, 101)
    assert output_codes == (116, 104, 101)
    assert decode_chord_hex('0' * 32) == (0,) * 12
    assert decode_phrase_hex('0258') == (600,)


@pytest.mark.parametrize('line', [
    'CML C0 nope', 'CML C0 10001', 'CML C0 1 1',
])
def test_malformed_count_replies_fail_closed(line):
    with pytest.raises(DeviceReadError):
        parse_cml_count(line)


def test_cml_count_status_distinguishes_rejection_from_malformed_status():
    with pytest.raises(MalformedDeviceReply):
        parse_cml_count('CML C0 1 abc')
    with pytest.raises(MalformedDeviceReply):
        parse_cml_count('CML C0 1 01')
    with pytest.raises(MalformedDeviceReply):
        parse_cml_count('CML C0 nope 1')
    with pytest.raises(DeviceRejected):
        parse_cml_count('CML C0 1 1')


@pytest.mark.parametrize('line', [
    'CML C1 0 bad 61',
    'CML C1 2 00000000000000000000000000000000 61',
    'CML C1 0 00000000000000000000000000000000 0 1',
    'CML C1 0 00000000000000000000000000000000 01',
])
def test_malformed_cml_entries_fail_closed(line):
    with pytest.raises(DeviceReadError):
        parse_cml_entry(line, 0)


def test_var_keymap_rejection_and_malformed_fields_have_distinct_failures():
    with pytest.raises(MalformedDeviceReply):
        parse_keymap_entry('VAR B3 A1 0 nope 0', 0)
    with pytest.raises(MalformedDeviceReply):
        parse_keymap_entry('VAR B3 A1 0 606 nope', 0)
    with pytest.raises(MalformedDeviceReply):
        parse_keymap_entry('VAR B3 A1 0 606 01', 0)
    with pytest.raises(DeviceRejected):
        parse_keymap_entry('VAR B3 A1 0 606 1', 0)
    with pytest.raises(DeviceIndexMismatch):
        parse_keymap_entry('VAR B3 A1 1 606 0', 0)


def test_cml_rejection_and_wrong_index_have_distinct_failures():
    valid_input = '00000000000000000000000000000000'
    with pytest.raises(MalformedDeviceReply):
        parse_cml_entry('CML C1 0 bad 61', 0)
    with pytest.raises(MalformedDeviceReply):
        parse_cml_entry('CML C1 0 {} 61 abc'.format(valid_input), 0)
    with pytest.raises(MalformedDeviceReply):
        parse_cml_entry('CML C1 0 bad 61 1', 0)
    with pytest.raises(MalformedDeviceReply):
        parse_cml_entry('CML C1 0 {} bad 1'.format(valid_input), 0)
    with pytest.raises(MalformedDeviceReply):
        parse_cml_entry('CML C1 0 {} 61 01'.format(valid_input), 0)
    with pytest.raises(DeviceRejected):
        parse_cml_entry('CML C1 0 {} 61 1'.format(valid_input), 0)
    with pytest.raises(DeviceIndexMismatch):
        parse_cml_entry('CML C1 1 {} 61'.format(valid_input), 0)


def test_identity_retries_after_usb_serial_endpoint_settles():
    replies = _complete_replies()
    attempts = iter([b'', b'ID CHARACHORDER TWO S3\r\n'])
    replies['ID'] = lambda: next(attempts)
    transport = FakeTransport(replies)
    snapshot = _reader(transport).read()
    assert snapshot.identity == 'CHARACHORDER TWO S3'
    assert transport.commands[:2] == ['ID', 'ID']
    assert transport.close_count == 1


def test_complete_snapshot_is_immutable_and_adapts_positional_layout():
    transport = FakeTransport(_complete_replies([
        ([ord('e'), ord('h'), ord('t')], [ord('t'), ord('h'), ord('e')]),
    ]))
    snapshot = _reader(transport).read()
    assert snapshot.identity == 'CHARACHORDER TWO S3'
    assert snapshot.profile == 'A'
    assert isinstance(snapshot.keymap, tuple)
    assert isinstance(snapshot.chords, tuple)
    chords = snapshot_to_chords(snapshot)
    assert chords['the'] == 't+h+e'
    assert chords['the'].device_order == 't+h+e'
    assert transport.close_count == 1
    assert transport.commands.index('CML C0') > transport.commands.index('VAR B3 A1 89')


def test_complete_unterminated_responses_load_layout_and_cml_snapshot():
    replies = {command: reply.rstrip(b'\r\n')
               for command, reply in _complete_replies([
                   ([116, 104, 101], [116, 104, 101]),
               ]).items()}
    transport = FakeTransport(replies)

    snapshot = _reader(transport).read()

    assert snapshot.keymap[:6] == (606, 116, 608, 104, 607, 101)
    assert snapshot.chords == (((116, 104, 101) + (0,) * 9,
                                (116, 104, 101)),)
    assert transport.close_count == 1


def test_pyserial_transport_waits_between_writes_without_flushing(monkeypatch):
    class FakeSerialPort:
        in_waiting = 0

        def __init__(self):
            self.timeout = None
            self.writes = []
            self.flushes = 0

        def write(self, request):
            self.writes.append(request)

        def read_until(self, _terminator):
            return b'ID CHARACHORDER TWO S3\r\n'

        def flush(self):
            self.flushes += 1

    delays = []
    transport = device_snapshot.PySerialTransport('/dev/fake')
    port = FakeSerialPort()
    transport._port = port
    monkeypatch.setattr(device_snapshot.time, 'sleep', delays.append)

    transport.exchange(b'ID\r\n', 1)
    transport.exchange(b'VERSION\r\n', 1)

    assert delays == [device_snapshot.INTER_REQUEST_DELAY_SECONDS]
    assert port.flushes == 0


def test_pyserial_transport_surfaces_immediately_buffered_second_reply():
    class FakeSerialPort:
        def __init__(self):
            self.in_waiting = len(b'VERSION 3.0.0\r\n')
            self.read_sizes = []

        def write(self, _request):
            pass

        def read_until(self, _terminator):
            return b'ID CHARACHORDER TWO S3\r\n'

        def read(self, size):
            self.read_sizes.append(size)
            self.in_waiting = 0
            return b'VERSION 3.0.0\r\n'

    transport = device_snapshot.PySerialTransport('/dev/fake')
    port = FakeSerialPort()
    transport._port = port

    raw = transport.exchange(b'ID\r\n', 1)

    assert raw == b'ID CHARACHORDER TWO S3\r\nVERSION 3.0.0\r\n'
    assert port.read_sizes == [len(b'VERSION 3.0.0\r\n')]


def test_pyserial_transport_drains_delayed_bytes_until_quiet(monkeypatch):
    class FakeSerialPort:
        in_waiting = 1

        def __init__(self):
            self.timeout = None
            self.chunks = [b' delayed-tail\r\n', b'']

        def read(self, _size):
            chunk = self.chunks.pop(0)
            clock[0] += 0.01 if chunk else 0.20
            return chunk

    clock = [0.0]
    transport = device_snapshot.PySerialTransport('/dev/fake')
    port = FakeSerialPort()
    transport._port = port
    monkeypatch.setattr(device_snapshot.time, 'monotonic', lambda: clock[0])

    assert transport.drain_until_quiet(0.15) == b' delayed-tail\r\n'
    assert port.timeout == pytest.approx(0.15)


def test_pyserial_transport_cancels_and_verifies_a_single_close():
    class FakeSerialPort:
        def __init__(self):
            self.is_open = True
            self.cancel_reads = 0
            self.closes = 0

        def cancel_read(self):
            self.cancel_reads += 1

        def close(self):
            self.closes += 1
            self.is_open = False

    transport = device_snapshot.PySerialTransport('/dev/fake')
    port = FakeSerialPort()
    transport._port = port

    transport.close()
    transport.close()

    assert port.cancel_reads == 1
    assert port.closes == 1
    assert transport._port is None


def test_pyserial_transport_rejects_an_unverified_close():
    class StuckSerialPort:
        is_open = True

        def cancel_read(self):
            pass

        def close(self):
            pass

    transport = device_snapshot.PySerialTransport('/dev/fake')
    transport._port = StuckSerialPort()

    with pytest.raises(DeviceCloseError, match='remained open'):
        transport.close()


def test_new_pyserial_session_discards_only_preexisting_input(monkeypatch):
    class FakeSerialPort:
        is_open = True

        def __init__(self):
            self.reset_calls = 0

        def reset_input_buffer(self):
            self.reset_calls += 1

        def cancel_read(self):
            pass

        def close(self):
            self.is_open = False

    port = FakeSerialPort()
    fake_serial = type('FakeSerialModule', (), {
        'Serial': staticmethod(lambda *_args, **_kwargs: port),
    })
    monkeypatch.setitem(sys.modules, 'serial', fake_serial)
    transport = device_snapshot.PySerialTransport('/dev/fake')

    transport.open()
    transport.close()

    assert port.reset_calls == 1


def test_cml_cell_retries_once_then_returns_a_complete_snapshot():
    replies = _complete_replies([([116, 104], [116, 104])])
    replies['CML C1 0'] = [b'CML C1 0 malformed\r\n',
                            _c1(0, [116, 104], [116, 104])]
    transport = FakeTransport(replies)

    snapshot = _reader(transport).read()

    assert snapshot.chords == (((116, 104) + (0,) * 10, (116, 104)),)
    assert transport.commands.count('CML C1 0') == 2
    assert transport.close_count == 1


def test_two_cml_failures_continue_later_cells_log_raw_and_fail_atomically(caplog):
    replies = _complete_replies([
        ([116, 104], [116, 104]),
        ([101], [101]),
    ])
    replies['CML C1 0'] = [b'CML C1 0 malformed\r\n',
                            b'CML C1 0 malformed-again\r\n']
    transport = FakeTransport(replies)
    caplog.set_level(logging.WARNING, logger='retype.services.device_snapshot')

    with pytest.raises(DeviceReadError, match='indexes 0'):
        _reader(transport).read()

    assert transport.commands.count('CML C1 0') == 2
    assert transport.commands.count('CML C1 1') == 1
    assert transport.close_count == 1
    assert 'index=0 attempt=1 failed: malformed CML C1 reply' in caplog.text
    assert "raw=b'CML C1 0 malformed\\r\\n'" in caplog.text
    assert 'index=0 attempt=2 failed: malformed CML C1 reply' in caplog.text


@pytest.mark.parametrize('index, raw', [
    (56, b'C1 56 00000000000000000000000000000000 61\r\n'),
    (182, b'C1 182 00000000000000000000000000000000 61\r\n'),
    (400, b'CML 400 00000000000000000000000000000000 61\r\n'),
    (401, b'CML C1 00000000000000000000000000000000 61\r\n'),
    (417, b'CML C1 00000000000000000000000000000000 61\r\n'),
    (446, b'CML C1'),
])
def test_observed_cml_fragments_retry_once_and_are_never_accepted(index, raw):
    command = 'CML C1 {}'.format(index)
    transport = FakeTransport({command: [raw, raw]})
    reader = _reader(transport)
    reader._transport = transport

    assert reader._read_cml_entry(index, device_snapshot.time.monotonic() + 1) is None
    assert transport.commands == [command, command]
    assert len(transport.drain_calls) == 2


def test_delayed_cml_tail_is_drained_before_the_retry(caplog):
    replies = _complete_replies([([116], [116])])
    replies['CML C1 0'] = [b'CML C1', _c1(0, [116], [116])]
    transport = FakeTransport(replies, delayed_output=[b' 0 stale-tail\r\n'])
    caplog.set_level(logging.WARNING, logger='retype.services.device_snapshot')

    snapshot = _reader(transport).read()

    assert snapshot.chords[0][0][0] == 116
    assert transport.commands.count('CML C1 0') == 2
    assert "discarded delayed stale output; raw=b' 0 stale-tail\\r\\n'" in caplog.text


def test_cascading_timeout_after_a_bare_cml_prefix_keeps_later_diagnostics():
    chords = [([116], [116])] * 448
    replies = _complete_replies(chords)
    replies['CML C1 446'] = [b'CML C1', b'']
    replies['CML C1 447'] = [b'', b'']
    transport = FakeTransport(replies)

    with pytest.raises(CmlSnapshotFailed, match='446, 447'):
        _reader(transport).read()

    assert transport.commands.count('CML C1 446') == 2
    assert transport.commands.count('CML C1 447') == 2
    assert transport.close_count == 1


def test_partial_cml_then_no_responses_cancels_and_closes_once():
    chords = [([116], [116])] * 250
    replies = _complete_replies(chords)
    replies['CML C1 248'] = [b'CML C1 248 ', b'']
    transport = FakeTransport(replies)
    reader = _reader(transport)
    replies['CML C1 249'] = lambda: (reader.cancel(), b'')[1]

    with pytest.raises(DeviceCancelled):
        reader.read()

    assert transport.commands.count('CML C1 248') == 2
    assert transport.commands.count('CML C1 249') == 1
    assert transport.cancel_read_count == 1
    assert transport.close_count == 1


def test_failed_startup_closes_before_a_new_reader_starts_cleanly():
    failed_replies = _complete_replies([([116], [116])])
    failed_replies['CML C1 0'] = [b'CML C1 0 ', b'']
    failed = FakeTransport(failed_replies, delayed_output=[b'old-session-tail\r\n'])
    fresh = FakeTransport(_complete_replies([([116], [116])]))

    with pytest.raises(CmlSnapshotFailed):
        _reader(failed).read()
    snapshot = _reader(fresh).read()

    assert failed.close_count == 1
    assert fresh.close_count == 1
    assert snapshot.chords[0][0][0] == 116
    assert fresh.commands[0] == 'ID'


def test_failed_cml_snapshot_cannot_fall_through_to_another_device():
    first = Port()
    first.device = '/dev/first'
    second = Port()
    second.device = '/dev/second'
    failed_replies = _complete_replies([([116], [116])])
    failed_replies['CML C1 0'] = [b'CML C1 0 malformed\r\n'] * 2
    failed = FakeTransport(failed_replies)
    later = FakeTransport(_complete_replies())
    reader = DeviceSnapshotReader(
        list_ports=lambda: [first, second],
        transport_factory=lambda path: {first.device: failed,
                                        second.device: later}[path],
        request_timeout=0.01,
        total_timeout=10,
    )

    with pytest.raises(CmlSnapshotFailed):
        reader.read()

    assert failed.close_count == 1
    assert later.commands == []
    assert later.close_count == 0


def test_incomplete_crlf_response_is_retried_then_fails_closed():
    replies = _complete_replies([([116], [116])])
    replies['CML C1 0'] = [b'CML C1 0 00000000000000000000000000000074 74\r',
                            b'CML C1 0 00000000000000000000000000000074 74\r']
    transport = FakeTransport(replies)

    with pytest.raises(DeviceReadError, match='indexes 0'):
        _reader(transport).read()

    assert transport.commands.count('CML C1 0') == 2


def test_incomplete_framing_is_distinct_from_a_valid_unterminated_reply():
    transport = FakeTransport(_complete_replies())
    transport.replies['ID'] = b'ID CHARACHORDER TWO S3\r'

    with pytest.raises(MissingResponseFraming):
        _reader(transport).read()

    assert transport.close_count == 1


def test_probe_continues_after_unsupported_candidate_and_closes_each_once():
    first = Port()
    first.device = '/dev/first'
    second = Port()
    second.device = '/dev/second'
    rejected = FakeTransport(_complete_replies())
    rejected.replies['ID'] = b'ID CHARACHORDER ONE M0\r\n'
    supported = FakeTransport(_complete_replies())
    transports = {first.device: rejected, second.device: supported}
    reader = DeviceSnapshotReader(
        list_ports=lambda: [first, second],
        transport_factory=lambda path: transports[path],
        request_timeout=0.01,
        total_timeout=10,
    )

    snapshot = reader.read()

    assert snapshot.identity == 'CHARACHORDER TWO S3'
    assert rejected.close_count == 1
    assert supported.close_count == 1


def test_timeout_unsupported_command_and_identity_close_the_port_once():
    timeout = FakeTransport(_complete_replies())
    timeout.replies['ID'] = b''
    with pytest.raises(TransportTimeout, match='timeout'):
        _reader(timeout).read()
    assert timeout.close_count == 1

    unknown = FakeTransport(_complete_replies())
    unknown.replies['ID'] = b'UKN ID\r\n'
    with pytest.raises(DeviceReadError, match='does not support ID'):
        _reader(unknown).read()
    assert unknown.close_count == 1

    unsupported = FakeTransport(_complete_replies())
    unsupported.replies['ID'] = b'ID CHARACHORDER ONE M0\r\n'
    with pytest.raises(UnsupportedDevice):
        _reader(unsupported).read()
    assert unsupported.close_count == 1

    open_failed = FakeTransport(_complete_replies(), open_error=OSError('busy'))
    with pytest.raises(OSError, match='busy'):
        _reader(open_failed).read()
    assert open_failed.close_count == 1

    exchange_failed = FakeTransport(_complete_replies(),
                                    exchange_error=OSError('disconnected'))
    with pytest.raises(OSError, match='disconnected'):
        _reader(exchange_failed).read()
    assert exchange_failed.close_count == 1

    close_failed = FakeTransport(_complete_replies(), close_error=OSError('close failed'))
    with pytest.raises(OSError, match='close failed'):
        _reader(close_failed).read()
    assert close_failed.close_count == 1


def test_cancellation_during_request_closes_the_port_once():
    transport = FakeTransport(_complete_replies())
    reader = _reader(transport)
    transport.on_exchange = reader.cancel
    with pytest.raises(DeviceCancelled):
        reader.read()
    assert transport.close_count == 1


def test_no_matching_port_does_not_open_or_claim_device_data():
    created = []
    reader = DeviceSnapshotReader(
        list_ports=lambda: [], transport_factory=lambda path: created.append(path))
    with pytest.raises(DeviceReadError, match='no CharaChorder'):
        reader.read()
    assert created == []
