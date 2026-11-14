/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_settings_native.h>
#include <storage_settings_export.h>
#include <storage_probe_io.h>
#include <storage_ufs_dma.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
static unsigned opens,counters,maps,captures,proofs,saves;
static uint64_t frequency=19200000;
static int mapping_rc,identity=1,load_result=KSHIM_STORAGE_EMPTY,save_result;
static struct kshim_ufs_settings *last;
uint64_t arm64_read_cntfrq(void){++counters;return frequency;}
uint64_t get_current_time(void){return 10;}
uint64_t delay(uint64_t us){return us;}
int storage_ufs_native_read32(void *c,uint32_t o,uint32_t *v){(void)c;(void)o;(void)v;assert(0);return -1;}
int storage_ufs_native_write32(void *c,uint32_t o,uint32_t v){(void)c;(void)o;(void)v;assert(0);return -1;}
void storage_ufs_native_sync_device(void *c,void *b,size_t n){(void)c;(void)b;(void)n;assert(0);}
void storage_ufs_native_sync_cpu(void *c,void *b,size_t n){(void)c;(void)b;(void)n;assert(0);}
int storage_ufs_native_dma_address(void *arena,size_t size,uint64_t *dma){
    assert(((uintptr_t)arena&4095)==0&&size==8192);++maps;*dma=0x10000000;return mapping_rc;
}
void storage_probe_native_capture(enum device_board board,struct storage_probe_snapshot *out){
    assert(board==DEVICE_BOARD_TB322FC||board==DEVICE_BOARD_TB323FU);
    ++captures;memset(out,0,sizeof(*out));out->board=board;
}
bool kshim_ufs_dma_is_identity(const struct storage_probe_snapshot *s,uint64_t dma,size_t size){
    assert(s->board==last->board&&dma==0x10000000&&size==8192);++proofs;return identity;
}
bool kshim_ufs_settings_handoff_safe(const struct kshim_ufs_settings *b){return !b->unsafe&&!b->host.owned;}
int kshim_ufs_settings_open(struct kshim_ufs_settings *b,const struct kshim_ufs_ops *ops,
    void *arena,uint32_t board,int (*preflight)(void *,uint64_t *),void *ctx,
    const struct kshim_config_values *defaults){
    ++opens;last=b;assert(ctx==b&&arena&&ops->manage_link&&defaults);
    b->board=board;b->preflight=preflight;uint64_t dma;int rc=preflight(ctx,&dma);
    b->load_status=rc?rc:load_result;return b->load_status;
}
int kshim_settings_save(struct kshim_settings_session *s,const struct kshim_config_values *v){
    assert(s==&last->settings&&v->device_choice==0&&v->default_target==2&&v->language==0);
    ++saves;return save_result;
}
int storage_settings_export(void *fdt,const struct kshim_ufs_settings *b,uint32_t performed,int status){
    assert(fdt==&opens&&b==last&&performed==1&&status==save_result);return 123;
}
/* A selected payload is recorded after platform_select has disabled the MMU.
 * Native DMA must see it enabled during the transaction, then restore it. */
