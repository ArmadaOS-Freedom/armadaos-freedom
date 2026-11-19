#!/usr/bin/env python3
"""Host lifecycle checks for the TB322FC/SM8750 camera dependencies.

The generated harnesses compile real functions from the patched kernel tree.
They cover GDSC interconnect votes, partial genpd registration, QCOM flash
release, CSID startup and camera I2C device lifecycle. Hardware, interrupts
and firmware commits are outside the scope of the stubs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def extract_function(source: str, signature: str) -> str:
    """Return one exact braced C function, ignoring braces in strings/comments."""
    assert source.count(signature) == 1, signature
    start = source.index(signature)
    depth = 0
    state = "code"
    escaped = False
    index = start

    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if state == "code":
            if char == "/" and following == "/":
                state = "line-comment"
                index += 2
                continue
            if char == "/" and following == "*":
                state = "block-comment"
                index += 2
                continue
            if char == '"':
                state = "string"
                escaped = False
            elif char == "'":
                state = "character"
                escaped = False
            elif char == "{":
                depth += 1
            elif char == "}" and depth:
                depth -= 1
                if depth == 0:
                    return source[start:index + 1] + "\n"
        elif state == "line-comment":
            if char == "\n":
                state = "code"
        elif state == "block-comment":
            if char == "*" and following == "/":
                state = "code"
                index += 2
                continue
        elif state in ("string", "character"):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif (state == "string" and char == '"') or (
                    state == "character" and char == "'"):
                state = "code"
        index += 1

    raise AssertionError(f"unterminated function: {signature}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compiler_command() -> list[str]:
    command = shlex.split(os.environ.get("HOSTCC", "gcc"))
    if not command:
        raise RuntimeError("HOSTCC is empty")
    return command


def run_harness(work: Path, name: str, body: str) -> dict[str, object]:
    source = work / f"{name}.c"
    binary = work / name
    with source.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    compile_command = compiler_command() + [
        "-std=gnu11",
        "-UNDEBUG",
        "-O1",
        "-g",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-sign-compare",
        "-Wno-unused-function",
        "-Wno-unused-parameter",
        "-Wno-unused-variable",
        "-fsanitize=address,undefined",
        "-fno-sanitize-recover=undefined",
        "-fno-omit-frame-pointer",
        str(source),
        "-o",
        str(binary),
    ]
    compiled = subprocess.run(
        compile_command, text=True, capture_output=True, check=False)
    (work / f"{name}-compile.log").write_text(
        compiled.stdout + compiled.stderr, encoding="utf-8")
    if compiled.returncode:
        raise RuntimeError(
            f"{name} compile failed\n{compiled.stdout}{compiled.stderr}")

    executed = subprocess.run(
        [str(binary)], text=True, capture_output=True, check=False)
    (work / f"{name}-run.log").write_text(
        executed.stdout + executed.stderr, encoding="utf-8")
    if executed.returncode:
        raise RuntimeError(
            f"{name} failed\n{executed.stdout}{executed.stderr}")

    return {
        "name": name,
        "source": str(source),
        "source_sha256": sha256(source.read_bytes()),
        "compile_command": compile_command,
        "compile_exit": compiled.returncode,
        "test_exit": executed.returncode,
        "result": executed.stdout.strip(),
    }


GDSC_ICC_PREFIX = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

#define EPROBE_DEFER 517
#define BIT(i) (1UL << (i))
#define BITS_PER_LONG (sizeof(unsigned long) * 8)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define domain_to_gdsc(domain) container_of(domain, struct gdsc, pd)

struct device { int unused; };
struct generic_pm_domain { const char *name; };
struct icc_path { int id; unsigned int avg, peak; };
struct icc_bulk_data {
	struct icc_path *path;
	const char *name;
	unsigned int avg_bw, peak_bw;
};
struct mutex { bool locked; };
struct work_struct { void (*run)(struct work_struct *); };
struct delayed_work { struct work_struct work; bool pending, disabled; };
struct gdsc {
	struct generic_pm_domain pd;
	struct icc_bulk_data *icc_paths;
	unsigned int num_icc_paths;
	unsigned long icc_votes;
	struct device *icc_dev, *rpm_dev;
	bool needs_icc, rpm_held;
	struct mutex icc_lock;
	struct delayed_work icc_retry_work;
	unsigned int icc_retries;
	bool icc_off;
};
enum gdsc_status { GDSC_OFF, GDSC_ON };

static struct device dev;
static struct icc_path paths[2];
static struct icc_bulk_data bulk[2];
struct event { int path; unsigned int avg; };
static struct event events[32];
static int calls, fail_call, fail_call2, get_error, null_index, action_error;
static bool fail_drops;
static int error_count, warning_count, action_count;
static int off_status, hw_enable_error, hw_disable_error, hw_calls;
static int rpm_error, rpm_gets, rpm_puts;

static int errlog(struct device *d, const char *fmt, ...)
{
	(void)d;
	(void)fmt;
	error_count++;
	return 0;
}
static int warnlog(struct device *d, const char *fmt, ...)
{
	(void)d;
	(void)fmt;
	warning_count++;
	return 0;
}
static int probelog(struct device *d, int ret, const char *fmt, ...)
{
	(void)d;
	(void)fmt;
	error_count++;
	return ret;
}
#define dev_err errlog
#define dev_warn warnlog
#define dev_err_probe probelog

#define mutex_init(lock) (*(lock) = (struct mutex){0})
#define INIT_DELAYED_WORK(item, callback) \
	(*(item) = (struct delayed_work){ .work = {callback} })
#define to_delayed_work(item) container_of(item, struct delayed_work, work)
#define msecs_to_jiffies(ms) (ms)
#define system_freezable_wq 1

static void mutex_lock(struct mutex *lock)
{
	assert(!lock->locked);
	lock->locked = true;
}

static void mutex_unlock(struct mutex *lock)
{
	assert(lock->locked);
	lock->locked = false;
}

static bool queue_delayed_work(int queue, struct delayed_work *item, int delay)
{
	assert(queue == system_freezable_wq && delay > 0);
	if (item->pending || item->disabled)
		return false;
	item->pending = true;
	return true;
}

static void cancel_delayed_work(struct delayed_work *item)
{
	item->pending = false;
}

static void disable_delayed_work_sync(struct delayed_work *item)
{
	item->pending = false;
	item->disabled = true;
}

static void run_pending_retry(struct gdsc *domain)
{
	struct delayed_work *item = &domain->icc_retry_work;
	assert(item->pending && !item->disabled);
	item->pending = false;
	item->work.run(&item->work);
}

static int icc_set_bw(struct icc_path *p, unsigned int a, unsigned int b)
{
	assert(p);
	assert(calls < (int)ARRAY_SIZE(events));
	events[calls] = (struct event){p->id, a};
	calls++;
	if (calls == fail_call || calls == fail_call2 || (!a && fail_drops))
		return -EIO;
	p->avg = a;
	p->peak = b;
	return 0;
}

static int devm_of_icc_bulk_get(
	struct device *d, int n, struct icc_bulk_data *b)
{
	(void)d;
	if (get_error)
		return get_error;
	for (int i = 0; i < n; i++)
		b[i].path = i == null_index ? NULL : &paths[i];
	return 0;
}

static int devm_add_action_or_reset(
	struct device *d, void (*action)(void *), void *data)
{
	(void)d;
	action_count++;
	if (action_error) {
		action(data);
		return action_error;
	}
	return 0;
}

static int gdsc_check_status(struct gdsc *s, enum gdsc_status state)
{
	(void)s;
	assert(state == GDSC_OFF);
	return off_status;
}

static int gdsc_enable_hw(struct gdsc *s)
{
	if (s->num_icc_paths)
		assert(s->icc_votes == 3);
	hw_calls++;
	return hw_enable_error;
}

static int gdsc_disable_hw(struct gdsc *s, struct generic_pm_domain *d)
{
	(void)s;
	(void)d;
	hw_calls++;
	return hw_disable_error;
}

static int pm_runtime_resume_and_get(struct device *d)
{
	(void)d;
	rpm_gets++;
	return rpm_error;
}

static void pm_runtime_put(struct device *d)
{
	(void)d;
	rpm_puts++;
}

static struct gdsc fresh(unsigned int n)
{
	memset(paths, 0, sizeof(paths));
	memset(events, 0, sizeof(events));
	for (int i = 0; i < 2; i++) {
		paths[i].id = i;
		bulk[i] = (struct icc_bulk_data){
			NULL, i ? "hf_mnoc" : "ahb", 1, 1
		};
	}
	calls = fail_call = fail_call2 = get_error = action_error = 0;
	fail_drops = false;
	error_count = warning_count = action_count = 0;
	hw_calls = hw_enable_error = hw_disable_error = 0;
	rpm_error = rpm_gets = rpm_puts = 0;
	null_index = -1;
	off_status = 1;
	struct gdsc s = {
		.pd = {"titan"},
		.icc_paths = bulk,
		.num_icc_paths = n,
		.icc_dev = &dev,
	};
	return s;
}
"""


