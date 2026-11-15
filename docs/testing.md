# Testing

The workflow in `.github/workflows/unit-tests.yml` runs five jobs. Each one
executes a different kind of test against the real tree, and each has its own
prerequisites.

## Automatic unit tests (`unittest`)

`tests/run_unit_tests.py` discovers every `tests/test_*.py` file with the
standard-library `unittest` loader. It prints the discovered groups and the
executed, skipped, failed, and error counts, and fails on a real failure or
import error.

One test is held back by exact name:

- `test_edl_raw_package.ProducerContractTests.test_filesystem_pair_real_ext4_and_btrfs_images`

It creates a real ext4/btrfs pair through loop devices, so it needs root,
`/dev/loop-control`, mount, and the btrfs tools. The default run lists it with
that prerequisite and leaves it out. A privileged environment can include it:

```bash
export TEST_ARTIFACT_ROOT="/path/to/test-output"
mkdir -p "$TEST_ARTIFACT_ROOT"
/usr/bin/python3 tests/run_unit_tests.py --integration
```

No other test is excluded or converted into a success. Skips the tests declare
themselves still report as skips.

## Shell and dashed-script tests (`script-tests`)

`tests/run_script_tests.py` discovers every `tests/*-test.sh` and
`tests/*-test.py` file in sorted order. It has two execution groups:

- `unit` runs as an unprivileged UID 1000 account inside a Fedora 44 container.
- `privileged` runs as root inside a disposable Fedora 44 container.

The members of the privileged group, the tests that need a separate upstream
source tree, and the per-test arguments or environment entries are listed in
`tests/script-test-groups.json`. Everything else is in `unit`.

Both groups use `quay.io/fedora/fedora:44`. The production image is built on
`quay.io/fedora/fedora-bootc:44`, so the tests run on the same Fedora 44
userspace instead of Ubuntu 24.04. That matters concretely: Ubuntu 24.04's
libadwaita 1.5 has no `prefer-wide-layout` property, which the GUI
`AdwAlertDialog` tests set, and Ubuntu has no `/usr/bin/bootc` at all. Fedora
44 carries libadwaita 1.9 and bootc 1.17.1, the versions the production base
uses.

```bash
/usr/bin/python3 tests/run_script_tests.py --list
/usr/bin/python3 tests/run_script_tests.py --group unit
/usr/bin/python3 tests/run_script_tests.py --group privileged
```

`--list` prints each discovered file and its group without running anything;
it also shows the reason for every manager-only entry.

Exit status `77` is the suites' "skipped: precondition unavailable" code and is
reported as a skip. Any other non-zero status is a failure, including an
exception, an import error, or a missing dependency. A failed test never stops
the remaining tests, and the runner exits non-zero if any test failed.

`script-tests` installs its dependencies with `dnf` before it checks the tree
out. That order matters twice: `actions/checkout` needs `git`, and
`tests/steam-pinyin-test.py` inspects the real Git tree modes of the checkout,
so the tree must be a genuine Git working tree. The list covers what the
scripts and dashed Python tests actually need: a C/C++ compiler, `jq`, `gdisk`,
`xvfb`/`xauth`, `dbus-x11`, `python3-gobject` together with
`gobject-introspection`, `python3-vdf`, `python3-pytest`, the GTK 3 and GTK
4/libadwaita typelibs, `ostree`/`ostree-libs`, the device-tree tools,
`diffutils`, `openssl`, and `procps-ng`. `diffutils` and `openssl` are real
dependencies, not padding: several scripts compare files with GNU `cmp`/`diff`
and call `openssl`; `procps-ng` supplies the `pgrep` the power-button tests use.
Weak dependencies stay off, which is why `gobject-introspection` is listed
explicitly — it carries the cairo typelib the GI bindings need — and why
`libasan` and `libubsan` are listed too: they carry the
`libasan.so.8.0.0`/`libubsan.so.1.0.0` runtimes that the C cases built with
`-fsanitize=address,undefined` link against, and `gcc` does not pull them in as
a hard dependency. Both Fedora `dnf` lists, the unprivileged one and the
disposable-container one, include `libasan` and `libubsan` for that reason.

