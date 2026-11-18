# Camera port provenance and review boundaries

Patches 0240–0250 are part of Armada's shared Linux 7.2.6 series. This is a
downstream bring-up port, with separate patches so hardware support and
error handling can be reviewed independently.

| Patch | Origin and purpose |
| --- | --- |
| 0240 | h0 camera squash: balance CSID parent power on startup failure; stop every previously started pipeline component and flush buffers. |
| 0241 | h0 SM8750 resource tables, CSID980, VFE980 and legacy internal PHY support. Omit the old SG-contiguity restriction; retain the legacy paired routing and SOF model. |
| 0242 | h0 S5KJNS, GC08A8 and GT9764 drivers and bindings. Preserve sensor tables, raw gain units and Gen4 supply names. |
| 0243 | h0 WL2868C driver and binding. |
| 0244 | h0 legacy CAMSS binding narrowed to the two driver-consumed paths; SM8750-only CAMCC binding for the separate provider floor. |
| 0245 | Local review fixes for checked power-off, first-error preservation and fail-closed unknown regulator-reference ownership. |
| 0246 | Local review fix for partial GDSC registration cleanup before adding new failure points. |
| 0247 | Local provider-owned dual-path floor. Inspired by Gen5's shared-domain lifetime issue; does not install its CAMSS-owned notifier. |
| 0248 | Limited adaptation of Qianhui Xu's WL2868C v11 output control, with teardown reset diagnostics. Preserve the current Gen4 parent-supply contract. |
| 0249 | Local camera2 dependency fix: reverse and bound the V4L2 flash release loop, release every created subdevice once, and unwind after the final torch-clamp write fails or the driver is removed. |
| 0250 | Shared-build integration: select the provider-owned ICC paths only on Elden; preserve CAMCC probe and the optional ICC binding contract on other SM8750 boards. |

Pinned sources:

- [h0 kernel head](https://github.com/h0cheung/tb322fc-linux-kernel/commit/bf96c3d4048973298ede78487177c6f1b4f6bc0f),
  [camera-containing board squash](https://github.com/h0cheung/tb322fc-linux-kernel/commit/dc7d14dc64c8d4f86220b30cd126b17d805c8474).
  Only camera-related files are imported. Existing file copyright/SPDX
  notices are retained.
- [Gen5 reference](https://github.com/joonhoekim/tb323fu-linux/tree/1508c247c8c833e7f742e58b4a870450309fd954).
  Register programs match for rear reset/init/mode (325 entries) and front
  init/mode (438 entries). Gen5 CSID/VFE addresses, GPIOs and independent
  PHY ABI are not a substitute for SM8750's implementation.
- [Gen4 reference and posted WL2868C v11](https://github.com/xiaquwan1001/lenovo-legion-y700-gen4-tb322fc-elden-linux/tree/f2763c98e36868caea52f4d9dbcbf935c19af307/patches/upstream/wl2868c-v11-20261113).
  The SYS_EN/SEQ semantics are traced to this source and its reference to
  WL2868C Rev 1.4 p.35. The original datasheet was not independently obtained.
- [Android SM8750 camera source](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/tree/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/camera-kernel).
  The local TB322FC_259 camera.ko's 103-entry 2.4.0 PHY table matches all
  four fields in that source. The third field is a delay, including five
  209-us waits. This port preserves the h0 legacy table and explicitly
  does not claim its skipped skew entries are identical to the full
  Android application sequence.

Completed host fault suites extract real implementation C functions into
stubbed APIs. They cover CAMSS partial-start cleanup, sensor/lens power
errors, WL channel concurrency, ICC transactions and registration unwind.
They do not simulate electrical sequencing, interrupts or RPMh firmware.
The camera2 configuration enables the V4L2 flash class required by the
S5KJNS async graph dependency. Its fault suite exercises one- and two-LED
normal unwind, second-LED registration failure and final torch-clamp write
failure against the patched release helper. These are host-side lifecycle
tests, not hardware camera or flash validation. Full kernel/module/DT and
schema validation must precede device testing.

Before any upstream Linux submission, replace imported placeholder binding
maintainer contacts with consenting maintainers and rebase the legacy
camera ABI against the relevant upstream CAMSS/PHY series. No consent or
upstream acceptance is implied by this downstream port.