GDSC_ICC_TESTS = r"""
int main(void)
{
	struct gdsc s;

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(action_count == 1);
	assert(!gdsc_enable(&s.pd));
	assert(s.icc_votes == 3 && calls == 2 && hw_calls == 1);
	assert(paths[0].avg == 1 && paths[1].peak == 1);
	assert(!gdsc_disable(&s.pd));
	assert(s.icc_votes == 0 && calls == 4 && hw_calls == 2);
	assert(events[2].path == 1 && events[2].avg == 0);
	assert(events[3].path == 0 && events[3].avg == 0);
	puts("PASS normal ON/OFF: vote before HW, reverse drop after confirmed OFF");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	fail_call = 1;
	assert(gdsc_enable(&s.pd) == -EIO);
	assert(hw_calls == 0 && s.icc_votes == 0);
	puts("PASS first vote error aborts enable");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	fail_call = 2;
	assert(gdsc_enable(&s.pd) == -EIO);
	assert(hw_calls == 0 && calls == 3 && s.icc_votes == 0);
	assert(events[2].path == 0 && events[2].avg == 0);
	puts("PASS second vote error rolls back only the partial transaction");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	fail_call = 2;
	fail_call2 = 3;
	assert(gdsc_enable(&s.pd) == -EIO);
	assert(s.icc_votes == 1 && error_count == 1);
	fail_call = fail_call2 = 0;
	assert(!gdsc_icc_enable(&s));
	assert(s.icc_votes == 3 && calls == 4);
	assert(!gdsc_icc_drop(&s, s.icc_votes));
	assert(s.icc_votes == 0);
	puts("PASS failed rollback retains and later releases the tracked vote");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	int before = calls;
	assert(!gdsc_icc_enable(&s) && calls == before && s.icc_votes == 3);
	puts("PASS provider pre-vote is idempotent");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	hw_enable_error = -ETIMEDOUT;
	assert(gdsc_enable(&s.pd) == -ETIMEDOUT);
	assert(s.icc_votes == 0 && calls == 4);
	puts("PASS failed HW enable drops votes after confirmed OFF");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	hw_enable_error = -ETIMEDOUT;
	off_status = 0;
	assert(gdsc_enable(&s.pd) == -ETIMEDOUT);
	assert(s.icc_votes == 3 && calls == 2 && error_count == 1);
	hw_enable_error = 0;
	off_status = 1;
	assert(!gdsc_disable(&s.pd));
	assert(s.icc_votes == 0);
	puts("PASS failed HW enable retains votes until a later confirmed OFF");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	off_status = 0;
	assert(!gdsc_disable(&s.pd) && s.icc_votes == 3 && warning_count == 1);
	assert(!s.icc_retry_work.pending);
	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	off_status = -EIO;
	assert(!gdsc_disable(&s.pd) && s.icc_votes == 3 && warning_count == 1);
	assert(!s.icc_retry_work.pending);
	puts("PASS unconfirmed hardware OFF cannot drop or schedule ICC votes");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	fail_call = 3;
	assert(!gdsc_disable(&s.pd));
	assert(s.icc_votes == 2 && error_count == 1);
	fail_call = 0;
	run_pending_retry(&s);
	assert(s.icc_votes == 0 && hw_calls == 1 && !s.icc_retry_work.pending);
	puts("PASS queued work drops failed votes without a second power-off");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	fail_drops = true;
	assert(!gdsc_disable(&s.pd));
	int retries = 0;
	while (s.icc_retry_work.pending) {
		assert(retries++ < 3);
		run_pending_retry(&s);
	}
	assert(retries == 3 && s.icc_votes == 3 && hw_calls == 1);
	gdsc_icc_release(&s);
	assert(s.icc_retry_work.disabled && !s.icc_retry_work.pending);
	puts("PASS persistent drop failure bounds retries and teardown stops work");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	fail_call = 3;
	assert(!gdsc_disable(&s.pd) && s.icc_retry_work.pending);
	assert(!gdsc_enable(&s.pd) && !s.icc_retry_work.pending);
	before = calls;
	s.icc_retry_work.work.run(&s.icc_retry_work.work);
	assert(s.icc_votes == 3 && calls == before && hw_calls == 2);
	puts("PASS re-enable cancels retry and a dequeued callback cannot drop votes");

	s = fresh(2);
	get_error = -EPROBE_DEFER;
	assert(gdsc_icc_get(&dev, &s) == -EPROBE_DEFER);
	assert(action_count == 0 && calls == 0);
	s = fresh(2);
	null_index = 1;
	assert(gdsc_icc_get(&dev, &s) == -ENODEV);
	assert(action_count == 0 && calls == 0);
	s = fresh(2);
	s.needs_icc = true;
	assert(gdsc_icc_get(&dev, &s) == -EINVAL && calls == 0);
	s = fresh(BITS_PER_LONG + 1);
	assert(gdsc_icc_get(&dev, &s) == -EINVAL && calls == 0);
	puts("PASS missing/deferred/conflicting/excess path setup fails visibly");

	s = fresh(2);
	action_error = -ENOMEM;
	assert(gdsc_icc_get(&dev, &s) == -ENOMEM && calls == 0);
	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_icc_enable(&s));
	off_status = 0;
	gdsc_icc_release(&s);
	assert(warning_count == 1 && s.icc_votes == 0 && calls == 4);
	puts("PASS devres action failure and release boundaries drop all votes");

	s = fresh(0);
	assert(!gdsc_icc_get(&dev, &s));
	assert(action_count == 0 && calls == 0);
	assert(!gdsc_enable(&s.pd));
	assert(!gdsc_disable(&s.pd));
	assert(calls == 0 && hw_calls == 2);
	s = fresh(0);
	s.needs_icc = true;
	assert(!gdsc_icc_get(&dev, &s));
	assert(!gdsc_enable(&s.pd));
	assert(!gdsc_disable(&s.pd));
	assert(calls == 0 && hw_calls == 2);
	puts("PASS zero paths and legacy single-ICC configurations are unchanged");

	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	s.rpm_dev = &dev;
	rpm_error = -EIO;
	assert(gdsc_enable(&s.pd) == -EIO);
	assert(rpm_gets == 1 && calls == 0 && hw_calls == 0);
	s = fresh(2);
	assert(!gdsc_icc_get(&dev, &s));
	s.rpm_dev = &dev;
	fail_call = 1;
	assert(gdsc_enable(&s.pd) == -EIO);
	assert(rpm_gets == 1 && rpm_puts == 1);
	assert(!s.rpm_held && hw_calls == 0);
	puts("PASS runtime-PM supplier failures balance refs and skip hardware");
	return 0;
}
"""


