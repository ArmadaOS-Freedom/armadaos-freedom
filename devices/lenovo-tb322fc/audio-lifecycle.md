# Y700 Gen4 AudioReach Lifecycle

## PR #159 scope

PR #159 contains patches
`0750-ASoC-qcom-harden-audioreach-teardown.patch` and
`0752-ASoC-dpcm-defer-route-update-after-failed-hw-free.patch`, the AudioReach
lifecycle host regression and its kernel-build integration. The Elden
SoundWire/LPASS system-sleep candidate `0751` was removed from this PR after
review identified it as a separate change. The `0751` candidate and its
review correction are retained outside the repository at
`D:\AIWORK\y700-elden-audio-sleep-candidate-20261121.patch` (SHA-256
`64c110808ac5784f16218ba6dc964e301ecf2cb2dd69b8fd379c130043b81af6`). It is
not applied by the kernel patch series.

CodeRabbit reviewed head `494f90ea` on 2026-11-20. Its scope check requires the
Elden sleep patch to be submitted separately; the wake-IRQ ordering comment
also identified a valid failure path in that patch. The saved local candidate
includes that correction, but the corrected candidate has not been rebuilt or
tested on hardware. This PR keeps the AudioReach lifecycle changes in `0750`
and the DPCM route guard in `0752`; the Elden sleep candidate remains separate.

## AudioReach change

Patch `0750` stops the capture frontend during component `hw_free` before DPCM
tears down its backend, serializes shared graph START/STOP reference updates,
and retains the last reference when DSP STOP fails. STOP and UNMAP responses
are matched to the pending command by opcode and request token. The UNMAP
caller publishes completion only after storing the DSP status, and clears
only its selected mapping handle after a matching successful response. Failed
prepare blocks START in either PCM direction until a later prepare succeeds.
If capture component `hw_free` cannot stop the FE graph, DPCM returns the error
and retains the BE parameters. A later `hw_params` retries only FE component
cleanup; after it succeeds, DPCM releases the old DAI/BE parameters before
configuring the new stream. PCM close makes one final FE cleanup attempt before
BE shutdown, then continues best-effort resource release if STOP still fails.
Callback lifetime after final port teardown is unchanged.

## Validation

### Historical device validation for `92fa202c` (2026-11-20)

The matching locally built `7.2.6-y700-audio1` kernel booted on TB322FC with
GNU build ID `4906e0d0df63a57b6efd02470f27659abac49231`. Capture prepare/free
without START, start/drop and reconfigure/reprepare passed on this kernel; the
trace showed FE2 STOP before BE120 shutdown, and all 72 observed synchronous
DSP commands returned success. The CI disk image for this revision was built,
but it was not installed or booted; the device results came from the matching
local build. The earlier device status above remains historical context.

This was normal-path coverage only. Final close after a real DSP STOP timeout,
other Qualcomm boards and acoustic output remain unvalidated.

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
mapping ownership and both PCM START guards. PR CI at `a281f9b0` reports the
previously recorded 223 harness checks; the current local route-recovery
extension reports 299 checks.

### STOP failure recovery review fix (2026-11-21)

CodeRabbit's earlier finding on capture FE STOP failure was valid: Linux
7.2.6 ignored the component `hw_free` result in `soc_pcm_hw_clean()`, so DPCM
continued freeing the BE. The fix propagates the FE component error, keeps BE
state allocated on failure, blocks prepare/START while cleanup is unresolved,
and retries FE component cleanup at the next `hw_params` before releasing the
old BE state. PCM close performs one last retry before BE shutdown and logs a
persistent failure while completing best-effort teardown.

Validation for this review fix passed on 2026-11-21. The complete 319-patch
base series applied to a clean Linux 7.2.6 source tree with zero fuzz. The
follow-up DPCM route guard is carried as patch `0752`, making 320 active kernel
patches in the tested tree. The lifecycle harness reported 223 checks, the full
kernel host source checks passed, and the ARM64 build compiled
`soc-component.o`, `soc-pcm.o`, `q6apm.o`, `q6apm-dai.o` and
`q6apm-lpass-dais.o`. Patch-catalogue validation is recorded with the
repository checks. Existing device results cover normal STOP only; DSP STOP
failure injection and recovery remain untested on hardware.

