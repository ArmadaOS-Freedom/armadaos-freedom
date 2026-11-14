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

Freedom Control also carries copies of some of these helpers for its own test
suite. This image does not install those copies; they are test fixtures or
legacy copies and are not authoritative.

Tests that exercise power behaviour load `armada-powerd` from the control
submodule, so they check the daemon the image actually installs.