GDSC_REGISTER_PREFIX = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>

#define ENODEV 19
#define EPROBE_DEFER 517
#define GFP_KERNEL 0
#define GDSC_ON 1
#define GDSC_OFF 0
#define PWRSTS_ON 4
#define IS_ERR(p) ((uintptr_t)(p) > (uintptr_t)-4096)
#define IS_ERR_OR_NULL(p) (!(p) || IS_ERR(p))
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))

struct generic_pm_domain { int id; const char *name; };
struct device { struct generic_pm_domain *pm_domain; void *of_node; };
struct gdsc {
	int id;
	bool needs_icc, rpm_held;
	void *icc_path;
	unsigned int icc_path_index;
	const char *supply;
	void *rsupply;
	void *regmap, *rcdev;
	int pwrsts, num_icc_paths;
	unsigned long icc_votes;
	int icc_lock;
	struct device *rpm_dev;
	struct generic_pm_domain pd, *parent;
};
struct genpd_onecell_data {
	struct generic_pm_domain **domains;
	size_t num_domains;
};
struct gdsc_desc {
	struct device *dev;
	struct gdsc **scs;
	size_t num;
	void *pd_list;
};
struct reset_controller_dev {};
struct regmap {};
struct fault {
	int get, vote, init, action, subdomain;
	int publish;
};

static struct fault fault;
static struct gdsc domains[3];
static struct gdsc *pdomains[3];
static struct device dev;
static bool active[3], linked[3];
static int removed[3], remove_count, unlink_count, publish_calls, hw_init_calls;
static int release_calls, allocation_count;
static void *allocations[4];

static void *devm_kzalloc(struct device *d, size_t n, int flags)
{
	(void)d;
	(void)flags;
	void *p = calloc(1, n);
	allocations[allocation_count++] = p;
	return p;
}
static void *devm_kcalloc(
	struct device *d, size_t n, size_t size, int flags)
{
	return devm_kzalloc(d, n * size, flags);
}
static int gdsc_icc_get(struct device *d, struct gdsc *s)
{
	(void)d;
	return s->id == fault.get ? -EPROBE_DEFER : 0;
}
static void *devm_of_icc_get_by_index(struct device *d, unsigned int i)
{
	(void)d;
	(void)i;
	return NULL;
}
static void *devm_regulator_get_optional(struct device *d, const char *name)
{
	(void)d;
	(void)name;
	return NULL;
}
static int gdsc_icc_enable(struct gdsc *s)
{
	if (s->id == fault.vote)
		return -EIO;
	s->icc_votes = 3;
	return 0;
}
static void mutex_lock(int *lock)
{
	assert(!*lock);
	*lock = 1;
}
static void mutex_unlock(int *lock)
{
	assert(*lock);
	*lock = 0;
}
static void gdsc_icc_mark_off(struct gdsc *s)
{
	assert(s->icc_lock);
	s->icc_votes = 0;
}
static int gdsc_init(struct gdsc *s)
{
	hw_init_calls++;
	if (s->id == fault.init)
		return -EIO;
	assert(!active[s->id]);
	active[s->id] = true;
	return 0;
}
static int gdsc_check_status(struct gdsc *s, int state)
{
	(void)s;
	return state == GDSC_ON ? 0 : 1;
}
static bool of_machine_is_compatible(const char *name)
{
	(void)name;
	return true;
}
static bool pm_runtime_enabled(struct device *d)
{
	(void)d;
	return true;
}
static void pm_runtime_get_noresume(struct device *d) { (void)d; }
static void gdsc_rpm_release(void *data) { (void)data; release_calls++; }
static int devm_add_action_or_reset(
	struct device *d, void (*action)(void *), void *data)
{
	(void)d;
	struct gdsc *s = data;
	if (s->id == fault.action) {
		action(data);
		return -ENOMEM;
	}
	return 0;
}
static int dev_err_probe(
	struct device *d, int err, const char *fmt, ...)
{
	(void)d;
	(void)fmt;
	return err;
}
static int pm_genpd_add_subdomain(
	struct generic_pm_domain *parent, struct generic_pm_domain *child)
{
	(void)parent;
	if (child->id == fault.subdomain)
		return -EIO;
	assert(active[child->id]);
	linked[child->id] = true;
	return 0;
}
static struct generic_pm_domain *pd_to_genpd(struct generic_pm_domain *pd)
{
	return pd;
}
static int gdsc_add_subdomain_list(
	void *pd_list, struct generic_pm_domain *child)
{
	(void)pd_list;
	return pm_genpd_add_subdomain(NULL, child);
}
static int pm_genpd_remove(struct generic_pm_domain *pd)
{
	assert(active[pd->id]);
	assert(!linked[pd->id]);
	active[pd->id] = false;
	removed[remove_count++] = pd->id;
	return 0;
}
static void gdsc_pm_subdomain_remove(struct gdsc_desc *desc, size_t num)
{
	for (int i = (int)num - 1; i >= 0; i--) {
		if (desc->scs[i]) {
			assert(linked[i]);
			linked[i] = false;
			unlink_count++;
		}
	}
}
static int of_genpd_add_provider_onecell(
	void *node, struct genpd_onecell_data *data)
{
	(void)node;
	(void)data;
	publish_calls++;
	return fault.publish;
}

