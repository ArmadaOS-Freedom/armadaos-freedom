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

The complete `7.2.6-y700-audio1` candidate was built from the tested camera2
plus reflected-fetch rotation source, with matching modules and a migrated
initramfs. The original camera2 deployment remains the fallback. That build
used the source baseline available before upstream PR #134 landed. The PR
branch is now rebased onto upstream `1197723` (PR #134); the full kernel build
has not been repeated against this rebased source.

The standard series applied 306 patches without fuzz or skipped hunks at the
time of the candidate build. After replacing the initial count-only STOP
candidate, all three AudioReach source hashes matched that device build tree.
Existing rotation/performance/SSPP and other applicable host checks passed.
The new `test_q6apm_lifecycle.py` compiles
the actual extracted C functions with host stubs: 79 candidate checks, or 94
with baseline negative controls. Its deterministic threaded test holds STOP
until a START attempt is observed, then checks that START reaches the DSP.
These tests are part of the normal kernel host-check entry point. They do not
exercise the real DSP, kernel scheduler or hardware error recovery.

## Validation status

The complete `7.2.6-y700-audio1` Image, DTBs, in-tree modules and two existing
out-of-tree modules were built and reconciled against the camera2 baseline.
The build contains 1,602 modules and has build ID
`bced30a50bfdc68f295e3d2d9ce24a5feaa27b8f`. The migrated initramfs passed its
independent content, module and dependency checks (SHA-256
`2ba0ffba26945c06e867597a2c7c45106b37226a44329d2025756d2ec9888abd`). The
overlay and isolated OSTree rehearsal also passed their payload, startup,
deployment-retention, BLS and bootlink checks.

The actual tablet remains on `7.2.6-y700-camera2`; the audio1 kernel has not
been installed or booted. The last read-only capacity snapshot reported
69,210,112 bytes of root-available space on `/boot`, zero bytes available to
an unprivileged user, while the complete candidate plus the 32 MiB safety
margin requires 155,377,664 bytes. A previous installer attempt stopped before
boot-partition writes when the OSTree boot copy ran out of space. No old OSTree
deployment, rollback entry, BLS entry or A/B boot slot was intentionally
removed or rewritten by that attempt.

On 2026-11-19 at 23:14 +08:00, read-only SSH verification over Wi-Fi
(`192.168.31.147`) confirmed the tablet was still running
`7.2.6-y700-camera2`. `/boot` was mounted read-only and had zero available
bytes (951,492,608 used of 1,020,702,720); the root overlay also reported zero
available bytes. The pending deployment was still pinned, as were the running
deployment, rollback and six other retained deployments. Boot storage used
about 908 MiB under `/boot/ostree`. The prior unregistered candidate directory
`dea2bdb1…` and its origin file were still present; they were not modified.
`armada-bootimg-sync.service` remained masked and inactive. This snapshot does
not authorize deleting deployments or residual files, changing pins or boot
entries, remounting `/boot`, or installing the candidate.

At 23:34 +08:00, the same read-only checks succeeded through the USB network
SSH endpoint (`172.16.42.1`, password authentication). It confirmed the same
kernel release, zero-free read-only boot filesystem, nine pinned deployments,
masked boot-image sync service, and unregistered `dea2bdb1…` residual. Both
Wi-Fi and USB network paths are available; the USB path is preferred for the
next device checks.

At 23:42 +08:00, read-only audio inventory over USB showed the user PipeWire,
PipeWire Pulse and WirePlumber services active, with the Lenovo-Y700-Gen4 ALSA
playback and capture endpoints present. PipeWire exposed the built-in speaker
sink at volume 0.34 and the built-in microphone source. Steam and gamescope
were running. This is baseline `camera2` service visibility only; no stream
restart, capture cycle, candidate-kernel test or audible test was performed.

The draft PR's latest GitHub run passed linked issue, package resolution,
automatic unit tests, patch catalogue, shell tests, kernel host source checks,
root/disposable-container tests and kShimC. An earlier run failed an unrelated
installer fixture lookup for `/v2_3A_latest`; the test passed on the next run
and is unchanged by this PR. The latest container workflow had applied all 321
kernel patches and passed the kernel host source checks, including the audio
lifecycle regression. Its overall image/package job was still running; a full
kernel compile against the rebased source has not yet been confirmed.

Host lifecycle regression passes 94 checks against the candidate and baseline
kernel C functions, including the constructed concurrent START/STOP window.
The same 94 checks passed under ASan/UBSan in the independent review. These
checks and the complete build do not substitute for running the new kernel on
the tablet.

After rebasing onto upstream `1197723`, the lifecycle test was reproduced
against the archived Linux 7.2.6 source with patches 0750 and 0751 applied;
all 94 candidate and baseline checks passed. Patch 0752 also applied cleanly
to the archived `q6apm-dai.c`. All three patches passed Linux 7.2.6
`checkpatch.pl --strict` with zero errors or warnings. The full kernel build
and the target runtime checks remain pending for the rebased source.

Still required on the tablet: capture prepare/free/close, start/drop/close,
reconfiguration and reprepare, playback/idle, filter add/remove with
WirePlumber restart, Steam restart and suspend/resume. Inspect command
completion, kernel logs, visible nodes and active services as well as
userspace return codes. Keep amplifier gain unchanged at 950/950 and do not
use card unbind/module hot swaps or ADSP stop/start as the validation method.

These patches do not repair final-close cleanup after a failed STOP or late
callbacks after graph/port destruction. Retaining the STOP count is not a
promise that an unresponsive DSP can safely recover through close/reopen. Avoid fault
injection or repeated card unbind/module replacement as the main target
validation. Use a normal reboot to establish a clean test state.
