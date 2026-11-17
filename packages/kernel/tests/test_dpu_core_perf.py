#!/usr/bin/env python3
"""Compile real DPU core-perf and post-helper atomic-check functions.

The fixture extracts function bodies from the supplied C sources. Device,
locking, atomic state, and plane geometry are fixture inputs; the performance
and late-check algorithms themselves are not reimplemented in Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys


def function(text: str, name: str) -> str:
    match = re.search(
        r"^(?:static\s+)?(?:inline\s+)?"
        r"(?:(?:bool|s32|s64|u32|u64|int|void|uint32_t)\s+"
        r"|(?:const\s+)?struct\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*\*\s*|\s+))"
        + re.escape(name)
        + r"\s*\(",
        text,
        re.MULTILINE,
    )
    if not match:
        raise ValueError("missing function: " + name)
    opening = text.find("{", match.end())
    if opening < 0:
        raise ValueError("missing function body: " + name)
    depth = 1
    cursor = opening + 1
    while cursor < len(text):
        if text[cursor] == "{":
            depth += 1
        elif text[cursor] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : cursor + 1]
        cursor += 1
    raise ValueError("unterminated function: " + name)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


PREFIX = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
typedef int64_t s64;

#define BIT(n) (1UL << (n))
#define min(a,b) ((a)<(b)?(a):(b))
#define max(a,b) ((a)>(b)?(a):(b))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DIV_ROUND_UP_ULL(a,b) (((a)+(b)-1)/(b))
#define do_div(n,d) ((n)/=(d))
#define mult_frac(x,n,d) ((x)*(n)/(d))
#define swap(a,b) do { typeof(a) __tmp=(a); (a)=(b); (b)=__tmp; } while(0)
#define DRM_MODE_ROTATE_0 BIT(0)
#define DRM_MODE_ROTATE_90 BIT(1)
#define DRM_MODE_ROTATE_180 BIT(2)
#define DRM_MODE_ROTATE_270 BIT(3)
#define DRM_MODE_REFLECT_X BIT(4)
#define DRM_MODE_REFLECT_Y BIT(5)
#define PIPES_PER_PLANE 4
#define DRM_DEBUG_ATOMIC(...) do { } while (0)
#define DPU_ERROR(...) do { } while (0)
#define MSM_FORMAT_IS_YUV(f) ((f)->is_yuv)
#define MSM_FORMAT_IS_UBWC(f) ((f)->ubwc)

struct drm_rect { int x1, y1, x2, y2; };
static int drm_rect_width(const struct drm_rect *r) { return r->x2-r->x1; }
static int drm_rect_height(const struct drm_rect *r) { return r->y2-r->y1; }
static bool drm_rotation_90_or_270(unsigned int r)
{ return !!(r & (DRM_MODE_ROTATE_90 | DRM_MODE_ROTATE_270)); }

struct msm_format {
 bool is_yuv;
 bool ubwc;
 u32 pixel_format;
 u32 bpp;
};
struct drm_framebuffer {
 int width, height;
 const struct msm_format *fmt;
};
struct drm_plane_state {
 struct drm_rect src, dst;
 struct drm_framebuffer *fb;
 struct drm_plane *plane;
 unsigned int rotation;
 bool visible;
};
struct drm_plane {
 int id;
 struct drm_plane_state *state;
};
struct drm_display_mode {
 int hdisplay, vdisplay, vtotal, vsync_start, vsync_end, fps;
};
static int drm_mode_vrefresh(const struct drm_display_mode *m) { return m->fps; }

struct dpu_sw_pipe_cfg {
 struct drm_rect src_rect, src_ext_rect, dst_rect;
 unsigned int rotation;
 bool is_right_pipe;
 int mixer_x;
 bool qseed3_v11_valid;
};
struct dpu_plane_state {
 struct drm_plane_state base;
 struct dpu_sw_pipe_cfg pipe_cfg[PIPES_PER_PLANE];
 u64 plane_fetch_bw, plane_clk;
};
struct dpu_perf_cfg {
 u32 min_prefill_lines;
 u32 max_bw_low;
 u32 max_bw_high;
 u32 min_core_ib;
 u32 min_llcc_ib;
 u32 min_dram_ib;
 u32 clk_inefficiency_factor;
 u32 bw_inefficiency_factor;
};
struct dpu_caps { u32 max_linewidth; };
struct dpu_mdss_cfg {
 const struct dpu_caps *caps;
 const struct dpu_perf_cfg *perf;
};
struct dpu_plane {
 struct drm_plane base;
 int pipe;
 const struct dpu_mdss_cfg *catalog;
};
struct dpu_core_perf_params {
 u64 bw_ctl;
 u32 max_per_pipe_ib;
 u64 core_clk_rate;
};
struct dpu_core_perf {
 const struct dpu_perf_cfg *perf_cfg;
 u64 max_core_clk_rate;
 u64 core_clk_rate;
};

struct drm_device;
struct drm_atomic_commit;
enum dpu_crtc_client_type { RT_CLIENT, NRT_CLIENT };
struct drm_modeset_acquire_ctx { int unused; };
struct mutex { int fixture_index; };
struct drm_crtc_state {
 struct drm_display_mode adjusted_mode;
 bool active;
 bool mode_changed;
 void *ctm;
 struct drm_atomic_commit *state;
 unsigned int num_planes;
 struct drm_plane_state *planes[8];
};
struct drm_crtc {
 int id;
 struct drm_device *dev;
 struct drm_crtc_state *state;
 bool enabled;
 struct mutex mutex;
};
struct dpu_crtc_state {
 struct drm_crtc_state base;
 struct dpu_core_perf_params new_perf;
};
struct dpu_crtc {
 struct drm_crtc base;
 enum dpu_crtc_client_type client_type;
};
struct drm_atomic_commit {
 struct drm_device *dev;
 struct drm_modeset_acquire_ctx *acquire_ctx;
 bool allow_modeset;
 unsigned int num_crtcs;
 struct drm_crtc *crtcs[8];
 struct drm_crtc_state *old_states[8];
 struct drm_crtc_state *new_states[8];
};
struct drm_device {
 void *dev_private;
 unsigned int num_crtcs;
 struct drm_crtc *crtcs[8];
};
struct msm_kms;
struct msm_kms_funcs {
 int (*check_mode_changed)(struct msm_kms *, struct drm_atomic_commit *);
 int (*atomic_check)(struct msm_kms *, struct drm_atomic_commit *);
};
struct msm_kms {
 const struct msm_kms_funcs *funcs;
 struct drm_device *dev;
};
struct dpu_kms {
 struct dpu_core_perf perf;
 struct drm_device *dev;
};
struct msm_drm_private {
 void *kms;
};

#define to_dpu_plane(p) ((struct dpu_plane *)(p))
#define to_dpu_plane_state(p) ((struct dpu_plane_state *)(p))
#define to_dpu_crtc(p) ((struct dpu_crtc *)(p))
#define to_dpu_crtc_state(p) ((struct dpu_crtc_state *)(p))
#define to_dpu_kms(p) ((struct dpu_kms *)(p))

static const struct msm_format *msm_framebuffer_format(struct drm_framebuffer *fb)
{ return fb->fmt; }
static bool fixture_elden = true;
static bool of_machine_is_compatible(const char *name)
{ return fixture_elden && !strcmp(name, "lenovo,elden"); }
static enum dpu_crtc_client_type dpu_crtc_get_client_type(struct drm_crtc *crtc)
{ return to_dpu_crtc(crtc)->client_type; }

#define drm_for_each_crtc(crtc, ddev) \
 for (unsigned int __crtc_i = 0; \
      __crtc_i < (ddev)->num_crtcs && \
      (((crtc) = (ddev)->crtcs[__crtc_i]), 1); \
      __crtc_i++)
#define for_each_new_crtc_in_state(astate, crtc, cstate, i) \
 for ((i) = 0; \
      (i) < (int)(astate)->num_crtcs && \
      (((crtc) = (astate)->crtcs[(i)]), \
       ((cstate) = (astate)->new_states[(i)]), 1); \
      (i)++)
#define for_each_oldnew_crtc_in_state(astate, crtc, old_state, new_state, i) \
 for ((i) = 0; \
      (i) < (int)(astate)->num_crtcs && \
      (((crtc) = (astate)->crtcs[(i)]), \
       (void)(crtc), \
       ((old_state) = (astate)->old_states[(i)]), \
       ((new_state) = (astate)->new_states[(i)]), 1); \
      (i)++)
#define drm_atomic_crtc_state_for_each_plane_state(plane, pstate, cstate) \
 for (unsigned int __plane_i = 0; \
      __plane_i < (cstate)->num_planes && \
      (((plane) = (cstate)->planes[__plane_i]->plane), \
       (void)(plane), \
       ((pstate) = (cstate)->planes[__plane_i]), 1); \
      __plane_i++)
#define drm_atomic_crtc_for_each_plane(plane, crtc) \
 for (unsigned int __plane_i = 0; \
      __plane_i < (crtc)->state->num_planes && \
      (((plane) = (crtc)->state->planes[__plane_i]->plane), 1); \
      __plane_i++)

static struct drm_crtc_state *drm_atomic_get_new_crtc_state(
 struct drm_atomic_commit *astate, struct drm_crtc *crtc)
{
 for (unsigned int i = 0; i < astate->num_crtcs; i++)
  if (astate->crtcs[i] == crtc)
   return astate->new_states[i];
 return NULL;
}

static int fixture_lock_rc[8];
static int fixture_lock_calls[8];
static int __attribute__((unused)) drm_modeset_lock(struct mutex *lock,
 struct drm_modeset_acquire_ctx *ctx)
{
 (void)ctx;
 int index = lock->fixture_index;
 if (index < 0 || index >= 8)
  return -EINVAL;
 fixture_lock_calls[index]++;
 return fixture_lock_rc[index];
}

static int fixture_helper_rc;
static int fixture_helper_calls;
static int drm_atomic_helper_check(struct drm_device *dev,
 struct drm_atomic_commit *state)
{
 (void)dev;
 (void)state;
 fixture_helper_calls++;
 return fixture_helper_rc;
}
"""