static struct gdsc_desc fresh(void)
{
	while (allocation_count)
		free(allocations[--allocation_count]);
	fault = (struct fault){-1, -1, -1, -1, -1, 0};
	for (int i = 0; i < 3; i++) {
		domains[i] = (struct gdsc){
			.id = i,
			.pd = {i, "titan"},
			.num_icc_paths = 2,
		};
		pdomains[i] = &domains[i];
		active[i] = linked[i] = false;
		removed[i] = -1;
	}
	remove_count = unlink_count = publish_calls = 0;
	hw_init_calls = release_calls = 0;
	return (struct gdsc_desc){&dev, pdomains, 3, &dev};
}
"""


GDSC_REGISTER_TESTS = r"""
int main(void)
{
	struct gdsc_desc d = fresh();
	fault.get = 2;
	assert(gdsc_register(&d, NULL, NULL) == -EPROBE_DEFER);
	assert(remove_count == 0 && hw_init_calls == 0);
	puts("PASS ICC acquisition failure occurs before domain initialization");

	for (int i = 0; i < 3; i++) {
		d = fresh();
		fault.vote = i;
		assert(gdsc_register(&d, NULL, NULL) == -EIO);
		assert(remove_count == i && publish_calls == 0 && unlink_count == 0);
		for (int j = 0; j < i; j++)
			assert(removed[j] == i - 1 - j);
		for (int j = 0; j < 3; j++)
			assert(!active[j]);
	}
	puts("PASS each ICC vote failure unwinds earlier domains in reverse");

	for (int i = 0; i < 3; i++) {
		d = fresh();
		fault.init = i;
		assert(gdsc_register(&d, NULL, NULL) == -EIO);
		assert(remove_count == i && publish_calls == 0);
		for (int j = 0; j < 3; j++)
			assert(!active[j]);
	}
	puts("PASS domain init failure unwinds initialized predecessors");

	for (int i = 0; i < 3; i++) {
		d = fresh();
		fault.action = i;
		assert(gdsc_register(&d, NULL, NULL) == -ENOMEM);
		assert(remove_count == i + 1 && release_calls == 1);
		for (int j = 0; j < 3; j++)
			assert(!active[j]);
	}
	puts("PASS devres action failure includes the current domain in unwind");

	for (int i = 0; i < 3; i++) {
		d = fresh();
		fault.subdomain = i;
		assert(gdsc_register(&d, NULL, NULL) == -EIO);
		assert(remove_count == 3 && unlink_count == i && publish_calls == 0);
		for (int j = 0; j < 3; j++)
			assert(!active[j] && !linked[j]);
	}
	puts("PASS subdomain link failure removes links before domains");

	d = fresh();
	fault.publish = -ENOMEM;
	assert(gdsc_register(&d, NULL, NULL) == -ENOMEM);
	assert(publish_calls == 1 && unlink_count == 3 && remove_count == 3);
	for (int i = 0; i < 3; i++)
		assert(!active[i] && !linked[i]);
	puts("PASS provider publication failure unwinds links and domains");

	d = fresh();
	assert(!gdsc_register(&d, NULL, NULL));
	assert(!remove_count && !unlink_count && publish_calls == 1);
	assert(hw_init_calls == 3);
	for (int i = 0; i < 3; i++)
		assert(active[i] && linked[i]);
	puts("PASS successful registration keeps all domains and links");

	d = fresh();
	fault.vote = 2;
	assert(gdsc_register(&d, NULL, NULL) == -EIO);
	fault.vote = -1;
	assert(!gdsc_register(&d, NULL, NULL));
	puts("PASS retry after failed provider setup does not duplicate a domain");

	while (allocation_count)
		free(allocations[--allocation_count]);
	return 0;
}
"""


FLASH_PREFIX = r"""
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef unsigned char u8;
struct v4l2_flash { unsigned int id; };
struct regmap_field { int unused; };
enum { REG_TORCH_CLAMP };
struct qcom_flash_data {
	struct v4l2_flash **v4l2_flash;
	struct regmap_field *r_fields[1];
	unsigned int torch_clamp;
	int lock;
	u8 leds_count;
};
struct platform_device { struct qcom_flash_data *data; };

#define MAX_TEST_LEDS 3
static struct v4l2_flash *release_order[MAX_TEST_LEDS];
static unsigned int release_count;
static unsigned int release_per_led[MAX_TEST_LEDS];
static struct regmap_field clamp_field;
static unsigned int clamp_writes, releases_at_mutex_destroy;
static int clamp_error;

static int regmap_field_write(struct regmap_field *field, unsigned int value)
{
	assert(field == &clamp_field && value == 23);
	clamp_writes++;
	return clamp_error;
}

static struct qcom_flash_data *platform_get_drvdata(struct platform_device *pdev)
{
	return pdev->data;
}

static void mutex_destroy(int *lock)
{
	assert(!*lock);
	*lock = 1;
	releases_at_mutex_destroy = release_count;
}

static void v4l2_flash_release(struct v4l2_flash *flash)
{
	if (!flash || flash->id >= MAX_TEST_LEDS ||
	    release_count >= MAX_TEST_LEDS) {
		fprintf(stderr, "invalid release\n");
		abort();
	}
	release_order[release_count++] = flash;
	release_per_led[flash->id]++;
}

static void reset_case(
	struct qcom_flash_data *data,
	struct v4l2_flash *storage[MAX_TEST_LEDS])
{
	memset(data, 0, sizeof(*data));
	memset(storage, 0, sizeof(*storage) * MAX_TEST_LEDS);
	memset(release_order, 0, sizeof(release_order));
	memset(release_per_led, 0, sizeof(release_per_led));
	release_count = 0;
	clamp_writes = releases_at_mutex_destroy = 0;
	clamp_error = 0;
	data->r_fields[REG_TORCH_CLAMP] = &clamp_field;
	data->torch_clamp = 23;
	data->v4l2_flash = storage;
}
"""


FLASH_TESTS = r"""
static void check_cleared(
	struct qcom_flash_data *data,
	struct v4l2_flash *storage[MAX_TEST_LEDS])
{
	if (data->leds_count != 0)
		abort();
	for (int i = 0; i < MAX_TEST_LEDS; i++)
		if (storage[i] != NULL)
			abort();
}

