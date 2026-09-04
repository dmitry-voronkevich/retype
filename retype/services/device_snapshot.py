"""Read one complete, supported CharaChorder snapshot without mutating a device.

Only the CharaChorder Two S3 / CCOS 3.x profile-A protocol path is supported.
The transport is deliberately small so protocol tests do not require hardware.
"""
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from qt import QObject, QThread, pyqtSignal

from retype.services.chords import build_chords, parse_layout

logger = logging.getLogger(__name__)

BAUD_RATE = 921600
KEY_COUNT = 90
MAX_CHORD_COUNT = 10000
REQUEST_TIMEOUT_SECONDS = 1.0
INTER_REQUEST_DELAY_SECONDS = 0.0001
# Opening a USB CDC port can wake an otherwise idle device. Retrying only the
# harmless identity request lets its serial endpoint settle without relaxing
# the strict reply contract or retrying any snapshot data.
IDENTITY_REQUEST_ATTEMPTS = 3
TOTAL_TIMEOUT_SECONDS = 150.0
_SUPPORTED_ID = ("CHARACHORDER", "TWO", "S3")
_HEX = re.compile(r"[0-9a-fA-F]+$")
_STRICT_STATUS = re.compile(r"(?:0|[1-9][0-9]*)$")
_VERSION = re.compile(r"3\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$")


class DeviceReadError(RuntimeError):
    """A device did not provide a complete snapshot we can safely use."""

    def __init__(self, message: str, raw: bytes | None = None):
        super().__init__(message)
        self.raw = raw


class DeviceCancelled(DeviceReadError):
    """The application is closing before its one-shot read completed."""


class UnsupportedDevice(DeviceReadError):
    """An attached serial device is not the verified profile."""


class TransportTimeout(DeviceReadError):
    """No bytes arrived before the request's bounded response timeout."""


class MissingResponseFraming(DeviceReadError):
    """A response contains contradictory or incomplete line framing."""


class MalformedDeviceReply(DeviceReadError):
    """A response is present but does not match the requested protocol grammar."""


class DeviceRejected(DeviceReadError):
    """The device explicitly rejected a supported command."""


class DeviceIndexMismatch(DeviceReadError):
    """A CML reply belongs to a different requested cell."""


class CmlSnapshotFailed(DeviceReadError):
    """One or more required CML cells still failed after their retry."""


class SerialTransport(Protocol):
    def open(self) -> None: ...
    def exchange(self, request: bytes, timeout: float) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class DeviceSnapshot:
    """All data required to derive hints, installed only as one unit."""
    identity: str
    version: str
    profile: str
    keymap: tuple[int, ...]
    chords: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...]


class PySerialTransport:
    """The sole runtime dependency wrapper around a pyserial connection."""
    def __init__(self, path: str):
        self.path = path
        self._port = None
        self._closed = False
        self._has_written = False

    def open(self) -> None:
        import serial
        self._port = serial.Serial(self.path, BAUD_RATE,
                                   timeout=REQUEST_TIMEOUT_SECONDS,
                                   write_timeout=REQUEST_TIMEOUT_SECONDS)

    def exchange(self, request: bytes, timeout: float) -> bytes:
        """Write one request and receive its one serialized response.

        ``read_until`` returns its buffered bytes when the timeout expires.  The
        reader deliberately validates those bytes instead of treating a missing
        LF as a timeout: CCOS may omit the final LF on an otherwise complete
        response.
        """
        if self._port is None:
            raise DeviceReadError("serial port was not opened")
        if self._has_written:
            time.sleep(INTER_REQUEST_DELAY_SECONDS)
        self._port.write(request)
        self._has_written = True
        self._port.timeout = timeout
        return self._port.read_until(b"\n")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._port is not None:
                self._port.close()


def is_chara_chorder_port(port: object) -> bool:
    """Narrow discovery before identity validation opens a candidate port."""
    raw_vendor = getattr(port, "vid", None)
    if raw_vendor is None:
        raw_vendor = getattr(port, "vendorId", "")
    vendor_matches = (raw_vendor == 0x303A or
                      str(raw_vendor or "").lower() in ("303a", "0x303a"))
    manufacturer = str(getattr(port, "manufacturer", "") or "").lower()
    return vendor_matches or "charachorder" in manufacturer


def serial_ports() -> list[object]:
    """List serial ports lazily so importing retype needs no device installed."""
    from serial.tools import list_ports
    return list(list_ports.comports())