The group runs as a dedicated UID 1000 account created inside the container,
with its home under `RUNNER_TEMP`. Only that home, the artifact root, and the
checkout are chowned to it, and it has no `wheel` membership and no sudo, so
nothing in `unit` can reach the root-only paths. `runuser` starts the runner as
that account with an explicit `HOME`, `TEST_SCRIPT_ARTIFACT_ROOT`,
`LC_ALL=C.UTF-8`, an empty `LANGUAGE`, and `GSK_RENDERER=cairo`; the C.UTF-8
locale lets tests that match translated message text see the C.UTF-8
catalogue.

The runner itself is started under `xvfb-run -a --server-num=70`, which gives
the GTK tests a display. `tests/installer-gui-test.py` starts and manages its
own `:99` server, so the outer display deliberately avoids 99.

`TEST_SCRIPT_ARTIFACT_ROOT` (or `TEST_ARTIFACT_ROOT`) must name a writable
directory; there is no temporary-directory fallback. For every test execution
the runner creates a fresh short directory directly under the artifact root
with `tempfile.mkdtemp` and points `TMPDIR` at it while it prints and logs the
full test name. Earlier directories are left in place, so a second run against
the same artifact root never inherits a work directory that already holds the
previous run's files. The short name matters, because
`tests/powerbutton-screen-test.sh` binds an AF_UNIX socket below `TMPDIR` and
the 108-byte `sun_path` limit is exceeded when the artifact path repeats the
whole test filename.

A test that needs an option or a container marker gets it from the
`arguments`/`environment` maps in `tests/script-test-groups.json` rather than
being demoted to a skip. Arguments are passed as literal `argv` entries with no
shell; the only placeholder is `{work}`, which expands to this execution's own
fresh short directory under the artifact root:

- `update-offline-privileges-test.py` takes `build_files/50-create-user.sh` and
  the expectation `allow`; it checks the real sudo policy the image ships.
- `kshim-write-loop-test.py` takes `--work {work}/loop` and `--repo-root .`.
  The runner already starts every test in the repository root, so the literal
  `.` is the checkout and `{work}` gives the test scratch space under the
  artifact root.
- `steam-pinyin-test.py` runs with `ARMADA_PINYIN_TEST_CONTAINER=1`; the
  privileged job deploys `system_files/usr/share/armada/ibus/profile` to
  `/usr/share/armada/ibus/profile` in the container first, because that path is
  a documented precondition the test refuses to create itself.

Run the ordinary scripts as an unprivileged user with the dependencies above. The OCI import test belongs to the privileged group because it must restore ownership on the staged repository.

The privileged group is run in a throwaway `quay.io/fedora/fedora:44` Docker
container with `--privileged` so the root-only tests and the real loop-device
case have the capabilities they need. It installs the same Fedora 44 dependency
set with `dnf`, plus `bootc`, `sudo`, `openssh-server`, and `procps-ng` (the
unprivileged list already carries `procps-ng` for `pgrep`), and both lists
include `libasan` and `libubsan` for the sanitizer-built C cases; there is no
Ubuntu `apt` step. The checkout is mounted read-write and credentials are not
written into it. The real ext4/btrfs test is invoked by its exact qualified
name only when `/dev/loop-control` exists in the container. No system user or
loop device on the runner host is modified.

`tests/run_script_tests.py` also reports the following as NOT RUN, because the
checkout or a plain container cannot supply their inputs:

- `gamescope-touch-orientation-test.py` — needs `GAMESCOPE_SOURCE_DIR` pointing
  at a patched gamescope tree.
- `kshim-ostree-lifecycle-test.py` — needs `--work`, `--repo-root`, and a native
  ARM64 container with the production ostree.
- `steam-oobe-path-test.sh` — needs systemd as PID 1 and the installed
  `armada-steam-oobe` units and fixups.

