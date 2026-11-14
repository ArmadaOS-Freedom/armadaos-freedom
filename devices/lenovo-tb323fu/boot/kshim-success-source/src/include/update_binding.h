#ifndef KSHIM_UPDATE_BINDING_H
#define KSHIM_UPDATE_BINDING_H

#include <stddef.h>
#include <stdint.h>

struct kshim_update_binding {
    char os_name[65];
    uint8_t commit[32];
    uint32_t serial;
};

int kshim_update_binding_parse(const char *cmdline, size_t bytes, struct kshim_update_binding *out);

#endif