Remaining limits: no UNMAP request was observed on hardware; no persistent DSP
failure recovery was tested; one RTC wake does not establish battery standby
power or validate every SoundWire delayed-resume path. The shared AudioReach
code affects other Qualcomm boards, which need their own regression coverage.

### DAPM route update after FE STOP failure (2026-11-22)

Reviewing the Linux 7.2.6 call graph found that a DAPM mixer/mux update enters
`snd_soc_dpcm_runtime_update()`, whose old-path shutdown continues to BE STOP,
`hw_free` and shutdown without consulting the FE component `hw_free` result.
Patch `0752` now checks all FE directions for an unresolved
`hw_free_failed` state before changing any DPCM path and returns `-EBUSY` while
one is present. This keeps a route update from bypassing the retained-BE
protection. It records the deferred route change and replays the current DAPM
graph after FE cleanup succeeds, or after close disconnects the FE graph.
Failures in either the old-path or new-path update retain the pending marker so
a later retry repeats the old/new-path update. Each DPCM link carries a
`route_stop_done` marker, so a retry completes the exact old-link teardown
without decrementing a shared BE reference twice. The retry path first forces
all retained old links through the normal shutdown path, then rebuilds the
current DAPM graph. `dpcm_add_paths()` records whether each operation created a
new link or reused a retained link; if a later connection fails, only links
created by that operation are removed and reused links are restored to their
previous state. A retained BE is restarted during reconnection only when its
FE is already START or PAUSED. If that restart was performed by the failed
transaction, rollback stops it again; a failed rollback leaves the link and
pending route attached for the next retry. Ordinary application STOP keeps its
existing best-effort behavior and does not use route-update rollback.
If the final shared graph STOP acknowledgement is lost, a shared graph
uncertainty flag rejects new START references until a STOP retry confirms the
graph state.

Patch `0750` keeps the component `hw_params` mark scoped to the locked
operation, matching its upstream use. Delayed FE cleanup retries all components
with the current substream instead of relying on the component-wide mark, which
another playback or capture stream may overwrite or clear. The extracted-C
host harness executes the production route and component functions. It covers
two-substream mark overwrite and capture recovery, STOP failure recovery across shared,
exclusive and PAUSED BEs, failed rollback START, retained START/PAUSED link
reconnection, FE recovery deferral, new-path startup failure, DAPM path-query
failure and retry.

Validation on 2026-11-22 started from a fresh extraction of the cached official
Linux 7.2.6 archive (SHA-256
`039aef84f2b0994aeda3f4fcfc3d02ec9d7a9bbb9020ea264c43f446c860f606`). All 320
kernel patches applied with strict `-F0` checking, and the complete
`KERNEL_HOST_CHECKS_ONLY=1` run passed, including 462 AudioReach lifecycle
checks and patch-catalogue tests 4/4. The harness includes retained
PAUSED-route reconnection and a lost final STOP acknowledgement followed by
START rejection and successful STOP recovery. Both `0750` and `0752` pass
`checkpatch.pl --strict` with no errors, warnings or checks.
With the repository ARM64 config merged, `soc-component.o`, `soc-pcm.o`,
`soc-compress.o`, `q6apm.o`, `q6apm-dai.o` and `q6apm-lpass-dais.o` compiled
successfully.

This is source, host-harness and compile validation only. The physical tablet
has normal capture lifecycle validation, but no injected DSP STOP failure
followed by a DAPM route change has been tested on hardware.

### Review-fix revision (2026-11-23)

Commit `5f48ca27` (`fix(audio): address DPCM recovery review findings`) was
pushed to PR #159. The extracted-C production harness passed 575 checks against
the Linux 7.2.6 candidate tree. Both `0750` and `0752` passed
`checkpatch.pl --strict` with zero errors, warnings or checks. The worktree is
clean and the remote PR head matches this commit.

For this revision, automatic unit tests, kernel host source checks, kShimC host
tests, root/disposable-container tests, shell tests, patch catalogue, package
resolution and CodeRabbit review all passed. The x86_64 package matrix was
skipped by workflow configuration. The ARM64 container-image job was still
running when this record was written; no failure had been reported. The
physical device has not been re-tested with injected DSP STOP failure and DAPM
route changes.