def _port_path(port: object) -> str:
    path = getattr(port, "device", None) or getattr(port, "path", None)
    if not isinstance(path, str) or not path:
        raise DeviceReadError("serial discovery returned a port without a path")
    return path


def _parse_decimal(value: str, description: str, maximum: int | None = None) -> int:
    if not value.isdecimal():
        raise MalformedDeviceReply("malformed {} in device reply".format(description))
    result = int(value)
    if maximum is not None and result > maximum:
        raise MalformedDeviceReply("{} exceeds supported limit".format(description))
    return result


def _parse_status(value: str) -> int:
    if not _STRICT_STATUS.fullmatch(value):
        raise MalformedDeviceReply("malformed device status in device reply")
    return int(value)


def decode_chord_hex(value: str) -> tuple[int, ...]:
    """Decode the 12 ten-bit slots in a CML C1 chord input word."""
    if len(value) != 32 or not _HEX.fullmatch(value):
        raise MalformedDeviceReply("malformed CML C1 chord input")
    number = int(value, 16)
    # The top eight bits are a chain id; profile-A chord hints do not use it.
    return tuple((number >> shift) & 0x3ff for shift in range(0, 120, 10))


def decode_phrase_hex(value: str) -> tuple[int, ...]:
    """Decode CCOS's variable-width action-code phrase encoding."""
    if value == "0":
        return ()
    if len(value) % 2 or not _HEX.fullmatch(value):
        raise MalformedDeviceReply("malformed CML C1 phrase")
    raw = [int(value[index:index + 2], 16) for index in range(0, len(value), 2)]
    decoded = []
    index = 0
    while index < len(raw):
        byte = raw[index]
        if 1 <= byte <= 31:
            if index + 1 == len(raw):
                raise MalformedDeviceReply("truncated CML C1 extended phrase code")
            decoded.append((byte << 8) | raw[index + 1])
            index += 2
        else:
            decoded.append(byte)
            index += 1
    return tuple(decoded)


def parse_keymap_entry(line: str, expected_index: int) -> int:
    """Parse one strict VAR B3 A1 response and classify its status."""
    parts = line.split()
    if len(parts) != 6 or parts[:3] != ["VAR", "B3", "A1"]:
        raise MalformedDeviceReply("malformed VAR B3 A1 reply")
    index = _parse_decimal(parts[3], "keymap index")
    if index != expected_index:
        raise DeviceIndexMismatch("VAR B3 A1 reply index did not match its request")
    action = _parse_decimal(parts[4], "keymap action", 1023)
    status = _parse_decimal(parts[5], "keymap status")
    if status != 0:
        raise DeviceRejected("VAR B3 A1 was rejected by the device")
    return action


def parse_cml_count(line: str) -> int:
    parts = line.split()
    if len(parts) not in (3, 4) or parts[:2] != ["CML", "C0"]:
        raise MalformedDeviceReply("malformed CML C0 reply")
    if len(parts) == 4 and _parse_status(parts[3]) != 0:
        raise DeviceRejected("CML C0 was rejected by the device")
    return _parse_decimal(parts[2], "chord count", MAX_CHORD_COUNT)


