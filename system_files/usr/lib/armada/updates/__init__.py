"""Linux system update policy: one logical deployment, no Android slot control.

Every ordinary Linux update, explicit rollback, boot and confirmation treats
the running system as one logical deployment regardless of which physical
Android slot the bootloader picked:

* Both physical boot slots are rebuilt and verified separately, each from that
  slot's own authenticated material, into the same new deployment with one
  shared transaction UUID and one shared OSTree deployment binding.  Each slot
  keeps its own boot image hash.
* The inactive slot is written first, then the currently running slot, each
  with a full readback; a partial write never reports success.
* ``fromSlot``/``targetSlot`` in plans and trial records only index those
  hashes and the write order.  They are not a request to boot the other slot.
* No Linux path selects an Android slot, marks slot success, arms retry,
  priority or boot LUN state, or writes Android boot-control (BCB) data.
  Android keeps owning slot selection and all raw boot state.
* Confirmation accepts a boot observed on either physical slot as long as the
  running deployment and that slot's mirrored boot hash match the trial
  record.  It then commits the new deployment as the OSTree default and keeps
  the previous deployment retained as the rollback target.
* Explicit rollback repacks both slots from the retained previous OSTree
  deployment, without any slot selection.

Read-only observation of physical slots, per-slot materials and boot evidence
remains required and is never a demand to reboot into the other slot.
"""
