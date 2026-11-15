# Power file ownership

Power behaviour is split between two source trees. Build the image from a
checkout with `decky/freedom-control` present; the `Containerfile` copies the
control-owned files over `/system_files` after the general `system_files`
layer, so they are the versions that ship.

## Freedom Control

These files belong to the control submodule and are installed from it:

- `usr/libexec/armada/armada-powerd` — the power daemon.
- `usr/lib/armada/devices/lenovo-tb323fu.conf` — the TB323FU board profile.
- `usr/share/armada/power-profiles-SM8850.conf` — the SM8850 chip layer.

The former root `system_files` copies of the daemon and the board profile have
been removed. The canonical files come from Freedom Control and reach the image
only through the `Containerfile` COPY layers, so do not recreate root copies:
nothing at those paths would ship.

## This repository

Shared defaults and runtime helpers stay here and ship from `system_files`:

- `usr/lib/armada/armada_perf.py`
- `usr/share/armada/power-profiles.conf`
- `usr/share/armada/game-tweaks.json`

Freedom Control also carries copies of exactly these three helpers for its own
standalone test suite:

- `usr/lib/armada/armada_perf.py`
- `usr/share/armada/power-profiles.conf`
- `usr/share/armada/game-tweaks.json`

They are test fixtures only. This image does not install them from Freedom
Control: the `Containerfile` installs the authoritative copies from this
repository's `system_files` through the general `COPY system_files
/system_files/` layer. Their presence in the control tree keeps the standalone
control tests runnable and is an explicit exception, not a second source of
truth.

## Test guard

`tests/test_power_source_ownership.py` reads the real checkouts and the
`Containerfile`; it keeps no manifest of its own. It fails when:

- a canonical control file is missing from Freedom Control, reappears in the
  root `system_files` (dangling symlinks included), or is not `COPY`d from the
  exact control path to the same root path;
- the two `system_files` trees overlap on any relative path other than the
  three named fixtures, or the root-owned fixture copies go missing;
- the `Containerfile` installs a control fixture, including through a broad
  `COPY` of the control `system_files` tree;
- no Freedom Control checkout is available, instead of passing silently.

The workflows run it in the existing "Check resolved power daemon and device
control" step, after the plugin resolver has refreshed the control submodule.

Tests that exercise power behaviour load `armada-powerd` from the control
submodule. The image workflows run the infinity scheduler test after
`build_files/resolve-plugin-sources.py`, which checks the submodule out at the
latest `main` commit, so it checks the daemon the image actually copies. A
developer checkout still sits at the recorded gitlink, so run the resolver
before relying on a local result.

## Scope of this change

This source-ownership cleanup aligns the tests with the shipped files and guards
the documented fixture exception.
It does not modify the TB323FU core mask 6-7 that has already shipped. No new
physical-device confirmation was performed for this change, so the parent
issue #69 remains open; this document makes no claim about physical behaviour
beyond what the source files record.