static int mmu_on=1,enable_rc,close_rc,note_rc,note_unsafe;
static unsigned enables,closes,notes;
static uint64_t root_table=0x4000;
static unsigned ab_calls;
static int ab_rc,ab_unsafe;
int kshim_ufs_settings_ab(struct kshim_ufs_settings *b,struct kshim_ab_status *status,
                         bool change,unsigned target,unsigned current){
    assert(b==last&&status&&mmu_on);
    assert(!change || (target==1&&current==0));
    ++ab_calls;if(ab_unsafe)b->unsafe=1;return ab_rc;
}
static unsigned mark_calls,mark_current;
static int mark_rc,mark_unsafe;
int kshim_ufs_settings_ab_mark_success(struct kshim_ufs_settings *b,unsigned current){
    assert(b==last&&mmu_on&&current<=1);++mark_calls;mark_current=current;
    if(mark_unsafe)
        b->unsafe=1;
    return mark_rc;
}
static unsigned trial_loads,trial_saves;
static int trial_load_rc,trial_save_rc,trial_load_unsafe,trial_save_unsafe;
static struct kshim_update_trial trial_value;
static unsigned trial_selected,trial_written;
int kshim_ufs_settings_trial_load(struct kshim_ufs_settings *b,
    struct kshim_update_trial *record,unsigned *selected){
    assert(b==last&&record&&selected&&mmu_on);++trial_loads;
    *record=trial_value;*selected=trial_selected;
    if(trial_load_unsafe)b->unsafe=1;
    return trial_load_rc;
}
int kshim_ufs_settings_trial_save(struct kshim_ufs_settings *b,
    uint64_t expected,const struct kshim_update_trial *next,unsigned *written){
    assert(b==last&&expected==55&&next&&written&&mmu_on);++trial_saves;
    *written=trial_written;if(trial_save_unsafe)b->unsafe=1;
    return trial_save_rc;
}
int kshim_mmu_is_enabled(void){return mmu_on;}
uint64_t kshim_mmu_root_table(void){return root_table;}
int kshim_mmu_enable_current_cpu(void){++enables;if(!enable_rc)mmu_on=1;return enable_rc;}
int kshim_mmu_prepare_handoff(void){++closes;if(!close_rc)mmu_on=0;return close_rc;}
int kshim_settings_note_boot(struct kshim_settings_session *s,uint32_t target){
    assert(s==&last->settings&&mmu_on==1);++notes;
    if(note_unsafe)last->unsafe=1;
    if(!note_rc)s->values.last_target=target;
    return note_rc;
}
static void late_history(void){
    last->settings.source=KSHIM_SETTINGS_INTERNAL;last->settings.values.last_target=0;
    mmu_on=0;
    assert(storage_settings_native_note_boot(2)==0);
    assert(notes==1&&enables==1&&closes==1&&!mmu_on&&last->settings.values.last_target==2);
    assert(storage_settings_native_note_boot(2)==0&&notes==1&&enables==1&&closes==1);
    last->settings.save_uncertain=1;
    assert(storage_settings_native_note_boot(2)==0&&notes==2&&enables==2&&closes==2);
    last->settings.save_uncertain=0;mmu_on=1;
    assert(storage_settings_native_note_boot(1)==0&&notes==3&&enables==2&&closes==2&&mmu_on);
    mmu_on=0;enable_rc=-1;
    assert(storage_settings_native_note_boot(2)!=0&&notes==3&&enables==3&&closes==2&&!mmu_on);
    enable_rc=0;root_table=0;
    assert(storage_settings_native_note_boot(2)!=0&&notes==3&&enables==3);
    root_table=0x4000;note_rc=-77;
    assert(storage_settings_native_note_boot(2)==-77&&notes==4&&closes==3&&!mmu_on);
    assert(last->settings.values.last_target==1);
    assert(storage_settings_native_note_boot(0)==KSHIM_STORAGE_ARG&&notes==4&&enables==4);
    last->settings.source=KSHIM_SETTINGS_NONE;
    assert(storage_settings_native_note_boot(2)==KSHIM_STORAGE_NOT_FOUND&&notes==4&&enables==4);
    last->settings.source=KSHIM_SETTINGS_INTERNAL;note_unsafe=1;
    assert(storage_settings_native_note_boot(2)!=0&&notes==5&&closes==3&&mmu_on);
    assert(!storage_settings_native_handoff_safe());
    assert(storage_settings_native_note_boot(2)!=0&&notes==5);
    last->unsafe=0;note_unsafe=0;mmu_on=0;close_rc=-1;note_rc=0;
    assert(storage_settings_native_note_boot(2)!=0&&notes==6&&closes==4);
    assert(!storage_settings_native_handoff_safe());
}
static void serial_ab(void){
    struct kshim_ab_status status;
    last->unsafe=0;close_rc=0;enables=closes=0;mmu_on=0;
    assert(storage_settings_native_ab_status(&status)==0);
    assert(ab_calls==1&&enables==1&&closes==1&&!mmu_on);
    assert(storage_settings_native_ab_select(1,0)==0);
    assert(ab_calls==2&&enables==2&&closes==2&&!mmu_on);
    mmu_on=1;
    assert(storage_settings_native_ab_status(&status)==0);
    assert(ab_calls==3&&enables==2&&closes==2&&mmu_on);
    mmu_on=0;root_table=0;
    assert(storage_settings_native_ab_select(1,0)==KSHIM_UFS_UNSUPPORTED&&ab_calls==3);
    root_table=0x4000;enable_rc=-1;
    assert(storage_settings_native_ab_status(&status)==KSHIM_UFS_UNSUPPORTED&&ab_calls==3&&!mmu_on);
    enable_rc=0;ab_rc=-77;
    assert(storage_settings_native_ab_select(1,0)==-77&&ab_calls==4&&closes==3&&!mmu_on);
    ab_rc=0;ab_unsafe=1;
    assert(storage_settings_native_ab_select(1,0)==KSHIM_UFS_QUARANTINED&&ab_calls==5&&closes==3&&mmu_on);
    assert(!storage_settings_native_handoff_safe());
    assert(storage_settings_native_ab_status(&status)!=0&&ab_calls==5);
    last->unsafe=0;ab_unsafe=0;mmu_on=0;close_rc=-1;
    assert(storage_settings_native_ab_status(&status)==KSHIM_UFS_QUARANTINED&&ab_calls==6&&closes==4);
    assert(!storage_settings_native_handoff_safe());
}
static void native_success(void){
    last->unsafe=0;mmu_on=0;root_table=0x4000;enable_rc=close_rc=0;
    mark_rc=mark_unsafe=0;mark_calls=mark_current=0;enables=closes=0;
    assert(storage_settings_native_ab_mark_success(0)==0);
    assert(mark_calls==1&&mark_current==0&&enables==1&&closes==1&&!mmu_on);
    assert(storage_settings_native_ab_mark_success(1)==0);
    assert(mark_calls==2&&mark_current==1&&enables==2&&closes==2&&!mmu_on);
    mmu_on=1;
    assert(storage_settings_native_ab_mark_success(0)==0);
    assert(mark_calls==3&&mark_current==0&&enables==2&&closes==2&&mmu_on);
    assert(storage_settings_native_ab_mark_success(2)==KSHIM_STORAGE_ARG);
    assert(mark_calls==3&&enables==2&&closes==2&&mmu_on);
    uint32_t board=last->board;last->board=DEVICE_BOARD_UNKNOWN;mmu_on=0;
    assert(storage_settings_native_ab_mark_success(0)==KSHIM_UFS_UNSUPPORTED);
    assert(mark_calls==3&&enables==2&&closes==2);
    last->board=board;root_table=0;
    assert(storage_settings_native_ab_mark_success(0)==KSHIM_UFS_UNSUPPORTED);
    assert(mark_calls==3&&enables==2&&closes==2);
    root_table=0x4000;enable_rc=-1;
    assert(storage_settings_native_ab_mark_success(0)==KSHIM_UFS_UNSUPPORTED);
    assert(mark_calls==3&&enables==3&&closes==2&&!mmu_on);
    enable_rc=0;mark_rc=-79;
    assert(storage_settings_native_ab_mark_success(1)==-79);
    assert(mark_calls==4&&mark_current==1&&enables==4&&closes==3&&!mmu_on);
    assert(storage_settings_native_handoff_safe());
    mark_rc=0;mark_unsafe=1;
    assert(storage_settings_native_ab_mark_success(0)==KSHIM_UFS_QUARANTINED);
    assert(mark_calls==5&&mark_current==0&&enables==5&&closes==3&&mmu_on);
    assert(!storage_settings_native_handoff_safe());
    assert(storage_settings_native_ab_mark_success(0)==KSHIM_UFS_UNSUPPORTED);
    assert(mark_calls==5&&enables==5&&closes==3);
    last->unsafe=0;mark_unsafe=0;mmu_on=0;close_rc=-1;
    assert(storage_settings_native_ab_mark_success(1)==KSHIM_UFS_QUARANTINED);
    assert(mark_calls==6&&mark_current==1&&enables==6&&closes==4&&mmu_on);
    assert(!storage_settings_native_handoff_safe());
    last->unsafe=0;close_rc=0;mmu_on=0;root_table=0x4000;
}
static void trial_io(void){
    struct kshim_update_trial out,next; uint8_t old[sizeof(out)];
    unsigned selected=99,written=99;
    last->unsafe=0;mmu_on=0;root_table=0x4000;enable_rc=close_rc=0;
    trial_load_rc=trial_save_rc=trial_load_unsafe=trial_save_unsafe=0;
    trial_loads=trial_saves=enables=closes=0;trial_selected=trial_written=1;
    memset(&trial_value,0xa5,sizeof(trial_value));memset(&next,0x5a,sizeof(next));
    memset(&out,0x33,sizeof(out));memcpy(old,&out,sizeof(out));
    assert(storage_settings_native_trial_load(&out,&selected)==0);
    assert(out.sequence==trial_value.sequence&&out.state==trial_value.state&&out.remaining==trial_value.remaining&&selected==1);
    assert(trial_loads==1&&enables==1&&closes==1&&!mmu_on);
    assert(storage_settings_native_trial_save(55,&next,&written)==0);
    assert(written==1&&trial_saves==1&&enables==2&&closes==2&&!mmu_on);
    mmu_on=1;selected=written=0;
    assert(storage_settings_native_trial_load(&out,&selected)==0&&selected==1);
    assert(storage_settings_native_trial_save(55,&next,&written)==0&&written==1);
    assert(trial_loads==2&&trial_saves==2&&enables==2&&closes==2&&mmu_on);
    mmu_on=0;memcpy(&out,old,sizeof(out));selected=written=77;trial_load_rc=trial_save_rc=-77;
    assert(storage_settings_native_trial_load(&out,&selected)==-77);
    assert(!memcmp(&out,&old,sizeof(out))&&selected==77&&closes==3);
    assert(storage_settings_native_trial_save(55,&next,&written)==-77);
    assert(written==77&&closes==4);
    root_table=0;
    assert(storage_settings_native_trial_load(&out,&selected)==KSHIM_UFS_UNSUPPORTED);
    assert(storage_settings_native_trial_save(55,&next,&written)==KSHIM_UFS_UNSUPPORTED);
    assert(trial_loads==3&&trial_saves==3&&enables==4&&closes==4);
    root_table=0x4000;enable_rc=-1;
    assert(storage_settings_native_trial_load(&out,&selected)==KSHIM_UFS_UNSUPPORTED);
    assert(storage_settings_native_trial_save(55,&next,&written)==KSHIM_UFS_UNSUPPORTED);
    assert(trial_loads==3&&trial_saves==3&&enables==6&&closes==4);
    enable_rc=0;trial_load_rc=trial_save_rc=0;trial_load_unsafe=1;
    assert(storage_settings_native_trial_load(&out,&selected)==KSHIM_UFS_QUARANTINED);
    assert(!memcmp(&out,&old,sizeof(out))&&selected==77&&closes==4&&mmu_on);
    last->unsafe=0;trial_load_unsafe=0;mmu_on=0;trial_save_unsafe=1;
    assert(storage_settings_native_trial_save(55,&next,&written)==KSHIM_UFS_QUARANTINED);
    assert(written==77&&closes==4&&mmu_on);
    last->unsafe=0;trial_save_unsafe=0;mmu_on=0;close_rc=-1;
    assert(storage_settings_native_trial_load(&out,&selected)==KSHIM_UFS_QUARANTINED);
    assert(!memcmp(&out,&old,sizeof(out))&&selected==77&&closes==5);
    last->unsafe=0;mmu_on=0;
    assert(storage_settings_native_trial_save(55,&next,&written)==KSHIM_UFS_QUARANTINED);
    assert(written==77&&closes==6);
    last->unsafe=0;close_rc=0;mmu_on=0;
    assert(storage_settings_native_trial_load(NULL,&selected)==KSHIM_STORAGE_ARG);
    assert(storage_settings_native_trial_save(55,NULL,&written)==KSHIM_STORAGE_ARG);
}
int main(void){
    const struct kshim_config_values defaults={0,0,1, 0, 0};
    assert(storage_settings_native_open(DEVICE_BOARD_UNKNOWN,&defaults)==KSHIM_UFS_UNSUPPORTED);
    assert(opens==0&&maps==0&&counters==0&&captures==0&&saves==0);
    frequency=0;assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==KSHIM_UFS_UNSUPPORTED);
    assert(counters==1&&maps==0&&saves==0);
    frequency=19200000;mapping_rc=-88;
    assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==-88&&maps==1&&captures==0);
    mapping_rc=0;identity=0;
    assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==KSHIM_UFS_UNSUPPORTED);
    assert(captures==1&&proofs==1&&saves==0);
    identity=1;assert(storage_settings_native_open(DEVICE_BOARD_TB323FU,&defaults)==KSHIM_STORAGE_EMPTY);
    assert(saves==0); /* the explicit seed test never writes on Gen5 */
    load_result=0;assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==0&&saves==0);
    load_result=KSHIM_STORAGE_NOT_FOUND;
    assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==KSHIM_STORAGE_NOT_FOUND&&saves==0);
    load_result=KSHIM_STORAGE_EMPTY;save_result=-89;
    assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==-89&&saves==1);
    assert(storage_settings_native_session()==&last->settings);
    assert(storage_settings_native_export(&opens)==123);
    late_history();
    serial_ab();
    native_success();
    trial_io();
    unsigned old=opens;last->unsafe=1;
    assert(!storage_settings_native_handoff_safe());
    assert(storage_settings_native_open(DEVICE_BOARD_TB322FC,&defaults)==KSHIM_UFS_QUARANTINED&&opens==old);
    puts("Native settings board/timer/DMA gates and Gen4-only write test: PASS");
}
