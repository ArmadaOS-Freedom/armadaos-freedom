# TB322FC initial camera device test, 2026-11-18

The tested kernel was built from commit
`0546b280f174e78feae40aee911a3858abb58d63`, with release
`7.2.6-y700-camera2` and GNU build ID
`0672cb1c4a4df2258fb685f7fccd134c48afaa73`. Later documentation changes do
not describe a new kernel build. This record covers the initial nighttime
checkpoint and the later bright-scene tests on the same boot. Basic optical
capture and rear manual focus now work; calibrated image quality and
software-ISP integration remain pending.

## Deployment and registration

The complete kernel/module/initramfs set was deployed together. Live and
deployment copies of all 1650 payload files, including 1602 modules, were
verified. The signed 96-MiB boot image was verified after writing both B and
A, with full readback SHA-256
`0f0f178e7ef4423b6c017c8fcbc5a4129ac0dcf11a82829d5354dba42608f46d`.
The partition tables and previous seven deployments were retained. A
matching fixed-270 rollback image and both pre-write slots were saved on
the host.

The flash V4L2 class registered and the async notifiers had no pending
fwnodes. The driver automatically created both enabled, immutable
sensor-to-PHY links. All 32 V4L2 subdevice nodes appeared. No sensor link
was created manually to bypass notifier completion.

## RAW capture

Each initial capture used four mmap buffers and stopped after five frames.
All returned sequence numbers were 0 through 4, with the expected
`bytesused` and approximately 33-ms intervals.

| Sensor | Route | Video format | Row stride | Frame bytes |
| --- | --- | --- | ---: | ---: |
| GC08A8 front | PHY4 → CSID4 pad1 → VFE4 RDI0 | pRAA, 3264×2448 | 4080 | 9987840 |
| S5KJNS rear | PHY3 → CSID3 pad1 → VFE3 RDI0 | pgAA, 4080×3072 | 5104 | 15679488 |

The two mutable links were enabled for each test and disabled in reverse
order afterwards. Sink-pad formats propagated to the respective source
pads and were read back before streaming. Sensor and video-node defaults
must not be assumed to match: the capture nodes initially used UYVY.

Both files were copied to the host and decoded as packed MIPI RAW10. The
rear format has four padding bytes per row. Its GRBG CFA is `[G R; B G]`;
the front RGGB CFA is `[R G; G B]`.

The front image contained weak scene structure near black level, while the
rear optical image was close to a dark frame. Illumination and obstruction
were not independently established. Increasing front gain to raw code
8192 changed the signal and noise; a rear test with exposure 3000 and gain
256 remained close to black. Both tests stayed within the advertised
control ranges, then restored front exposure/gain to 2308/1024 and rear
to 256/32. These observations do not establish correct normal-light image
quality or fully validate either gain model.

Rear `test_pattern=2` produced five identical, complete white/yellow/cyan/
green/magenta/red/blue/black bars after decoding. The preview was visually
inspected and showed no row offset or half-image discontinuity. The test
pattern was restored to zero. The flash LED remained off throughout.

## Bright-scene optical and manual-focus test

After the user confirmed adequate illumination, eight additional captures
each delivered five consecutive frames (sequence 0 through 4), totalling
40 frames and 570263040 bytes. Both sensors used the formats and row strides
listed above, with approximately 33-ms intervals. Full-file and individual
frame SHA-256 values matched the downloaded evidence. All capture and
cleanup commands returned zero, both mutable links were disabled after
each test, and all eight capture kernel-log intervals were empty.

The front default exposure/gain 2308/1024 produced a recognizable real
scene. Changing its gain to 4096 (4x) increased the signal. The rear
default 256/32 produced a very dark keyboard image. Raising exposure to
3000, then gain to 256 (8x), produced a clearly visible keyboard. Frame 4
raw means were 64.696, 73.744 and 143.035 respectively; these changing-scene
measurements demonstrate control response, not a calibrated gain curve.

With rear exposure/gain held at 3000/256, GT9764 positions 256, 512 and
768 were tested. The lens subdevice remained open throughout each capture
so the actuator stayed powered. At 768, keyboard legends such as "caps
lock", "shift", "ctrl" and "fn" became readable; 256 and 512 were blurred.
This verifies manual focus response at this subject distance. It does not
establish a universal focus default or provide autofocus. The lens was
restored to 512, front exposure/gain to 2308/1024, rear to 256/32, and rear
test pattern to zero. All camera file descriptors were closed.

The primary decoded previews were visually inspected. No row offset or
half-image discontinuity was observed in those samples. The previews use
2x2 CFA binning, percentile black subtraction, gray-world gains and gamma;
they are uncalibrated host visualizations, not libcamera or hardware-ISP
output. Sensor rows are deliberately unrotated. Application mounting
orientation still needs validation independently of the fixed-270 DPU.

The first post-test state check found all camera devices suspended and
all six GDSCs off. A subsequent collector observed rear CCI/CAMCC active
after control reads had reopened and powered the lens. A final read-only
check, without opening V4L2 nodes, again found CAMSS, CAMCC, both CCIs, both
sensors and the lens suspended, all six camera GDSCs off, all 118 camera
clock prepare/enable/protect counts zero and all 24 CAMSS/CAMCC ICC rows
zero. There were no open camera file descriptors. This verifies eventual
release; the control-read power-down latency was not measured.

