#!/usr/bin/env python3
"""Exercise actual q6apm lifecycle source with host-side kernel stubs.

Give this script a Linux kernel source tree and a writable work directory.
The candidate function bodies are extracted verbatim from that tree.  An
optional baseline kernel tree enables negative controls; the normal build
recipe does not need a second checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


Q6APM_C_REL = Path("sound/soc/qcom/qdsp6/q6apm.c")
Q6APM_H_REL = Path("sound/soc/qcom/qdsp6/q6apm.h")
Q6APM_DAI_NAME = "q6apm-dai.c"


def extract_function(source: str, signature: str) -> tuple[str, int]:
    """Extract one complete C function, including its closing brace."""
    start = source.find(signature)
    if start < 0:
        raise ValueError(f"signature not found: {signature}")
    if source.find(signature, start + 1) >= 0:
        raise ValueError(f"signature is not unique: {signature}")

    brace = source.find("{", start)
    if brace < 0:
        raise ValueError(f"opening brace not found for: {signature}")

    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for pos in range(brace, len(source)):
        char = source[pos]
        if escaped:
            escaped = False
            continue
        if char == "\\" and (in_string or in_char):
            escaped = True
            continue
        if char == '"' and not in_char:
            in_string = not in_string
            continue
        if char == "'" and not in_string:
            in_char = not in_char
            continue
        if in_string or in_char:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                line = source.count("\n", 0, start) + 1
                return source[start : pos + 1], line
    raise ValueError(f"closing brace not found for: {signature}")


def require_once(function: str, needle: str, label: str) -> None:
    count = function.count(needle)
    if count != 1:
        raise ValueError(f"{label}: expected one {needle!r}, found {count}")


def extract_if_block(function: str, condition: str) -> str:
    start = function.find(condition)
    if start < 0:
        raise ValueError(f"conditional block not found: {condition}")
    brace = function.find("{", start)
    if brace < 0:
        raise ValueError(f"conditional block has no body: {condition}")
    depth = 0
    for pos in range(brace, len(function)):
        if function[pos] == "{":
            depth += 1
        elif function[pos] == "}":
            depth -= 1
            if depth == 0:
                return function[start : pos + 1]
    raise ValueError(f"unterminated conditional block: {condition}")


def require_order(function: str, *needles: str, label: str) -> None:
    positions = []
    for needle in needles:
        position = function.find(needle)
        if position < 0:
            raise ValueError(f"{label}: source is missing {needle!r}")
        positions.append(position)
    if positions != sorted(positions):
        raise ValueError(f"{label}: source operations are out of order")


def require_absent(function: str, needle: str, label: str) -> None:
    if needle in function:
        raise ValueError(f"{label}: unexpected {needle!r}")


def extract_case(function: str, case_label: str, next_case: str) -> str:
    start = function.find(case_label)
    if start < 0:
        raise ValueError(f"case not found: {case_label}")
    end = function.find(next_case, start)
    if end < 0:
        raise ValueError(f"next case not found after {case_label}")
    return function[start:end]


def classify_sources(
    candidate_text: str,
    candidate_header: str,
    dai_text: str,
    soc_pcm_text: str,
    soc_component_text: str,
    baseline_text: str | None,
) -> dict[str, tuple[str, int]]:
    signatures = {
        "candidate_start": (
            candidate_text,
            "int q6apm_graph_start(struct q6apm_graph *graph)",
        ),
        "candidate_stop": (
            candidate_text,
            "int q6apm_graph_stop(struct q6apm_graph *graph)",
        ),
        "candidate_send_tracked": (
            candidate_text,
            "static int q6apm_send_tracked_cmd(struct q6apm *apm, struct gpr_pkt *pkt)",
        ),
        "candidate_trigger": (
            dai_text,
            "static int q6apm_dai_trigger(struct snd_soc_component *component,",
        ),
        "candidate_unmap": (
            candidate_text,
            "static int __q6apm_unmap_memory_fixed_region(struct device *dev, "
            "unsigned int graph_id,",
        ),
        "candidate_callback": (
            candidate_text,
            "static int apm_callback(const struct gpr_resp_pkt *data, void *priv, int op)",
        ),
        "candidate_soc_pcm_hw_clean": (
            soc_pcm_text,
            "static int soc_pcm_hw_clean(struct snd_soc_pcm_runtime *rtd,",
        ),
        "candidate_soc_pcm_clean_components": (
            soc_pcm_text,
            "static int soc_pcm_hw_clean_components(struct snd_pcm_substream *substream,",
        ),
        "candidate_soc_pcm_clean_dais": (
            soc_pcm_text,
            "static void soc_pcm_hw_clean_dais(struct snd_soc_pcm_runtime *rtd,",
        ),
        "candidate_component_hw_free": (
            soc_component_text,
            "int snd_soc_pcm_component_hw_free(struct snd_pcm_substream *substream,",
        ),
        "candidate_dpcm_hw_free": (
            soc_pcm_text,
            "static int dpcm_fe_dai_hw_free(struct snd_pcm_substream *substream)",
        ),
        "candidate_dpcm_hw_params": (
            soc_pcm_text,
            "static int dpcm_fe_dai_hw_params(struct snd_pcm_substream *substream,",
        ),
        "candidate_dpcm_shutdown": (
            soc_pcm_text,
            "static int dpcm_fe_dai_shutdown(struct snd_pcm_substream *substream)",
        ),
        "candidate_dpcm_trigger": (
            soc_pcm_text,
            "static int dpcm_fe_dai_do_trigger(struct snd_pcm_substream *substream, int cmd)\n{",
        ),
        "candidate_dpcm_runtime_update": (
            soc_pcm_text,
            "int snd_soc_dpcm_runtime_update(struct snd_soc_card *card)",
        ),
    }
    if baseline_text is not None:
        signatures.update(
            {
                "baseline_start": (
                    baseline_text,
                    "int q6apm_graph_start(struct q6apm_graph *graph)",
                ),
                "baseline_stop": (
                    baseline_text,
                    "int q6apm_graph_stop(struct q6apm_graph *graph)",
                ),
                "baseline_unmap": (
                    baseline_text,
                    "static int __q6apm_unmap_memory_fixed_region(struct device *dev, "
                    "unsigned int graph_id,",
                ),
                "baseline_callback": (
                    baseline_text,
                    "static int apm_callback(const struct gpr_resp_pkt *data, "
                    "void *priv, int op)",
                ),
            }
        )
    extracted = {
        name: extract_function(text, signature)
        for name, (text, signature) in signatures.items()
    }

    candidate_stop = extracted["candidate_stop"][0]
    candidate_start = extracted["candidate_start"][0]
    require_once(
        candidate_header,
        "\tstruct mutex start_stop_lock;",
        "candidate shared graph lock field",
    )
    require_once(
        candidate_text,
        "\tmutex_init(&graph->start_stop_lock);",
        "candidate shared graph lock initialization",
    )
    require_once(
        candidate_start,
        "mutex_lock(&ar_graph->start_stop_lock);",
        "candidate START lock",
    )
    require_once(
        candidate_start,
        "mutex_unlock(&ar_graph->start_stop_lock);",
        "candidate START unlock",
    )
    require_once(candidate_stop, "if (ar_graph->start_count > 1)", "candidate STOP")
    require_once(
        candidate_stop,
        "mutex_lock(&ar_graph->start_stop_lock);",
        "candidate STOP lock",
    )
    require_once(
        candidate_stop,
        "mutex_unlock(&ar_graph->start_stop_lock);",
        "candidate STOP unlock",
    )
    require_once(
        candidate_stop,
        "if (!ret)\n\t\tar_graph->start_count--;",
        "candidate STOP successful commit",
    )
    if "--ar_graph->start_count" in candidate_stop:
        raise ValueError("candidate STOP still decrements before command")

    if baseline_text is not None:
        baseline_start = extracted["baseline_start"][0]
        if "start_stop_lock" in baseline_start:
            raise ValueError("baseline unexpectedly contains the shared graph lock")
        baseline_stop = extracted["baseline_stop"][0]
        require_once(
            baseline_stop, "if (--ar_graph->start_count > 0)", "baseline STOP"
        )

        baseline_unmap = extracted["baseline_unmap"][0]
        require_once(
            baseline_unmap,
            "APM_CMD_SHARED_MEM_UNMAP_REGIONS, graph_id);",
            "baseline request token",
        )

        baseline_callback = extracted["baseline_callback"][0]
        baseline_unmap_case = extract_case(
            baseline_callback,
            "case APM_CMD_SHARED_MEM_UNMAP_REGIONS:",
            "case APM_CMD_RSP_SHARED_MEM_MAP_REGIONS:",
        )
        require_once(
            baseline_unmap_case,
            "apm->result.opcode = hdr->opcode;",
            "baseline callback outer opcode",
        )
        require_once(
            baseline_unmap_case,
            "idr_find(&apm->graph_info_idr, hdr->token);",
            "baseline callback unmasked token",
        )

    clean = extracted["candidate_soc_pcm_hw_clean"][0]
    require_order(
        clean,
        "ret = soc_pcm_hw_clean_components(substream, rollback, retain_failed);",
        "if (ret < 0 && stop_on_component_error)",
        "return ret;",
        "soc_pcm_hw_clean_dais(rtd, substream, rollback);",
        label="DPCM FE cleanup aborts before DAI cleanup on component error",
    )
    require_once(
        extracted["candidate_soc_pcm_clean_components"][0],
        "return snd_soc_pcm_component_hw_free(substream, rollback, retain_failed);",
        "component cleanup helper",
    )
    component_hw_free = extracted["candidate_component_hw_free"][0]
    require_order(
        component_hw_free,
        "ret = 0;",
        "ret = component->driver->hw_free(component, substream);",
        "if (ret >= 0 || !retain_failed)",
        "soc_component_mark_pop(component, hw_params);",
        label="failed component marks remain available for recovery retry",
    )
    require_once(
        extracted["candidate_soc_pcm_clean_dais"][0],
        "snd_soc_dai_hw_free(dai, substream, rollback);",
        "DAI cleanup helper",
    )

    hw_free = extracted["candidate_dpcm_hw_free"][0]
    require_order(
        hw_free,
        "ret = soc_pcm_hw_clean(fe, substream, 0, true, true);",
        "if (ret < 0)",
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, true);",
        "goto out;",
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, false);",
        "dpcm_be_dai_hw_free(fe, stream);",
        label="FE STOP failure retains the backend",
    )
    hw_free_failure = extract_if_block(
        hw_free, "if (ret < 0) {\n\t\tWRITE_ONCE(fe->dpcm[stream].hw_free_failed, true);"
    )
    require_order(
        hw_free_failure,
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, true);",
        "goto out;",
        label="FE STOP failure exits before backend cleanup",
    )
    require_absent(
        hw_free_failure,
        "dpcm_be_dai_hw_free(fe, stream);",
        "failed FE STOP preserves backend parameters",
    )

    hw_params = extracted["candidate_dpcm_hw_params"][0]
    require_order(
        hw_params,
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "ret = soc_pcm_hw_clean_components(substream, 1, true);",
        "if (ret < 0)",
        "soc_pcm_hw_clean_dais(fe, substream, 0);",
        "dpcm_be_dai_hw_free(fe, stream);",
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, false);",
        "memcpy(&fe->dpcm[stream].hw_params, params,",
        label="DPCM retries failed FE cleanup before reconfiguring",
    )
    hw_params_recovery = extract_if_block(
        hw_params, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    require_absent(
        hw_params_recovery,
        "snd_soc_dapm_stream_stop",
        "hw_params recovery does not repeat DAPM cleanup",
    )
    require_absent(
        hw_params_recovery,
        "snd_soc_link_hw_free",
        "hw_params recovery does not repeat machine-link cleanup",
    )
    require_once(
        hw_params_recovery,
        "ret = soc_pcm_hw_clean_components(substream, 1, true);",
        "hw_params component-only recovery attempt",
    )

    shutdown = extracted["candidate_dpcm_shutdown"][0]
    require_order(
        shutdown,
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "ret = soc_pcm_hw_clean_components(substream, 1, false);",
        "dev_err(fe->dev,",
        "soc_pcm_hw_clean_dais(fe, substream, 0);",
        "dpcm_be_dai_hw_free(fe, stream);",
        "dpcm_be_dai_shutdown(fe, stream);",
        label="PCM close performs final FE cleanup before backend shutdown",
    )
    shutdown_recovery = extract_if_block(
        shutdown, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    require_order(
        shutdown_recovery,
        "ret = soc_pcm_hw_clean_components(substream, 1, false);",
        "soc_pcm_hw_clean_dais(fe, substream, 0);",
        "dpcm_be_dai_hw_free(fe, stream);",
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, false);",
        label="close completes best-effort cleanup after the final STOP attempt",
    )

    trigger = extracted["candidate_dpcm_trigger"][0]
    require_order(
        trigger,
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed)",
        "cmd == SNDRV_PCM_TRIGGER_START",
        "goto out;",
        "switch (trigger)",
        label="DPCM blocks START while FE STOP remains unresolved",
    )

    runtime_update = extracted["candidate_dpcm_runtime_update"][0]
    runtime_update_guard = extract_if_block(
        runtime_update, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    require_order(
        runtime_update,
        "for_each_card_rtds(card, fe)",
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "ret = -EBUSY;",
        "goto out;",
        "/* shutdown all old paths first */",
        "ret = soc_dpcm_fe_runtime_update(fe, 0);",
        label="runtime routing checks failed STOP before changing BE paths",
    )
    require_absent(
        runtime_update_guard,
        "soc_dpcm_fe_runtime_update",
        "failed STOP leaves DAPM backend paths untouched",
    )
    return extracted


PREAMBLE = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef uint8_t u8;
typedef uint32_t u32;

#define BIT(n) (1UL << (n))
#define __free(x)
#define GPR_HDR_SIZE 24U
#define HZ 1000
#define READ_ONCE(value) (value)
#define WRITE_ONCE(target, value) ((target) = (value))
#define EXPORT_SYMBOL_GPL(symbol)
#define lockdep_assert_held(lock) assert(pthread_mutex_trylock(lock) == EBUSY)

static u32 cmpxchg(u32 *value, u32 expected, u32 replacement)
{
	u32 old = *value;

	if (old == expected)
		*value = replacement;
	return old;
}

#define APM_CMD_SHARED_MEM_UNMAP_REGIONS      0x0100100dU
#define APM_CMD_SHARED_MEM_MAP_REGIONS        0x0100100cU
#define APM_CMD_GRAPH_START                   0x01001002U
#define APM_CMD_GRAPH_OPEN                    0x01001000U
#define APM_CMD_GRAPH_PREPARE                 0x01001001U
#define APM_CMD_GRAPH_CLOSE                   0x01001004U
#define APM_CMD_GRAPH_FLUSH                   0x01001005U
#define APM_CMD_GRAPH_STOP                    0x01001003U
#define APM_CMD_SET_CFG                       0x01001006U
#define APM_CMD_RSP_GET_SPF_STATE             0x02001007U
#define GPR_BASIC_RSP_RESULT                  0x02001005U
#define APM_CMD_RSP_SHARED_MEM_MAP_REGIONS     0x02001001U

#define APM_MMAP_TOKEN_GID_MASK 0xffffU
#define APM_MMAP_TOKEN_MAP_TYPE_POS_BUF BIT(16)

struct device {
	struct device *parent;
	void *drv;
};

typedef struct {
	struct device dev;
	u32 svc_id;
} gpr_device_t;

struct wait_queue_head {
	unsigned int wakes;
};
typedef struct wait_queue_head wait_queue_head_t;

struct gpr_ibasic_rsp_result_t {
	uint32_t status;
	uint32_t opcode;
};

struct gpr_hdr {
	uint32_t token;
	uint32_t opcode;
	uint32_t src_port;
};

struct completion {
	unsigned int done;
};

struct gpr_resp_pkt {
	struct gpr_hdr hdr;
	void *payload;
};

struct apm_cmd_rsp_shared_mem_map_regions {
	uint32_t mem_map_handle;
};

struct audioreach_graph_info {
	uint32_t graph_id;
	uint32_t mem_map_handle;
	uint32_t pos_buf_mem_map_handle;
};

struct fake_idr {
	struct audioreach_graph_info *info;
	unsigned int last_lookup;
	unsigned int lookups;
};

struct q6apm {
	struct device *dev;
	gpr_device_t *gdev;
	pthread_mutex_t lock;
	pthread_mutex_t reply_lock;
	struct completion reply_done;
	u32 reply_token;
	u32 reply_opcode;
	u32 reply_status;
	struct gpr_ibasic_rsp_result_t result;
	wait_queue_head_t wait;
	uint32_t state;
	struct fake_idr graph_info_idr;
};

struct gpr_pkt {
	union {
		struct gpr_hdr hdr;
		unsigned char data[96] __attribute__((aligned(8)));
	};
};

struct apm_cmd_shared_mem_unmap_regions {
	uint32_t mem_map_handle;
};

struct audioreach_graph {
	pthread_mutex_t start_stop_lock;
	int start_count;
};

struct q6apm_graph {
	struct audioreach_graph *ar_graph;
};

enum {
	SNDRV_PCM_TRIGGER_START, SNDRV_PCM_TRIGGER_RESUME,
	SNDRV_PCM_TRIGGER_PAUSE_RELEASE, SNDRV_PCM_TRIGGER_STOP,
	SNDRV_PCM_TRIGGER_SUSPEND, SNDRV_PCM_TRIGGER_PAUSE_PUSH,
};
enum { SNDRV_PCM_STREAM_PLAYBACK, SNDRV_PCM_STREAM_CAPTURE };
enum { Q6APM_STREAM_IDLE, Q6APM_STREAM_RUNNING, Q6APM_STREAM_STOPPED };

struct q6apm_dai_rtd {
	bool prepare_failed;
	int state;
	unsigned int queue_ptr;
	unsigned int last_pos_index;
};

struct snd_pcm_runtime {
	void *private_data;
};

struct snd_pcm_substream {
	struct snd_pcm_runtime *runtime;
	int stream;
};

struct snd_soc_component;

static struct gpr_pkt alloc_packet;
static uint32_t alloc_opcode;
static uint32_t alloc_token;
static bool alloc_fail;
static int stop_results[16];
static int start_results[16];
static int stop_calls;
static int start_calls;
static int dsp_running;
static pthread_mutex_t control_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t stop_entered_cond = PTHREAD_COND_INITIALIZER;
static pthread_cond_t start_attempted_cond = PTHREAD_COND_INITIALIZER;
static bool stop_entered;
static bool start_attempted;
static bool wait_for_start_attempt;
static pthread_t start_thread_id;
static unsigned int error_logs;

static void mutex_lock(pthread_mutex_t *mutex)
{
	assert(pthread_mutex_lock(&control_lock) == 0);
	if (!start_attempted &&
	    pthread_equal(pthread_self(), start_thread_id)) {
		start_attempted = true;
		assert(pthread_cond_broadcast(&start_attempted_cond) == 0);
	}
	assert(pthread_mutex_unlock(&control_lock) == 0);
	assert(pthread_mutex_lock(mutex) == 0);
}

static void mutex_unlock(pthread_mutex_t *mutex)
{
	assert(pthread_mutex_unlock(mutex) == 0);
}

static void spin_lock(pthread_mutex_t *mutex)
{
	assert(pthread_mutex_lock(mutex) == 0);
}

static void spin_unlock(pthread_mutex_t *mutex)
{
	assert(pthread_mutex_unlock(mutex) == 0);
}

static void reinit_completion(struct completion *completion)
{
	completion->done = 0;
}

static void complete(struct completion *completion)
{
	completion->done++;
}

static int wait_for_completion_timeout(struct completion *completion, int timeout)
{
	assert(timeout > 0);
	return completion->done ? completion->done-- : 0;
}

static int gpr_send_pkt(gpr_device_t *gdev, const struct gpr_pkt *pkt);

static void *dev_get_drvdata(struct device *dev)
{
	return dev ? dev->drv : NULL;
}

static struct audioreach_graph_info *idr_find(struct fake_idr *idr,
					      unsigned int graph_id)
{
	idr->last_lookup = graph_id;
	idr->lookups++;
	if (idr->info && idr->info->graph_id == graph_id)
		return idr->info;
	return NULL;
}

static bool IS_ERR(const void *ptr)
{
	return ptr == NULL;
}

static long PTR_ERR(const void *ptr)
{
	(void)ptr;
	return -ENOMEM;
}

static void __attribute__((unused)) kfree(void *ptr)
{
	(void)ptr;
}

static struct gpr_pkt *audioreach_alloc_apm_cmd_pkt(size_t size,
						    uint32_t opcode,
						    uint32_t token)
{
	assert(size <= sizeof(alloc_packet.data) - GPR_HDR_SIZE);
	alloc_opcode = opcode;
	alloc_token = token;
	alloc_packet.hdr = (struct gpr_hdr) { .opcode = opcode, .token = token };
	return alloc_fail ? NULL : &alloc_packet;
}

#ifdef HAVE_BASELINE
static int q6apm_send_cmd_sync(struct q6apm *apm, struct gpr_pkt *pkt,
			       uint32_t opcode)
{
	return 0;
}
#endif

static void dev_err(struct device *dev, const char *fmt, ...)
{
	(void)dev;
	(void)fmt;
	error_logs++;
}

static void wake_up(wait_queue_head_t *wait)
{
	wait->wakes++;
}

static int audioreach_graph_mgmt_cmd(struct audioreach_graph *graph,
				     uint32_t opcode)
{
	int ret;

	(void)graph;
	if (opcode == APM_CMD_GRAPH_START) {
		ret = start_results[start_calls];
		start_calls++;
		if (!ret)
			dsp_running = 1;
		return ret;
	}

	assert(opcode == APM_CMD_GRAPH_STOP);
	dsp_running = 0;
	ret = stop_results[stop_calls];
	stop_calls++;
	assert(pthread_mutex_lock(&control_lock) == 0);
	stop_entered = true;
	assert(pthread_cond_broadcast(&stop_entered_cond) == 0);
	if (wait_for_start_attempt) {
		struct timespec deadline;

		assert(clock_gettime(CLOCK_REALTIME, &deadline) == 0);
		deadline.tv_sec += 2;
		while (!start_attempted) {
			int wait_ret = pthread_cond_timedwait(
				&start_attempted_cond, &control_lock, &deadline);

			assert(wait_ret == 0);
		}
	}
	assert(pthread_mutex_unlock(&control_lock) == 0);
	return ret;
}

static void reset_graph_commands(const int *stops, size_t stop_count,
				 const int *starts, size_t start_count)
{
	stop_calls = 0;
	start_calls = 0;
	dsp_running = 0;
	stop_entered = false;
	start_attempted = false;
	wait_for_start_attempt = false;
	start_thread_id = 0;
	memset(stop_results, 0, sizeof(stop_results));
	memset(start_results, 0, sizeof(start_results));
	memcpy(stop_results, stops, stop_count * sizeof(stop_results[0]));
	memcpy(start_results, starts, start_count * sizeof(start_results[0]));
}

static void init_graph(struct audioreach_graph *graph, int count)
{
	memset(graph, 0, sizeof(*graph));
	assert(pthread_mutex_init(&graph->start_stop_lock, NULL) == 0);
	graph->start_count = count;
}

static void destroy_graph(struct audioreach_graph *graph)
{
	assert(pthread_mutex_destroy(&graph->start_stop_lock) == 0);
}

static void reset_request(struct q6apm *apm,
			  struct audioreach_graph_info *info,
			  unsigned int graph_id)
{
	memset(apm, 0, sizeof(*apm));
	memset(info, 0, sizeof(*info));
	info->graph_id = graph_id;
	info->mem_map_handle = 0x11;
	info->pos_buf_mem_map_handle = 0x22;
	apm->graph_info_idr.info = info;
	alloc_fail = false;
	alloc_opcode = 0;
	alloc_token = UINT32_MAX;
	error_logs = 0;
}

static void run_callback(int (*callback)(const struct gpr_resp_pkt *, void *, int),
			 struct q6apm *apm, gpr_device_t *gdev,
			 uint32_t token, uint32_t result_opcode,
			 uint32_t result_status)
{
	struct gpr_ibasic_rsp_result_t response = {
		.opcode = result_opcode,
		.status = result_status,
	};
	struct gpr_resp_pkt packet = {
		.hdr = {
			.token = token,
			.opcode = GPR_BASIC_RSP_RESULT,
		},
		.payload = &response,
	};
	int ret;

	gdev->dev.drv = apm;
	ret = callback(&packet, gdev, 0);
	assert(ret == 0);
}

static unsigned int checks;
#define CHECK(expr) do { \
	checks++; \
	if (!(expr)) { \
		fprintf(stderr, "%s:%d CHECK failed: %s\n", \
			__FILE__, __LINE__, #expr); \
		exit(1); \
	} \
} while (0)
'''


TESTS = r'''
struct lifecycle_thread_arg {
	struct q6apm_graph *graph;
	int ret;
};

static void *run_stop_thread(void *opaque)
{
	struct lifecycle_thread_arg *arg = opaque;
	arg->ret = candidate_graph_stop(arg->graph);
	return NULL;
}

static void *run_start_thread(void *opaque)
{
	struct lifecycle_thread_arg *arg = opaque;

	assert(pthread_mutex_lock(&control_lock) == 0);
	start_thread_id = pthread_self();
	assert(pthread_mutex_unlock(&control_lock) == 0);
	arg->ret = candidate_graph_start(arg->graph);
	return NULL;
}

static void check_start_stop_sequences(void)
{
	struct audioreach_graph shared;
	struct q6apm_graph graph = {.ar_graph = &shared};
	int starts[4];
	int stops[4];

	/* START failure leaves count at zero; a later START reaches the DSP. */
	init_graph(&shared, 0);
	starts[0] = -ETIMEDOUT;
	starts[1] = 0;
	reset_graph_commands(stops, 0, starts, 2);
	CHECK(candidate_graph_start(&graph) == -ETIMEDOUT);
	CHECK(shared.start_count == 0);
	CHECK(start_calls == 1);
	CHECK(dsp_running == 0);
	CHECK(candidate_graph_start(&graph) == 0);
	CHECK(shared.start_count == 1);
	CHECK(start_calls == 2);
	CHECK(dsp_running == 1);

	/* Nested START increments without issuing another DSP START. */
	shared.start_count = 1;
	starts[0] = 0;
	reset_graph_commands(stops, 0, starts, 1);
	CHECK(candidate_graph_start(&graph) == 0);
	CHECK(shared.start_count == 2);
	CHECK(start_calls == 0);

	/* A nested STOP only drops the reference; the final STOP decrements after
	 * the synchronous DSP acknowledgement succeeds.
	 */
	stops[0] = 0;
	reset_graph_commands(stops, 1, starts, 1);
	shared.start_count = 2;
	dsp_running = 1;
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 1);
	CHECK(stop_calls == 0);
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(stop_calls == 1);
	CHECK(dsp_running == 0);

	/* STOP failure preserves the final reference so retry sends STOP again. */
	shared.start_count = 1;
	stops[0] = -EINVAL;
	stops[1] = 0;
	reset_graph_commands(stops, 2, starts, 1);
	dsp_running = 1;
	CHECK(candidate_graph_stop(&graph) == -EINVAL);
	CHECK(shared.start_count == 1);
	CHECK(stop_calls == 1);
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(stop_calls == 2);

	/* count=0 remains a no-op. */
	shared.start_count = 0;
	reset_graph_commands(stops, 1, starts, 1);
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(stop_calls == 0);

#ifdef HAVE_BASELINE
	/* Negative control: baseline loses its final reference on STOP error and
	 * therefore cannot retry the DSP command.
	 */
	shared.start_count = 1;
	stops[0] = -ETIMEDOUT;
	stops[1] = 0;
	reset_graph_commands(stops, 2, starts, 1);
	dsp_running = 1;
	CHECK(baseline_graph_stop(&graph) == -ETIMEDOUT);
	CHECK(shared.start_count == 0);
	CHECK(stop_calls == 1);
	CHECK(baseline_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(stop_calls == 1);
#endif
	destroy_graph(&shared);
}

static void check_concurrent_start_during_stop(void)
{
	struct audioreach_graph shared;
	struct q6apm_graph graph = {.ar_graph = &shared};
	struct lifecycle_thread_arg stop_arg = {.graph = &graph};
	struct lifecycle_thread_arg start_arg = {.graph = &graph};
	pthread_t stop_thread;
	pthread_t start_thread;
	struct timespec deadline;
	int starts[1] = {0};
	int stops[1] = {0};

	init_graph(&shared, 1);
	reset_graph_commands(stops, 1, starts, 1);
	dsp_running = 1;
	wait_for_start_attempt = true;

	CHECK(pthread_create(&stop_thread, NULL, run_stop_thread, &stop_arg) == 0);
	CHECK(pthread_mutex_lock(&control_lock) == 0);
	CHECK(clock_gettime(CLOCK_REALTIME, &deadline) == 0);
	deadline.tv_sec += 2;
	while (!stop_entered)
		CHECK(pthread_cond_timedwait(&stop_entered_cond, &control_lock,
					     &deadline) == 0);
	CHECK(pthread_mutex_unlock(&control_lock) == 0);

	/* STOP is still waiting for its DSP response.  START must wait for the
	 * shared lifecycle lock, then observe count=0 and issue a real DSP START.
	 */
	CHECK(pthread_create(&start_thread, NULL, run_start_thread, &start_arg) == 0);
	CHECK(pthread_join(stop_thread, NULL) == 0);
	CHECK(pthread_join(start_thread, NULL) == 0);
	CHECK(stop_arg.ret == 0);
	CHECK(start_arg.ret == 0);
	CHECK(stop_calls == 1);
	CHECK(start_calls == 1);
	CHECK(shared.start_count == 1);
	CHECK(dsp_running == 1);
	destroy_graph(&shared);
}

#ifdef HAVE_BASELINE
static void check_baseline_unmap(void)
{
	struct q6apm apm;
	struct audioreach_graph_info info;
	gpr_device_t gdev = {};
	struct device dev = { .parent = &gdev.dev };

	/* Negative controls: baseline reports the outer opcode and clears a
	 * normal mapping even when the DSP command failed. Its sender also
	 * leaves off the bit distinguishing position-buffer UNMAP.
	 */
	reset_request(&apm, &info, 120);
	gdev.dev.drv = &apm;
	CHECK(baseline_unmap_memory(&dev, 120, true) == 0);
	CHECK(alloc_opcode == APM_CMD_SHARED_MEM_UNMAP_REGIONS);
	CHECK(alloc_token == 120);
	run_callback(baseline_apm_callback, &apm, &gdev, 120,
		     APM_CMD_SHARED_MEM_UNMAP_REGIONS, 9);
	CHECK(apm.result.opcode == GPR_BASIC_RSP_RESULT);
	CHECK(apm.result.status == 0);
	CHECK(info.mem_map_handle == 0);

	reset_request(&apm, &info, 120);
	run_callback(baseline_apm_callback, &apm, &gdev,
		     120U | APM_MMAP_TOKEN_MAP_TYPE_POS_BUF,
		     APM_CMD_SHARED_MEM_UNMAP_REGIONS, 0);
	CHECK(apm.graph_info_idr.last_lookup ==
	      (120U | APM_MMAP_TOKEN_MAP_TYPE_POS_BUF));
	CHECK(info.mem_map_handle == 0x11);
	CHECK(info.pos_buf_mem_map_handle == 0x22);
}
#endif

static struct {
	u32 opcode;
	u32 stale_token;
	u32 token;
	u32 handle;
	u32 status;
	u32 *replace_handle;
	int send_error;
	unsigned int sends;
	bool wrong_opcode;
	bool respond;
} transaction;

static int gpr_send_pkt(gpr_device_t *gdev, const struct gpr_pkt *pkt)
{
	struct q6apm *apm = gdev->dev.drv;
	u32 opcode = pkt->hdr.opcode;
	u32 token = pkt->hdr.token;

	CHECK(pkt->hdr.src_port == gdev->svc_id);
	CHECK(opcode == transaction.opcode);
	CHECK(token != 0);
	CHECK(apm->reply_opcode == opcode);
	transaction.token = token;
	transaction.sends++;
	if (opcode == APM_CMD_SHARED_MEM_UNMAP_REGIONS) {
		const struct apm_cmd_shared_mem_unmap_regions *cmd =
			(const void *)pkt + GPR_HDR_SIZE;

		CHECK(cmd->mem_map_handle == transaction.handle);
	}
	if (transaction.send_error)
		return transaction.send_error;
	if (transaction.wrong_opcode) {
		u32 other = opcode == APM_CMD_GRAPH_STOP ?
			APM_CMD_SHARED_MEM_UNMAP_REGIONS : APM_CMD_GRAPH_STOP;

		run_callback(candidate_apm_callback, apm, gdev, token, other, 0);
		CHECK(apm->reply_done.done == 0);
	}
	if (transaction.stale_token) {
		CHECK(transaction.stale_token != token);
		run_callback(candidate_apm_callback, apm, gdev, transaction.stale_token,
			     opcode, 0);
		CHECK(apm->reply_done.done == 0);
	}
	if (transaction.respond) {
		if (transaction.replace_handle)
			*transaction.replace_handle = 0x33;
		run_callback(candidate_apm_callback, apm, gdev, token,
			     opcode, transaction.status);
		CHECK(apm->reply_done.done == 1);
		CHECK(apm->reply_status == transaction.status);
		/* A duplicate success must not replace the accepted DSP status. */
		run_callback(candidate_apm_callback, apm, gdev, token, opcode, 0);
		CHECK(apm->reply_done.done == 1);
		CHECK(apm->reply_status == transaction.status);
	}
	return sizeof(pkt->hdr);
}

static int send_stop(struct q6apm *apm, struct gpr_pkt *pkt)
{
	int ret;

	mutex_lock(&apm->lock);
	ret = q6apm_send_tracked_cmd(apm, pkt);
	mutex_unlock(&apm->lock);
	return ret;
}

static void check_stop_replies(void)
{
	gpr_device_t gdev = { .svc_id = 1 };
	struct q6apm apm = {
		.gdev = &gdev,
		.dev = &gdev.dev,
		.lock = PTHREAD_MUTEX_INITIALIZER,
		.reply_lock = PTHREAD_MUTEX_INITIALIZER,
	};
	struct gpr_pkt pkt = { .hdr.opcode = APM_CMD_GRAPH_STOP };

	gdev.dev.drv = &apm;
	transaction.opcode = APM_CMD_GRAPH_STOP;
	CHECK(send_stop(&apm, &pkt) == -ETIMEDOUT);
	CHECK(apm.reply_opcode == 0);
	transaction.stale_token = pkt.hdr.token;
	transaction.wrong_opcode = true;
	CHECK(send_stop(&apm, &pkt) == -ETIMEDOUT);
	CHECK(apm.reply_opcode == 0);
	transaction.respond = true;
	transaction.status = 9;
	CHECK(send_stop(&apm, &pkt) == -EINVAL);
	transaction.status = 0;
	CHECK(send_stop(&apm, &pkt) == 0);
	CHECK(apm.wait.wakes == 0);
	CHECK(apm.reply_opcode == 0);
	CHECK(pthread_mutex_destroy(&apm.reply_lock) == 0);
	CHECK(pthread_mutex_destroy(&apm.lock) == 0);
}

static void check_unmap_replies(void)
{
	gpr_device_t gdev = { .svc_id = 1 };
	struct device dev = { .parent = &gdev.dev };
	struct audioreach_graph_info info;
	struct q6apm apm;
	u32 first_token;

	reset_request(&apm, &info, 0);
	CHECK(pthread_mutex_init(&apm.lock, NULL) == 0);
	CHECK(pthread_mutex_init(&apm.reply_lock, NULL) == 0);
	apm.gdev = &gdev;
	apm.dev = &gdev.dev;
	gdev.dev.drv = &apm;
	memset(&transaction, 0, sizeof(transaction));
	transaction.opcode = APM_CMD_SHARED_MEM_UNMAP_REGIONS;
	transaction.handle = 0x11;
	CHECK(candidate_unmap_memory(&dev, 0, false) == -ETIMEDOUT);
	CHECK(apm.reply_opcode == 0);
	first_token = transaction.token;
	run_callback(candidate_apm_callback, &apm, &gdev, first_token,
		     APM_CMD_SHARED_MEM_UNMAP_REGIONS, 0);
	CHECK(info.mem_map_handle == 0x11);
	CHECK(apm.reply_done.done == 0);

	/* A delayed data-buffer reply cannot finish the position UNMAP. */
	transaction.stale_token = first_token;
	transaction.handle = 0x22;
	transaction.wrong_opcode = true;
	CHECK(candidate_unmap_memory(&dev, 0, true) == -ETIMEDOUT);
	CHECK(info.mem_map_handle == 0x11);
	CHECK(info.pos_buf_mem_map_handle == 0x22);

	/* Repeating the exact same data UNMAP gets a different token too. */
	transaction.handle = 0x11;
	transaction.respond = true;
	transaction.status = 9;
	CHECK(candidate_unmap_memory(&dev, 0, false) == -EINVAL);
	CHECK(info.mem_map_handle == 0x11);
	CHECK(info.pos_buf_mem_map_handle == 0x22);

	transaction.status = 0;
	transaction.replace_handle = &info.mem_map_handle;
	CHECK(candidate_unmap_memory(&dev, 0, false) == 0);
	CHECK(info.mem_map_handle == 0x33);
	transaction.replace_handle = NULL;
	transaction.handle = 0x33;
	CHECK(candidate_unmap_memory(&dev, 0, false) == 0);
	CHECK(info.mem_map_handle == 0);
	CHECK(info.pos_buf_mem_map_handle == 0x22);

	transaction.handle = 0x22;
	transaction.send_error = -EIO;
	CHECK(candidate_unmap_memory(&dev, 0, true) == -EIO);
	CHECK(apm.reply_opcode == 0);
	CHECK(info.pos_buf_mem_map_handle == 0x22);
	transaction.send_error = 0;
	CHECK(candidate_unmap_memory(&dev, 0, true) == 0);
	CHECK(info.pos_buf_mem_map_handle == 0);
	CHECK(apm.wait.wakes == 0);
	CHECK(transaction.sends == 7);
	CHECK(candidate_unmap_memory(&dev, 0, true) == 0);
	CHECK(candidate_unmap_memory(&dev, 1, false) == -ENODEV);
	CHECK(transaction.sends == 7);
	CHECK(pthread_mutex_destroy(&apm.reply_lock) == 0);
	CHECK(pthread_mutex_destroy(&apm.lock) == 0);
}

static void check_dpcm_stop_failure_recovery(void)
{
	struct snd_soc_dai_link link = { .name = "FE2" };
	struct device device = {};
	struct snd_soc_pcm_runtime fe = {
		.dai_link = &link,
		.dev = &device,
	};
	struct snd_pcm_substream substream = { .stream = SNDRV_PCM_STREAM_CAPTURE };
	int ret;

	memset(fe.dpcm, 0, sizeof(fe.dpcm));
	simulated_whole_clean_calls = 0;
	simulated_component_clean_calls = 0;
	simulated_dai_free_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;

	/* Failed FE STOP keeps the DAI and BE allocations alive. */
	simulated_component_result = -ETIMEDOUT;
	ret = simulate_dpcm_hw_free(&fe, &substream);
	CHECK(ret == -ETIMEDOUT);
	CHECK(fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
	CHECK(simulated_whole_clean_calls == 1);
	CHECK(simulated_dai_free_calls == 0);
	CHECK(simulated_be_free_calls == 0);

	/* A failed hw_params retry leaves the backend intact for another retry. */
	simulated_component_clean_calls = 0;
	ret = simulate_dpcm_hw_params_recovery(&fe, &substream);
	CHECK(ret == -ETIMEDOUT);
	CHECK(fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
	CHECK(simulated_component_clean_calls == 1);
	CHECK(simulated_component_rollback == 1);
	CHECK(simulated_component_retain);
	CHECK(simulated_dai_free_calls == 0);
	CHECK(simulated_be_free_calls == 0);
	CHECK(simulated_whole_clean_calls == 1);

	/* A successful retry releases the stale BE state and clears the guard. */
	simulated_component_result = 0;
	ret = simulate_dpcm_hw_params_recovery(&fe, &substream);
	CHECK(ret == 0);
	CHECK(!fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
	CHECK(fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].state == SND_SOC_DPCM_STATE_HW_FREE);
	CHECK(simulated_component_rollback == 1);
	CHECK(simulated_component_retain);
	CHECK(simulated_dai_free_calls == 1);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_whole_clean_calls == 1);

	/* Close drops the retained mark and finishes teardown despite STOP failure. */
	fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = true;
	simulated_component_result = -ETIMEDOUT;
	simulated_component_clean_calls = 0;
	simulated_dai_free_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;
	simulate_dpcm_shutdown_recovery(&fe, &substream);
	CHECK(simulated_component_clean_calls == 1);
	CHECK(simulated_component_rollback == 1);
	CHECK(!simulated_component_retain);
	CHECK(simulated_dai_free_calls == 1);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_be_shutdown_calls == 1);
	CHECK(!fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
}

static void check_dpcm_runtime_update_guard(void)
{
	struct device device = {};
	struct snd_soc_pcm_runtime first = {};
	struct snd_soc_pcm_runtime second = {};
	struct snd_soc_pcm_runtime *rtds[] = { &first, &second };
	struct snd_soc_card card = {
		.dev = &device,
		.rtds = rtds,
		.num_rtds = 2,
	};

	first.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = false;
	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = true;
	simulated_runtime_update_calls = 0;
	CHECK(snd_soc_dpcm_runtime_update(&card) == -EBUSY);
	CHECK(simulated_runtime_update_calls == 0);

	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = false;
	CHECK(snd_soc_dpcm_runtime_update(&card) == 0);
	CHECK(simulated_runtime_update_calls == 4);
}

static void check_failed_pcm_prepare(void)
{
	struct q6apm_dai_rtd prtd = { .state = Q6APM_STREAM_RUNNING };
	struct snd_pcm_runtime runtime = { .private_data = &prtd };
	struct snd_pcm_substream substream = { .runtime = &runtime };
	const int commands[] = { SNDRV_PCM_TRIGGER_START, SNDRV_PCM_TRIGGER_RESUME,
				 SNDRV_PCM_TRIGGER_PAUSE_RELEASE };

	for (int stream = SNDRV_PCM_STREAM_PLAYBACK; stream <= SNDRV_PCM_STREAM_CAPTURE;
	     stream++) {
		substream.stream = stream;
		for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); i++) {
			prtd.prepare_failed = true;
			CHECK(q6apm_dai_trigger(NULL, &substream, commands[i]) == -EPIPE);
			prtd.prepare_failed = false;
			CHECK(q6apm_dai_trigger(NULL, &substream, commands[i]) == 0);
		}
	}
}

int main(void)
{
	check_start_stop_sequences();
	check_concurrent_start_during_stop();
	check_stop_replies();
	check_unmap_replies();
	check_dpcm_stop_failure_recovery();
	check_dpcm_runtime_update_guard();
	check_failed_pcm_prepare();
#ifdef HAVE_BASELINE
	check_baseline_unmap();
	printf("PASS: %u checks against extracted candidate and baseline C\n",
	       checks);
#else
	printf("PASS: %u checks against extracted candidate C\n", checks);
#endif
	return 0;
}
'''


DPCM_SIM_PREAMBLE = r'''
struct snd_soc_dai_link {
	const char *name;
};

struct fake_dpcm_runtime {
	bool hw_free_failed;
	int state;
};

struct snd_soc_pcm_runtime {
	struct fake_dpcm_runtime dpcm[2];
	struct snd_soc_dai_link *dai_link;
	struct device *dev;
};

struct snd_soc_card {
	struct device *dev;
	struct snd_soc_pcm_runtime **rtds;
	int num_rtds;
};

#define SND_SOC_DPCM_UPDATE_FE 1
#define SND_SOC_DPCM_UPDATE_NO 0
#define SND_SOC_DPCM_STATE_HW_FREE 8
#define for_each_card_rtds(card, rtd) \
	for (int __i = 0; __i < (card)->num_rtds && \
	     (((rtd) = (card)->rtds[__i]), true); __i++)
#define for_each_pcm_streams(stream) for ((stream) = 0; (stream) < 2; (stream)++)

static int simulated_component_result;
static int simulated_whole_clean_calls;
static int simulated_component_clean_calls;
static int simulated_component_rollback;
static int simulated_component_retain;
static int simulated_dai_free_calls;
static int simulated_be_free_calls;
static int simulated_be_shutdown_calls;
static int simulated_runtime_update_calls;

static void dpcm_set_fe_update_state(struct snd_soc_pcm_runtime *fe,
				     int stream, int state)
{
	(void)fe;
	(void)stream;
	(void)state;
}

static int soc_pcm_hw_clean(struct snd_soc_pcm_runtime *fe,
			    struct snd_pcm_substream *substream,
			    int rollback, bool stop_on_component_error,
			    bool retain_failed)
{
	(void)fe;
	(void)substream;
	(void)rollback;
	(void)stop_on_component_error;
	(void)retain_failed;
	simulated_whole_clean_calls++;
	return simulated_component_result;
}

static int soc_pcm_hw_clean_components(struct snd_pcm_substream *substream,
				       int rollback, bool retain_failed)
{
	(void)substream;
	simulated_component_clean_calls++;
	simulated_component_rollback = rollback;
	simulated_component_retain = retain_failed;
	return simulated_component_result;
}

static void soc_pcm_hw_clean_dais(struct snd_soc_pcm_runtime *fe,
				 struct snd_pcm_substream *substream, int rollback)
{
	(void)fe;
	(void)substream;
	(void)rollback;
	simulated_dai_free_calls++;
}

static void dpcm_be_dai_hw_free(struct snd_soc_pcm_runtime *fe, int stream)
{
	(void)fe;
	(void)stream;
	simulated_be_free_calls++;
}

static void dpcm_be_dai_shutdown(struct snd_soc_pcm_runtime *fe, int stream)
{
	(void)fe;
	(void)stream;
	simulated_be_shutdown_calls++;
}

static void snd_soc_dpcm_mutex_unlock(void *lockable)
{
	(void)lockable;
}

static int soc_pcm_ret(struct snd_soc_pcm_runtime *fe, int ret)
{
	(void)fe;
	return ret;
}

static void snd_soc_dpcm_mutex_lock(struct snd_soc_card *card)
{
	(void)card;
}

static int soc_dpcm_fe_runtime_update(struct snd_soc_pcm_runtime *fe, int new)
{
	(void)fe;
	(void)new;
	simulated_runtime_update_calls++;
	return 0;
}

static int snd_soc_ret(struct device *dev, int ret, const char *fmt, ...)
{
	(void)dev;
	(void)fmt;
	return ret;
}

'''


def build_harness(extracted: dict[str, tuple[str, int]]) -> str:
    candidate_start, _ = extracted["candidate_start"]
    candidate_stop, _ = extracted["candidate_stop"]
    candidate_send_tracked, _ = extracted["candidate_send_tracked"]
    candidate_trigger, _ = extracted["candidate_trigger"]
    candidate_unmap, _ = extracted["candidate_unmap"]
    candidate_callback, _ = extracted["candidate_callback"]
    hw_free, _ = extracted["candidate_dpcm_hw_free"]
    hw_params, _ = extracted["candidate_dpcm_hw_params"]
    shutdown, _ = extracted["candidate_dpcm_shutdown"]
    hw_free_core_start = hw_free.find(
        "ret = soc_pcm_hw_clean(fe, substream, 0, true, true);"
    )
    hw_free_core_end = hw_free.find("\nout:", hw_free_core_start)
    if hw_free_core_start < 0 or hw_free_core_end < 0:
        raise ValueError("could not isolate DPCM FE hw_free state transition")
    hw_free_core = hw_free[hw_free_core_start:hw_free_core_end]
    hw_params_retry = extract_if_block(
        hw_params, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    shutdown_retry = extract_if_block(
        shutdown, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    runtime_update, _ = extracted["candidate_dpcm_runtime_update"]
    parts = [
        "#define HAVE_BASELINE 1" if "baseline_start" in extracted else "",
        PREAMBLE,
        "#define q6apm_graph_start candidate_graph_start",
        candidate_start,
        "#undef q6apm_graph_start",
        "#define q6apm_graph_stop candidate_graph_stop",
        candidate_stop,
        "#undef q6apm_graph_stop",
        candidate_send_tracked,
        candidate_trigger,
        "#define __q6apm_unmap_memory_fixed_region candidate_unmap_memory",
        candidate_unmap,
        "#undef __q6apm_unmap_memory_fixed_region",
        "#define apm_callback candidate_apm_callback",
        candidate_callback,
        "#undef apm_callback",
        DPCM_SIM_PREAMBLE,
        runtime_update,
        "static int simulate_dpcm_hw_free(struct snd_soc_pcm_runtime *fe, "
        "struct snd_pcm_substream *substream)\n{\n"
        "\tint stream = substream->stream;\n\tint ret;\n"
        + hw_free_core
        + "\nout:\n"
        "\tdpcm_set_fe_update_state(fe, stream, SND_SOC_DPCM_UPDATE_NO);\n"
        "\tsnd_soc_dpcm_mutex_unlock(fe);\n"
        "\treturn soc_pcm_ret(fe, ret);\n}\n",
        "static int simulate_dpcm_hw_params_recovery(struct snd_soc_pcm_runtime *fe, "
        "struct snd_pcm_substream *substream)\n{\n"
        "\tint ret = 0, stream = substream->stream;\n"
        + hw_params_retry
        + "\nout:\n\treturn ret;\n}\n",
        "static void simulate_dpcm_shutdown_recovery(struct snd_soc_pcm_runtime *fe, "
        "struct snd_pcm_substream *substream)\n{\n"
        "\tint stream = substream->stream;\n\tint ret = 0;\n"
        + shutdown_retry
        + "\tdpcm_be_dai_shutdown(fe, stream);\n}\n",
    ]
    if "baseline_start" in extracted:
        baseline_start, _ = extracted["baseline_start"]
        baseline_stop, _ = extracted["baseline_stop"]
        baseline_unmap, _ = extracted["baseline_unmap"]
        baseline_callback, _ = extracted["baseline_callback"]
        parts.extend(
            [
            "#define q6apm_graph_start baseline_graph_start",
            baseline_start,
            "#undef q6apm_graph_start",
                "#define q6apm_graph_stop baseline_graph_stop",
                baseline_stop,
                "#undef q6apm_graph_stop",
                "#define __q6apm_unmap_memory_fixed_region baseline_unmap_memory",
                baseline_unmap,
                "#undef __q6apm_unmap_memory_fixed_region",
                "#define apm_callback baseline_apm_callback",
                baseline_callback,
                "#undef apm_callback",
            ]
        )
    parts.append(TESTS)
    return "\n".join(parts)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_q6apm_c(path: Path, label: str) -> Path:
    candidate = path / Q6APM_C_REL if path.is_dir() else path
    if not candidate.is_file():
        raise SystemExit(
            f"{label}: q6apm.c not found at {candidate}; pass a kernel source "
            "tree or a direct q6apm.c path"
        )
    return candidate.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kernel_source_tree",
        type=Path,
        help="candidate Linux kernel source tree, or its direct q6apm.c path",
    )
    parser.add_argument(
        "--work",
        required=True,
        type=Path,
        help="writable directory retaining the generated C harness and binary",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="optional baseline kernel source tree or direct q6apm.c path",
    )
    parser.add_argument("--cc", default="cc")
    args = parser.parse_args()

    candidate = resolve_q6apm_c(args.kernel_source_tree, "candidate")
    candidate_header = candidate.with_name(Q6APM_H_REL.name)
    candidate_dai = candidate.with_name(Q6APM_DAI_NAME)
    if args.kernel_source_tree.is_dir():
        candidate_soc_pcm = args.kernel_source_tree / "sound/soc/soc-pcm.c"
        candidate_soc_component = args.kernel_source_tree / "sound/soc/soc-component.c"
    else:
        raise SystemExit(
            "DPCM teardown validation requires a kernel source tree; pass the "
            "tree directory instead of a direct q6apm.c path"
        )
    if not candidate_header.is_file():
        raise SystemExit(f"candidate header not found at {candidate_header}")
    if not candidate_soc_pcm.is_file():
        raise SystemExit(f"candidate DPCM source not found at {candidate_soc_pcm}")
    if not candidate_soc_component.is_file():
        raise SystemExit(f"candidate component source not found at {candidate_soc_component}")
    baseline = (
        resolve_q6apm_c(args.baseline, "baseline") if args.baseline else None
    )
    candidate_text = candidate.read_text(encoding="utf-8")
    candidate_header_text = candidate_header.read_text(encoding="utf-8")
    dai_text = candidate_dai.read_text(encoding="utf-8")
    soc_pcm_text = candidate_soc_pcm.read_text(encoding="utf-8")
    soc_component_text = candidate_soc_component.read_text(encoding="utf-8")
    baseline_text = baseline.read_text(encoding="utf-8") if baseline else None
    extracted = classify_sources(
        candidate_text,
        candidate_header_text,
        dai_text,
        soc_pcm_text,
        soc_component_text,
        baseline_text,
    )
    harness = build_harness(extracted)

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source_regression.c"
    binary = work / "source_regression"
    source.write_text(harness, encoding="utf-8")
    compile_cmd = [
        args.cc,
        "-std=c11",
        "-D_DEFAULT_SOURCE",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-unused-parameter",
        "-pthread",
        str(source),
        "-o",
        str(binary),
    ]
    compiled = subprocess.run(compile_cmd, text=True, capture_output=True)
    if compiled.returncode:
        sys.stderr.write(compiled.stdout)
        sys.stderr.write(compiled.stderr)
        sys.stderr.write(f"\nGenerated harness: {source}\n")
        return compiled.returncode
    try:
        tested = subprocess.run([str(binary)], text=True, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"AudioReach lifecycle harness timed out: {binary}\n")
        return 1
    sys.stdout.write(tested.stdout)
    sys.stderr.write(tested.stderr)
    if tested.returncode:
        return tested.returncode

    report = {
        "candidate": str(candidate),
        "candidate_sha256": digest(candidate),
        "candidate_header": str(candidate_header),
        "candidate_header_sha256": digest(candidate_header),
        "candidate_soc_pcm": str(candidate_soc_pcm.resolve()),
        "candidate_soc_pcm_sha256": digest(candidate_soc_pcm),
        "candidate_soc_component": str(candidate_soc_component.resolve()),
        "candidate_soc_component_sha256": digest(candidate_soc_component),
        "candidate_dai_sha256": digest(candidate_dai),
        "baseline_negative_controls": baseline is not None,
        "extracted": {
            name: {
                "line": line,
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
            for name, (body, line) in extracted.items()
        },
        "compiler": args.cc,
    }
    if baseline is not None:
        report.update(
            {
                "baseline": str(baseline),
                "baseline_sha256": digest(baseline),
            }
        )
    print(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
