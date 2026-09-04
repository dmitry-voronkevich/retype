"""Protocol tests use generated CML lines; no sanitized physical C1 capture exists.

The checked-in cho fixture records the physical Two S3 identity and profile-A
keymap but not individual CML C1 replies, so the C1 examples below encode the
published/report format rather than claiming to be a hardware capture.
"""
import sys
import types

import pytest

from retype.services.device_snapshot import (
    DeviceCancelled, DeviceReadError, DeviceSnapshotReader, PySerialTransport,
    UnsupportedDevice,
    decode_chord_hex, decode_phrase_hex, is_chara_chorder_port,
    parse_cml_count, parse_cml_entry, snapshot_to_chords)


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
    def __init__(self, replies=None, on_read=None, open_error=None):
        self.replies = replies or {}
        self.on_read = on_read
        self.open_error = open_error
        self.commands = []
        self.writes = []
        self.opened = False
        self.close_count = 0

    def open(self):
        self.opened = True
        if self.open_error:
            raise self.open_error

    def write(self, data):
        self.writes.append(data)
        self.commands.append(data.decode().strip())

    def readline(self, _timeout):
        if self.on_read:
            self.on_read()
        command = self.commands[-1]
        reply = self.replies.get(command, b'')
        return reply() if callable(reply) else reply

    def close(self):
        self.close_count += 1


class SerializedFakeTransport(FakeTransport):
    """Fail if a request is sent before its predecessor has been read."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_in_flight = False

    def write(self, data):
        assert not self.request_in_flight
        super().write(data)
        self.request_in_flight = True

    def readline(self, timeout):
        assert self.request_in_flight
        try:
            return super().readline(timeout)
        finally:
            self.request_in_flight = False


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


def test_pyserial_transport_writes_framed_commands_without_flush(monkeypatch):
    opened = []

    class PortWithoutFlush:
        def __init__(self, path, baudrate, timeout, write_timeout):
            self.path = path
            self.baudrate = baudrate
            self.timeout = timeout
            self.write_timeout = write_timeout
            self.writes = []
            self.close_count = 0
            opened.append(self)

        def write(self, data):
            self.writes.append(data)

        def close(self):
            self.close_count += 1

    monkeypatch.setitem(sys.modules, 'serial', types.SimpleNamespace(
        Serial=PortWithoutFlush))
    transport = PySerialTransport('/dev/fake-charachorder')

    transport.open()
    transport.write(b'ID\r\n')
    transport.write(b'CML C0\r\n')
    transport.close()
    transport.close()

    assert opened[0].writes == [b'ID\r\n', b'CML C0\r\n']
    assert opened[0].close_count == 1


def test_reader_serializes_framed_requests_without_transport_flush():
    transport = SerializedFakeTransport(_complete_replies())

    _reader(transport).read()

    assert not transport.request_in_flight
    assert all(write.endswith(b'\r\n') for write in transport.writes)
    assert transport.commands[:2] == ['ID', 'VERSION']


def test_cml_parsing_strictly_decodes_ccos_three_replies():
    assert parse_cml_count('CML C0 2') == 2
    line = _c1(0, [116, 104, 101], [116, 104, 101]).decode().strip()
    input_codes, output_codes = parse_cml_entry(line, 0)
    assert input_codes[:3] == (116, 104, 101)
    assert output_codes == (116, 104, 101)
    assert parse_cml_entry(line + ' 0', 0) == (input_codes, output_codes)
    assert decode_chord_hex('0' * 32) == (0,) * 12
    assert decode_phrase_hex('0258') == (600,)


@pytest.mark.parametrize('line', [
    'CML C0 nope', 'CML C0 10001', 'CML C0 1 1',
])
def test_malformed_count_replies_fail_closed(line):
    with pytest.raises(DeviceReadError):
        parse_cml_count(line)


@pytest.mark.parametrize('line', [
    'CML C1 0 bad 61',
    'CML C1 2 00000000000000000000000000000000 61',
    'CML C1 0 00000000000000000000000000000000 0 1',
    'CML C1 0 00000000000000000000000000000000 01',
])
def test_malformed_cml_entries_fail_closed(line):
    with pytest.raises(DeviceReadError):
        parse_cml_entry(line, 0)


def test_identity_retries_after_usb_serial_endpoint_settles():
    replies = _complete_replies()
    attempts = iter([b'', b'ID CHARACHORDER TWO S3\r\n'])
    replies['ID'] = lambda: next(attempts)
    transport = FakeTransport(replies)
    snapshot = _reader(transport).read()
    assert snapshot.identity == 'CHARACHORDER TWO S3'
    assert transport.commands[:2] == ['ID', 'ID']
    assert transport.close_count == 1


def test_reading_600_cml_entries_reports_device_entry_progress():
    entries = [([116, 104], [116, 104])] * 600
    progress = []

    snapshot = _reader(FakeTransport(_complete_replies(entries))).read(progress.append)

    assert len(snapshot.chords) == 600
    assert progress[-1] == 'Reading CharaChorder CML entries: 600 of 600…'
    assert 'Reading CharaChorder CML entries: 1 of 600…' in progress
    assert 'Reading CharaChorder CML entries: 50 of 600…' in progress


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


def test_malformed_cml_entry_payload_is_skipped_after_its_reply_is_consumed(
        caplog):
    entries = [([116, 104], [116, 104])] * 260
    replies = _complete_replies(entries)
    # cho treats this exact-index, cho-compatible synthetic partial entry as unusable and
    # continues. retype must do the same only after consuming its full line.
    replies['CML C1 258'] = b'CML C1 258 0 0\r\n'
    transport = FakeTransport(replies)

    snapshot = _reader(transport).read()

    assert len(snapshot.chords) == 259
    assert snapshot.skipped_chord_entries == 1
    assert 'CML C1 259' in transport.commands
    assert 'Skipping malformed CML C1 entry 258' in caplog.text
    assert transport.close_count == 1


def test_timeout_does_not_send_another_cml_request_that_could_misattribute_reply():
    replies = _complete_replies([([116, 104], [116, 104])] * 2)
    replies['CML C1 0'] = b''
    transport = FakeTransport(replies)

    with pytest.raises(DeviceReadError, match='timeout waiting for reply to "CML C1 0"'):
        _reader(transport).read()

    assert 'CML C1 1' not in transport.commands
    assert transport.close_count == 1


def test_wrong_index_cml_reply_aborts_before_a_late_reply_can_be_misattributed():
    replies = _complete_replies([([116, 104], [116, 104])] * 2)
    replies['CML C1 0'] = _c1(1, [116, 104], [116, 104])
    transport = FakeTransport(replies)

    with pytest.raises(DeviceReadError, match='index did not match'):
        _reader(transport).read()

    assert 'CML C1 1' not in transport.commands
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
    with pytest.raises(DeviceReadError, match='timeout'):
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


def test_cancellation_during_request_closes_the_port_once():
    transport = FakeTransport(_complete_replies())
    reader = _reader(transport)
    transport.on_read = reader.cancel
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
