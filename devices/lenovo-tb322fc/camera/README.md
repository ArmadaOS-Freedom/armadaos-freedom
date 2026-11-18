# Y700 Gen4 camera bus initialization and idle resource release

The shared kernel includes the legacy SM8750 CAMSS capture path, S5KJNS rear
sensor, GC08A8 front sensor, GT9764 lens actuator and WL2868C camera PMIC.
It is under development. Device testing on 2026-11-18 passed five-frame
RAW capture on both sensors, rear sensor color bars and capture after an
RTC s2idle cycle. A later bright-scene test captured recognizable scenes
from both sensors and verified exposure/gain response and rear manual
focus. Calibrated image quality, automatic controls and application
integration remain unvalidated. See [the device test record](DEVICE-TEST-20261118.md).

The ordinary kernel build and `sm8750-lenovo-elden.dtb` select this support;
no overlay variable or alternate DTB is required. Install a complete matching
kernel, modules and DTB together. The hardware validation below used the
earlier opt-in `7.2.6-y700-camera2` build and `sm8750-lenovo-elden-camera1.dtb`.
The ten tested driver patches are promoted unchanged; patch 0250 limits
the provider-owned ICC contract to Elden so other SM8750 boards retain
their existing CAMCC behavior. The normal Elden board DTB now
receives the same CAMCC/CAMSS interconnect properties through its existing
adaptation patch. The DPU fixed 270-degree configuration is retained.
The accelerometer is temporarily
unavailable; it is not used to decide camera or display orientation.

The port is based on camera-only deltas from
`h0cheung/tb322fc-linux-kernel@bf96c3d4048973298ede78487177c6f1b4f6bc0f`,
board squash `dc7d14dc64c8d4f86220b30cd126b17d805c8474`. The two sensor
programming tables are shared with the Gen5 reference, but the SM8750
CSID980/VFE offsets, Gen4 GPIOs, CCI/PHY routing, supply names and MCLKs
remain Gen4-specific. The original PHY delays, including five 209-us
delays, are preserved. The h0 additional SG contiguity restriction is not
included. CSID and VFE use the legacy same-index pairing restriction.

The target is RAW capture followed by a software ISP. The supported modes
are rear 4080x3072 and front 3264x2448. This does not implement the complete
Qualcomm ICP/BPS/IPE hardware ISP, a 50-MP mode or autofocus. User-space
sensor gain conversion must match this driver's raw gain units: rear
1/32 and front 1/1024. Captures ran at approximately 30 fps. Rear defaults
underexposed the bright test scene; exposure 3000 and gain code 256 (8x)
produced a usable uncalibrated preview. Focus code 768 made the nearby
keyboard clearer than 256 or 512; this is a distance-dependent manual test,
not a new default or an autofocus implementation. Test controls were restored
to their original values. Color calibration, AE/AWB/AF and software-ISP
behavior still need device validation.

CAMSS holds its runtime throughput votes. The shared TITAN_TOP provider
holds separate minimum votes for the domain lifetime, including
CCI-only users. CAMCC is built in for this candidate so its shared genpd
objects cannot outlive its module. The minimum vote value must be checked
on hardware during repeated capture with the display off. At idle the
provider and CAMSS release their votes, all six camera domains are off, and
the four NoC providers can finish `sync_state` and drop their boot floors.

The former enabled-but-unbound SM8750 camera node blocked that synchronization
and kept DDR at its highest reported frequency. The tested build completed
the dependency graph, passed both RAW streams and 10/20-minute RTC s2idle,
and spent over 98.7% of those sampling windows at 200 MHz. DDR/CX/AOSS deep
residency still did not increase; the fuel-gauge interface did not support a
credible watt measurement. These are separate limits, not claims of complete
platform deep sleep.

The S5KJNS backend waits for the PM8550 flash V4L2 async subdevice, so the
camera2 module set enables `CONFIG_V4L2_FLASH_LED_CLASS` together with the
Qualcomm flash LED dependencies. Patch 0249 fixes the flash driver's reverse
release loop, including the one-LED out-of-bounds read, skipped entry,
missing cleanup after the final `REG_TORCH_CLAMP` write fails, and cleanup on
remove. The dependency fix completed the async graph on the tablet. Sensor
RAW capture has been tested; hardware flash and the hardware ISP have not.

WL2868C parent VIN supplies remain unresolved; no parent rail is invented.
Sensor power-down failures must be reported and must block unsafe reuse.
The PMIC output mask is serialized and SYS_EN is cleared after its last
channel shuts down. Sequencer takeover follows the cited v11 driver;
the underlying datasheet and this tablet's register state still need
independent confirmation. Failed provider probe with a physical domain
still on is a candidate rejection. RPMh commit errors are not propagated
by the existing ICC set callback, so device logs are part of acceptance.
Do not load these camera modules into an existing kernel with a different
release, build ID or module set.

This change is scoped to kernel initialization, RAW transport and idle
release. Software-ISP packaging, calibrated image quality and AE/AWB/AF are
future work rather than prerequisites for this bus initialization repair.

Validation sequence: full kernel/modules/DT build and schema checks;
driver binding and idle ICC/clock/rail release; five RAW frames per sensor;
repeated start/stop; display-off capture; RTC s2idle and DDR residency.
Record each stage separately. Successful compilation is not a hardware
test or a measured standby-power improvement.
