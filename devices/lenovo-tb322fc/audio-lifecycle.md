# AudioReach capture lifecycle candidate

This change addresses three driver state/response defects found while testing
speaker processing on TB322FC. It does not change amplifier gain, firmware,
UCM, topology or the PipeWire speaker graph. The shared AudioReach paths also
serve other Qualcomm boards, so those devices need separate regression tests.

Capture `prepare` starts the frontend graph before a capture START trigger.
Parameter tracing on TB322FC found that prepare followed by close could close
the backend graph 120 before stopping the active frontend graph 2. Component
`hw_free` now stops the capture frontend ahead of backend shutdown. A mutex
serializes capture read requeue against the STOPPED transition and is released
before waiting for the DSP response. Fragments retain their existing
close/reprepare owner, and a successful graph STOP is tracked separately from
the software trigger state to avoid releasing the same start reference twice.

Two independent core fixes accompany the ordering change:

- Shared-memory UNMAP now completes with the command opcode and actual DSP
  status. Its request carries the existing position-buffer token bit; only
  the corresponding handle is cleared after success. The old callback stored
  the outer response opcode, which could never satisfy the command waiter.
- The final graph start reference is released after a successful STOP.
  A failed STOP keeps the reference, so a subsequent call sends a real retry
  instead of falsely succeeding from a zero local count. Nested references
  retain their existing local decrement behavior. A mutex in the shared
  `audioreach_graph` serializes START and STOP across the count decision,
  command wait and count commit. This prevents a concurrent START from
  mistaking an in-flight STOP reference for an already running DSP graph.

On the original kernel, capture prepare/close and playback followed by
WirePlumber restart often succeeded. The observed ordering therefore does not
establish the unique cause of the intermittent DSP timeouts. A temporary FE
module test proved seven capture boundaries with FE2 STOP before BE120
shutdown and 279 successful synchronous commands. Sound-card hot swaps also
produced UNMAP timeouts and contaminated that comparison. These observations
justify a complete candidate and normal-boot regression; they do not prove
all audio failures resolved.

The complete candidate is being built as `7.2.6-y700-audio1` from the exact
tested camera2 plus reflected-fetch rotation source, with all matching modules
and a migrated initramfs. The original camera2 deployment remains the fallback.
The PR patch series is based on upstream `d4c3b13`; its clean source application
and host checks are verified separately from the device build.

The standard series applied 306 patches without fuzz or skipped hunks. After
replacing the initial count-only STOP candidate, all three AudioReach source
hashes match the device build tree. Existing rotation/performance/SSPP and
other applicable host checks pass. The new `test_q6apm_lifecycle.py` compiles
the actual extracted C functions with host stubs: 79 candidate checks, or 94
with baseline negative controls. Its deterministic threaded test holds STOP
until a START attempt is observed, then checks that START reaches the DSP.
These tests are part of the normal kernel host-check entry point. They do not
exercise the real DSP, kernel scheduler or hardware error recovery.

Validation is still in progress. Required target checks are capture
prepare/free/close, start/drop/close, reconfiguration and reprepare,
playback/idle, filter add/remove with WirePlumber restart, Steam restart and
suspend/resume. Inspect command completion, status and kernel logs in addition
to application return codes, visible nodes and active services.

These patches do not repair final-close cleanup after a failed STOP or late
callbacks after graph/port destruction. Retaining the STOP count is not a
promise that an unresponsive DSP can safely recover through close/reopen. Avoid fault
injection or repeated card unbind/module replacement as the main target
validation. Use a normal reboot to establish a clean test state.
