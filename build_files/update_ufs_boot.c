// SPDX-License-Identifier: GPL-3.0-only
#define _GNU_SOURCE

#include "update_ufs_boot.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/bsg.h>
#include <scsi/scsi_bsg_ufs.h>
#include <scsi/sg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>

/*
 * Scope note: This helper operates strictly on a caller-supplied UFS BSG
 * character device fd without opening or closing descriptors or touching sysfs
 * or partition tables. Caller (kshim_ab_select) is responsible for proving fd
 * belongs to the same UFS controller anchored by the recovery partition and
 * handling GPT updates and rollback policy.
 */

enum {
    UFS_UPIU_QUERY_REQ_TRANS = 0x16U,
    UFS_UPIU_QUERY_RSP_TRANS = 0x36U,
    UFS_QUERY_FUNC_STD_READ  = 0x01U,
    UFS_QUERY_FUNC_STD_WRITE = 0x81U,
    UFS_QUERY_OP_READ_ATTR   = 0x03U,
    UFS_QUERY_OP_WRITE_ATTR  = 0x04U,
    UFS_ATTR_IDN_BOOT_LUN_EN = 0x00U,
    UFS_BSG_TIMEOUT_MS       = 5000U
};

static bool is_valid_boot_lun(uint32_t lun)
{
    return lun == 1U || lun == 2U;
}

static int validate_bsg_fd(int fd, bool require_rdwr)
{
    if (fd < 0) {
        return -EBADF;
    }

    int flags = fcntl(fd, F_GETFL);
    if (flags < 0) {
        return -EBADF;
    }

#ifdef O_PATH
    if ((flags & O_PATH) != 0) {
        return -EBADF;
    }
#endif

    int accmode = flags & O_ACCMODE;
    if (require_rdwr) {
        if (accmode != O_RDWR) {
            return -EBADF;
        }
    } else {
        if (accmode != O_RDONLY && accmode != O_RDWR) {
            return -EBADF;
        }
    }

    struct stat st;
    memset(&st, 0, sizeof(st));
    if (fstat(fd, &st) != 0) {
        return -EBADF;
    }
    if (!S_ISCHR(st.st_mode)) {
        return -EINVAL;
    }

    return 0;
}

static int ufs_bsg_query_boot_lun(int fd, bool is_write, uint32_t in_val, uint32_t *out_val)
{
    struct ufs_bsg_request req;
    struct ufs_bsg_reply rsp;
    struct sg_io_v4 io;

    memset(&req, 0, sizeof(req));
    memset(&rsp, 0, sizeof(rsp));
    memset(&io, 0, sizeof(io));

    uint8_t func = is_write ? UFS_QUERY_FUNC_STD_WRITE : UFS_QUERY_FUNC_STD_READ;
    uint8_t opcode = is_write ? UFS_QUERY_OP_WRITE_ATTR : UFS_QUERY_OP_READ_ATTR;

    req.msgcode = UFS_UPIU_QUERY_REQ_TRANS;
    req.upiu_req.header.dword_0 = htonl((uint32_t)UFS_UPIU_QUERY_REQ_TRANS << 24);
    req.upiu_req.header.dword_1 = htonl((uint32_t)func << 16);
    req.upiu_req.header.dword_2 = 0U;

    req.upiu_req.qr.opcode = opcode;
    req.upiu_req.qr.idn = UFS_ATTR_IDN_BOOT_LUN_EN;
    req.upiu_req.qr.index = 0U;
    req.upiu_req.qr.selector = 0U;
    req.upiu_req.qr.length = 0U;
    req.upiu_req.qr.value = is_write ? htonl(in_val) : 0U;

    io.guard = 'Q';
    io.protocol = BSG_PROTOCOL_SCSI;
    io.subprotocol = BSG_SUB_PROTOCOL_SCSI_TRANSPORT;
    io.request_len = (uint32_t)sizeof(req);
    io.request = (uint64_t)(uintptr_t)&req;
    io.max_response_len = (uint32_t)sizeof(rsp);
    io.response = (uint64_t)(uintptr_t)&rsp;
    io.timeout = UFS_BSG_TIMEOUT_MS;

    /* Single SG_IO attempt: do not auto-retry as write state may be uncertain on interruption. */
    if (ioctl(fd, SG_IO, &io) != 0) {
        return -EIO;
    }

    if (io.driver_status != 0U || io.transport_status != 0U ||
        io.device_status != 0U || io.info != 0U ||
        io.response_len != sizeof(rsp) ||
        io.din_resid != 0 || io.dout_resid != 0) {
        return -EIO;
    }

    if (rsp.result != 0 || rsp.reply_payload_rcv_len != 0U) {
        return -EIO;
    }

    uint32_t dw0 = ntohl(rsp.upiu_rsp.header.dword_0);
    uint32_t dw1 = ntohl(rsp.upiu_rsp.header.dword_1);
    uint32_t dw2 = ntohl(rsp.upiu_rsp.header.dword_2);

    if (((dw0 >> 24) & 0x3fU) != UFS_UPIU_QUERY_RSP_TRANS) {
        return -EIO;
    }
    if (((dw1 >> 8) & 0xffU) != 0U || (dw1 & 0xffU) != 0U) {
        return -EIO;
    }
    if ((dw2 & 0xffffU) != 0U) {
        return -EIO;
    }

    if (rsp.upiu_rsp.qr.opcode != opcode ||
        rsp.upiu_rsp.qr.idn != UFS_ATTR_IDN_BOOT_LUN_EN ||
        rsp.upiu_rsp.qr.index != 0U ||
        rsp.upiu_rsp.qr.selector != 0U ||
        rsp.upiu_rsp.qr.length != 0U) {
        return -EIO;
    }

    uint32_t val = ntohl(rsp.upiu_rsp.qr.value);
    if (!is_valid_boot_lun(val)) {
        return -EIO;
    }
    if (is_write && val != in_val) {
        return -EIO;
    }

    *out_val = val;
    return 0;
}

__attribute__((visibility("default")))
int armada_ufs_get_boot_lun(int fd, uint32_t *value)
{
    if (value == NULL) {
        return -EINVAL;
    }

    int rc = validate_bsg_fd(fd, false);
    if (rc != 0) {
        return rc;
    }

    uint32_t current = 0U;
    rc = ufs_bsg_query_boot_lun(fd, false, 0U, &current);
    if (rc != 0) {
        return rc;
    }

    *value = current;
    return 0;
}

__attribute__((visibility("default")))
int armada_ufs_set_boot_lun(int fd, uint32_t expected, uint32_t target)
{
    if (!is_valid_boot_lun(expected) || !is_valid_boot_lun(target)) {
        return -EINVAL;
    }

    int rc = validate_bsg_fd(fd, true);
    if (rc != 0) {
        return rc;
    }

    uint32_t current = 0U;
    rc = ufs_bsg_query_boot_lun(fd, false, 0U, &current);
    if (rc != 0) {
        return rc;
    }
    if (current != expected) {
        return -ESTALE;
    }
    if (expected == target) {
        return 0;
    }

    uint32_t write_rsp = 0U;
    rc = ufs_bsg_query_boot_lun(fd, true, target, &write_rsp);
    if (rc != 0) {
        return rc;
    }

    uint32_t verify = 0U;
    rc = ufs_bsg_query_boot_lun(fd, false, 0U, &verify);
    if (rc != 0 || verify != target) {
        return -EIO;
    }

    return 0;
}
