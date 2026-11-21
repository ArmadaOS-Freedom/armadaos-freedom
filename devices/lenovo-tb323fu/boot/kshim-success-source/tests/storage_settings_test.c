/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_settings.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct disk { uint8_t head[12288],tail[65536];unsigned writes;int fail; };
static void p32(uint8_t *p,uint32_t n){for(unsigned i=0;i<4;++i)p[i]=n>>(8*i);}
static void p64(uint8_t *p,uint64_t n){p32(p,n);p32(p+4,n>>32);}
static struct disk internal,tf;
static const uint64_t start=16,blocks=524288,total=524320;
static int read_disk(void *ctx,uint64_t b,uint32_t n,void *out){
    struct disk *d=ctx;
    if(b+n<=3){memcpy(out,d->head+b*4096,n*4096);return 0;}
    uint64_t tail=start+blocks-16;
    if(b<tail||n>tail+16-b)return -1;
    memcpy(out,d->tail+(b-tail)*4096,n*4096);return 0;
}
static int write_disk(void *ctx,uint64_t b,uint32_t n,const void *in){
    struct disk *d=ctx;uint64_t tail=start+blocks-16;
    assert((b==tail+1||b==tail+2)&&n==1);++d->writes;
    if(d->fail)return -1;
    memcpy(d->tail+(b-tail)*4096,in,4096);return 0;
}
static int flush(void *ctx){return ((struct disk*)ctx)->fail?-1:0;}
static struct kshim_blockdev device(struct disk *d)
{return (struct kshim_blockdev){d,4096,total,read_disk,write_disk,flush};}
static void provision(struct disk *d){
    memset(d,0,sizeof(*d));uint8_t *h=d->head+4096,*e=d->head+8192,*c=d->tail;
    memcpy(h,"EFI PART",8);p32(h+8,0x10000);p32(h+12,92);p64(h+24,1);
    p64(h+32,total-1);p64(h+40,3);p64(h+48,total-3);h[56]=1;
    p64(h+72,2);p32(h+80,1);p32(h+84,128);
    e[0]=1;e[16]=1;p64(e+32,start);p64(e+40,start+blocks-1);
    const char name[]="ARMADA_BACKUP";
    for(unsigned i=0;i<sizeof(name)-1;++i)e[56+2*i]=(uint8_t)name[i];
    p32(h+88,kshim_crc32(e,128));p32(h+16,kshim_crc32(h,92));
    memcpy(c,"KSCFGV3",8);p32(c+8,1);p32(c+12,4096);p64(c+16,2147483648ULL);
    p64(c+24,100000);p64(c+32,2147418112ULL);p32(c+40,65536);p32(c+44,4096);
    c[51]=1;c[64]=2;p32(c+80,4096);p32(c+84,8192);p32(c+88,1);
    p32(c+4092,kshim_crc32(c,4092));
}
struct transaction { unsigned begins,ends; int begin_rc,end_rc,active; };
static int begin_io(void *ctx){
    struct transaction *t=ctx;assert(!t->active);++t->begins;
    if(t->begin_rc)return t->begin_rc;
    t->active=1;return 0;
}
static int end_io(void *ctx){
    struct transaction *t=ctx;assert(t->active);++t->ends;t->active=0;
    return t->end_rc;
}
static void transaction_tests(void){
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal);
    struct kshim_config_values defaults={0,0,0, 0, 0},v={0,2,1,0, 0};
    struct transaction t={0};
    provision(&internal);
    assert(kshim_settings_open(&s,&i,NULL,1,&defaults)==KSHIM_STORAGE_EMPTY);
    s.io_context=&t;s.io_begin=begin_io;s.io_end=end_io;
    assert(kshim_settings_save(&s,&v)==0);
    assert(t.begins==1&&t.ends==1&&!t.active&&s.sequence==1);
    unsigned writes=internal.writes;
    t.begin_rc=-41;
    assert(kshim_settings_save(&s,&defaults)==-41);
    assert(t.begins==2&&t.ends==1&&internal.writes==writes&&s.sequence==1);
    t.begin_rc=0;internal.fail=1;
    assert(kshim_settings_save(&s,&defaults)!=0);
    assert(t.begins==3&&t.ends==2&&!t.active&&s.sequence==1&&s.save_uncertain);
    internal.fail=0;t.end_rc=-42;
    assert(kshim_settings_save(&s,&defaults)==-42);
    assert(t.begins==4&&t.ends==3&&!t.active&&s.sequence==1&&s.save_uncertain);
    assert(s.values.default_target==2&&s.values.language==1);
    /* Failed finish cannot publish success even though the new disk record
     * was written. A retry reads its generation before choosing the next. */
    t.end_rc=0;
    assert(kshim_settings_save(&s,&defaults)==0&&s.sequence==3&&!s.save_uncertain);
    assert(t.begins==5&&t.ends==4);
    writes=internal.writes;s.io_end=NULL;
    assert(kshim_settings_save(&s,&v)==KSHIM_STORAGE_ARG&&internal.writes==writes);
    assert(t.begins==5);s.io_begin=NULL;s.io_end=end_io;
    assert(kshim_settings_save(&s,&v)==KSHIM_STORAGE_ARG&&internal.writes==writes);
    assert(t.ends==4);
}
static void history_tests(void){
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal);struct kshim_config_values v={0,4,0,0, 0};
    provision(&internal);assert(kshim_settings_open(&s,&i,NULL,1,&v)==KSHIM_STORAGE_EMPTY);
    assert(kshim_settings_note_boot(&s,2)==0 && s.values.last_target==2 && s.sequence==1);
    unsigned writes=internal.writes;
    assert(kshim_settings_note_boot(&s,2)==0 && internal.writes==writes && s.sequence==1);
    assert(kshim_settings_note_boot(&s,1)==0 && s.values.last_target==1 && s.sequence==2);
    v=s.values;v.default_target=2;assert(kshim_settings_save(&s,&v)==0);
    assert(kshim_settings_note_boot(&s,3)==0 && s.values.default_target==2 && s.values.last_target==3);
    writes=internal.writes;
    assert(kshim_settings_note_boot(&s,0)==KSHIM_STORAGE_ARG && internal.writes==writes);
    assert(kshim_settings_note_boot(&s,4)==KSHIM_STORAGE_ARG && internal.writes==writes);
    internal.fail=1;
    assert(kshim_settings_note_boot(&s,1)!=0 && s.values.last_target==3 && s.save_uncertain);
    internal.fail=0;writes=internal.writes;
    assert(kshim_settings_note_boot(&s,3)==0 && internal.writes==writes+1 && !s.save_uncertain);
    memset(&s,0,sizeof(s));assert(kshim_settings_note_boot(&s,1)==KSHIM_STORAGE_NOT_FOUND);
}
static void one_shot_tests(void){
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal);struct kshim_config_values v={0,4,1,2,1};
    provision(&internal);assert(kshim_settings_open(&s,&i,NULL,1,&v)==KSHIM_STORAGE_EMPTY);
    assert(kshim_settings_save(&s,&v)==0);
    uint32_t action=99;internal.fail=1;
    assert(kshim_settings_take_action(&s,&action)!=0 && action==0 && s.values.next_action==1);
    internal.fail=0;
    assert(kshim_settings_take_action(&s,&action)==0 && action==1 && s.values.next_action==0);
    assert(s.values.default_target==4 && s.values.language==1 && s.values.last_target==2);
    unsigned writes=internal.writes;
    assert(kshim_settings_take_action(&s,&action)==0 && action==0 && internal.writes==writes);
    assert(kshim_settings_open(&s,&i,NULL,1,&v)==0 && s.values.next_action==0);
    /* Unsupported packed action (5) is rejected; no save, no publication. */
    s.values.next_action=5;
    assert(kshim_settings_take_action(&s,&action)==KSHIM_STORAGE_ARG && action==0 && internal.writes==writes);
}
static void v4_take_action_tests(void){
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal);
    /* One-shot clears action but preserves the no-boot-log flag and history. */
    struct kshim_config_values v={1,4,1,2, kshim_next_action_pack(KSHIM_ACTION_LINUX, KSHIM_FLAG_NO_BOOT_LOG)};
    provision(&internal);assert(kshim_settings_open(&s,&i,NULL,1,&(struct kshim_config_values){0})==KSHIM_STORAGE_EMPTY);
    assert(kshim_settings_save(&s,&v)==0);
    uint32_t action=99;
    assert(kshim_settings_take_action(&s,&action)==0 && action==KSHIM_ACTION_LINUX);
    assert(s.values.next_action==KSHIM_FLAG_NO_BOOT_LOG);
    assert(s.values.device_choice==1 && s.values.default_target==4 &&
           s.values.language==1 && s.values.last_target==2);
    /* Reload from disk matches: durable clear, flag preserved. */
    assert(kshim_settings_open(&s,&i,NULL,1,&v)==0);
    assert(kshim_next_action_value(s.values.next_action)==KSHIM_ACTION_NONE);
    assert(kshim_next_action_flags(s.values.next_action)==KSHIM_FLAG_NO_BOOT_LOG);
    /* Flag-only: no save, action stays 0, flag stays. */
    unsigned writes=internal.writes;
    assert(kshim_settings_take_action(&s,&action)==0 && action==0 && internal.writes==writes);
    /* Reboot must not dispatch after a failed clear. */
    s.values.next_action=kshim_next_action_pack(KSHIM_ACTION_EDL, KSHIM_FLAG_NO_BOOT_LOG);
    internal.fail=1;
    assert(kshim_settings_take_action(&s,&action)!=0 && action==0);
    internal.fail=0;
}
/* TEMP diagnostic auto-return candidate: action 4 consumed as diagnostic
 * publishes no action and latches next plain Linux while retaining flag. */
