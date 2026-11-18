# Patches

Patches applied on top of BASE.env. Each entry's `source` is an upstream URL pinned
to a commit, or `armada` if it is original; a URL source with no `notes` is verbatim.
`notes` mean the file was modified.

- `patches/0001-rpcd-listener-fetch-large-input-buffers-with-get_in_.patch`
  source: armada
  notes: The listener fetches input buffers above next2's 256 bytes with adsp_listener method 5 (get_in_bufs2) instead of dropping the request. The TB710FU (SM8650) sensor hub sends registry writes larger than that.
- `patches/0002-rpcd-apps_std-optional-writable-sensor-registry.patch`
  source: armada
  notes: With HEXAGONRPCD_WRITABLE_REGISTRY set, the persist sensor registry directory maps to that host directory and the DSP may create, write, fsync, remove and rename plain files there (the directory itself and its registry/ subdirectory only). Without the variable the registry stays read-only, as upstream. The TB710FU sensor hub writes a probe file and parsed_file_list.csv there and loads no sensors when that fails.
