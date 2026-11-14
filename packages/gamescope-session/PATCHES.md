# Patches

Patches applied on top of BASE.env. Each entry's `source` is an upstream URL pinned
to a commit, or `armada` if it is original; a URL source with no `notes` is verbatim.
`notes` mean the file was modified. Where the upstream repository cannot be
established from the patch header, `source` is a pinned snapshot of the file in
this repository and `notes` name the header author and commit.

- `patches/0001-Disable-session-launcher-xtrace.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0001-Disable-session-launcher-xtrace.patch
  upstream: unknown
  notes: Header author virtudude (dated 2026-07-30); the header carries no commit id. Removes `set -x` from the session launcher; upstream repository could not be established.
- `patches/0002-Extend-the-startup-socket-timeout.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0002-Extend-the-startup-socket-timeout.patch
  upstream: unknown
  notes: Header author virtudude (dated 2026-07-30); the header carries no commit id. Raises the first-launch socket timeout from five to fifteen seconds; upstream repository could not be established.
- `patches/0003-Add-an-independent-HDR-ITM-target-option.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0003-Add-an-independent-HDR-ITM-target-option.patch
  upstream: unknown
  notes: Header author virtudude (dated 2026-07-30); the header carries no commit id. Adds `ARMADA_HDR_NITS` and the internal device id without forcing HDR at startup; upstream repository could not be established.
- `patches/0004-source-armada-specific-quirks.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0004-source-armada-specific-quirks.patch
  upstream: unknown
  notes: Header author Radical, header commit `4b370f318e143f97178ab8ae55c10f3f6f5ccee3`. Sources Armada's device environment and applies the fake-output-mm and rotation-shader quirks; upstream repository could not be established.
- `patches/0005-add-drm-lease-functions.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0005-add-drm-lease-functions.patch
  upstream: unknown
  notes: Header author Radical, header commit `c02ac14361022d79960de96afcbab08a6511267b`. Adds `--lease-connector` and `--ignore-touch-device` for the secondary display when Gamescope supports them; upstream repository could not be established.
- `patches/0006-armada-force-vulkan-realtime-option.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0006-armada-force-vulkan-realtime-option.patch
  upstream: unknown
  notes: Header author Radical, header commit `ecc948c47e11857d2444b5be787fd6c1400ae1bf`. Sets the Gamescope Vulkan realtime flag from Armada's game-tweaks configuration; upstream repository could not be established.
- `patches/0007-rp5-rpf2-visionox-panel-quirks.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0007-rp5-rpf2-visionox-panel-quirks.patch
  upstream: unknown
  notes: Header author JPyke3, header commit `4727f0d3f075d89edebd02370eff9db2616cae27`. Adds Retroid Pocket 5 and Pocket Flip2 Visionox display quirks; upstream repository could not be established.
- `patches/0008-fix-konkr-fit-elite-scaling.patch`
  source: armada
  upstream: local
  notes: Armada-authored (patch header). Corrects the KONKR Pocket FIT Elite quirk from `OUTPUT_MM` to `FAKE_OUTPUT_MM`, which is the variable forwarded to Gamescope for SteamUI physical-size scaling.
- `patches/0009-fix-konkr-fit-refresh-rates.patch`
  source: https://github.com/ArmadaOS-Freedom/armadaos-freedom/blob/b75a6d48e006782b886627dc2dee2b8691aa64f6/packages/gamescope-session/patches/0009-fix-konkr-fit-refresh-rates.patch
  upstream: unknown
  notes: Header author FeralAI, header commit `3629c715113b875a1da8e8a4e0cc16953fb0209e`. Extends the KONKR Pocket FIT refresh-rate list to 60, 90, 120 and 144 Hz; upstream repository could not be established.