int main(void)
{
	struct qcom_flash_data data;
	struct v4l2_flash *storage[MAX_TEST_LEDS];

	reset_case(&data, storage);
	qcom_flash_release_v4l2(&data);
	qcom_flash_release_v4l2(&data);
	if (release_count != 0)
		abort();
	check_cleared(&data, storage);
	puts("PASS zero LEDs: NULL-count state is safe and idempotent");

	struct v4l2_flash led0 = { .id = 0 };
	reset_case(&data, storage);
	storage[0] = &led0;
	data.leds_count = 1;
	qcom_flash_release_v4l2(&data);
	if (release_count != 1 || release_order[0] != &led0 ||
	    release_per_led[0] != 1)
		abort();
	check_cleared(&data, storage);
	puts("PASS one LED: first object is released exactly once");

	struct v4l2_flash led1 = { .id = 1 };
	reset_case(&data, storage);
	storage[0] = &led0;
	storage[1] = &led1;
	data.leds_count = 2;
	qcom_flash_release_v4l2(&data);
	if (release_count != 2 ||
	    release_order[0] != &led1 || release_order[1] != &led0 ||
	    release_per_led[0] != 1 || release_per_led[1] != 1)
		abort();
	check_cleared(&data, storage);
	qcom_flash_release_v4l2(&data);
	if (release_count != 2)
		abort();
	puts("PASS two LEDs: reverse order, no OOB, NULLed, idempotent");

	struct v4l2_flash led2 = { .id = 2 };
	reset_case(&data, storage);
	storage[0] = &led0;
	storage[1] = NULL;
	storage[2] = &led2;
	data.leds_count = 3;
	qcom_flash_release_v4l2(&data);
	if (release_count != 2 ||
	    release_order[0] != &led2 || release_order[1] != &led0 ||
	    release_per_led[0] != 1 || release_per_led[2] != 1)
		abort();
	check_cleared(&data, storage);
	puts("PASS disabled V4L2 child entries are skipped without truncation");

	reset_case(&data, storage);
	storage[0] = &led0;
	storage[1] = &led1;
	data.leds_count = 2;
	clamp_error = -EIO;
	assert(finish_flash_probe(&data) == -EIO);
	assert(clamp_writes == 1 && release_count == 2);
	assert(release_order[0] == &led1 && release_order[1] == &led0);
	check_cleared(&data, storage);
	puts("PASS failed torch-clamp write releases the registered V4L2 LEDs");

	reset_case(&data, storage);
	storage[0] = &led0;
	storage[1] = &led1;
	data.leds_count = 2;
	assert(finish_flash_probe(&data) == 0);
	assert(clamp_writes == 1 && release_count == 0 && data.leds_count == 2);
	assert(storage[0] == &led0 && storage[1] == &led1);
	qcom_flash_led_remove(&(struct platform_device){ .data = &data });
	assert(data.lock == 1 && releases_at_mutex_destroy == 2);
	check_cleared(&data, storage);
	puts("PASS successful probe keeps the LEDs registered until removal");
	return 0;
}
"""


CSID_PREFIX = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef uint8_t u8;
typedef uint32_t u32;

#define EPROBE_DEFER 517

struct device { int unused; };
struct camss { struct device *dev; };
struct v4l2_subdev { void *private_data; };
struct regulator_bulk_data { int dummy; };
struct camss_clock { int dummy; };
struct csid_device;
struct parent_dev_ops {
	int (*get)(struct camss *camss, int id);
	int (*put)(struct camss *camss, int id);
};
struct csid_hw_ops {
	int (*reset)(struct csid_device *csid);
	void (*hw_version)(struct csid_device *csid);
};
struct csid_subdev_resources {
	const struct parent_dev_ops *parent_dev_ops;
	const struct csid_hw_ops *hw_ops;
};
struct csid_phy_config { u8 need_vc_update; };
struct csid_device {
	struct camss *camss;
	u8 id;
	struct v4l2_subdev subdev;
	struct camss_clock *clock;
	int nclocks;
	struct regulator_bulk_data *supplies;
	int num_supplies;
	u32 irq;
	struct csid_phy_config phy;
	const struct csid_subdev_resources *res;
};

enum fail_phase {
	FAIL_NONE,
	FAIL_PARENT_GET,
	FAIL_RUNTIME_PM,
	FAIL_REGULATOR,
	FAIL_CLOCK_RATE,
	FAIL_CLOCK_ENABLE,
	FAIL_RESET,
};

static enum fail_phase fail_phase;
static int parent_gets, parent_puts, pm_gets, pm_puts;
static int regulator_enables, regulator_disables;
static int clock_rate_sets, clock_enables, clock_disables;
static int irq_enables, irq_disables, reset_calls, version_calls;
static int cleanup_step, regulator_disable_step, pm_put_step;

static void *v4l2_get_subdevdata(struct v4l2_subdev *sd)
{
	return sd->private_data;
}
static int parent_get(struct camss *camss, int id)
{
	(void)camss;
	(void)id;
	parent_gets++;
	return fail_phase == FAIL_PARENT_GET ? -EPROBE_DEFER : 0;
}
static int parent_put(struct camss *camss, int id)
{
	(void)camss;
	(void)id;
	parent_puts++;
	return 0;
}
static int pm_runtime_resume_and_get(struct device *dev)
{
	(void)dev;
	pm_gets++;
	return fail_phase == FAIL_RUNTIME_PM ? -ETIMEDOUT : 0;
}
static int pm_runtime_put_sync(struct device *dev)
{
	(void)dev;
	pm_puts++;
	pm_put_step = ++cleanup_step;
	return 0;
}
static int regulator_bulk_enable(
	int count, struct regulator_bulk_data *supplies)
{
	(void)count;
	(void)supplies;
	regulator_enables++;
	return fail_phase == FAIL_REGULATOR ? -EIO : 0;
}
static int regulator_bulk_disable(
	int count, struct regulator_bulk_data *supplies)
{
	(void)count;
	(void)supplies;
	regulator_disables++;
	regulator_disable_step = ++cleanup_step;
	return 0;
}
static int csid_set_clock_rates(struct csid_device *csid)
{
	(void)csid;
	clock_rate_sets++;
	return fail_phase == FAIL_CLOCK_RATE ? -ERANGE : 0;
}
static int camss_enable_clocks(
	int count, struct camss_clock *clock, struct device *dev)
{
	(void)count;
	(void)clock;
	(void)dev;
	clock_enables++;
	return fail_phase == FAIL_CLOCK_ENABLE ? -EIO : 0;
}
static void camss_disable_clocks(int count, struct camss_clock *clock)
{
	(void)count;
	(void)clock;
	clock_disables++;
}
static void enable_irq(u32 irq)
{
	(void)irq;
	irq_enables++;
}
static void disable_irq(u32 irq)
{
	(void)irq;
	irq_disables++;
}
static int hw_reset(struct csid_device *csid)
{
	(void)csid;
	reset_calls++;
	return fail_phase == FAIL_RESET ? -ETIMEDOUT : 0;
}
static void hw_version(struct csid_device *csid)
{
	(void)csid;
	version_calls++;
}
static struct parent_dev_ops parent_ops = { parent_get, parent_put };
static struct csid_hw_ops hw_ops = { hw_reset, hw_version };
static struct csid_subdev_resources resources = {
	.parent_dev_ops = &parent_ops,
	.hw_ops = &hw_ops,
};

static void reset_counts(void)
{
	fail_phase = FAIL_NONE;
	parent_gets = parent_puts = pm_gets = pm_puts = 0;
	regulator_enables = regulator_disables = 0;
	clock_rate_sets = clock_enables = clock_disables = 0;
	irq_enables = irq_disables = reset_calls = version_calls = 0;
	cleanup_step = regulator_disable_step = pm_put_step = 0;
}

#define CHECK(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "%s:%d: CHECK failed: %s\n", \
			__FILE__, __LINE__, #condition); \
		exit(1); \
	} \
} while (0)
"""


