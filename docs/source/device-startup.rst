CharaChorder device startup
============================

retype reads chord hints only from a connected CharaChorder Two S3 running
CCOS 3.x. At application startup it discovers a likely USB serial port,
validates ``ID CHARACHORDER TWO S3`` and ``VERSION 3.x``, reads profile A's
primary keymap with ``VAR B3 A1 <index>``, then reads the complete chord map
with ``CML C0`` and ``CML C1 <index>``. The operation is read-only and releases
the port once the attempt finishes.

The snapshot runs in a Qt worker thread, so the window remains usable while
large chord maps are enumerated. A short non-blocking status-bar message was
chosen instead of a progress bar because the existing window has no startup
progress surface and the message reports both availability and actionable
failure without adding a second stateful UI control.

Failure behavior
----------------

No device, unsupported identity/version, malformed payload, explicit device
rejection, transport timeout, missing framing, index mismatch, cancellation, or
an incomplete keymap/chord enumeration leaves chord features unavailable. The
Book View receives a new map only after one immutable snapshot has been fully
validated and converted. retype never reuses old or
partial device data. Connect the supported device and restart to retry; the
window status message and application log include the reason.

Each command uses one serialized request/response exchange. A syntactically
complete response is accepted when CCOS omits its trailing LF, but incomplete
CRLF framing, multiple replies, non-ASCII data, and malformed or mismatched
payloads fail closed. The transport waits about 100 microseconds between writes
without flushing individual commands. For each ``CML C1`` cell, a failed reply
is retried exactly once. If both replies fail, retype logs the requested index,
escaped raw output when received, and its specific reason; it still reads later
cells for diagnostics, then discards the entire snapshot.

After a failed CML exchange, retype first reads until the CDC connection has
been quiet for 150 milliseconds. This bounded recovery drain records and
discards a delayed tail before the required retry or later diagnostic request.
It prevents a fragment such as ``CML C1`` or a late ``C1 <index>`` suffix from
being attributed to the next request, while retaining strict validation rather
than accepting the fragment. The published Serial API requires a restful
request/response sequence, at least 100 microseconds between commands, and
warns that overflowing the device input buffer can crash it. The local Cho
reference reader uses the same quiet-period approach for USB CDC replies.

Shutdown and later startup attempts
-----------------------------------

Cancellation interrupts PySerial's pending read before the worker releases the
port. The synchronous close is attempted exactly once in the reader's
``finally`` path and verified through PySerial's ``is_open`` state. A new
transport clears only pre-existing host input bytes before its first request,
so a late line from a closed session cannot be mistaken for its handshake.
retype never sends ``RST`` as recovery: if the device's CDC endpoint remains
unavailable after retype has quit, disconnect or power-cycle the device before
trying again.

Developer protocol boundary
---------------------------

``retype.services.device_snapshot`` owns serial discovery, request ordering,
strict response parsing, cancellation, deadlines, and port closure. Its narrow
``SerialTransport`` exchange protocol is injectable for tests; test transports
must be fake and must not enumerate or open a physical device. It allows one
request in flight, bounds both individual request and complete-read time, and
supports only the verified profile-A path; do not generalize this to other
devices, firmware, or profiles without hardware evidence.

The protocol tests use generated/sanitized CML C1 lines. The available physical
Two S3 / CCOS 3.0.0 reference fixture records identity and profile-A keymap but
not a captured CML C1 response, so C1 fixture values are format tests rather
than a claim of a physical capture. Live key attribution is deliberately out of
scope: chord detection continues to use ordinary Qt keyboard events.