These are listed rather than silently skipped so the gap stays visible.
`gamescope-lowpower-test.py` is not in that list: it compiles the code carried
by the tracked patch `packages/gamescope/patches/0043-*` and needs no external
gamescope source tree, so it runs in `unit`.

## Root-private ownership (`privileged-tests`)

The same disposable container runs eight `tests/test_update_*.py` modules as
root, by exact module name:

```
test_update_offline_jobs
test_update_offline_session
test_update_system_manager
test_update_offline_retirement
test_update_finalization_receipt
test_update_channel_selection
test_update_desktop
test_update_recovery_channel
```

Their private session storage is created with `0700` root-owned directories and
opened with `fcntl`/`O_NOFOLLOW`, so the root run is what makes them meaningful.
The ordinary `unittest` job still discovers and runs the same files; there they
report their declared root-ownership skips, and those skips are never counted as
passes.

The container also runs the qualified ext4/btrfs loop test described above when
loop devices are available.

`tests/update-oci-pull-test.py` is in the privileged group because it drives
the real `/usr/bin/bootc` OCI import, and that import changes ownership of the
staged OSTree repository, which needs root. `bootc` is installed in the
container for that reason, and the test is never demoted to a `NOT RUN` entry.

## Kernel host source checks (`kernel-source-checks`)

`packages/kernel/scripts/build-kernel.sh` runs six source checks after the base
and overlay patches and the DTS stages, and before it generates the kernel
config. The checks read the actual patched tree:

```bash
python3 packages/kernel/tests/test_dpu_rotation.py TREE --work DIR
python3 packages/kernel/tests/test_dpu_sspp_format.py TREE --work DIR
KERNEL_TREE=TREE python3 packages/kernel/tests/test_msm_telemetry_pm.py
KERNEL_TREE=TREE python3 packages/kernel/tests/test_sm8850_lutdma.py
python3 packages/kernel/tests/test_infinity_package.py
KERNEL_TREE=TREE python3 packages/kernel/tests/test_infinity_review.py
```

The infinity review runs only when the `infinity` overlay is applied; the other
five always run. Because the checks are part of the build script, a normal
package build fails if they find a regression.

`KERNEL_HOST_CHECKS_ONLY=1` stops the script after the checks, before config
generation and compilation. That mode does not require the cross compiler:

```bash
KERNEL_OVERLAYS=infinity KERNEL_HOST_CHECKS_ONLY=1 WORK_DIR=/path/to/work \
  bash packages/kernel/scripts/build-kernel.sh
```

The CI job selects the infinity overlay so all six checks execute. It does not
extract script fragments and does not reimplement the patch series.

## kShimC host tests (`kshim-host-tests`)

The CMake host suite compiles and runs the real cases from
`devices/lenovo-tb323fu/boot/kshim-success-source/tests`:

```bash
cmake -S devices/lenovo-tb323fu/boot/kshim-success-source/tests \
  -B /path/to/kshim-host -DKSHIM_TEST_SANITIZERS=ON
cmake --build /path/to/kshim-host --parallel 2
ctest --test-dir /path/to/kshim-host --output-on-failure
```

The vendored snapshot deliberately omits its third-party sources, and a
recursive checkout does not prepare them. CrDK and LVGL are cloned at an exact
commit and checked out detached before CMake runs:

```bash
cd devices/lenovo-tb323fu/boot/kshim-success-source
git init lib/CrDK && git -C lib/CrDK remote add origin https://github.com/Project-Aloha/CrDK
git -C lib/CrDK fetch --depth 1 origin b6c3335136b7f6b28271917de11e8f11982975fc
git -C lib/CrDK checkout --detach FETCH_HEAD
git init third_party/lvgl && git -C third_party/lvgl remote add origin https://github.com/lvgl/lvgl
git -C third_party/lvgl fetch --depth 1 origin 7f07a129e8d77f4984fff8e623fd5be18ff42e74
git -C third_party/lvgl checkout --detach FETCH_HEAD
```

`lib/CrDK` keeps its real `.git` directory because the CrDK preparation reads it
with `rev-parse` and `archive`. The job verifies the resulting `HEAD` against
the pinned commit, so a floating branch cannot stand in for the pin.

