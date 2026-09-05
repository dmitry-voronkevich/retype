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

# The published Serial API specifies 115200 bps.  In particular, this is not
# the higher baud rate used by some older CharaChorder tooling.
BAUD_RATE = 115200
KEY_COUNT = 90
MAX_KEYMAP_ACTION = 2047
MAX_CHORD_COUNT = 10000
REQUEST_TIMEOUT_SECONDS = 1.0
# CCOS requires at least 100 microseconds between commands.  Responses normally
# take longer than this, but enforcing it in the transport keeps fast/fake CDC
# implementations from overrunning the device input buffer.
INTER_REQUEST_DELAY_SECONDS = 0.0001
# Opening a USB CDC port can wake an otherwise idle device. Retrying only the
# harmless identity request lets its serial endpoint settle without relaxing
# the strict reply contract or retrying any snapshot data.
IDENTITY_REQUEST_ATTEMPTS = 3
TOTAL_TIMEOUT_SECONDS = 150.0
_SUPPORTED_ID = ("CHARACHORDER", "TWO", "S3")
_HEX = re.compile(r"[0-9a-fA-F]+$")
_VERSION = re.compile(r"3\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$")


class DeviceReadError(RuntimeError):
    """A device did not provide a complete snapshot we can safely use."""


class DeviceCancelled(DeviceReadError):
    """The application is closing before its one-shot read completed."""


class UnsupportedDevice(DeviceReadError):
    """An attached serial device is not the verified profile."""


