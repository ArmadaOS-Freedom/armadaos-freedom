# Y700 Gen4 AudioReach Lifecycle

## PR #159 scope

PR #159 contains patch `0750-ASoC-qcom-harden-audioreach-teardown.patch`, the
AudioReach lifecycle host regression and its kernel-build integration. The
Elden SoundWire/LPASS system-sleep candidate `0751` was removed from this PR
after review identified it as a separate change. The `0751` candidate and its
review correction are retained outside the repository at
`D:\AIWORK\y700-elden-audio-sleep-candidate-20261121.patch` (SHA-256
`64c110808ac5784f16218ba6dc964e301ecf2cb2dd69b8fd379c130043b81af6`). It is
not applied by the kernel patch series.

CodeRabbit reviewed head `494f90ea` on 2026-11-20. Its scope check requires the
Elden sleep patch to be submitted separately; the wake-IRQ ordering comment
also identified a valid failure path in that patch. The saved local candidate
includes that correction, but the corrected candidate has not been rebuilt or
tested on hardware. This PR keeps the tested AudioReach patch `0750` only.

## AudioReach change

Patch `0750` stops the capture frontend during component `hw_free` before DPCM
tears down its backend, serializes shared graph START/STOP reference updates,
and retains the last reference when DSP STOP fails. STOP and UNMAP responses
are matched to the pending command by opcode and request token. The UNMAP
caller publishes completion only after storing the DSP status, and clears
only its selected mapping handle after a matching successful response. Failed
prepare blocks START in either PCM direction until a later prepare succeeds.
Persistent DSP failure still leaves DPCM to perform best-effort cleanup; this
does not repair callback or graph-object lifetime after final teardown.

## Validation

The `494f90ea` audio2 kernel was built locally, installed and booted on the
TB322FC. The tested kernel included both `0750` and the now-separated `0751`.
Consequently the boot and AudioReach results below validate the combined test
image; system-sleep behavior cannot be attributed to this PR's `0750` patch.

On audio2, capture prepare followed by close without START, start/drop/close,
same-handle reconfiguration and silent playback completed. Three capture
cycles confirmed FE STOP before BE 120 shutdown. All seven observed STOP
requests received matching token responses with DSP status zero. The selected
workflows did not issue an UNMAP request, so firmware UNMAP behavior is not
claimed as device-tested. Playback and capture both reopened after one 45.170
second RTC `s2idle`; that is a smoke result for the combined image, not a
standby-power measurement.

The kernel host lifecycle harness executes extracted candidate C functions
and covers delayed and duplicate replies, token/opcode mismatches, DSP errors,
mapping ownership and both PCM START guards. The PR review fix passed 320/320
patch applications and the full kernel host source checks in a clean Linux
7.2.6 tree; the AudioReach harness reported 191 checks. Patch-catalogue tests
passed 4/4. These local source checks stop before kernel compilation. CI for
the updated PR head is pending. The previous head's x86_64 package matrix item
was skipped. CodeRabbit's `COMMENTED` status is an automated review result,
not a human approval. Current checks must be read from PR #159.

Remaining limits: no UNMAP request was observed on hardware; no persistent DSP
failure recovery was tested; one RTC wake does not establish battery standby
power or validate every SoundWire delayed-resume path. The shared AudioReach
code affects other Qualcomm boards, which need their own regression coverage.