`lib/libfdt` is restored separately from the pinned public
`sunflower2333/kShimC` commit, and only that one subdirectory is exported:

```bash
dep=build/dependency-sources/kShimC
git init "$dep"
git -C "$dep" remote add origin https://github.com/sunflower2333/kShimC
git -C "$dep" fetch --depth 1 origin 82ce6eaa10c36e64cfd3f16799172d854f9a955b
git -C "$dep" checkout --detach FETCH_HEAD
git -C "$dep" archive 82ce6eaa10c36e64cfd3f16799172d854f9a955b lib/libfdt | tar -x -C .
```

The archive contains `lib/libfdt` only, so the rest of the snapshot is never
replaced by the upstream tree. The restored `include/libfdt_env.h` still needs
the freestanding adaptation. Let the `git archive` step above rewrite the file
first, then insert `#include <stdlib.h>` after `#include <stdint.h>` — the
anchored `sed` runs once on that fresh export, so repeating the export/sed pair
cannot add a duplicate line. The result must be LF-terminated and match
`132cb14b8f0b323e5701d4bfcb0ebba4b1f35726bfdfa6739f18ad638d20e582`:

```bash
sed -i '/^#include <stdint.h>$/a #include <stdlib.h>' lib/libfdt/include/libfdt_env.h
printf '%s  %s\n' \
  132cb14b8f0b323e5701d4bfcb0ebba4b1f35726bfdfa6739f18ad638d20e582 \
  lib/libfdt/include/libfdt_env.h | sha256sum -c -
```

The suite needs a C/C++ compiler, CMake, Python 3 with `kconfiglib` (the
`python3-kconfiglib` package, because the configure step imports it), `dtc`,
`pkg-config`, and the host `libfdt` development files. The host `libfdt` is a
convenience only: the CMake targets compile `lib/libfdt/fdt*.c` from the
restored tree and put `lib/libfdt/include` ahead of the system headers, so a
host `libfdt-dev` install cannot replace the missing directory. The ARM
picolibc side is not involved in host tests.

## MangoHud battery check

The RPM build runs the local battery-percent regression unconditionally. The
spec declares `battery_percent_test.cpp` as `Source1`; `build.sh` copies it from
the package's `tests/` directory into `SOURCES`, and `%check` compiles it against
the patched `src/battery.cpp`.

MangoHud 0.8.4 ships the single-header ghc implementation as
`include/filesystem.h`. There is no `ghc/filesystem.hpp` in the tarball and no
`filesystem.wrap` subproject for Meson to fetch, so `%check` compiles with
`-I"$src/src" -I"$src/include"` and fails early if that header is absent:

```bash
g++ -std=c++17 -O2 -o battery_percent_test \
  %{SOURCE1} "$src/src/battery.cpp" -I"$src/src" -I"$src/include" \
  -lspdlog -lfmt
```

A normal `rpmbuild -bb` therefore runs the check. The upstream Meson test suite
may remain disabled through `%bcond_with tests` and `-Dtests=disabled`.

## Running locally

Install the dependencies the relevant job installs, then run its command from
the repository root. The script commands below assume a Fedora 44 container
with the packages listed above and an unprivileged UID 1000 account; the unit
commands still run on the host. Replace `/path/to/test-output` below:

```bash
export TEST_ARTIFACT_ROOT="/path/to/test-output"
export TEST_SCRIPT_ARTIFACT_ROOT="/path/to/test-output/scripts"
mkdir -p "$TEST_SCRIPT_ARTIFACT_ROOT"
/usr/bin/python3 tests/run_unit_tests.py
LC_ALL=C.UTF-8 LANGUAGE= GSK_RENDERER=cairo \
  xvfb-run -a --server-num=70 /usr/bin/python3 tests/run_script_tests.py --group unit
```

The script runner refuses to start when neither `TEST_SCRIPT_ARTIFACT_ROOT`
nor `TEST_ARTIFACT_ROOT` is set, because it needs them for its short per-test
temporary directories. It does not fall back to a default location.