TESTS = r"""
static int failures;
static int cases;
#define CHECK(c) do { if (!(c)) { \
 fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #c); failures++; } \
} while (0)

#define MAX_TEST_CRTCS 4
#define MAX_TEST_PLANES 2
struct fixture_context {
 struct drm_device dev;
 struct msm_drm_private priv;
 struct dpu_kms kms;
 struct dpu_perf_cfg perf;
 struct dpu_mdss_cfg catalog;
 struct dpu_caps caps;
 struct dpu_crtc crtcs[MAX_TEST_CRTCS];
 struct dpu_crtc_state committed[MAX_TEST_CRTCS];
 struct dpu_crtc_state pending[MAX_TEST_CRTCS];
 struct dpu_plane planes[MAX_TEST_CRTCS][MAX_TEST_PLANES];
 struct dpu_plane_state pstates[MAX_TEST_CRTCS][MAX_TEST_PLANES];
 struct drm_framebuffer fbs[MAX_TEST_CRTCS][MAX_TEST_PLANES];
 struct msm_format formats[MAX_TEST_CRTCS][MAX_TEST_PLANES];
};

static void reset_context(struct fixture_context *ctx, int count)
{
 memset(ctx, 0, sizeof(*ctx));
 fixture_elden = true;
 memset(fixture_lock_rc, 0, sizeof(fixture_lock_rc));
 memset(fixture_lock_calls, 0, sizeof(fixture_lock_calls));
 ctx->perf = (struct dpu_perf_cfg) {
  .min_prefill_lines = 35,
  .max_bw_low = 18900000,
  .max_bw_high = 28500000,
  .min_core_ib = 2500000,
  .min_dram_ib = 800000,
  .clk_inefficiency_factor = 105,
  .bw_inefficiency_factor = 120,
 };
 ctx->caps.max_linewidth = 8192;
 ctx->catalog.caps = &ctx->caps;
 ctx->catalog.perf = &ctx->perf;
 ctx->kms.perf.perf_cfg = &ctx->perf;
 ctx->kms.dev = &ctx->dev;
 ctx->priv.kms = &ctx->kms;
 ctx->dev.dev_private = &ctx->priv;
 ctx->dev.num_crtcs = count;
 for (int i = 0; i < count; i++) {
  struct dpu_crtc *crtc = &ctx->crtcs[i];
  crtc->base.id = i;
  crtc->base.dev = &ctx->dev;
  crtc->base.enabled = true;
  crtc->base.mutex.fixture_index = i;
  crtc->base.state = &ctx->committed[i].base;
  crtc->client_type = RT_CLIENT;
  ctx->committed[i].base.active = true;
  ctx->pending[i].base.active = true;
  ctx->pending[i].base.state = NULL;
  ctx->dev.crtcs[i] = &crtc->base;
  for (int plane = 0; plane < MAX_TEST_PLANES; plane++) {
   ctx->planes[i][plane].base.id = i * MAX_TEST_PLANES + plane;
   ctx->planes[i][plane].catalog = &ctx->catalog;
   ctx->pstates[i][plane].base.plane = &ctx->planes[i][plane].base;
  }
 }
}

static struct dpu_plane_state *set_panel_plane(
 struct fixture_context *ctx, int crtc, int plane, bool visible, bool has_fb)
{
 struct dpu_plane_state *state = &ctx->pstates[crtc][plane];
 struct msm_format *format = &ctx->formats[crtc][plane];
 *format = (struct msm_format) { .bpp = 4, .pixel_format = 1 };
 ctx->fbs[crtc][plane] = (struct drm_framebuffer) {
  .width = 3040,
  .height = 1904,
  .fmt = format,
 };
 state->base.fb = has_fb ? &ctx->fbs[crtc][plane] : NULL;
 state->base.plane = &ctx->planes[crtc][plane].base;
 state->base.visible = visible;
 state->base.rotation = DRM_MODE_ROTATE_90;
 state->pipe_cfg[0] = (struct dpu_sw_pipe_cfg) {
  .src_rect = {0, 0, 3040, 952},
  .dst_rect = {0, 0, 952, 3040},
  .rotation = DRM_MODE_ROTATE_90,
 };
 state->pipe_cfg[1] = (struct dpu_sw_pipe_cfg) {
  .src_rect = {0, 952, 3040, 1904},
  .dst_rect = {952, 0, 1904, 3040},
  .rotation = DRM_MODE_ROTATE_90,
  .is_right_pipe = true,
 };
 return state;
}

static void bind_planes(struct fixture_context *ctx, int crtc, int count)
{
 ctx->pending[crtc].base.num_planes = count;
 ctx->committed[crtc].base.num_planes = count;
 for (int plane = 0; plane < count; plane++) {
  ctx->pending[crtc].base.planes[plane] =
   &ctx->pstates[crtc][plane].base;
  ctx->committed[crtc].base.planes[plane] =
   &ctx->pstates[crtc][plane].base;
 }
}

static void setup_panel_mode(struct dpu_crtc_state *state, int fps)
{
 state->base.adjusted_mode = (struct drm_display_mode) {
  .hdisplay = 1904,
  .vdisplay = 3040,
  .vtotal = 3120,
  .vsync_start = 3066,
  .vsync_end = 3070,
  .fps = fps,
 };
}

static void panel_numeric_case(int fps, int layers, u64 expected_bw,
                               u64 expected_clk)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx = {0};
 reset_context(&ctx, 1);
 setup_panel_mode(&ctx.pending[0], fps);
 for (int plane = 0; plane < layers; plane++)
  set_panel_plane(&ctx, 0, plane, true, true);
 bind_planes(&ctx, 0, layers);
 tx.dev = &ctx.dev;
 tx.num_crtcs = 1;
 tx.crtcs[0] = &ctx.crtcs[0].base;
 tx.new_states[0] = &ctx.pending[0].base;
 ctx.pending[0].base.state = &tx;
 cases++;
 int rc = dpu_core_perf_crtc_check(&ctx.crtcs[0].base,
  &ctx.pending[0].base);
 CHECK(rc == 0);
 CHECK(ctx.pending[0].new_perf.bw_ctl == expected_bw);
 CHECK(ctx.pending[0].new_perf.core_clk_rate == expected_clk);
}

static void hidden_and_no_fb_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx = {0};
 reset_context(&ctx, 1);
 setup_panel_mode(&ctx.pending[0], 165);
 set_panel_plane(&ctx, 0, 0, false, true);
 set_panel_plane(&ctx, 0, 1, true, false);
 bind_planes(&ctx, 0, 2);
 tx.dev = &ctx.dev;
 tx.num_crtcs = 1;
 tx.crtcs[0] = &ctx.crtcs[0].base;
 tx.new_states[0] = &ctx.pending[0].base;
 ctx.pending[0].base.state = &tx;
 cases++;
 int rc = dpu_core_perf_crtc_check(&ctx.crtcs[0].base,
  &ctx.pending[0].base);
 CHECK(rc == 0);
 CHECK(ctx.pending[0].new_perf.bw_ctl == 0);
 CHECK(ctx.pending[0].new_perf.core_clk_rate != 0);
}

static void set_bus_plane(struct fixture_context *ctx, int crtc, u32 width)
{
 struct dpu_plane_state *plane = &ctx->pstates[crtc][0];
 ctx->formats[crtc][0] = (struct msm_format) {
  .bpp = 4,
  .pixel_format = 1,
 };
 ctx->fbs[crtc][0] = (struct drm_framebuffer) {
  .width = width,
  .height = 500,
  .fmt = &ctx->formats[crtc][0],
 };
 plane->base.fb = &ctx->fbs[crtc][0];
 plane->base.plane = &ctx->planes[crtc][0].base;
 plane->base.visible = true;
 plane->base.rotation = DRM_MODE_ROTATE_0;
 plane->pipe_cfg[0] = (struct dpu_sw_pipe_cfg) {
  .src_rect = {0, 0, width, 500},
  .dst_rect = {0, 0, 500, 500},
 };
 ctx->pending[crtc].base.adjusted_mode = (struct drm_display_mode) {
  .hdisplay = 500,
  .vdisplay = 500,
  .vtotal = 1000,
  .vsync_start = 526,
  .vsync_end = 530,
  .fps = 1000,
 };
 bind_planes(ctx, crtc, 1);
}

static void setup_bus_context(struct fixture_context *ctx, int count)
{
 reset_context(ctx, count);
 // Order regression uses abstract 5/20 GB/s bounds without the catalog factor.
 ctx->perf.bw_inefficiency_factor = 100;
 for (int i = 0; i < count; i++)
  set_bus_plane(ctx, i, 5000);
}

static void set_old_perf(struct fixture_context *ctx, int crtc, u64 bw)
{
 ctx->committed[crtc].new_perf.bw_ctl = bw;
 ctx->committed[crtc].new_perf.max_per_pipe_ib = 800000;
 ctx->pending[crtc].new_perf = ctx->committed[crtc].new_perf;
}

static void prepare_swap_scenario(struct fixture_context *ctx,
                                  struct drm_atomic_commit *tx)
{
 setup_bus_context(ctx, 2);
 set_bus_plane(ctx, 0, 5000);
 set_bus_plane(ctx, 1, 1250);
 set_old_perf(ctx, 0, 5000000000ULL);
 set_old_perf(ctx, 1, 20000000000ULL);
 *tx = (struct drm_atomic_commit) {0};
 tx->dev = &ctx->dev;
 tx->acquire_ctx = calloc(1, sizeof(struct drm_modeset_acquire_ctx));
 tx->num_crtcs = 2;
 tx->crtcs[0] = &ctx->crtcs[0].base;
 tx->crtcs[1] = &ctx->crtcs[1].base;
 tx->new_states[0] = &ctx->pending[0].base;
 tx->new_states[1] = &ctx->pending[1].base;
 ctx->pending[0].base.state = tx;
 ctx->pending[1].base.state = tx;
}

static int call_crtc_check_order(struct fixture_context *ctx,
 struct drm_atomic_commit *tx, int first, int second, int *second_rc)
{
 // Pending CRTC states are copied from committed new_perf.
 ctx->pending[0].new_perf = ctx->committed[0].new_perf;
 ctx->pending[1].new_perf = ctx->committed[1].new_perf;
 set_bus_plane(ctx, 0, 5000);
 set_bus_plane(ctx, 1, 1250);
 int first_rc = dpu_core_perf_crtc_check(&ctx->crtcs[first].base,
  &ctx->pending[first].base);
 if (first_rc)
  return first_rc;
 *second_rc = dpu_core_perf_crtc_check(&ctx->crtcs[second].base,
  &ctx->pending[second].base);
 return 0;
}

#ifndef HAS_LATE_CHECK
static int old_driver_regression(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 int second_rc = 999;
 prepare_swap_scenario(&ctx, &tx);
 int a_rc = call_crtc_check_order(&ctx, &tx, 0, 1, &second_rc);
 int a_second = second_rc;
 second_rc = 999;
 int b_rc = call_crtc_check_order(&ctx, &tx, 1, 0, &second_rc);
 int b_second = second_rc;
 free(tx.acquire_ctx);
 fprintf(stderr,
  "DRIVER_NEGATIVE_NUMBERS A_first_rc=%d A_second_called=%d "
  "B_first_rc=%d B_second_rc=%d committed=%llu,%llu pending=%llu,%llu\n",
  a_rc, a_second, b_rc, b_second,
  (unsigned long long)ctx.committed[0].new_perf.bw_ctl,
  (unsigned long long)ctx.committed[1].new_perf.bw_ctl,
  (unsigned long long)ctx.pending[0].new_perf.bw_ctl,
  (unsigned long long)ctx.pending[1].new_perf.bw_ctl);
 return a_rc == -E2BIG && a_second == 999 &&
        b_rc == 0 && b_second == 0;
}
#endif

static void __attribute__((unused)) require_correct_crtc_checks(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 int second_rc = 999;
 prepare_swap_scenario(&ctx, &tx);
 int a_rc = call_crtc_check_order(&ctx, &tx, 0, 1, &second_rc);
 int a_second = second_rc;
 second_rc = 999;
 int b_rc = call_crtc_check_order(&ctx, &tx, 1, 0, &second_rc);
 cases++;
 CHECK(a_rc == 0);
 CHECK(a_second == 0);
 CHECK(b_rc == 0);
 CHECK(second_rc == 0);
 free(tx.acquire_ctx);
}

#ifdef HAS_LATE_CHECK
static int prepare_late(struct fixture_context *ctx,
                        struct drm_atomic_commit *tx, int count)
{
 *tx = (struct drm_atomic_commit) {0};
 tx->dev = &ctx->dev;
 tx->acquire_ctx = calloc(1, sizeof(struct drm_modeset_acquire_ctx));
 tx->num_crtcs = count;
 for (int i = 0; i < count; i++) {
  tx->crtcs[i] = &ctx->crtcs[i].base;
  tx->old_states[i] = &ctx->committed[i].base;
  tx->new_states[i] = &ctx->pending[i].base;
  ctx->pending[i].base.state = tx;
 }
 return 0;
}

static void prepare_late_swap(struct fixture_context *ctx,
                              struct drm_atomic_commit *tx)
{
 prepare_swap_scenario(ctx, tx);
 dpu_core_perf_crtc_check(&ctx->crtcs[0].base, &ctx->pending[0].base);
 dpu_core_perf_crtc_check(&ctx->crtcs[1].base, &ctx->pending[1].base);
}

static void late_swap_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 prepare_late_swap(&ctx, &tx);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 CHECK(ctx.pending[0].new_perf.bw_ctl == 20000000000ULL);
 CHECK(ctx.pending[1].new_perf.bw_ctl == 5000000000ULL);
 free(tx.acquire_ctx);
}

static void late_over_budget_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 prepare_late(&ctx, &tx, 2);
 dpu_core_perf_crtc_check(&ctx.crtcs[0].base, &ctx.pending[0].base);
 dpu_core_perf_crtc_check(&ctx.crtcs[1].base, &ctx.pending[1].base);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == -E2BIG);
 free(tx.acquire_ctx);
}

static void late_pending_disable_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 ctx.pending[1].base.active = false;
 prepare_late(&ctx, &tx, 2);
 dpu_core_perf_crtc_check(&ctx.crtcs[0].base, &ctx.pending[0].base);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 free(tx.acquire_ctx);
}

static void late_untouched_committed_reject_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 set_bus_plane(&ctx, 0, 1250);
 ctx.crtcs[1].base.enabled = true;
 ctx.committed[1].base.active = true;
 ctx.committed[1].new_perf.bw_ctl = 28000000000ULL;
 prepare_late(&ctx, &tx, 1);
 dpu_core_perf_crtc_check(&ctx.crtcs[0].base, &ctx.pending[0].base);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == -E2BIG);
 free(tx.acquire_ctx);
}

static void late_empty_transaction_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx = {0};
 setup_bus_context(&ctx, 2);
 tx.dev = &ctx.dev;
 tx.acquire_ctx = calloc(1, sizeof(struct drm_modeset_acquire_ctx));
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 CHECK(fixture_lock_calls[0] == 0);
 CHECK(fixture_lock_calls[1] == 0);
 free(tx.acquire_ctx);
}

static void late_boundary_equal_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 set_bus_plane(&ctx, 0, 5000);
 set_bus_plane(&ctx, 1, 2125);
 prepare_late(&ctx, &tx, 2);
 dpu_core_perf_crtc_check(&ctx.crtcs[0].base, &ctx.pending[0].base);
 dpu_core_perf_crtc_check(&ctx.crtcs[1].base, &ctx.pending[1].base);
 cases++;
 CHECK(ctx.pending[0].new_perf.bw_ctl +
       ctx.pending[1].new_perf.bw_ctl == 28500000000ULL);
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 free(tx.acquire_ctx);
}

static void late_zero_threshold_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 1);
 set_bus_plane(&ctx, 0, 1250);
 ctx.perf.max_bw_high = 0;
 prepare_late(&ctx, &tx, 1);
 dpu_core_perf_crtc_check(&ctx.crtcs[0].base, &ctx.pending[0].base);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == -E2BIG);
 free(tx.acquire_ctx);
}

static void late_nrt_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 ctx.crtcs[0].client_type = NRT_CLIENT;
 ctx.crtcs[1].client_type = NRT_CLIENT;
 prepare_late(&ctx, &tx, 2);
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 CHECK(fixture_lock_calls[0] == 0);
 free(tx.acquire_ctx);
}

static void late_non_elden_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 prepare_late(&ctx, &tx, 2);
 fixture_elden = false;
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == 0);
 CHECK(fixture_lock_calls[0] == 0);
 fixture_elden = true;
 free(tx.acquire_ctx);
}

static void late_deadlock_case(void)
{
 struct fixture_context ctx;
 struct drm_atomic_commit tx;
 setup_bus_context(&ctx, 2);
 prepare_late(&ctx, &tx, 2);
 fixture_lock_rc[1] = -EDEADLK;
 cases++;
 CHECK(dpu_core_perf_atomic_check(&ctx.kms, &tx) == -EDEADLK);
 CHECK(fixture_lock_calls[0] == 1);
 CHECK(fixture_lock_calls[1] == 1);
 free(tx.acquire_ctx);
}
#endif

static int fixture_post_rc;
static int fixture_post_calls;
static int fixture_check_mode_calls;
static int fixture_post(struct msm_kms *kms, struct drm_atomic_commit *state)
{
 (void)kms;
 (void)state;
 fixture_post_calls++;
 return fixture_post_rc;
}
static int fixture_check_mode(struct msm_kms *kms,
                              struct drm_atomic_commit *state)
{
 (void)kms;
 (void)state;
 fixture_check_mode_calls++;
 return 0;
}

static void __attribute__((unused)) hook_case(
                      int helper_rc, int post_rc, bool have_post,
                      bool have_kms, int expected_rc, int expected_post)
{
 struct drm_device dev = {0};
 struct msm_drm_private priv = {0};
 struct msm_kms kms = {0};
 struct msm_kms_funcs funcs = {
  .check_mode_changed = fixture_check_mode,
  .atomic_check = have_post ? fixture_post : NULL,
 };
 struct drm_atomic_commit state = {0};
 kms.funcs = &funcs;
 kms.dev = &dev;
 priv.kms = have_kms ? &kms : NULL;
 dev.dev_private = &priv;
 state.dev = &dev;
 fixture_helper_rc = helper_rc;
 fixture_helper_calls = 0;
 fixture_post_rc = post_rc;
 fixture_post_calls = 0;
 fixture_check_mode_calls = 0;
 cases++;
 CHECK(msm_atomic_check(&dev, &state) == expected_rc);
 CHECK(fixture_helper_calls == 1);
 CHECK(fixture_post_calls == expected_post);
}

int main(void)
{
 int fps[] = {60, 90, 120, 165};
 u64 one[] = {1710858240ULL, 2566287360ULL,
              3421716480ULL, 4704860160ULL};
 u64 two[] = {3421716480ULL, 5132574720ULL,
              6843432960ULL, 9409720320ULL};
 u64 clocks[] = {374250240ULL, 561375360ULL,
                 748500480ULL, 1029188160ULL};
 for (unsigned int i = 0; i < ARRAY_SIZE(fps); i++) {
  panel_numeric_case(fps[i], 1, one[i], clocks[i]);
  panel_numeric_case(fps[i], 2, two[i], clocks[i]);
 }
 hidden_and_no_fb_case();

#ifndef HAS_LATE_CHECK
 if (failures || cases != 9) {
  fprintf(stderr,
   "DRIVER_NEGATIVE_PRECONDITION_FAILED cases=%d failures=%d\n",
   cases, failures);
  return 3;
 }
 if (old_driver_regression()) {
  fprintf(stderr,
   "DRIVER_NEGATIVE_EXPECTED old_0230_order_regression_reproduced\n");
  return 1;
 }
 fprintf(stderr,
  "DRIVER_NEGATIVE_FAILED old_0230_regression_not_reproduced\n");
 return 2;
#else
 require_correct_crtc_checks();
 late_swap_case();
 late_over_budget_case();
 late_pending_disable_case();
 late_untouched_committed_reject_case();
 late_empty_transaction_case();
 late_boundary_equal_case();
 late_zero_threshold_case();
 late_nrt_case();
 late_non_elden_case();
 late_deadlock_case();
 hook_case(0, 0, true, true, 0, 1);
 hook_case(-EINVAL, 0, true, true, -EINVAL, 0);
 hook_case(0, -EDEADLK, true, true, -EDEADLK, 1);
 hook_case(0, 0, false, true, 0, 0);
 hook_case(0, 0, false, false, 0, 0);
#endif

 fprintf(stderr, "DPU_CORE_PERF %d cases, %d failed assertions\n",
  cases, failures);
 return failures ? 1 : 0;
}
"""