CSID_TESTS = r"""
int main(void)
{
	struct camss camss = { .dev = &(struct device){0} };
	struct csid_device csid = {
		.camss = &camss,
		.id = 3,
		.irq = 42,
		.res = &resources,
	};
	csid.subdev.private_data = &csid;

	reset_counts();
	CHECK(csid_set_power(&csid.subdev, 1) == 0);
	CHECK(parent_gets == 1 && parent_puts == 0);
	CHECK(pm_gets == 1 && pm_puts == 0);
	CHECK(regulator_enables == 1 && regulator_disables == 0);
	CHECK(clock_rate_sets == 1 && clock_enables == 1);
	CHECK(irq_enables == 1 && irq_disables == 0);
	CHECK(reset_calls == 1 && version_calls == 1);
	CHECK(csid.phy.need_vc_update);
	CHECK(csid_set_power(&csid.subdev, 0) == 0);
	CHECK(irq_disables == 1 && clock_disables == 1);
	CHECK(regulator_disables == 1 && pm_puts == 1 && parent_puts == 1);
	puts("PASS CSID normal ON/OFF balance");

	reset_counts();
	fail_phase = FAIL_PARENT_GET;
	CHECK(csid_set_power(&csid.subdev, 1) == -EPROBE_DEFER);
	CHECK(parent_gets == 1 && parent_puts == 0 && pm_gets == 0);
	CHECK(regulator_enables == 0 && clock_rate_sets == 0);
	puts("PASS parent acquisition failure touches no child resource");

	reset_counts();
	fail_phase = FAIL_RUNTIME_PM;
	CHECK(csid_set_power(&csid.subdev, 1) == -ETIMEDOUT);
	CHECK(parent_gets == 1 && parent_puts == 1 && pm_gets == 1);
	CHECK(regulator_enables == 0 && clock_rate_sets == 0);
	puts("PASS runtime-PM failure puts the acquired parent");

	reset_counts();
	fail_phase = FAIL_REGULATOR;
	CHECK(csid_set_power(&csid.subdev, 1) == -EIO);
	CHECK(parent_puts == 1 && pm_puts == 1 && pm_gets == 1);
	CHECK(regulator_enables == 1 && regulator_disables == 0);
	CHECK(clock_rate_sets == 0);
	puts("PASS regulator enable failure puts PM and parent");

	reset_counts();
	fail_phase = FAIL_CLOCK_RATE;
	CHECK(csid_set_power(&csid.subdev, 1) == -ERANGE);
	CHECK(regulator_disables == 1 && pm_puts == 1 && parent_puts == 1);
	CHECK(regulator_disable_step < pm_put_step);
	CHECK(clock_enables == 0 && irq_enables == 0);
	puts("PASS clock-rate failure disables regulators before PM unwind");

	reset_counts();
	fail_phase = FAIL_CLOCK_ENABLE;
	CHECK(csid_set_power(&csid.subdev, 1) == -EIO);
	CHECK(regulator_disables == 1 && pm_puts == 1 && parent_puts == 1);
	CHECK(clock_enables == 1 && clock_disables == 0);
	puts("PASS clock-enable failure balances prepared child resources");

	reset_counts();
	fail_phase = FAIL_RESET;
	CHECK(csid_set_power(&csid.subdev, 1) == -ETIMEDOUT);
	CHECK(irq_enables == 1 && irq_disables == 1);
	CHECK(clock_enables == 1 && clock_disables == 1);
	CHECK(regulator_disables == 1 && pm_puts == 1 && parent_puts == 1);
	CHECK(version_calls == 0);
	puts("PASS CSID reset failure unwinds IRQ, clocks, rails, PM, parent");
	return 0;
}
"""


CAMCC_PREFIX = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

struct device {
	void *of_node;
};
struct platform_device {
	struct device dev;
};
struct icc_bulk_data {
	const char *name;
	unsigned int avg_bw;
	unsigned int peak_bw;
};
struct gdsc {
	struct icc_bulk_data *icc_paths;
	unsigned int num_icc_paths;
};
struct qcom_cc_desc {
	int sentinel;
};

static struct icc_bulk_data cam_cc_titan_top_icc[] = {
	{ .name = "ahb", .avg_bw = 1, .peak_bw = 1 },
	{ .name = "hf_mnoc", .avg_bw = 1, .peak_bw = 1 },
};
static struct gdsc cam_cc_titan_top_gdsc;
static const struct qcom_cc_desc cam_cc_sm8750_desc = { 0 };
static const char *machine;
static int provider_result;
static int provider_calls;

static bool of_machine_is_compatible(const char *compatible)
{
	return machine && strcmp(machine, compatible) == 0;
}

static int qcom_cc_probe(
	struct platform_device *pdev, const struct qcom_cc_desc *desc)
{
	(void)pdev;
	assert(desc == &cam_cc_sm8750_desc);
	if (strcmp(machine, "lenovo,elden") == 0) {
		assert(cam_cc_titan_top_gdsc.icc_paths == cam_cc_titan_top_icc);
		assert(cam_cc_titan_top_gdsc.num_icc_paths == 2);
	} else {
		assert(cam_cc_titan_top_gdsc.icc_paths == NULL);
		assert(cam_cc_titan_top_gdsc.num_icc_paths == 0);
	}
	provider_calls++;
	return provider_result;
}

