/* SPDX-License-Identifier: GPL-3.0-only */
#if !defined(KSHIM_HOST_TEST)
#include <config.h>
#endif
#include <storage_settings_export.h>
#include <libfdt.h>
int storage_settings_export(void *fdt,const struct kshim_ufs_settings *b,
                             uint32_t test_performed,int test_status)
{
    if (!fdt || !b) return -FDT_ERR_BADVALUE;
    int rc=fdt_check_header(fdt);
    if (rc) return rc;
    int node=fdt_path_offset(fdt,"/chosen");
    if (node<0) return node;
    const uint32_t values[16]={1,b->board,(uint32_t)b->load_status,
        (uint32_t)b->begin_status,(uint32_t)b->release_status,b->settings.source,
        (uint32_t)(b->settings.sequence>>32),(uint32_t)b->settings.sequence,
        b->settings.values.device_choice,b->settings.values.default_target,
        b->settings.values.language,b->save_attempts,b->settings.save_uncertain,
        !b->unsafe&&!b->host.owned,test_performed,(uint32_t)test_status};
    fdt32_t wire[16];
    for (unsigned i=0;i<16;++i) wire[i]=cpu_to_fdt32(values[i]);
    rc=fdt_setprop(fdt,node,KSHIM_SETTINGS_FDT_PROPERTY,wire,sizeof(wire));
    if (rc) return rc;
    const fdt32_t history[2]={cpu_to_fdt32(1),cpu_to_fdt32(b->settings.values.last_target)};
    rc=fdt_setprop(fdt,node,"kshim,last-boot-v1",history,sizeof(history));
    if (rc) return rc;
    rc=fdt_setprop_u32(fdt,node,"kshim,next-action",kshim_next_action_value(b->settings.values.next_action));
    if (rc) return rc;
#if defined(CONFIG_KSHIM_MENU_SESSION_SETTINGS) && CONFIG_KSHIM_MENU_SESSION_SETTINGS
    if (b->settings.source==KSHIM_SETTINGS_INTERNAL &&
        (b->board==1||b->board==2) &&
        b->settings.medium[0].config.ready) {
        rc=fdt_setprop_u32(fdt,node,"kshim,launcher-settings-version",4);
        if (rc) return rc;
    }
#endif
    return 0;
}