def extract_core_functions(source: str) -> list[str]:
    names = [
        "dpu_core_perf_adjusted_mode_clk",
        "_dpu_core_perf_calc_bw",
        "_dpu_core_perf_calc_clk",
        "_dpu_crtc_get_kms",
        "_dpu_core_perf_calc_crtc",
        "dpu_core_perf_aggregate",
        "dpu_core_perf_aggregate_atomic",
        "dpu_core_perf_crtc_check",
    ]
    if "static void dpu_core_perf_calc_atomic(" in source:
        names.insert(4, "dpu_core_perf_calc_atomic")
    names = [name for name in names if name in source]
    return [function(source, name) for name in names]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-source", required=True, type=pathlib.Path)
    parser.add_argument("--plane-source", required=True, type=pathlib.Path)
    parser.add_argument("--msm-atomic-source", required=True, type=pathlib.Path)
    parser.add_argument("--work", required=True, type=pathlib.Path)
    parser.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    args = parser.parse_args()

    core_text = args.core_source.read_text(encoding="utf-8")
    plane_text = args.plane_source.read_text(encoding="utf-8")
    atomic_text = args.msm_atomic_source.read_text(encoding="utf-8")
    has_late = "int dpu_core_perf_atomic_check(" in core_text

    parts = [PREFIX]
    if has_late:
        parts.append("#define HAS_LATE_CHECK 1")
    parts.extend(
        [
            function(plane_text, "_dpu_plane_calc_bw"),
            function(plane_text, "_dpu_plane_calc_clk"),
            function(plane_text, "dpu_plane_calc_perf"),
        ]
    )
    parts.extend(extract_core_functions(core_text))
    if has_late:
        parts.append(function(core_text, "dpu_core_perf_atomic_check"))
    parts.append(function(atomic_text, "msm_atomic_check"))
    parts.append(TESTS)

    args.work.mkdir(parents=True, exist_ok=True)
    source = args.work / "dpu_core_perf_fixture.c"
    executable = args.work / (
        "dpu_core_perf_fixture.exe" if os.name == "nt" else "dpu_core_perf_fixture"
    )
    source.write_text("\n\n".join(parts), encoding="utf-8")

    compiler = shlex.split(args.cc, posix=os.name != "nt")
    flags = compiler + [
        "-std=gnu11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-sign-compare",
        "-Wno-unused-parameter",
        "-O1",
        "-g",
    ]
    sanitize = os.environ.get("DPU_TEST_SANITIZE", "1") != "0"
    if sanitize:
        flags.append("-fsanitize=undefined")
    compile_argv = flags + [str(source), "-o", str(executable)]
    test_runner = pathlib.Path(__file__).resolve()
    manifest = {
        "compiler_argv": compile_argv,
        "generated_source": str(source),
        "generated_source_sha256": sha256(source),
        "has_late_check": has_late,
        "inputs": {
            "core": {
                "path": str(args.core_source),
                "sha256": sha256(args.core_source),
            },
            "plane": {
                "path": str(args.plane_source),
                "sha256": sha256(args.plane_source),
            },
            "msm_atomic": {
                "path": str(args.msm_atomic_source),
                "sha256": sha256(args.msm_atomic_source),
            },
        },
        "sanitizer": "undefined" if sanitize else "off",
        "test_runner": {
            "path": str(test_runner),
            "sha256": sha256(test_runner),
        },
    }
    (args.work / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(compile_argv)
    if completed.returncode:
        return completed.returncode

    print(
        "DPU_CORE_INPUTS "
        f"core={sha256(args.core_source)} "
        f"plane={sha256(args.plane_source)} "
        f"msm_atomic={sha256(args.msm_atomic_source)} "
        f"late={int(has_late)}",
        flush=True,
    )
    return subprocess.run([str(executable)]).returncode


if __name__ == "__main__":
    sys.exit(main())
