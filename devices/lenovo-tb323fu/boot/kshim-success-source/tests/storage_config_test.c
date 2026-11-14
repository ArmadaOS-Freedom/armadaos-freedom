/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
/* A sparse memory disk backs the complete 64 KiB config region plus a
 * preceding guard block. Access outside the backed extent is an IO failure. */
static unsigned char media[69632], scratch[8192], old_slot[4096], before[69632];
static struct kshim_blockdev dev;
static struct kshim_partition part;
static struct kshim_config_store store;
static uint64_t base_lba;
static unsigned reads,writes,flushes,fail_read_call;
static int partial_bytes,fail_write,fail_flush,corrupt_readback;
static uint64_t write_lba;
static uint32_t write_count;
static const unsigned char partition_uuid[16]={0xb0,0xe3,0x4a,0x51,0x64,0x12,0x45,0xec,0xb0,0x45,0xbe,0xf3,0x66,0x4d,0x14,0xd3};
static const unsigned char install_uuid[16]={0xda,0xb9,0x8c,0x77,0xcc,0x2d,0x48,0xe1,0x9f,0x93,0x69,0xdb,0x40,0x8d,0x46,0x1e};
static void le32(unsigned char *p,uint32_t n) { for(unsigned i=0;i<4;i++)p[i]=(unsigned char)(n>>(8*i)); }
static void le64(unsigned char *p,uint64_t n) { for(unsigned i=0;i<8;i++)p[i]=(unsigned char)(n>>(8*i)); }
static uint32_t fixture_crc(const unsigned char *p,size_t n) {
    uint32_t crc=~0u; while(n--) { unsigned char b=*p++; for(unsigned i=0;i<8;i++) {
        unsigned bit=((crc>>31)^(b>>i))&1u; crc<<=1; if(bit)crc^=0x04c11db7u;
    }} uint32_t out=0; for(unsigned i=0;i<32;i++)out|=((crc>>i)&1u)<<(31-i); return ~out;
}
static void seal(unsigned char *p) { le32(p+4092,fixture_crc(p,4092)); }
static unsigned char *descriptor(void) { return media+4096; }
static unsigned char *slot(unsigned n) { return media+8192+n*4096; }
static int rd(void *ctx,uint64_t lba,uint32_t n,void *p) {
    (void)ctx; reads++; if(reads==fail_read_call)return -1;
    if(lba<base_lba || lba-base_lba>sizeof(media)/dev.block_size || n>sizeof(media)/dev.block_size-(lba-base_lba))return -1;
    memcpy(p,media+(lba-base_lba)*dev.block_size,(size_t)n*dev.block_size);
    if(corrupt_readback && writes)((unsigned char *)p)[100]^=1;
    return 0;
}
static int wr(void *ctx,uint64_t lba,uint32_t n,const void *p) {
    (void)ctx; writes++; write_lba=lba; write_count=n;
    assert(lba>=base_lba && (lba-base_lba)*dev.block_size+(uint64_t)n*dev.block_size<=sizeof(media));
    size_t bytes=(size_t)n*dev.block_size;
    if(partial_bytes>=0 && bytes>(size_t)partial_bytes)bytes=(size_t)partial_bytes;
    memcpy(media+(lba-base_lba)*dev.block_size,p,bytes);
    return fail_write ? -1:0;
}
static int fl(void *ctx) { (void)ctx; flushes++; return fail_flush ? -1:0; }
static void fixture(unsigned bs) {
    memset(media,0,sizeof(media)); memset(media,0xa5,4096);
    dev=(struct kshim_blockdev){0,bs,0,rd,wr,fl};
    part.first_lba=128; part.last_lba=part.first_lba+KSHIM_CONFIG_PARTITION_BYTES/bs-1;
    memcpy(part.uuid,partition_uuid,16); dev.block_count=part.last_lba+129;
    base_lba=part.last_lba+1-sizeof(media)/bs;
    unsigned char *d=descriptor(); memcpy(d,"KSCFGV3",8); le32(d+8,1); le32(d+12,4096);
    le64(d+16,UINT64_C(2147483648)); le64(d+24,UINT64_C(82127306));
    le64(d+32,UINT64_C(2147418112)); le32(d+40,65536); le32(d+44,4096);
    memcpy(d+48,partition_uuid,16); memcpy(d+64,install_uuid,16);
    le32(d+80,4096); le32(d+84,8192); le32(d+88,2); seal(d);
    reads=writes=flushes=fail_read_call=0; partial_bytes=-1;
    fail_write=fail_flush=corrupt_readback=0; write_lba=0; write_count=0;
    memset(&store,0,sizeof(store));
}
static int open_store(void) { return kshim_config_open(&store,&dev,&part,2,scratch,sizeof(scratch)); }
static void make_slot(unsigned n,uint64_t seq,uint32_t device,uint32_t target,uint32_t language) {
    unsigned char *s=slot(n); memset(s,0,4096); memcpy(s,"KSCFGR1",8);
    le32(s+8,1); le32(s+12,4096); le64(s+16,seq); memcpy(s+24,partition_uuid,16); memcpy(s+40,install_uuid,16);
    le32(s+56,device); le32(s+60,target); le32(s+64,language); seal(s);
}
static void assert_only_slot_changed(unsigned n) {
    size_t begin=8192+n*4096;
    assert(memcmp(before,media,begin)==0);
    assert(memcmp(before+begin+4096,media+begin+4096,sizeof(media)-begin-4096)==0);
    assert(write_lba==base_lba+begin/dev.block_size && write_count==4096/dev.block_size && writes==1);
}
static void test_descriptor(unsigned bs) {
    /* Permissive identity/geometry validation would enable writes to wrong media. */
    fixture(bs); assert(open_store()==0);
    fixture(bs); memset(descriptor(),0,4096); assert(open_store()!=0 && writes==0);
    fixture(bs); descriptor()[4095]^=1; assert(open_store()==KSHIM_STORAGE_CRC);
    const unsigned offsets[]={8,12,16,24,32,40,44,48,80,84,88,92};
    for(unsigned i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++) {
        fixture(bs); descriptor()[offsets[i]]^=0x80; seal(descriptor());
        /* An arbitrary payload end is valid if still bounded; use explicit overflow. */
        if(offsets[i]==24){le64(descriptor()+24,UINT64_MAX);seal(descriptor());}
        assert(open_store()!=0 && writes==0);
    }
    fixture(bs); memset(descriptor()+64,0,16); seal(descriptor()); assert(open_store()!=0);
    fixture(bs); media[sizeof(media)-1]=1; assert(open_store()==KSHIM_STORAGE_FORMAT && writes==0);
    fixture(bs); part.last_lba--; assert(open_store()==KSHIM_STORAGE_RANGE);
    fixture(bs); assert(kshim_config_open(&store,&dev,&part,1,scratch,sizeof(scratch))!=0);
    fixture(bs); fail_read_call=1; assert(open_store()==KSHIM_STORAGE_IO);
    fixture(bs); assert(kshim_config_open(&store,&dev,&part,2,scratch,8191)==KSHIM_STORAGE_ARG);
    fixture(bs); dev.write=0;dev.flush=0;assert(open_store()==0);
    struct kshim_config_values v={1,2,1, 0, 0};uint64_t seq=99;
    assert(kshim_config_load(&store,&v,&seq)==KSHIM_STORAGE_EMPTY && seq==99);
    assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_READ_ONLY && writes==0);
}
static void test_descriptor_versions(unsigned bs) {
    fixture(bs);assert(open_store()==0 && store.descriptor_version==1);

    fixture(bs);le32(descriptor()+8,2);seal(descriptor());
    memset(descriptor()+12288,0xa5,16384);
    assert(open_store()==0 && store.descriptor_version==2);
    descriptor()[28672]=1;assert(open_store()==KSHIM_STORAGE_FORMAT && writes==0);

    fixture(bs);le32(descriptor()+8,3);le32(descriptor()+92,28672);
    le32(descriptor()+96,32768);le32(descriptor()+100,4096);seal(descriptor());
    memset(descriptor()+28672,0x5a,8192);descriptor()[32768]=0xc3;
    assert(open_store()==0 && store.descriptor_version==3);
    descriptor()[36864]=1;assert(open_store()==KSHIM_STORAGE_FORMAT && writes==0);

    const uint32_t wrong[]={28673,32769,8192};
    for(unsigned i=0;i<4;i++) {
        fixture(bs);le32(descriptor()+8,3);le32(descriptor()+92,28672);
        le32(descriptor()+96,32768);le32(descriptor()+100,4096);
        if(i<3)le32(descriptor()+92+4*i,wrong[i]);else descriptor()[104]=1;
        seal(descriptor());assert(open_store()==KSHIM_STORAGE_FORMAT && writes==0);
    }
    fixture(bs);le32(descriptor()+8,4);seal(descriptor());
    assert(open_store()==KSHIM_STORAGE_FORMAT && writes==0);

    fixture(bs);le32(descriptor()+8,3);le32(descriptor()+92,28672);
    le32(descriptor()+96,32768);le32(descriptor()+100,4096);seal(descriptor());
    memset(descriptor()+12288,0xa5,16384);memset(descriptor()+28672,0x3c,8192);
    descriptor()[28672]=0xf0;descriptor()[32768]=0x0f;
    assert(open_store()==0 && store.descriptor_version==3);
    memcpy(before,media,sizeof(media));writes=0;
    struct kshim_config_values v={2,3,1,2,1};uint64_t seq=99;
    assert(kshim_config_save(&store,&v,&seq)==0 && seq==1);
    assert_only_slot_changed(0);
}
static void test_selection(unsigned bs) {
    /* First-valid selection, bad-CRC acceptance and equal-sequence ambiguity fail. */
    struct kshim_config_values v={9,9,9, 0, 0};uint64_t seq=999;
    fixture(bs);make_slot(0,7,1,2,0);make_slot(1,8,2,3,1);assert(open_store()==0);
    assert(kshim_config_load(&store,&v,&seq)==0 && seq==8 && v.device_choice==2 && v.default_target==3 && v.language==1);
    slot(1)[4095]^=1;assert(kshim_config_load(&store,&v,&seq)==0 && seq==7);
    make_slot(1,7,2,3,1);assert(kshim_config_load(&store,&v,&seq)==KSHIM_STORAGE_CONFLICT);
    memcpy(slot(1),slot(0),4096);assert(kshim_config_load(&store,&v,&seq)==0 && seq==7);
    const unsigned invalid[]={8,12,16,24,40,56,60,64,68};
    for(unsigned i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++) {
        fixture(bs);make_slot(0,7,1,2,0);make_slot(1,8,2,3,1);
        le32(slot(1)+invalid[i],0xffffffffu); if(invalid[i]==16)le64(slot(1)+16,0);seal(slot(1));
        assert(open_store()==0);assert(kshim_config_load(&store,&v,&seq)==0 && seq==7);
    }
    fixture(bs);slot(0)[0]=1;assert(open_store()==0);assert(kshim_config_load(&store,&v,&seq)==KSHIM_STORAGE_FORMAT);
    fixture(bs);make_slot(0,7,0,0,0);assert(open_store()==0);fail_read_call=reads+2;
    assert(kshim_config_load(&store,&v,&seq)==KSHIM_STORAGE_IO);
}
static void test_saving(unsigned bs) {
    struct kshim_config_values v={2,3,1, 0, 0},got;uint64_t seq=99;
    fixture(bs);assert(open_store()==0);memcpy(before,media,sizeof(media));
    assert(kshim_config_save(&store,&v,&seq)==0 && seq==1 && flushes==1);assert_only_slot_changed(0);
    assert(kshim_config_load(&store,&got,&seq)==0 && seq==1 && got.device_choice==2 && got.default_target==3 && got.language==1);
    memcpy(before,media,sizeof(media));writes=0;v.device_choice=1;
    assert(kshim_config_save(&store,&v,&seq)==0 && seq==2);assert_only_slot_changed(1);
    memcpy(before,media,sizeof(media));writes=0;
    assert(kshim_config_save(&store,&v,&seq)==0 && seq==3);assert_only_slot_changed(0);
    fixture(bs);make_slot(0,UINT64_MAX,0,0,0);assert(open_store()==0);
    assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_OVERFLOW && writes==0);
    fixture(bs);make_slot(0,7,0,0,0);make_slot(1,7,1,0,0);assert(open_store()==0);
    assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_CONFLICT && writes==0);
    fixture(bs);assert(open_store()==0);v.device_choice=3;assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_ARG && writes==0);
    v.device_choice=0;v.default_target=5;assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_ARG && writes==0);
    v.default_target=0;v.language=2;assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_ARG && writes==0);
}
static void test_faults(unsigned bs) {
    /* Overwriting newest slot, ignoring flush, or trusting a write callback fails. */
    const int cuts[]={0,16,64,512,2048,4095};
    struct kshim_config_values v={2,3,1, 0, 0},got;uint64_t seq;
    for(unsigned i=0;i<sizeof(cuts)/sizeof(cuts[0]);i++) {
        fixture(bs);make_slot(0,7,1,2,0);assert(open_store()==0);memcpy(before,media,sizeof(media));
        partial_bytes=cuts[i];fail_write=1;seq=99;
        assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_IO && seq==99);assert_only_slot_changed(1);
        assert(kshim_config_load(&store,&got,&seq)==0 && seq==7 && got.device_choice==1);
    }
    for(unsigned fault=0;fault<4;fault++) {
        fixture(bs);make_slot(0,7,1,2,0);assert(open_store()==0);memcpy(before,media,sizeof(media));memcpy(old_slot,slot(0),4096);
        if(fault==0){partial_bytes=64;} /* callback lies about completed write */
        if(fault==1){fail_flush=1;}
        if(fault==2){corrupt_readback=1;}
        if(fault==3){fail_read_call=reads+3;}
        seq=99;assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_IO && seq==99);
        assert(memcmp(old_slot,slot(0),4096)==0);assert_only_slot_changed(1);
    }
    fixture(bs);make_slot(0,7,1,2,0);assert(open_store()==0);fail_read_call=reads+1;
    assert(kshim_config_save(&store,&v,&seq)==KSHIM_STORAGE_IO && writes==0);
}
static void test_last_boot_v2(unsigned bs) {
    fixture(bs);make_slot(0,7,0,2,0);make_slot(1,8,0,4,0);
    le32(slot(1)+8,2);le32(slot(1)+68,1);seal(slot(1));
    assert(open_store()==0);
    struct kshim_config_values v={0};uint64_t sequence=0;
    assert(kshim_config_load(&store,&v,&sequence)==0);
    assert(sequence==8 && v.default_target==4 && v.last_target==1);
    v.last_target=2;writes=0;memcpy(before,media,sizeof(media));
    assert(kshim_config_save(&store,&v,&sequence)==0 && sequence==9);assert_only_slot_changed(0);
    memset(&v,0,sizeof(v));assert(kshim_config_load(&store,&v,&sequence)==0 && v.last_target==2 && v.default_target==4);
    /* A corrupt/unknown newer record must not mask a compatible older one. */
    for (unsigned version=4;version<=5;++version) {
        le32(slot(0)+8,version);seal(slot(0));
        assert(kshim_config_load(&store,&v,&sequence)==0 && sequence==8 && v.last_target==1);
    }
    le32(slot(0)+8,2);le32(slot(0)+68,4);seal(slot(0));
    assert(kshim_config_load(&store,&v,&sequence)==0 && sequence==8);
    le32(slot(0)+68,2);slot(0)[72]=1;seal(slot(0));
    assert(kshim_config_load(&store,&v,&sequence)==0 && sequence==8);
    fixture(bs);make_slot(0,7,0,2,0);assert(open_store()==0);v.last_target=3;
    assert(kshim_config_load(&store,&v,&sequence)==0 && v.last_target==0 && sequence==7);
}
static void test_one_shot_v3(unsigned bs) {
    fixture(bs);make_slot(0,7,0,4,1);
    le32(slot(0)+8,3);le32(slot(0)+68,2);le32(slot(0)+72,1);seal(slot(0));
    assert(open_store()==0);
    struct kshim_config_values v={0};uint64_t seq=0;
    assert(kshim_config_load(&store,&v,&seq)==0 && v.next_action==1 && v.last_target==2);
    v.next_action=0;
    assert(kshim_config_save(&store,&v,&seq)==0 && seq==8);
    assert(slot(1)[8]==3 && slot(1)[72]==0);
    assert(kshim_config_load(&store,&v,&seq)==0 && v.next_action==0 && v.default_target==4 && v.language==1);
    le32(slot(1)+72,2);seal(slot(1));
    assert(kshim_config_load(&store,&v,&seq)==0 && seq==7 && v.next_action==1);
    le32(slot(1)+72,0);slot(1)[76]=1;seal(slot(1));
    assert(kshim_config_load(&store,&v,&seq)==0 && seq==7);
    fixture(bs);make_slot(0,1,0,2,0);assert(open_store()==0);v.next_action=1;
    assert(kshim_config_load(&store,&v,&seq)==0 && v.next_action==0);
}
int main(void) {
    test_one_shot_v3(512);test_one_shot_v3(4096);
    test_last_boot_v2(512);test_last_boot_v2(4096);
    assert(fixture_crc((const unsigned char *)"123456789",9)==0xcbf43926u);
    test_descriptor(512);test_descriptor(4096);
    test_descriptor_versions(512);test_descriptor_versions(4096);
    test_selection(512);test_selection(4096);
    test_saving(512);test_saving(4096);test_faults(512);test_faults(4096);
    puts("PASS storage_config: descriptor, identity, enums, sequences, write bounds, partial/flush/readback faults (512/4096)");
    return 0;
}
