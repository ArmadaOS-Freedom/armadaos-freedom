# Patches

Patches applied on top of BASE.env. Each entry's `source` is an upstream URL pinned
to a commit, or `armada` if it's original; a URL source with no `notes` is verbatim.
`notes` mean the file was modified.

- `patches/0001-fix-gamepad-share-raw-input.patch`
  source: armada
- `patches/0002-fix-force-feedback-reset-effects-when-replacing-targets.patch`
  source: armada
- `patches/0003-feat-Hardware-Support-Add-AYN-Thor-Lite.patch`
  source: https://github.com/ShadowBlip/InputPlumber/pull/746
  notes: AYN Thor Lite support

- `patches/0004-add-dbus-touch-events.patch`
  source: armadaos-freedom
  notes: Adds a `SendTouchEvent` method to the CompositeDevice D-Bus interface;
  it clamps a normalized touchpad contact and queues it via `write_event` so
  contact updates retain FIFO order without the button-delay mechanism.
  `SendEvent` awaits `write_send_event` instead of
  `blocking_write_send_event`. Authorizes through the existing
  `org.shadowblip.Input.CompositeDevice.SendEvent` polkit action; no new
  action or policy change.

- `patches/0005-freedom-touch-virtual-source.patch`
  source: armadaos-freedom
  notes: Adds the Freedom Touch plugin's private source name, "Freedom Touch
  Controller Source", alongside the other virtual pad and hotkey names in
  `src/input/manager.rs`.

- `patches/0006-preserve-digital-edges.patch`
  source: armada
  notes: Adds the opt-in `preserve_digital_edges` composite-device option
  (schema and `CompositeDeviceConfigOptions`, default false). When enabled,
  `SteamDeckUhidDevice::write_event` snapshots the packed digital-state bytes
  before applying an event and writes a report immediately if they changed,
  preserving rapid press/release/repress edges that would otherwise be
  coalesced until the next poll. Frame increment moves into `write_state` so
  every report keeps a unique frame. Only the Freedom Touch virtual source
  profile opts in. Unit tests in `src/input/target/steam_deck_uhid.rs` cover
  opt-in button/hat/trigger edges, default-off behavior, duplicate-state and
  pure-analog suppression, and frame wraparound.
