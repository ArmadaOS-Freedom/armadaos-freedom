#define _GNU_SOURCE
#include "update_ufs_boot.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/bsg.h>
#include <scsi/scsi_bsg_ufs.h>
#include <scsi/sg.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #x); \
    exit(1); \
} } while (0)

enum fault {
    F_NONE, F_IOCTL, F_TRANSPORT, F_DEVICE, F_DRIVER, F_INFO, F_SHORT,
    F_RESULT, F_PAYLOAD, F_RESPONSE_CODE, F_STATUS, F_TRANSACTION,
    F_DATA_SEGMENT, F_OPCODE, F_IDN, F_INDEX, F_SELECTOR, F_LENGTH,
    F_VALUE_ZERO, F_VALUE_THREE, F_WRONG_READBACK
};

static struct {
    uint32_t current;
    int reads;
    int writes;
    int calls;
    int fault_at;
    enum fault fault;
    int fault_errno;
} fake;

static void reset_fake(uint32_t current)
{
    memset(&fake, 0, sizeof(fake));
    fake.current = current;
}

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    struct sg_io_v4 *io;
    struct ufs_bsg_request *req;
    struct ufs_bsg_reply *rsp;
    uint32_t dw0, dw1, value = 0U;
    bool write;

    (void)fd;
    va_start(ap, request);
    io = va_arg(ap, struct sg_io_v4 *);
    va_end(ap);

    fake.calls++;
    CHECK(request == SG_IO);
    CHECK(io != NULL);
    CHECK(io->guard == 'Q');
    CHECK(io->protocol == BSG_PROTOCOL_SCSI);
    CHECK(io->subprotocol == BSG_SUB_PROTOCOL_SCSI_TRANSPORT);
    CHECK(io->request_len == sizeof(*req));
    CHECK(io->max_response_len == sizeof(*rsp));
    CHECK(io->request != 0U && io->response != 0U);
    CHECK(io->dout_xfer_len == 0U && io->din_xfer_len == 0U);
    CHECK(io->dout_xferp == 0U && io->din_xferp == 0U);
    CHECK(io->timeout == 5000U);

    req = (struct ufs_bsg_request *)(uintptr_t)io->request;
    rsp = (struct ufs_bsg_reply *)(uintptr_t)io->response;
    CHECK(req->msgcode == 0x16U);
    CHECK((ntohl(req->upiu_req.header.dword_0) >> 24) == 0x16U);
    dw1 = ntohl(req->upiu_req.header.dword_1);
    CHECK(ntohl(req->upiu_req.header.dword_2) == 0U);
    CHECK(req->upiu_req.qr.idn == 0U);
    CHECK(req->upiu_req.qr.index == 0U);
    CHECK(req->upiu_req.qr.selector == 0U);
    CHECK(req->upiu_req.qr.length == 0U);
    if (((dw1 >> 16) & 0xffU) == 0x01U) {
        write = false;
        CHECK(req->upiu_req.qr.opcode == 3U);
        CHECK(req->upiu_req.qr.value == 0U);
    } else {
        CHECK(((dw1 >> 16) & 0xffU) == 0x81U);
        write = true;
        CHECK(req->upiu_req.qr.opcode == 4U);
        value = ntohl(req->upiu_req.qr.value);
        CHECK(value == 1U || value == 2U);
    }

    if (write)
        fake.writes++;
    else
        fake.reads++;

    if (fake.fault_at == fake.calls && fake.fault == F_IOCTL) {
        errno = fake.fault_errno;
        return -1;
    }

    memset(rsp, 0, sizeof(*rsp));
    dw0 = 0x36U << 24;
    rsp->upiu_rsp.header.dword_0 = htonl(dw0);
    rsp->upiu_rsp.header.dword_1 = 0U;
    rsp->upiu_rsp.header.dword_2 = 0U;
    rsp->upiu_rsp.qr.opcode = write ? 4U : 3U;
    rsp->upiu_rsp.qr.idn = 0U;
    rsp->upiu_rsp.qr.index = 0U;
    rsp->upiu_rsp.qr.selector = 0U;
    rsp->upiu_rsp.qr.length = 0U;
    value = write ? value : fake.current;
    rsp->upiu_rsp.qr.value = htonl(value);
    io->response_len = sizeof(*rsp);
    io->transport_status = 0U;
    io->device_status = 0U;
    io->driver_status = 0U;
    io->info = 0U;
    io->din_resid = 0;
    io->dout_resid = 0;

    if (fake.fault_at == fake.calls) {
        switch (fake.fault) {
        case F_TRANSPORT: io->transport_status = 1U; break;
        case F_DEVICE: io->device_status = 1U; break;
        case F_DRIVER: io->driver_status = 1U; break;
        case F_INFO: io->info = 1U; break;
        case F_SHORT: io->response_len = sizeof(*rsp) - 1U; break;
        case F_RESULT: rsp->result = -1; break;
        case F_PAYLOAD: rsp->reply_payload_rcv_len = 1U; break;
        case F_RESPONSE_CODE: rsp->upiu_rsp.header.dword_1 = htonl(1U << 8); break;
        case F_STATUS: rsp->upiu_rsp.header.dword_1 = htonl(1U); break;
        case F_TRANSACTION: rsp->upiu_rsp.header.dword_0 = htonl(0x35U << 24); break;
        case F_DATA_SEGMENT: rsp->upiu_rsp.header.dword_2 = htonl(1U); break;
        case F_OPCODE: rsp->upiu_rsp.qr.opcode = write ? 3U : 4U; break;
        case F_IDN: rsp->upiu_rsp.qr.idn = 1U; break;
        case F_INDEX: rsp->upiu_rsp.qr.index = 1U; break;
        case F_SELECTOR: rsp->upiu_rsp.qr.selector = 1U; break;
        case F_LENGTH: rsp->upiu_rsp.qr.length = 1U; break;
        case F_VALUE_ZERO: rsp->upiu_rsp.qr.value = htonl(0U); break;
        case F_VALUE_THREE: rsp->upiu_rsp.qr.value = htonl(3U); break;
        case F_WRONG_READBACK: rsp->upiu_rsp.qr.value = htonl(value == 2U ? 1U : 2U); break;
        default: break;
        }
    }

    if (write && (fake.fault_at != fake.calls || fake.fault == F_NONE)) {
        fake.current = value;
    }
    return 0;
}