#define CHECK(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "%s:%d: CHECK failed: %s\n", \
			__FILE__, __LINE__, #condition); \
		exit(1); \
	} \
} while (0)
"""


CAMCC_TESTS = r"""
int main(void)
{
	struct platform_device pdev;

	machine = "lenovo,elden";
	provider_result = 0;
	CHECK(cam_cc_sm8750_probe(&pdev) == 0);
	CHECK(provider_calls == 1);
	CHECK(cam_cc_titan_top_gdsc.icc_paths == cam_cc_titan_top_icc);
	CHECK(cam_cc_titan_top_gdsc.num_icc_paths == 2);
	puts("PASS Elden probe installs the two provider-owned ICC paths");

	machine = "qcom,sm8750-other";
	provider_result = -517;
	CHECK(cam_cc_sm8750_probe(&pdev) == -517);
	CHECK(provider_calls == 2);
	CHECK(cam_cc_titan_top_gdsc.icc_paths == NULL);
	CHECK(cam_cc_titan_top_gdsc.num_icc_paths == 0);
	puts("PASS non-Elden SM8750 probe leaves ICC paths unset");

	machine = "lenovo,elden";
	provider_result = 0;
	CHECK(cam_cc_sm8750_probe(&pdev) == 0);
	CHECK(provider_calls == 3);
	CHECK(cam_cc_titan_top_gdsc.icc_paths == cam_cc_titan_top_icc);
	CHECK(cam_cc_titan_top_gdsc.num_icc_paths == 2);
	puts("PASS Elden probe restore is stateless across repeated probes");

	provider_result = -ETIMEDOUT;
	CHECK(cam_cc_sm8750_probe(&pdev) == -ETIMEDOUT);
	CHECK(provider_calls == 4);
	puts("PASS qcom_cc_probe result is forwarded unchanged");
	return 0;
}
"""


NODE_PREFIX = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#define CONFIG_MEDIA_CONTROLLER 1

enum { V4L2_FL_REGISTERED, V4L2_FL_SUBDEV_MANAGED_DEVNODE };
struct mutex { bool held; };
struct media_device { struct mutex graph_mutex; };
struct media_gobj { struct media_device *mdev; };
struct media_interface { struct media_gobj graph_obj; bool linked; };
struct media_intf_devnode { struct media_interface intf; };
struct v4l2_device { unsigned int refs; };
struct v4l2_subdev { struct video_device *devnode; };
struct device { unsigned int refs; };
struct video_device {
	struct device dev;
	struct mutex *lock;
	struct v4l2_device *v4l2_dev;
	struct v4l2_subdev *sd;
	struct media_intf_devnode *intf_devnode;
	unsigned long flags;
};

static struct mutex videodev_lock;
static unsigned int wakeups, unregisters, freed_interfaces;

static void mutex_lock(struct mutex *lock)
{
	assert(!lock->held);
	lock->held = true;
}

static void mutex_unlock(struct mutex *lock)
{
	assert(lock->held);
	lock->held = false;
}

static bool test_bit(unsigned int bit, unsigned long *flags) { return *flags & (1UL << bit); }
static void clear_bit(unsigned int bit, unsigned long *flags) { *flags &= ~(1UL << bit); }
static bool video_is_registered(struct video_device *vdev)
{
	return test_bit(V4L2_FL_REGISTERED, &vdev->flags);
}

static struct v4l2_subdev *vdev_to_v4l2_subdev(struct video_device *vdev) { return vdev->sd; }

static void __media_remove_intf_links(struct media_interface *intf)
{
	assert(intf->graph_obj.mdev->graph_mutex.held && intf->linked);
	intf->linked = false;
}

static void media_gobj_destroy(struct media_gobj *object)
{
	assert(object->mdev->graph_mutex.held);
	object->mdev = NULL;
}

static void kfree(struct media_intf_devnode *node)
{
	assert(!node->intf.graph_obj.mdev && !node->intf.linked);
	freed_interfaces++;
	free(node);
}

static void v4l2_event_wake_all(struct video_device *vdev)
{
	assert(!video_is_registered(vdev) && !vdev->lock->held);
	assert(!vdev->v4l2_dev && !vdev->intf_devnode && !vdev->sd->devnode);
	assert(freed_interfaces == 1);
	wakeups++;
}

static void device_unregister(struct device *dev)
{
	assert(wakeups == 1 && dev->refs == 2);
	dev->refs--;
	unregisters++;
}

static void v4l2_device_put(struct v4l2_device *dev)
{
	assert(unregisters == 1 && dev->refs == 2);
	dev->refs--;
}
"""


NODE_TESTS = r"""
int main(void)
{
	struct media_device mdev = {0};
	struct v4l2_device bridge = { .refs = 2 };
	struct v4l2_subdev subdev = {0};
	struct mutex node_lock = {0};
	struct video_device vdev = {
		.dev.refs = 2,
		.lock = &node_lock,
		.v4l2_dev = &bridge,
		.sd = &subdev,
		.flags = (1UL << V4L2_FL_REGISTERED) | (1UL << V4L2_FL_SUBDEV_MANAGED_DEVNODE),
	};

	assert((vdev.intf_devnode = calloc(1, sizeof(*vdev.intf_devnode))));
	vdev.intf_devnode->intf.graph_obj.mdev = &mdev;
	vdev.intf_devnode->intf.linked = true;
	subdev.devnode = &vdev;
	video_unregister_device(&vdev);
	assert(bridge.refs == 1 && vdev.dev.refs == 1);
	assert(!node_lock.held && !mdev.graph_mutex.held && !videodev_lock.held);
	video_unregister_device(&vdev);
	assert(wakeups == 1 && unregisters == 1 && freed_interfaces == 1);
	puts("PASS node unregister removes the interface under the graph lock and drops the bridge reference");

	struct media_intf_devnode *disconnected = calloc(1, sizeof(*disconnected));
	assert(disconnected);
	media_devnode_remove(disconnected);
	assert(freed_interfaces == 2);
	puts("PASS freeing an already disconnected interface needs no media device");
	return 0;
}
"""


def extract_flash_probe_tail(source: str) -> str:
    """Run the production probe after V4L2 LED registration has succeeded."""
    probe = extract_function(source, "static int qcom_flash_led_probe(")
    start = probe.index("\n\trc = regmap_field_write(flash_data->r_fields[REG_TORCH_CLAMP],")
    return ("static int finish_flash_probe(struct qcom_flash_data *flash_data)\n"
            "{\n\tint rc;\n" + probe[start:])


def camera_device_harness(name: str, source: str, regulator_core: str) -> str:
    template = Path(__file__).with_name("y700_camera_devices.c").read_text(encoding="utf-8")
    supplies = ""
    if name == "gt9764":
        defines = "#define TEST_LENS 1\n"
        signatures = (
            "static struct gt9764 *to_gt9764(",
            "static void gt9764_free(",
            "static void gt9764_put(",
            "static void gt9764_devnode_get(",
            "static void gt9764_devnode_put(",
            "static int gt9764_set_position(",
            "static int gt9764_power_off(",
            "static int gt9764_power_on(",
            "static int gt9764_runtime_resume(",
            "static int gt9764_open(",
            "static int gt9764_close(",
            "static void gt9764_stop_pm(",
        )
    else:
        defines = (
            f"#define SENSOR {name}\n"
            f"#define SENSOR_IS_REAR {int(name == 's5kjns')}\n"
            f"#define SENSOR_RESUME {name}_runtime_resume\n"
            f"#define SENSOR_OFF {name}_power_off\n"
            f"#define SENSOR_STREAM {name}_s_stream\n"
        )
        supply_start = source.index(f"static const char * const {name}_supply_names[]")
        supplies = source[supply_start:source.index(";", supply_start) + 1]
        signatures = (
            f"static struct {name} *to_{name}(",
            f"static int {name}_set_reset(",
            *((f"static int {name}_exit_reset(",) if name == "s5kjns" else ()),
            f"static int {name}_restore_reset(",
            f"static int {name}_disable_supplies(",
            f"static int {name}_power_off(",
            f"static int {name}_power_on(",
            f"static int {name}_runtime_resume(",
            f"static int {name}_s_stream(",
        )
    functions = "\n".join((
        supplies,
        extract_function(source, f"\nstruct {name} {{") + ";",
        extract_function(regulator_core, "\nint regulator_disable_with_status("),
        *(extract_function(source, signature) for signature in signatures),
    ))
    return defines + template.replace("/* FUNCTIONS */", functions)


