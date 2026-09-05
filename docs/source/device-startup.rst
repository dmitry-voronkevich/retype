CharaChorder device loading
===========================

retype reads chord hints only from a connected CharaChorder Two S3 running
CCOS 3.x. Startup loading is enabled by default and can be disabled in the
:doc:`customisation-dialog`; the same read can also be started there with
**Load chords now**. Each read discovers a likely USB serial port, validates
``ID CHARACHORDER TWO S3`` and ``VERSION 3.x``, reads profile A's primary
keymap with ``VAR B3 A1 <index>``, then reads the complete chord map with
``CML C0`` and ``CML C1 <index>``. The operation is read-only and releases the
port once the attempt finishes.

The snapshot runs in a Qt worker thread, so the window remains usable while
large chord maps are enumerated. The settings dialog shows loading progress,
disables the on-demand action during a read, and reports success,
unavailability, cancellation, or failure. A snapshot replaces the current
chord map only after the complete read succeeds; a disabled startup read or
an unsuccessful read retains local or previously loaded chord data.

Hardware diagnostics
--------------------

For a device-assisted diagnosis, start retype from the repository root and
capture a debug log::

   uv run --locked python bin/retype -l DEBUG 2>&1 | tee charachorder.log

The log records discovered serial-port metadata, the selected baud rate,
request/reply timing and raw Serial API replies, keymap and CML counts, and the
number of usable word chords produced. Raw CML replies contain the phrases in
your personal chord library; inspect or redact ``charachorder.log`` before
sharing it publicly.

Failure behavior
----------------

No device, unsupported identity/version, malformed reply, unsupported command,
timeout, cancellation, or an incomplete keymap/chord enumeration replaces the
current chord map. retype never installs old or partial device data from an
unsuccessful read. Connect the supported device and use **Load chords now** to
retry; the window status message and application log include the reason.

Developer protocol boundary
---------------------------

``retype.services.device_snapshot`` owns serial discovery, request ordering,
strict response parsing, cancellation, deadlines, and port closure. Its narrow
``SerialTransport`` protocol is injectable for tests. It uses the published
115200-bps rate, allows one request in flight, enforces the protocol's minimum
100-microsecond inter-command interval, bounds both individual request and
complete-read time, and supports only the verified profile-A path; do not
generalize this to other devices, firmware, or profiles without hardware
evidence.

The protocol tests use generated/sanitized CML C1 lines. The available physical
Two S3 / CCOS 3.0.0 reference fixture records identity and profile-A keymap but
not a captured CML C1 response, so C1 fixture values are format tests rather
than a claim of a physical capture. Live key attribution is deliberately out of
scope: chord detection continues to use ordinary Qt keyboard events.