static void test_get(void)
{
    int fd = open("/dev/null", O_RDONLY);
    uint32_t value = 0U;
    CHECK(fd >= 0);
    reset_fake(1U);
    CHECK(armada_ufs_get_boot_lun(fd, &value) == 0 && value == 1U);
    reset_fake(2U);
    CHECK(armada_ufs_get_boot_lun(fd, &value) == 0 && value == 2U);
    reset_fake(1U);
    fake.fault_at = 1;
    fake.fault = F_IOCTL;
    fake.fault_errno = EIO;
    value = 77U;
    CHECK(armada_ufs_get_boot_lun(fd, &value) < 0 && value == 77U);
    CHECK(fcntl(fd, F_GETFL) >= 0);
    close(fd);
}

static void test_set(void)
{
    int fd = open("/dev/null", O_RDWR);
    CHECK(fd >= 0);
    reset_fake(1U);
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 2U) == 0);
    CHECK(fake.calls == 3 && fake.reads == 2 && fake.writes == 1 && fake.current == 2U);
    reset_fake(2U);
    CHECK(armada_ufs_set_boot_lun(fd, 2U, 1U) == 0);
    CHECK(fake.calls == 3 && fake.current == 1U);
    reset_fake(1U);
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 1U) == 0);
    CHECK(fake.calls == 1 && fake.reads == 1 && fake.writes == 0);
    reset_fake(2U);
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 2U) == -ESTALE);
    CHECK(fake.calls == 1 && fake.writes == 0 && fake.current == 2U);
    CHECK(fcntl(fd, F_GETFL) >= 0);
    close(fd);
}