static void diag_auto_return_tests(uint32_t board_id){
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal);
    struct kshim_config_values v={1,4,1,2, kshim_next_action_pack(KSHIM_ACTION_LINUX_DIAG, KSHIM_FLAG_NO_BOOT_LOG)};
    provision(&internal);
    p32(internal.tail+88,board_id);
    p32(internal.tail+4092,kshim_crc32(internal.tail,4092));
    assert(kshim_settings_open(&s,&i,NULL,board_id,&(struct kshim_config_values){0})==KSHIM_STORAGE_EMPTY);
    assert(kshim_settings_save(&s,&v)==0);
    uint32_t action=99;internal.fail=1;
    assert(kshim_settings_take_action(&s,&action)!=0 && action==0);
    assert(kshim_next_action_value(s.values.next_action)==KSHIM_ACTION_LINUX_DIAG);
    assert(kshim_next_action_flags(s.values.next_action)==KSHIM_FLAG_NO_BOOT_LOG);
    internal.fail=0;
    assert(kshim_settings_take_action(&s,&action)==0 && action==KSHIM_ACTION_LINUX_DIAG);
    assert(s.values.device_choice==1 && s.values.default_target==4 &&
           s.values.language==1 && s.values.last_target==2);
    assert(kshim_next_action_value(s.values.next_action)==KSHIM_ACTION_LINUX);
    assert(kshim_next_action_flags(s.values.next_action)==KSHIM_FLAG_NO_BOOT_LOG);
    assert(kshim_settings_open(&s,&i,NULL,board_id,&v)==0);
    assert(kshim_next_action_value(s.values.next_action)==KSHIM_ACTION_LINUX);
    assert(kshim_next_action_flags(s.values.next_action)==KSHIM_FLAG_NO_BOOT_LOG);
    assert(s.values.default_target==4 && s.values.language==1 && s.values.device_choice==1);
    unsigned writes=internal.writes;
    assert(kshim_settings_take_action(&s,&action)==0 && action==KSHIM_ACTION_LINUX);
    assert(internal.writes==writes+1);
    assert(kshim_next_action_value(s.values.next_action)==KSHIM_ACTION_NONE);
    assert(kshim_next_action_flags(s.values.next_action)==KSHIM_FLAG_NO_BOOT_LOG);
}
int main(void){
    one_shot_tests();
    v4_take_action_tests();
    diag_auto_return_tests(1);
    diag_auto_return_tests(2);
    history_tests();
    static struct kshim_settings_session s;
    struct kshim_blockdev i=device(&internal),t=device(&tf);
    struct kshim_config_values defaults={0,0,0, 0, 0},a={1,2,1,0, 0},b={2,3,0,0, 0};
    provision(&internal);provision(&tf);
    assert(kshim_settings_open(&s,&i,&t,1,&defaults)==KSHIM_STORAGE_EMPTY);
    assert(s.source==KSHIM_SETTINGS_INTERNAL&&internal.writes==0&&tf.writes==0);
    assert(kshim_settings_save(&s,&a)==0&&s.sequence==1&&internal.writes==1&&tf.writes==0);
    /* Valid internal wins over a newer TF generation; no cross-media merge. */
    assert(kshim_settings_open(&s,NULL,&t,1,&defaults)==KSHIM_STORAGE_EMPTY);
    assert(kshim_settings_save(&s,&b)==0);assert(kshim_settings_save(&s,&b)==0);
    assert(kshim_settings_open(&s,&i,&t,1,&defaults)==0);
    assert(s.source==KSHIM_SETTINGS_INTERNAL&&s.values.default_target==2&&s.sequence==1);
    unsigned tf_writes=tf.writes;internal.fail=1;
    assert(kshim_settings_save(&s,&b)!=0&&tf.writes==tf_writes&&s.sequence==1);
    assert(s.values.default_target==2);internal.fail=0;
    /* Missing/v2 descriptor must not be provisioned implicitly. */
    memset(internal.tail,0,65536);
    assert(kshim_settings_open(&s,&i,&t,1,&defaults)==0);
    assert(s.source==KSHIM_SETTINGS_TF&&s.values.default_target==3);
    assert(s.medium[0].status==KSHIM_STORAGE_FORMAT);
    assert(kshim_settings_save(&s,&a)==0&&internal.writes==2);
    memset(tf.tail,0,65536);
    assert(kshim_settings_open(&s,&i,&t,1,&defaults)==KSHIM_STORAGE_NOT_FOUND);
    assert(s.source==KSHIM_SETTINGS_NONE);
    assert(kshim_settings_save(&s,&a)==KSHIM_STORAGE_NOT_FOUND);
    provision(&internal);
    assert(kshim_settings_open(&s,&i,NULL,2,&defaults)==KSHIM_STORAGE_NOT_FOUND);
    assert(s.medium[0].status==KSHIM_STORAGE_FORMAT); /* wrong actual board */
    transaction_tests();
    puts("Settings internal-first/TF fallback/v2 refusal/transaction ownership: PASS");
}