def camera_pmic_harness(source: str) -> str:
    probe = extract_function(source, "static int wl2868c_probe(")
    assert (probe.index("wl2868c_enable_control_supply(dev)")
            < probe.index("devm_gpiod_get_optional(")
            < probe.index("WL2868C_DEVICE_ID"))
    template = Path(__file__).with_name("y700_camera_pmic.c").read_text(encoding="utf-8")
    defines = "\n".join(line for line in source.splitlines()
                        if line.startswith("#define WL2868C_") and "(_num" not in line)
    functions = "\n".join((
        extract_function(source, "\nstruct wl2868c {") + ";",
        *(extract_function(source, f"static int wl2868c_{name}(") for name in (
            "write_outputs", "enable", "disable", "is_enabled", "init_outputs",
            "enable_control_supply",
        )),
    ))
    return defines + "\n" + template.replace("/* FUNCTIONS */", functions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kernel_tree", type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args(argv)

    tree = args.kernel_tree.resolve()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    results_path = work / "results.json"
    results_path.unlink(missing_ok=True)
    gdsc_path = tree / "drivers/clk/qcom/gdsc.c"
    flash_path = tree / "drivers/leds/flash/leds-qcom-flash.c"
    csid_path = tree / "drivers/media/platform/qcom/camss/camss-csid.c"
    camcc_path = tree / "drivers/clk/qcom/camcc-sm8750.c"
    camera_paths = {name: tree / f"drivers/media/i2c/{name}.c"
                    for name in ("gc08a8", "s5kjns", "gt9764")}
    regulator_core = tree / "drivers/regulator/core.c"
    pmic_path = tree / "drivers/regulator/wl2868c-regulator.c"
    media_entity_path = tree / "drivers/media/mc/mc-entity.c"
    video_dev_path = tree / "drivers/media/v4l2-core/v4l2-dev.c"
    for path in (gdsc_path, flash_path, csid_path, camcc_path,
                 regulator_core, pmic_path, media_entity_path, video_dev_path,
                 *camera_paths.values()):
        if not path.is_file():
            parser.error(f"missing kernel source: {path}")
    gdsc_bytes = gdsc_path.read_bytes()
    gdsc_source = gdsc_bytes.decode("utf-8")
    flash_bytes = flash_path.read_bytes()
    flash_source = flash_bytes.decode("utf-8")
    csid_bytes = csid_path.read_bytes()
    csid_source = csid_bytes.decode("utf-8")
    camcc_bytes = camcc_path.read_bytes()
    camcc_source = camcc_bytes.decode("utf-8")

    icc_functions = "\n\n".join(
        extract_function(gdsc_source, signature)
        for signature in (
            "static int gdsc_icc_drop(",
            "static void gdsc_icc_queue_retry(",
            "static void gdsc_icc_retry(",
            "static void gdsc_icc_mark_off(",
            "static int gdsc_icc_enable(",
            "static void gdsc_icc_release(",
            "static int gdsc_icc_get(",
            "static int gdsc_enable(",
            "static int gdsc_disable(",
        )
    )
    register_function = extract_function(
        gdsc_source, "int gdsc_register(")
    flash_functions = "\n".join((
        extract_function(flash_source, "static void qcom_flash_release_v4l2("),
        extract_flash_probe_tail(flash_source),
        extract_function(flash_source, "static void qcom_flash_led_remove("),
    ))
    csid_power = extract_function(
        csid_source, "static int csid_set_power(")
    camcc_probe = extract_function(
        camcc_source, "static int cam_cc_sm8750_probe(")
    regulator_core_bytes = regulator_core.read_bytes()
    pmic_bytes = pmic_path.read_bytes()
    camera_bytes = {name: path.read_bytes() for name, path in camera_paths.items()}

    results = [
        run_harness(
            work, "v4l2-managed-node-unregister", "\n".join((
                NODE_PREFIX,
                extract_function(media_entity_path.read_text(encoding="utf-8"),
                                 "\nvoid media_devnode_remove("),
                extract_function(video_dev_path.read_text(encoding="utf-8"),
                                 "\nvoid video_unregister_device("),
                NODE_TESTS,
            )),
        ),
        run_harness(
            work,
            "gdsc-icc-lifecycle",
            GDSC_ICC_PREFIX + "\n" + icc_functions + "\n" + GDSC_ICC_TESTS,
        ),
        run_harness(
            work,
            "gdsc-register-unwind",
            GDSC_REGISTER_PREFIX + "\n" + register_function
            + "\n" + GDSC_REGISTER_TESTS,
        ),
        run_harness(
            work,
            "qcom-flash-release",
            FLASH_PREFIX + "\n" + flash_functions + "\n" + FLASH_TESTS,
        ),
        run_harness(
            work,
            "camss-csid-power-unwind",
            CSID_PREFIX + "\n" + csid_power + "\n" + CSID_TESTS,
        ),
        run_harness(
            work,
            "camcc-sm8750-board-isolation",
            CAMCC_PREFIX + "\n" + camcc_probe + "\n" + CAMCC_TESTS,
        ),
        *(run_harness(
            work, f"{name}-lifecycle", camera_device_harness(
                name, data.decode("utf-8"), regulator_core_bytes.decode("utf-8")),
        ) for name, data in camera_bytes.items()),
        run_harness(work, "wl2868c-output-lifecycle",
                    camera_pmic_harness(pmic_bytes.decode("utf-8"))),
    ]
    manifest = {
        "kernel_tree": str(tree),
        "work": str(work),
        "sources": {
            "gdsc": {
                "path": str(gdsc_path),
                "sha256": sha256(gdsc_bytes),
            },
            "qcom_flash": {
                "path": str(flash_path),
                "sha256": sha256(flash_bytes),
            },
            "camss_csid": {
                "path": str(csid_path),
                "sha256": sha256(csid_bytes),
            },
            "camcc_sm8750": {
                "path": str(camcc_path),
                "sha256": sha256(camcc_bytes),
            },
            "regulator_core": {
                "path": str(regulator_core),
                "sha256": sha256(regulator_core_bytes),
            },
            "wl2868c": {"path": str(pmic_path), "sha256": sha256(pmic_bytes)},
            **{key: {"path": str(path), "sha256": sha256(path.read_bytes())}
               for key, path in (("media_entity", media_entity_path), ("video_dev", video_dev_path))},
            **{name: {"path": str(camera_paths[name]), "sha256": sha256(data)}
               for name, data in camera_bytes.items()},
        },
        "harnesses": results,
        "scope": (
            "Extracted-C lifecycle checks only; no RPMh firmware commit, "
            "electrical sequencing, interrupt or hardware validation."
        ),
    }
    results_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for result in results:
        print(f"{result['name']}: {result['result']}")
    print(f"results: {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
