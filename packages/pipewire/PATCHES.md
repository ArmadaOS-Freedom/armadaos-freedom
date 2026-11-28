# Patches

Patches applied on top of `BASE.env` (`pipewire-1.6.12-1.fc44`). Each entry's
`source` is an upstream PipeWire commit on `master` backported to `1.6.12`.
Unless noted below, patches are verbatim.

- `patches/0001-bluez5-add-LHDC-v5-codec-using-liblhdcv5.patch`
  source: https://gitlab.freedesktop.org/pipewire/pipewire/-/commit/eee19d3ed5ea1c971a3499880a18d71be7f37090
  notes: Appended `SPA_BLUETOOTH_AUDIO_CODEC_LHDC_V5` after `SPA_BLUETOOTH_AUDIO_CODEC_OPUS_G` in `spa/include/spa/param/bluetooth/audio.h` to avoid renumbering existing 1.6.x A2DP codec enum IDs.
- `patches/0002-bluez5-improve-LHDC-v5-codec-handling.patch`
  source: https://gitlab.freedesktop.org/pipewire/pipewire/-/commit/641631fa6ed0436a6846badc3f9ef9f5854e481f
- `patches/0003-bluez5-allow-manual-LHDC-v5-quality-overrides.patch`
  source: https://gitlab.freedesktop.org/pipewire/pipewire/-/commit/08a92f017e51378a1a3aa9025e5d58a16359090f
- `patches/0004-bluez5-fix-LHDC-v5-encoder-setup.patch`
  source: https://gitlab.freedesktop.org/pipewire/pipewire/-/commit/f300f5992571ffcc7b94069502fa48bc7ff58867
