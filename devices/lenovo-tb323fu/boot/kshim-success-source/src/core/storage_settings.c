/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_settings.h>
#include <string.h>
static int values_valid(const struct kshim_config_values *v)
{
    return v && v->device_choice <= 2 && v->default_target <= 4 &&
           v->language <= 1 && v->last_target <= 3 &&
           (v->next_action & ~(KSHIM_ACTION_MASK | KSHIM_FLAGS_KNOWN)) == 0 &&
           kshim_next_action_value(v->next_action) <= KSHIM_ACTION_MAX;
}
int kshim_settings_open(struct kshim_settings_session *s,
                        const struct kshim_blockdev *internal,
                        const struct kshim_blockdev *tf,uint32_t board,
                        const struct kshim_config_values *defaults)
{
    if(!s||!board||!values_valid(defaults))return KSHIM_STORAGE_ARG;
    memset(s,0,sizeof(*s));s->values=*defaults;
    const struct kshim_blockdev *devices[2]={internal,tf};
    struct kshim_config_values values[2];
    uint64_t sequence[2]={0,0};
    for(unsigned n=0;n<2;++n){
        struct kshim_settings_medium *m=&s->medium[n];
        m->status=KSHIM_STORAGE_NOT_FOUND;
        if(!devices[n])continue;
        struct kshim_partition partition;
        m->status=kshim_gpt_find_backup(devices[n],&partition,m->scratch,sizeof(m->scratch));
        if(m->status)continue;
        m->status=kshim_config_open(&m->config,devices[n],&partition,board,
                                    m->scratch,sizeof(m->scratch));
        if(m->status)continue;
        m->status=kshim_config_load(&m->config,&values[n],&sequence[n]);
    }
    /* Cross-media sequence numbers are unrelated installation histories. */
    for(unsigned n=0;n<2;++n){
        if(s->medium[n].status==KSHIM_STORAGE_OK){
            s->source=(enum kshim_settings_source)(n+1);
            s->values=values[n];s->sequence=sequence[n];
            return KSHIM_STORAGE_OK;
        }
    }
    for(unsigned n=0;n<2;++n){
        if(s->medium[n].status==KSHIM_STORAGE_EMPTY){
            s->source=(enum kshim_settings_source)(n+1);
            return KSHIM_STORAGE_EMPTY;
        }
    }
    return KSHIM_STORAGE_NOT_FOUND;
}
int kshim_settings_save(struct kshim_settings_session *s,
                        const struct kshim_config_values *values)
{
    if(!s||!values_valid(values))return KSHIM_STORAGE_ARG;
    if(s->source!=KSHIM_SETTINGS_INTERNAL&&s->source!=KSHIM_SETTINGS_TF)
        return KSHIM_STORAGE_NOT_FOUND;
    if (!!s->io_begin != !!s->io_end) return KSHIM_STORAGE_ARG;
    if (s->io_begin) {
        int rc = s->io_begin(s->io_context);
        if (rc) return rc; /* begin owns cleanup of a partial claim */
    }
    struct kshim_settings_medium *m=&s->medium[(unsigned)s->source-1];
    uint64_t sequence=s->sequence;
    int rc=kshim_config_save(&m->config,values,&sequence);
    if (s->io_end) {
        int finish = s->io_end(s->io_context);
        if (!rc) rc = finish;
    }
    if(rc){s->save_uncertain=1;return rc;}
    s->save_uncertain=0;
    s->values=*values;s->sequence=sequence;m->status=KSHIM_STORAGE_OK;
    return 0;
}
int kshim_settings_note_boot(struct kshim_settings_session *s,uint32_t target)
{
    if (!s || target<1 || target>3) return KSHIM_STORAGE_ARG;
    if (s->source!=KSHIM_SETTINGS_INTERNAL && s->source!=KSHIM_SETTINGS_TF)
        return KSHIM_STORAGE_NOT_FOUND;
    if (s->values.last_target==target && !s->save_uncertain) return 0;
    struct kshim_config_values next=s->values;
    next.last_target=target;
    return kshim_settings_save(s,&next);
}
/* Return the action only after its removal is durable. Reboot callers must
 * not dispatch a request after a failed clear. Normal settings/history and
 * the no-boot-log flag stay in place across the clear. */
int kshim_settings_take_action(struct kshim_settings_session *s,uint32_t *action)
{
    if (!s || !action) return KSHIM_STORAGE_ARG;
    *action=0;
    uint32_t packed=s->values.next_action;
    uint32_t requested=kshim_next_action_value(packed);
    if (!values_valid(&s->values)) return KSHIM_STORAGE_ARG;
    if (!requested) return 0;   /* flag-only or none: nothing to consume */
    /* Unlocked TB322FC internal medium: convert the diagnostic request to a
     * plain Linux boot in the same verified transaction.  Other actions and
     * other media/boards keep the existing cleared-to-NONE behavior. */
    uint32_t replacement=(requested==KSHIM_ACTION_LINUX_DIAG &&
                          s->source==KSHIM_SETTINGS_INTERNAL &&
                          s->medium[0].config.board_id==1) ?
                          KSHIM_ACTION_LINUX : KSHIM_ACTION_NONE;
    struct kshim_config_values next=s->values;
    next.next_action=kshim_next_action_pack(replacement,kshim_next_action_flags(packed));
    int rc=kshim_settings_save(s,&next);
    if (!rc) *action=requested;
    return rc;
}
