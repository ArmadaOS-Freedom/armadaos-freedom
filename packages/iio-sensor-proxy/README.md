# iio-sensor-proxy

Fedora's iio-sensor-proxy is built without its Qualcomm Sensor Core (SSC)
backend, and Fedora does not ship libssc. This package builds both and replaces
Fedora's iio-sensor-proxy in the image:

- [libssc](https://codeberg.org/DylanVanAssche/libssc), pinned to
  `033e2a32095c024d6dbdfb1d4ae1db4a94be32f1` (0.4.4 plus fixes, among them
  giving up when the SSC service never appears). Only the library and `ssccli`
  are installed; the mock server and installed tests are dropped.
- [iio-sensor-proxy](https://gitlab.freedesktop.org/hadess/iio-sensor-proxy)
  3.9 (`0085ddf8ecb173a1c5fcf2344aa40e561125354f`) with `-Dssc-support=enabled`.

`build.sh` builds libssc first, installs it in the builder and builds
iio-sensor-proxy against it; both runtime RPMs land in `out/`. Both projects
are GPL-3.0-or-later; no source patches are applied.

IIO sensors work as with the distribution package. Upstream's udev rule also
lists `ssc-light ssc-compass` on every ADSP/SDSP FastRPC node, which starts the
proxy on every Qualcomm device; without hexagonrpcd serving the DSP the SSC
never answers and libssc waits 30 s per sensor. The spec removes those two
lines (and fails if upstream changes them), so SSC sensors are only used where
a board rule names their types (`IIO_SENSOR_PROXY_TYPE`, for example
`ssc-accel`) and hexagonrpcd (`packages/hexagonrpc`) serves the DSP; see the
TB710FU device README.