def parse_cml_entry(line: str, expected_index: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    parts = line.split()
    if len(parts) not in (5, 6) or parts[:2] != ["CML", "C1"]:
        raise MalformedDeviceReply("malformed CML C1 reply")
    if _parse_decimal(parts[2], "chord index") != expected_index:
        raise DeviceIndexMismatch("CML C1 reply index did not match its request")
    if len(parts) == 6 and _parse_status(parts[5]) != 0:
        raise DeviceRejected("CML C1 was rejected by the device")
    return decode_chord_hex(parts[3]), decode_phrase_hex(parts[4])


class DeviceSnapshotReader:
    """Synchronous protocol reader intended to run in ``DeviceStartupLoader``.

    Every request is written only after its predecessor's validated response.
    An error, timeout, or cancellation discards the accumulating local data.
    """
    def __init__(self, list_ports: Callable[[], list[object]] = serial_ports,
                 transport_factory: Callable[[str], SerialTransport] = PySerialTransport,
                 request_timeout: float = REQUEST_TIMEOUT_SECONDS,
                 total_timeout: float = TOTAL_TIMEOUT_SECONDS):
        self.list_ports = list_ports
        self.transport_factory = transport_factory
        self.request_timeout = request_timeout
        self.total_timeout = total_timeout
        self._cancelled = threading.Event()
        self._transport = None  # type: SerialTransport | None
        self._closed = False
        self._last_response_raw = None  # type: bytes | None

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._transport is not None:
                self._transport.close()

    def _check(self, deadline: float) -> None:
        if self._cancelled.is_set():
            raise DeviceCancelled("CharaChorder read cancelled during shutdown")
        if time.monotonic() >= deadline:
            raise TransportTimeout("timed out reading CharaChorder snapshot")

    @staticmethod
    def _response_text(command: str, raw: bytes) -> str:
        """Decode exactly one reply, accepting a complete reply without LF.

        A bare response is accepted only when its complete grammar is later
        validated by the command parser. A CR without LF, multiple LFs, or
        bytes after LF would make response boundaries ambiguous and fails
        closed rather than risking attribution to a later request.
        """
        if not raw:
            raise TransportTimeout('transport timeout waiting for reply to "{}"'.format(command))
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise MalformedDeviceReply("device reply was not ASCII", raw) from exc

        if "\n" in text:
            if text.count("\n") != 1 or not text.endswith("\n"):
                raise MissingResponseFraming("ambiguous response framing", raw)
            text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
            if "\r" in text:
                raise MissingResponseFraming("ambiguous response framing", raw)
        elif "\r" in text:
            raise MissingResponseFraming("response ended with incomplete CRLF framing", raw)

        if not text or text != text.strip():
            raise MalformedDeviceReply("empty or whitespace-padded device reply", raw)
        return text

    @staticmethod
    def _with_raw(error: DeviceReadError, raw: bytes) -> DeviceReadError:
        if error.raw is not None:
            return error
        return type(error)(str(error), raw)

    def _request(self, command: str, deadline: float) -> str:
        self._check(deadline)
        assert self._transport is not None
        timeout = min(self.request_timeout, max(0.0, deadline - time.monotonic()))
        try:
            raw = self._transport.exchange((command + "\r\n").encode("ascii"), timeout)
        except TimeoutError as exc:
            raise TransportTimeout(
                'transport timeout waiting for reply to "{}"'.format(command)) from exc
        self._last_response_raw = raw
        try:
            self._check(deadline)
        except DeviceReadError as exc:
            raise self._with_raw(exc, raw)
        line = self._response_text(command, raw)
        if line.startswith("UKN "):
            rejected = line.split(maxsplit=1)[1] if len(line.split()) == 2 else command
            raise DeviceRejected("device does not support {}".format(rejected), raw)
        return line

    def _identity(self, deadline: float) -> tuple[str, str]:
        id_line = None
        for attempt in range(IDENTITY_REQUEST_ATTEMPTS):
            try:
                id_line = self._request("ID", deadline)
                break
            except DeviceReadError as exc:
                # A CDC endpoint can need one request timeout after open before
                # it accepts commands. Do not retry malformed/unsupported
                # replies: those are definitive and must fail closed.
                if (attempt + 1 == IDENTITY_REQUEST_ATTEMPTS or
                        not isinstance(exc, TransportTimeout)):
                    raise
        assert id_line is not None
        id_parts = id_line.split()
        if tuple(id_parts) != ("ID",) + _SUPPORTED_ID:
            got = " ".join(id_parts[1:]) if len(id_parts) > 1 else "invalid reply"
            raise UnsupportedDevice(
                "unsupported device '{}'; retype supports CharaChorder Two S3 / CCOS 3.x".format(got))
        version_parts = self._request("VERSION", deadline).split()
        if len(version_parts) != 2 or version_parts[0] != "VERSION" or not _VERSION.fullmatch(version_parts[1]):
            raise UnsupportedDevice(
                "unsupported CCOS version; retype supports CharaChorder Two S3 / CCOS 3.x")
        return " ".join(_SUPPORTED_ID), version_parts[1]

    def _keymap(self, deadline: float) -> tuple[int, ...]:
        values = []
        for index in range(KEY_COUNT):
            values.append(parse_keymap_entry(
                self._request("VAR B3 A1 {}".format(index), deadline), index))
        if parse_layout([values]) is None:
            raise DeviceReadError("profile-A keymap did not contain a usable layout")
        return tuple(values)

    @staticmethod
    def _escaped_raw(raw: bytes | None) -> str:
        """Represent untrusted device bytes safely in one log field."""
        return repr(raw) if raw is not None else "<no output>"

    def _read_cml_entry(
            self, index: int, deadline: float
    ) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
        """Read a cell twice at most, retaining diagnostics without partial data."""
        command = "CML C1 {}".format(index)
        for attempt in range(1, 3):
            try:
                raw_line = self._request(command, deadline)
                try:
                    return parse_cml_entry(raw_line, index)
                except DeviceReadError as exc:
                    # Parsing happens after raw framing validation; retain the
                    # exact received response in the per-attempt diagnostic.
                    raw = self._last_response_raw
                    raise self._with_raw(exc, raw)
            except DeviceCancelled:
                raise
            except (DeviceReadError, OSError) as exc:
                raw = exc.raw if isinstance(exc, DeviceReadError) else None
                logger.warning(
                    "CharaChorder CML C1 request index=%d attempt=%d failed: %s; raw=%s",
                    index, attempt, exc, self._escaped_raw(raw))
        return None

    def _read_candidate(self, path: str, deadline: float,
                        progress: Callable[[str], None] | None) -> DeviceSnapshot:
        # A hardware identity check follows discovery; do not trust USB metadata.
        self._transport = self.transport_factory(path)
        try:
            self._transport.open()
            identity, version = self._identity(deadline)
            if progress:
                progress("Reading CharaChorder profile-A layout…")
            keymap = self._keymap(deadline)
            total = parse_cml_count(self._request("CML C0", deadline))
            chords = []
            failed_indices = []
            for index in range(total):
                self._check(deadline)
                chord = self._read_cml_entry(index, deadline)
                if chord is None:
                    failed_indices.append(index)
                else:
                    chords.append(chord)
                if progress and (index == 0 or index + 1 == total or (index + 1) % 50 == 0):
                    progress("Reading CharaChorder chords: {} of {}…".format(index + 1, total))
            if failed_indices:
                raise CmlSnapshotFailed(
                    "CML C1 requests failed after one retry for indexes {}".format(
                        ", ".join(str(index) for index in failed_indices)))
            return DeviceSnapshot(identity, version, "A", keymap, tuple(chords))
        finally:
            transport = self._transport
            self._transport = None
            if transport is not None:
                transport.close()

    def read(self, progress: Callable[[str], None] | None = None) -> DeviceSnapshot:
        deadline = time.monotonic() + self.total_timeout
        try:
            candidates = [port for port in self.list_ports()
                          if is_chara_chorder_port(port)]
            if not candidates:
                raise DeviceReadError("no CharaChorder serial device found; connect a Two S3 running CCOS 3.x and restart retype")
            last_error = None
            for candidate in candidates:
                self._check(deadline)
                try:
                    return self._read_candidate(_port_path(candidate), deadline, progress)
                except DeviceCancelled:
                    raise
                except CmlSnapshotFailed:
                    # A verified device produced an incomplete snapshot. A
                    # later candidate must not turn this failed read into a
                    # successful, unrelated handoff.
                    raise
                except (DeviceReadError, OSError) as exc:
                    last_error = exc
            assert last_error is not None
            raise last_error
        finally:
            self.close()


def snapshot_to_chords(snapshot: DeviceSnapshot) -> dict[str, object]:
    """Adapt a complete device snapshot to the existing BookView map contract."""
    return build_chords(snapshot.chords, [list(snapshot.keymap)])


class DeviceStartupLoader(QObject):
    """Run the blocking one-shot reader on a Qt worker thread."""
    status = pyqtSignal(str)
    snapshotReady = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, reader: DeviceSnapshotReader | None = None):
        super().__init__()
        self.reader = reader or DeviceSnapshotReader()
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.started.connect(self.run)
        self.finished.connect(self.thread.quit)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.reader.cancel()

    def run(self) -> None:
        try:
            snapshot = self.reader.read(self.status.emit)
        except DeviceCancelled:
            logger.info("CharaChorder startup read cancelled")
        except DeviceReadError as exc:
            logger.warning("CharaChorder startup read failed: %s", exc)
            self.failed.emit(str(exc))
        except Exception:
            logger.exception("Unexpected CharaChorder startup read failure")
            self.failed.emit(
                "unable to read CharaChorder; check its connection and restart retype (see application log)")
        else:
            self.snapshotReady.emit(snapshot)
        finally:
            self.thread.quit()
            self.finished.emit()