static void test_validation(void)
{
    int rd = open("/dev/null", O_RDONLY);
    int wr = open("/dev/null", O_WRONLY);
    int pathfd = open("/dev/null", O_PATH);
    const char *tmp = getenv("TMPDIR");
    char name[4096];
    int regular;
    uint32_t value = 9U;

    CHECK(rd >= 0 && wr >= 0 && pathfd >= 0);
    CHECK(tmp != NULL);
    CHECK(snprintf(name, sizeof(name), "%s/ufs-test-XXXXXX", tmp) > 0);
    regular = mkstemp(name);
    CHECK(regular >= 0);

    reset_fake(1U);
    CHECK(armada_ufs_get_boot_lun(rd, NULL) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 0U, 1U) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 3U, 1U) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 1U, 0U) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 1U, 3U) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, (uint32_t)-1, 1U) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 1U, (uint32_t)-1) == -EINVAL);
    CHECK(armada_ufs_get_boot_lun(-1, &value) == -EBADF);
    CHECK(armada_ufs_get_boot_lun(wr, &value) == -EBADF);
    CHECK(armada_ufs_get_boot_lun(pathfd, &value) == -EBADF);
    CHECK(armada_ufs_get_boot_lun(regular, &value) == -EINVAL);
    CHECK(armada_ufs_set_boot_lun(rd, 1U, 2U) == -EBADF);
    CHECK(armada_ufs_set_boot_lun(wr, 1U, 2U) == -EBADF);
    CHECK(armada_ufs_set_boot_lun(pathfd, 1U, 2U) == -EBADF);
    CHECK(armada_ufs_set_boot_lun(regular, 1U, 2U) == -EINVAL);
    CHECK(fake.calls == 0 && value == 9U);
    CHECK(fcntl(rd, F_GETFL) >= 0 && fcntl(wr, F_GETFL) >= 0);
    close(regular);
    unlink(name);
    close(pathfd);
    close(wr);
    close(rd);
}

static void test_response_faults(void)
{
    static const enum fault faults[] = {
        F_TRANSPORT, F_DEVICE, F_DRIVER, F_INFO, F_SHORT, F_RESULT,
        F_PAYLOAD, F_RESPONSE_CODE, F_STATUS, F_TRANSACTION,
        F_DATA_SEGMENT, F_OPCODE, F_IDN, F_INDEX, F_SELECTOR, F_LENGTH,
        F_VALUE_ZERO, F_VALUE_THREE
    };
    int fd = open("/dev/null", O_RDONLY);
    size_t i;
    uint32_t value;

    CHECK(fd >= 0);
    reset_fake(1U);
    fake.fault_at = 1;
    fake.fault = F_IOCTL;
    fake.fault_errno = EINTR;
    value = 123U;
    CHECK(armada_ufs_get_boot_lun(fd, &value) == -EIO);
    CHECK(value == 123U && fake.calls == 1);
    for (i = 0; i < sizeof(faults) / sizeof(faults[0]); ++i) {
        reset_fake(1U);
        fake.fault_at = 1;
        fake.fault = faults[i];
        value = 123U;
        CHECK(armada_ufs_get_boot_lun(fd, &value) == -EIO);
        CHECK(value == 123U && fake.calls == 1);
    }
    CHECK(fcntl(fd, F_GETFL) >= 0);
    close(fd);
}

static void test_write_and_readback_faults(void)
{
    int fd = open("/dev/null", O_RDWR);
    CHECK(fd >= 0);

    reset_fake(1U);
    fake.fault_at = 2;
    fake.fault = F_IOCTL;
    fake.fault_errno = EINTR;
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 2U) == -EIO);
    CHECK(fake.calls == 2 && fake.writes == 1 && fake.current == 1U);

    reset_fake(1U);
    fake.fault_at = 3;
    fake.fault = F_STATUS;
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 2U) == -EIO);
    CHECK(fake.calls == 3 && fake.writes == 1 && fake.current == 2U);

    reset_fake(1U);
    fake.fault_at = 3;
    fake.fault = F_WRONG_READBACK;
    CHECK(armada_ufs_set_boot_lun(fd, 1U, 2U) == -EIO);
    CHECK(fake.calls == 3 && fake.writes == 1 && fake.current == 2U);
    CHECK(fcntl(fd, F_GETFL) >= 0);
    close(fd);
}

int main(void)
{
    test_get();
    test_set();
    test_validation();
    test_response_faults();
    test_write_and_readback_faults();
    puts("PASS update_ufs_boot offline tests");
    return 0;
}
