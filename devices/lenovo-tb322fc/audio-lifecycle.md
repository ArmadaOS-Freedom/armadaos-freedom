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

## 2026-11-20 new baseline and boot inventory

The PR source is rebased on upstream `1197723` (including PR #134). PR
workflow run `44562085116` completed successfully for head `069f304`; package,
container, disk-image, kernel host, unit, shell, root/container and kShimC
checks all passed. The disk artifact is `armada-disk-pr159`, downloaded as
`armada-20261119.6663d23.img.gz` and verified against its sidecar SHA-256
`d87c3bf8bd4f264f0e8564128333d77824bef29a719537191d5b931e62df9806`.

USB SSH inventory on 2026-11-20 resolved the boot menu entries to OSTree
deployment checksums. Initially `/boot` was `/dev/sda17`, mounted read-only,
with zero available bytes (908 MiB used of 974 MiB). `loader.0` and `loader.1`
were OSTree bootconfig generations, not Qualcomm firmware A/B slots. Do not
edit their entry files directly or touch firmware slots.

Retain the running deployment `2f63f785395f380f7e0a944cf1de5c1c5bd99a65fd6d222c5538cb32aa1d33f9`
(rotation90 candidate), pending deployment
`5311c3ca1bbece2fc682b044f0e1a34594a1c1dbaa1658e58bbb4f08ba5fa370`, and
rollback deployment
`8fb3dbb55fa6e46804f90b9982be58088f023f0179877b53359f98ab2e43f254`.
The six old pinned history deployments were removed through `ostree admin
undeploy`, checking the live deployment order and special current/pending/
rollback markers as indexes shifted: `e3d797599ce1c4d2655fea6ebb74a543d594bad0debc04f1f9576f4e9d50dd66`,
`cb58f54cfee21446a33df42ad2d29c6850ad26d2ab1f252f5b7521ace26e5bd5`,
`d7e1bfb9b9231e868d333fe58c8f5f45f71afced70f36d57485761acf421cc5e`,
`3fad6cae26fad1ce2f16c232d4e0a7328f597556770a4c192f2f9516a6ca0628`,
`b8cd23fb24915e69652e2a5646add08c77949abc816c66d2a81f2f74a6227202`, and
`b93133ab3041360bdc937f14e506518c1f92c480402339b130e4286aed806e0e`.
The pending image is the signed Armada `testing` deployment with image version
`20261111.7943625`; it remains pending and has not been selected as the audio
PR candidate.

After cleanup, only the running, pending and rollback deployments remain. The
active bootconfig is `loader.1`, with exactly three entries for those checksums;
OSTree retired the inactive `loader.0` generation. After cleanup `/boot` had
556 MiB available (351 MiB used of 974 MiB); it was then remounted read-only.
OSTree cleanup also removed
the unregistered pre-PR-#134 `7.2.6-y700-audio1` residual kernel directory;
it was not manually deleted. Qualcomm A/B firmware slots were not touched.

The CI artifact is a generic compressed raw disk image (about 28.49 GiB
expanded) with an MBR-compatible partition layout. It is not the signed,
model-specific split EDL package and it is not the signed descriptor/payload
format consumed by `armada-offline-update`; it must not be written directly to
UFS or imported as an OTA payload. The existing TB322FC EDL workflow requires a
separately produced model package and installer preflight, including recovery
and A/B-slot checks.

The rebased PR candidate has therefore not been deployed or booted. The device
remains on `7.2.6-y700-camera2` with `/boot` read-only and 556 MiB available,
and the current, pending and rollback deployments intact. The next authorized
step is to produce or obtain a TB322FC EDL package from the signed image source,
run the installer's non-writing preflight, and only then perform a controlled
EDL deployment with a verified recovery path. No Qualcomm A/B firmware slot was
modified during this work.

## 2026-11-20 installer fixture CI follow-up

The PR head `cfb500dc` unit workflow `44582788286` failed only in
`tests/installer-source-test.py::test_pending_image_does_not_change_selected_commit`:
Fedora/bootc encoded the temporary OCI reference with the checkout path, while
the test searched for the old literal `/v2_3A_latest`. The same hard-coded
encoding also appeared in `test_seed_selects_explicit_commit_over_newer_import`.
The test now finds the single new `ostree/container/image/` ref by comparing
refs before and after each pull. History fixtures keep their original OCI
input so the fresh-import comparison continues to exercise a real second pull.

The root/container job cannot be rerun with the available GitHub credentials
(`gh run rerun 44582788286 --failed` reports that repository admin rights are
required). The targeted test was rerun locally in a privileged disposable
Fedora 44 container with OSTree, bootc, PyGObject and e2fsprogs installed:
29 tests passed, one existing mount-capability test skipped. No full image or
kernel rebuild was started. The unrelated PR container image run
`44582788186` was still in progress at the time of this update; its already
successful baseline build for `069f304` remains the only completed disk image.

The existing PR disk artifact is generic and the successful signed TB322FC EDL
artifact from `44477168804` is based on source `585f39ec`, not this audio
candidate. Neither artifact is suitable for deploying PR head `cfb500dc`.
The device remains unchanged on `7.2.6-y700-camera2`; the new audio kernel has
not been installed or booted, and Qualcomm A/B slots have not been modified.

## 2026-11-20 rebased candidate kernel build

The kernel candidate is based on PR commit `8f65e1289b76df5671ca4643ad3ba52da2b25aa0`
on top of upstream `1197723`. The build record was committed separately as
`1f74771a47c120c663836a3551494ad08882691c`. The first ad-hoc compile attempt produced a generic defconfig with
MSM and Qualcomm AudioReach disabled; it was stopped before being treated as a
candidate. The existing patched source tree was retained, and the final build
regenerated configuration with the repository's `arm64 defconfig` plus
`packages/kernel/config/armada-kernel.config.overrides`. The resulting config
has `CONFIG_DRM_MSM=y`, `CONFIG_SND_SOC_QCOM=m`,
`CONFIG_SND_SOC_QDSP6=m`, `CONFIG_SND_SOC_QDSP6_APM=m`, and
`CONFIG_SND_SOC_AW88461=m`; its SHA-256 is
`a639aa4e036cae271e51127d734d4e012292ebdb7a69646d5a6599a5e72a243a`.

Using that configuration, `packages/kernel/scripts/build-kernel.sh` completed
with 321 kernel patches applied and no patch failures. Kernel host checks
passed, including DPU rotation (135 cases), DPU performance (25 cases), SSPP
format (38 cases), camera power-management checks and 79 AudioReach lifecycle
checks against the candidate C functions. The full AArch64 `Image`, DTBs, all
1,602 in-tree modules, and the existing AW882xx and ALS bridge modules built
and staged successfully. The build ID is
`198a09bae6b13f4e0859fd713590c433a2d88b9e`; the Image SHA-256 is
`661f392d8f28ec838a22d1f2c2c53ba5d2c62835f1a7968cd34ba09ea5b9b306`.

The compressed kernel package is
`D:\AIWORK\y700-audio-candidate-20261120\armada-kernel-7.2.6.tar.zst`,
SHA-256 `968dd27b34abb992e8aba6b67b4a21e473654661a3641f28331398f28d4709d2`.
It contains `sm8750-lenovo-elden.dtb` (model `Lenovo Legion Y700 Gen4`,
compatible `lenovo,elden`) and the built `snd-q6apm.ko`, `q6apm-dai.ko`, and
`q6apm-lpass-dais.ko` modules. This is a generic kernel tarball, not the
model-specific signed EDL package or signed update descriptor. It is suitable
for build review but is not a direct flash/install payload. The tablet remains
on `7.2.6-y700-camera2`; this candidate has not been installed or booted, and
Qualcomm A/B firmware slots remain unchanged.

At the time of this record, PR #159 remained a draft with no submitted human
review. For head `1f74771a47c120c663836a3551494ad08882691c`, linked issue,
patch catalogue, unit tests, shell tests, kernel host-source checks,
root/disposable-container tests, kShimC, package resolution and the PR's
integration checks all passed. PR run `44596999099` had resolved every package
and was still executing its ARM container-image job; no new image or disk
artifact was available yet. Any eventual PR disk artifact is a generic disk
image, not the signed model-specific EDL package. The earlier disk artifact is
from the pre-audio baseline. The build log is retained at
`D:\AIWORK\y700-audio-kernel-build-20261120-final.log`.