Evidence directory on the test host:
`D:/AIWORK/Y700-Boot-Failure-Handoff-20261115/reports/device-tests/camera2-bright-scene-20261118-1147/`.
It contains capture scripts and upload hashes, all RAW files, media graphs,
control readbacks, previews, `capture-verification.json`, and the final
`pm-followup.json`. These tests did not change the kernel, deployment or
signed A/B images. They do not add a new sleep or watt measurement.

## Stream-off and sleep

After both initial streams and control tests, CAMSS, CAMCC, both CCIs and
both sensors returned to runtime suspend. All six camera GDSCs were off,
camera ICC average/peak votes were zero, and camera clock prepare/enable/
protect counts were zero. No camera error was logged in the capture
intervals. The first front interval contained an ath12k status warning.

A 45-second RTC alarm woke the device after 45.169736 seconds measured by
the difference between boottime and monotonic elapsed time. APSS sleep
duration increased by 45.162540 seconds. The 47.541427-second sampling
window included entry and resume work; DDR spent 92.497% at 200 MHz and
0.135% at 4761 MHz. The previous fixed-270 baseline had remained entirely
at 4761 MHz during its 90-second test. The environments were not identical,
so this establishes removal of the frequency lock, not a measured watt
reduction. DDR/CX/AOSS deep-sleep counters still did not increase.

After resume, each camera again captured five consecutive RAW frames
without new kernel messages, then released its mutable links. The two
active display planes retained rotation 8 at 60 Hz. Physical display,
touch, long-duration sleep and software-ISP regression tests remain open.

The RTC alarm was cleared and the transient service was collected. The
system's sole failed unit was tmpfiles cleanup of an older immutable
OSTree test fixture, logged before this sleep. Existing Iris firmware
initialization errors reappeared after resume. Earlier DPU vblank timeouts
preceded this sleep; their cause remains separate and unresolved.

## Battery-only Wi-Fi sleep tests

Later on the same boot, Wi-Fi SSH was used for separate 600-second and
1200-second RTC-woken s2idle tests. External power was offline at all
recorded endpoints, and the battery reported discharging. Neither test
changed the kernel, deployment, fixed-270 configuration or sleep policy.

| Test | Measured sleep | APSS sleep delta | DDR 200 MHz | DDR 4761 MHz |
| --- | ---: | ---: | ---: | ---: |
| 600-second RTC | 600.562419 s | 600.445378 s | 98.7658% | 0% |
| 1200-second RTC | 1200.328906 s | 1200.216929 s | 98.9903% | 0.00799% |

Frequency percentages cover the whole before-to-immediate-after interval,
including approximately 2.54 seconds of awake entry/resume work per test.
Both wakes were RTC IRQ 214, with one suspend success increment, no failure
increment and no reboot. DDR/CX/AOSS deep-sleep counters and DDR LPM states
0xd4, 0xd3 and 0x11 still had no increment. Low DDR frequency does not
establish full platform power collapse.

Neither window produced a usable watt measurement. Reported remaining
charge increased by 22540 and 29100 uAh while discharging. In both cases,
`charge_now` exactly equalled the changing `charge_full` estimate multiplied
by unchanged integer SOC (98% and 97%). `charge_counter` uses the same
firmware property. Two minutes of post-wake samples in the second test
showed further capacity-estimate changes. Even a positive later difference
was rejected as sleep power; post-wake current is not sleep current.

All three camera-resource snapshots in each test and the final restoration
check passed: seven devices suspended, six camera GDSCs off, 118 camera
clocks with zero prepare/enable/protect counts and 24 CAMSS/CAMCC ICC rows
at zero. Wi-Fi reconnected, SDDM remained active, the display retained two
rotation-8 planes at 60 Hz, the RTC alarm was cleared and transient units
were collected. No camera/DPU error or call trace appeared in either
interval. Existing Iris firmware errors, a Wi-Fi hwmon parent-PM warning
and a USB HS-PHY L2 warning remain separately tracked.

The host evidence is in
`D:/AIWORK/Y700-Boot-Failure-Handoff-20261115/reports/device-tests/camera2-wifi-standby-20261118/`.
Both complete result files and executed scripts were downloaded and
compared against remote SHA-256 values. A high-resolution fuel-gauge
source or calibrated measurement is needed for watt acceptance; resource
transition tracing is the next step for the remaining deep-sleep issue.

## Accepted scope and future work

The user accepted this checkpoint for kernel bus initialization and idle
resource release. The tested driver patches are promoted into the shared
kernel, with the same CAMCC/CAMSS properties in the ordinary board DTB.
The validation above names the original camera2 build explicitly; it is
not a claim that every new system image has been retested on hardware.

Software ISP, AE/AWB, calibrated colors, sensor mounting orientation and
application integration remain future work. Any sensor helpers must use
rear raw gain divided by 32 and front raw gain divided by 1024. Autofocus,
display-off capture and remaining platform deep-sleep blockers are tracked
separately from this bus initialization repair.

Full Qualcomm ICP/BPS/IPE processing, 50-MP mode and autofocus are still
outside this implementation. Initial RAW and color-bar tests do not
constitute acceptance of those features.
