#!/usr/bin/bash
# Armada display orientation override for the gamescope Steam session.
#
# Sourced by gamescope-session-plus after the session and environment
# configuration loops, so a saved user choice wins over the device profile.
# The configuration is parsed as data and is never evaluated or sourced.

_armada_display_orientation_config=${ARMADA_DISPLAY_ORIENTATION_CONFIG:-/etc/armada/display-orientation.conf}
_armada_display_orientation=
if [[ -f "$_armada_display_orientation_config" && -r "$_armada_display_orientation_config" ]]; then
	while IFS= read -r _armada_display_orientation_line || [[ -n "$_armada_display_orientation_line" ]]; do
		if [[ "$_armada_display_orientation_line" =~ ^[[:space:]]*orientation[[:space:]]*=(.*)$ ]]; then
			_armada_display_orientation_value=${BASH_REMATCH[1]}
			_armada_display_orientation_value=${_armada_display_orientation_value#"${_armada_display_orientation_value%%[![:space:]]*}"}
			_armada_display_orientation_value=${_armada_display_orientation_value%"${_armada_display_orientation_value##*[![:space:]]}"}
			case "$_armada_display_orientation_value" in
				normal|left|right|upsidedown)
					_armada_display_orientation=$_armada_display_orientation_value
					;;
				auto)
					# Game Mode starts at the device default and
					# armada-gamemode-autorotate follows the sensor.
					_armada_display_orientation=
					;;
				*)
					# An empty or malformed value disables the override.
					_armada_display_orientation=
					;;
			esac
		fi
	done <"$_armada_display_orientation_config"
fi

if [[ -n "$_armada_display_orientation" ]]; then
	ORIENTATION=$_armada_display_orientation
	export ORIENTATION
fi

unset _armada_display_orientation_config _armada_display_orientation_line \
	_armada_display_orientation_value _armada_display_orientation