class SerialTransport(Protocol):
    def open(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def readline(self, timeout: float) -> bytes: ...
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
        self._last_write = None  # type: float | None

    def open(self) -> None:
        import serial
        logger.info("Opening CharaChorder candidate port=%s baud=%d", self.path,
                    BAUD_RATE)
        self._port = serial.Serial(self.path, BAUD_RATE,
                                   timeout=REQUEST_TIMEOUT_SECONDS,
                                   write_timeout=REQUEST_TIMEOUT_SECONDS)
        logger.debug("Opened serial port=%s", self.path)

    def write(self, data: bytes) -> None:
        if self._port is None:
            raise DeviceReadError("serial port was not opened")
        if self._last_write is not None:
            remaining = INTER_REQUEST_DELAY_SECONDS - (
                time.monotonic() - self._last_write)
            if remaining > 0:
                time.sleep(remaining)
        self._port.write(data)
        self._last_write = time.monotonic()

    def readline(self, timeout: float) -> bytes:
        if self._port is None:
            raise DeviceReadError("serial port was not opened")
        self._port.timeout = timeout
        return self._port.readline()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._port is not None:
                self._port.close()
                logger.debug("Closed serial port=%s", self.path)


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
        raise DeviceReadError("malformed {} in device reply".format(description))
    result = int(value)
    if maximum is not None and result > maximum:
        raise DeviceReadError("{} exceeds supported limit".format(description))
    return result


def decode_chord_hex(value: str) -> tuple[int, ...]:
    """Decode the 12 ten-bit slots in a CML C1 chord input word."""
    if len(value) != 32 or not _HEX.fullmatch(value):
        raise DeviceReadError("malformed CML C1 chord input")
    number = int(value, 16)
    # The top eight bits are a chain id; profile-A chord hints do not use it.
    # The protocol prints key 1 in the most-significant 10-bit slot and trailing
    # unused slots as zeroes. Preserve that documented order here.
    return tuple((number >> shift) & 0x3ff for shift in range(110, -1, -10))


def decode_phrase_hex(value: str) -> tuple[int, ...]:
    """Decode CCOS's variable-width action-code phrase encoding."""
    if value == "0":
        return ()
    if len(value) % 2 or not _HEX.fullmatch(value):
        raise DeviceReadError("malformed CML C1 phrase")
    raw = [int(value[index:index + 2], 16) for index in range(0, len(value), 2)]
    decoded = []
    index = 0
    while index < len(raw):
        byte = raw[index]
        if 1 <= byte <= 31:
            if index + 1 == len(raw):
                raise DeviceReadError("truncated CML C1 extended phrase code")
            decoded.append((byte << 8) | raw[index + 1])
            index += 2
        else:
            decoded.append(byte)
            index += 1
    return tuple(decoded)


def parse_cml_count(line: str) -> int:
    parts = line.split()
    # GET_CHORDMAP_COUNT has no trailing status field in the published API.
    if len(parts) != 3 or parts[:2] != ["CML", "C0"]:
        raise DeviceReadError("malformed CML C0 reply")
    return _parse_decimal(parts[2], "chord count", MAX_CHORD_COUNT)


def parse_cml_entry(line: str, expected_index: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    parts = line.split()
    # GET_CHORDMAP_BY_INDEX always returns its success/error status as field 5.
    if len(parts) != 6 or parts[:2] != ["CML", "C1"]:
        raise DeviceReadError("malformed CML C1 reply")
    if _parse_decimal(parts[2], "chord index") != expected_index:
        raise DeviceReadError("CML C1 reply index did not match its request")
    if parts[5] != "0":
        raise DeviceReadError("CML C1 was rejected by the device (status {})".format(
            parts[5]))
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
            raise DeviceReadError("timed out reading CharaChorder snapshot")

    def _request(self, command: str, deadline: float) -> str:
        self._check(deadline)
        assert self._transport is not None
        request = (command + "\r\n").encode("ascii")
        started = time.monotonic()
        logger.debug("CharaChorder serial TX command=%r bytes=%r", command,
                     request)
        self._transport.write(request)
        timeout = min(self.request_timeout, max(0.0, deadline - time.monotonic()))
        raw = self._transport.readline(timeout)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.debug(
            "CharaChorder serial RX command=%r elapsed_ms=%.1f bytes=%r",
            command, elapsed_ms, raw)
        self._check(deadline)
        if not raw.endswith(b"\n"):
            raise DeviceReadError(
                'timeout or incomplete reply to "{}" after {:.1f} ms; raw={!r}'.format(
                    command, elapsed_ms, raw))
        try:
            line = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise DeviceReadError(
                "device reply was not ASCII; raw={!r}".format(raw)) from exc
        if not line:
            raise DeviceReadError("empty device reply")
        if line.startswith("UKN "):
            rejected = line.split(maxsplit=1)[1]
            raise DeviceReadError("device does not support {}".format(rejected))
        return line

    def _identity(self, deadline: float) -> tuple[str, str]:
        id_line = None
        for attempt in range(IDENTITY_REQUEST_ATTEMPTS):
            try:
                logger.debug("Requesting device identity attempt=%d/%d",
                             attempt + 1, IDENTITY_REQUEST_ATTEMPTS)
                id_line = self._request("ID", deadline)
                break
            except DeviceReadError as exc:
                # A CDC endpoint can need one request timeout after open before
                # it accepts commands. Do not retry malformed/unsupported
                # replies: those are definitive and must fail closed.
                if (attempt + 1 == IDENTITY_REQUEST_ATTEMPTS or
                        not str(exc).startswith(("timeout waiting for reply",
                                                 "timeout or incomplete reply"))):
                    raise
        assert id_line is not None
        id_parts = id_line.split()
        if tuple(id_parts) != ("ID",) + _SUPPORTED_ID:
            got = " ".join(id_parts[1:]) if len(id_parts) > 1 else "invalid reply"
            raise UnsupportedDevice(
                "unsupported device '{}'; retype supports CharaChorder Two S3 / CCOS 3.x".format(got))
        logger.info("Validated CharaChorder identity=%s", " ".join(_SUPPORTED_ID))
        version_parts = self._request("VERSION", deadline).split()
        if len(version_parts) != 2 or version_parts[0] != "VERSION" or not _VERSION.fullmatch(version_parts[1]):
            raise UnsupportedDevice(
                "unsupported CCOS version; retype supports CharaChorder Two S3 / CCOS 3.x")
        logger.info("Validated CharaChorder CCOS version=%s", version_parts[1])
        return " ".join(_SUPPORTED_ID), version_parts[1]

    def _keymap(self, deadline: float) -> tuple[int, ...]:
        values = []
        for index in range(KEY_COUNT):
            parts = self._request("VAR B3 A1 {}".format(index), deadline).split()
            if len(parts) != 6 or parts[:3] != ["VAR", "B3", "A1"] or parts[3] != str(index) or parts[5] != "0":
                raise DeviceReadError("malformed or rejected VAR B3 A1 reply")
            values.append(_parse_decimal(
                parts[4], "keymap action", MAX_KEYMAP_ACTION))
        layout = parse_layout([values])
        if layout is None:
            raise DeviceReadError("profile-A keymap did not contain a usable layout")
        logger.info(
            "Read profile-A keymap entries=%d switches=%d mapped_characters=%d",
            len(values), len(layout.switch_order), len(layout.char_to_switch))
        logger.debug("CharaChorder profile-A keymap=%r", values)
        return tuple(values)

    def _read_candidate(self, path: str, deadline: float,
                        progress: Callable[[str], None] | None) -> DeviceSnapshot:
        # A hardware identity check follows discovery; do not trust USB metadata.
        self._transport = self.transport_factory(path)
        try:
            logger.info("Probing CharaChorder candidate port=%s", path)
            self._transport.open()
            identity, version = self._identity(deadline)
            if progress:
                progress("Reading CharaChorder profile-A layout…")
            keymap = self._keymap(deadline)
            total = parse_cml_count(self._request("CML C0", deadline))
            logger.info("Device reports CML entry_count=%d", total)
            chords = []
            for index in range(total):
                self._check(deadline)
                chords.append(parse_cml_entry(
                    self._request("CML C1 {}".format(index), deadline), index))
                if progress and (index == 0 or index + 1 == total or (index + 1) % 50 == 0):
                    progress("Reading CharaChorder chords: {} of {}…".format(index + 1, total))
            logger.info("Completed CharaChorder snapshot keymap_entries=%d cml_entries=%d",
                        len(keymap), len(chords))
            return DeviceSnapshot(identity, version, "A", keymap, tuple(chords))
        finally:
            self._transport.close()
            self._transport = None

    def read(self, progress: Callable[[str], None] | None = None) -> DeviceSnapshot:
        deadline = time.monotonic() + self.total_timeout
        try:
            ports = self.list_ports()
            logger.info("Serial discovery found %d port(s)", len(ports))
            for port in ports:
                logger.info(
                    "Serial port path=%r vid=%r pid=%r manufacturer=%r product=%r candidate=%s",
                    getattr(port, "device", getattr(port, "path", None)),
                    getattr(port, "vid", getattr(port, "vendorId", None)),
                    getattr(port, "pid", None), getattr(port, "manufacturer", None),
                    getattr(port, "product", None), is_chara_chorder_port(port))
            candidates = [port for port in ports if is_chara_chorder_port(port)]
            if not candidates:
                raise DeviceReadError("no CharaChorder serial device found; connect a Two S3 running CCOS 3.x and restart retype")
            last_error = None
            for candidate in candidates:
                self._check(deadline)
                try:
                    return self._read_candidate(_port_path(candidate), deadline, progress)
                except DeviceCancelled:
                    raise
                except (DeviceReadError, OSError) as exc:
                    logger.warning("CharaChorder candidate port=%s failed: %s",
                                   getattr(candidate, "device", "<unknown>"), exc)
                    last_error = exc
            assert last_error is not None
            raise last_error
        finally:
            self.close()


def snapshot_to_chords(snapshot: DeviceSnapshot) -> dict[str, object]:
    """Adapt a complete device snapshot to the existing BookView map contract."""
    chords = build_chords(snapshot.chords, [list(snapshot.keymap)])
    logger.info("Converted CharaChorder snapshot cml_entries=%d usable_word_chords=%d",
                len(snapshot.chords), len(chords))
    return chords


class DeviceStartupLoader(QObject):
    """Run the blocking one-shot reader on a Qt worker thread."""
    status = pyqtSignal(str)
    snapshotReady = pyqtSignal(object)
    unavailable = pyqtSignal(str)
    cancelled = pyqtSignal()
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
            self.cancelled.emit()
        except UnsupportedDevice as exc:
            logger.warning("CharaChorder startup read unavailable: %s", exc)
            self.unavailable.emit(str(exc))
        except DeviceReadError as exc:
            if str(exc).startswith("no CharaChorder serial device found"):
                logger.warning("CharaChorder startup read unavailable: %s", exc)
                self.unavailable.emit(str(exc))
            else:
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
