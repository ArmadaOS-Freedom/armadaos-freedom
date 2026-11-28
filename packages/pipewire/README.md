# pipewire

Fedora's `pipewire` package with upstream PipeWire `master` LHDC v5 Bluetooth
codec support backported onto `1.6.12`, linked against `liblhdcv5`.

`build.sh` first packages the prebuilt `aarch64` `liblhdcv5.so` release binary
and Apache-2.0 headers from `DBeidachazi/liblhdcv5` into `liblhdcv5` and
`liblhdcv5-devel`, installs them in the builder, and then rebuilds Fedora's
pinned `pipewire` SRPM with `-D bluez5-codec-lhdc=enabled`. Both `liblhdcv5`
and the built runtime `pipewire*` RPMs are exported to `./out`.
