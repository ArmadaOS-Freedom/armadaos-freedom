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
Q6APM_LPASS_NAME = "q6apm-lpass-dais.c"
SOC_DPCM_H_REL = Path("include/sound/soc-dpcm.h")


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
    in_line_comment = False
    in_block_comment = False
    escaped = False
    pos = brace
    while pos < len(source):
        char = source[pos]
        following = source[pos + 1] if pos + 1 < len(source) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            pos += 1
            continue
        if in_block_comment:
            if char == "*" and following == "/":
                in_block_comment = False
                pos += 2
            else:
                pos += 1
            continue
        if escaped:
            escaped = False
            pos += 1
            continue
        if char == "\\" and (in_string or in_char):
            escaped = True
            pos += 1
            continue
        if not in_string and not in_char:
            if char == "/" and following == "/":
                in_line_comment = True
                pos += 2
                continue
            if char == "/" and following == "*":
                in_block_comment = True
                pos += 2
                continue
        if char == '"' and not in_char:
            in_string = not in_string
            pos += 1
            continue
        if char == "'" and not in_string:
            in_char = not in_char
            pos += 1
            continue
        if in_string or in_char:
            pos += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                line = source.count("\n", 0, start) + 1
                return source[start : pos + 1], line
        pos += 1
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
    search_start = 0
    for needle in needles:
        position = function.find(needle, search_start)
        if position < 0:
            raise ValueError(f"{label}: source is missing {needle!r}")
        positions.append(position)
        search_start = position + len(needle)
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
    lpass_text: str,
    soc_pcm_text: str,
    soc_dpcm_header_text: str,
    soc_component_text: str,
    soc_compress_text: str,
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
        "candidate_component_hw_params": (
            soc_component_text,
            "int snd_soc_pcm_component_hw_params(struct snd_pcm_substream *substream,",
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
        "candidate_dpcm_close": (
            soc_pcm_text,
            "static int dpcm_fe_dai_close(struct snd_pcm_substream *fe_substream)",
        ),
        "candidate_dpcm_trigger": (
            soc_pcm_text,
            "static int dpcm_fe_dai_do_trigger(struct snd_pcm_substream *substream, int cmd)\n{",
        ),
        "candidate_dpcm_route_stop_done": (
            soc_pcm_text,
            "static bool dpcm_route_stop_done(struct snd_soc_dpcm *dpcm, int stream)",
        ),
        "candidate_dpcm_prune_paths": (
            soc_pcm_text,
            "static int dpcm_prune_paths(struct snd_soc_pcm_runtime *fe, int stream,",
        ),
        "candidate_dpcm_be_connect": (
            soc_pcm_text,
            "static int dpcm_be_connect(struct snd_soc_pcm_runtime *fe,",
        ),
        "candidate_dpcm_rollback_path_changes": (
            soc_pcm_text,
            "static void dpcm_rollback_path_changes(struct snd_soc_pcm_runtime *fe,",
        ),
        "candidate_dpcm_be_disconnect_one": (
            soc_pcm_text,
            "static void dpcm_be_disconnect_one(struct snd_soc_pcm_runtime *fe,",
        ),
        "candidate_dpcm_add_paths": (
            soc_pcm_text,
            "int dpcm_add_paths(struct snd_soc_pcm_runtime *fe, int stream,",
        ),
        "candidate_dpcm_be_dai_trigger": (
            soc_pcm_text,
            "int dpcm_be_dai_trigger(struct snd_soc_pcm_runtime *fe, int stream,",
        ),
        "candidate_dpcm_fe_dai_open": (
            soc_pcm_text,
            "static int dpcm_fe_dai_open(struct snd_pcm_substream *fe_substream)",
        ),
        "candidate_dpcm_runtime_update": (
            soc_pcm_text,
            "int snd_soc_dpcm_runtime_update(struct snd_soc_card *card)",
        ),
        "candidate_dpcm_runtime_update_locked": (
            soc_pcm_text,
            "static int snd_soc_dpcm_runtime_update_locked(struct snd_soc_card *card)",
        ),
        "candidate_dpcm_run_update_shutdown": (
            soc_pcm_text,
            "static int dpcm_run_update_shutdown(struct snd_soc_pcm_runtime *fe, int stream)",
        ),
        "candidate_dpcm_run_update": (
            soc_pcm_text,
            "static int dpcm_run_update(struct snd_soc_pcm_runtime *fe, int stream, int new)",
        ),
        "candidate_soc_dpcm_fe_runtime_update": (
            soc_pcm_text,
            "static int soc_dpcm_fe_runtime_update(struct snd_soc_pcm_runtime *fe, int new,",
        ),
        "candidate_retry_pending_routes": (
            soc_pcm_text,
            "static int snd_soc_dpcm_retry_pending_route_update(struct snd_soc_card *card)\n{",
        ),
        "candidate_soc_compr_open_fe": (
            soc_compress_text,
            "static int soc_compr_open_fe(struct snd_compr_stream *cstream)",
        ),
        "candidate_lpass_trigger": (
            lpass_text,
            "static int q6apm_lpass_dai_trigger(struct snd_pcm_substream *substream, "
            "int cmd,",
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
        candidate_header,
        "\tbool start_uncertain;",
        "candidate shared graph uncertainty state",
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
    require_order(
        candidate_start,
        "mutex_lock(&ar_graph->start_stop_lock);",
        "if (ar_graph->start_uncertain && ar_graph->start_count)",
        "ret = -EIO;",
        "if (ar_graph->start_count == 0)",
        "ar_graph->start_uncertain = false;",
        "ar_graph->start_count++;",
        label="uncertain shared graph blocks START until STOP is reconciled",
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
        "if (!ret) {\n\t\tar_graph->start_count--;\n"
        "\t\tar_graph->start_uncertain = false;\n\t} else {\n"
        "\t\tar_graph->start_uncertain = true;\n\t}",
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
        "ret = soc_pcm_hw_clean_components(substream, rollback);",
        "if (ret < 0 && stop_on_component_error)",
        "return ret;",
        "soc_pcm_hw_clean_dais(rtd, substream, rollback);",
        label="DPCM FE cleanup aborts before DAI cleanup on component error",
    )
    require_once(
        extracted["candidate_soc_pcm_clean_components"][0],
        "return snd_soc_pcm_component_hw_free(substream, rollback);",
        "component cleanup helper",
    )
    component_hw_free = extracted["candidate_component_hw_free"][0]
    component_hw_params = extracted["candidate_component_hw_params"][0]
    require_once(
        component_hw_params,
        "soc_component_mark_push(component, substream, hw_params);",
        "component hw_params records its substream",
    )
    require_absent(
        soc_pcm_text,
        "retain_failed",
        "DPCM recovery no longer depends on a persistent component mark",
    )
    require_absent(
        soc_component_text,
        "retain_failed",
        "component cleanup no longer depends on a persistent mark",
    )
    require_order(
        component_hw_free,
        "ret = 0;",
        "ret = component->driver->hw_free(component, substream);",
        "soc_component_ret(component, ret);",
        "soc_component_mark_pop(component, hw_params);",
        label="component marks are always popped after a cleanup attempt",
    )
    require_once(
        extracted["candidate_soc_pcm_clean_dais"][0],
        "snd_soc_dai_hw_free(dai, substream, rollback);",
        "DAI cleanup helper",
    )

    hw_free = extracted["candidate_dpcm_hw_free"][0]
    require_order(
        hw_free,
        "ret = soc_pcm_hw_clean(fe, substream, 0, true);",
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
        "ret = soc_pcm_hw_clean_components(substream, 0);",
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
        "ret = soc_pcm_hw_clean_components(substream, 0);",
        "hw_params recovery replays components without an unwind mark",
    )

    shutdown = extracted["candidate_dpcm_shutdown"][0]
    require_order(
        shutdown,
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "ret = soc_pcm_hw_clean_components(substream, 0);",
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
        "ret = soc_pcm_hw_clean_components(substream, 0);",
        "soc_pcm_hw_clean_dais(fe, substream, 0);",
        "dpcm_be_dai_hw_free(fe, stream);",
        "WRITE_ONCE(fe->dpcm[stream].hw_free_failed, false);",
        label="close completes best-effort cleanup after the final STOP attempt",
    )

    trigger = extracted["candidate_dpcm_trigger"][0]
    require_order(
        trigger,
        "if ((READ_ONCE(fe->dpcm[stream].hw_free_failed) ||",
        "READ_ONCE(fe->card->dpcm_route_update_pending))",
        "cmd == SNDRV_PCM_TRIGGER_START",
        "goto out;",
        "switch (trigger)",
        label="DPCM blocks START while FE or route cleanup remains unresolved",
    )
    require_once(
        soc_dpcm_header_text,
        "\tbool route_stop_done;",
        "runtime route STOP completion marker",
    )
    route_stop_done = extracted["candidate_dpcm_route_stop_done"][0]
    require_order(
        route_stop_done,
        "snd_pcm_stream_lock_irqsave_nested(be_substream, flags);",
        "READ_ONCE(dpcm->route_stop_done)",
        "snd_pcm_stream_unlock_irqrestore(be_substream, flags);",
        label="route STOP marker reads are protected by the BE stream lock",
    )
    be_trigger = extracted["candidate_dpcm_be_dai_trigger"][0]
    require_order(
        be_trigger,
        "snd_pcm_stream_lock_irqsave_nested(be_substream, flags);",
        "if (READ_ONCE(dpcm->route_stop_done))",
        "be->dpcm[stream].be_start--;",
        "if (fe->dpcm[stream].runtime_update ==",
        "WRITE_ONCE(dpcm->route_stop_done, true);",
        "snd_pcm_stream_unlock_irqrestore(be_substream, flags);",
        label="shared route STOP references are retired once under the BE lock",
    )
    be_connect = extracted["candidate_dpcm_be_connect"][0]
    prune_paths = extracted["candidate_dpcm_prune_paths"][0]
    rollback_path_changes = extracted["candidate_dpcm_rollback_path_changes"][0]
    add_paths = extracted["candidate_dpcm_add_paths"][0]
    require_absent(
        soc_pcm_text,
        "stop_snapshot",
        "path transactions do not keep an in-link STOP snapshot",
    )
    require_absent(
        be_trigger,
        "rollback_stop",
        "ordinary BE STOP retains upstream best-effort semantics",
    )
    require_order(
        be_connect,
        "struct dpcm_path_change *change)",
        "for_each_dpcm_be(fe, stream, dpcm)",
        "if (dpcm->be != be)",
        "continue;",
        "if (dpcm_route_stop_done(dpcm, stream))",
        "return snd_soc_ret(be->dev, -EBUSY,",
        "if (dpcm->state != SND_SOC_DPCM_LINK_STATE_FREE)",
        "return 0;",
        "change->dpcm = dpcm;",
        "change->type = DPCM_PATH_REUSED;",
        "change->update = be->dpcm[stream].runtime_update;",
        "change->state = be->dpcm[stream].state;",
        "change->be_start = be->dpcm[stream].be_start;",
        "change->be_pause = be->dpcm[stream].be_pause;",
        "SND_SOC_DPCM_STATE_STOP &&",
        "ret = soc_pcm_trigger(be_substream,",
        "change->hardware_started = true;",
        "if (ret)",
        "return snd_soc_ret(be->dev, ret,",
        "be->dpcm[stream].state != SND_SOC_DPCM_STATE_START",
        "be->dpcm[stream].state != SND_SOC_DPCM_STATE_PAUSED",
        "be->dpcm[stream].state != SND_SOC_DPCM_STATE_STOP",
        "be->dpcm[stream].state != SND_SOC_DPCM_STATE_NEW",
        "be->dpcm[stream].state != SND_SOC_DPCM_STATE_CLOSE",
        "dpcm->state = SND_SOC_DPCM_LINK_STATE_NEW;",
        "if (be->dpcm[stream].state == SND_SOC_DPCM_STATE_NEW ||",
        "be->dpcm[stream].state == SND_SOC_DPCM_STATE_CLOSE)",
        "SND_SOC_DPCM_UPDATE_BE);",
        "dpcm_set_be_update_state(be, stream,",
        "SND_SOC_DPCM_UPDATE_NO);",
        label="retained routes record their previous state and any hardware START",
    )
    require_order(
        prune_paths,
        "bool replay)",
        "for_each_dpcm_be(fe, stream, dpcm)",
        "if (dpcm_route_stop_done(dpcm, stream))",
        "else if (!replay && dpcm_be_is_active(dpcm, stream, *list_))",
        "dpcm->state = SND_SOC_DPCM_LINK_STATE_FREE;",
        "dpcm_set_be_update_state(dpcm->be, stream, SND_SOC_DPCM_UPDATE_BE);",
        label="full replays force every retained link through old-path cleanup",
    )
    require_absent(
        be_connect,
        "dpcm_be_dai_startup(",
        "retained route reconnect does not start the BE again",
    )
    require_order(
        rollback_path_changes,
        "for (i = count - 1; i >= 0; i--)",
        "if (change->type == DPCM_PATH_NEW)",
        "dpcm_set_be_update_state(change->dpcm->be, stream,",
        "change->update);",
        "dpcm_be_disconnect_one(fe, stream, change->dpcm);",
        "if (change->hardware_started)",
        "ret = soc_pcm_trigger(be_substream,",
        "SNDRV_PCM_TRIGGER_STOP);",
        "be->dpcm[stream].state = SND_SOC_DPCM_STATE_STOP;",
        "if (ret)",
        "WRITE_ONCE(fe->card->dpcm_route_update_pending,",
        "true);",
        "continue;",
        "change->dpcm->state = SND_SOC_DPCM_LINK_STATE_FREE;",
        "be->dpcm[stream].state = change->state;",
        "be->dpcm[stream].be_start = change->be_start;",
        "be->dpcm[stream].be_pause = change->be_pause;",
        "dpcm_set_be_update_state(be, stream, change->update);",
        label="failed path adds undo only explicitly recorded links",
    )
    require_order(
        add_paths,
        "changes = kcalloc(list->num_widgets, sizeof(*changes), GFP_KERNEL);",
        "struct dpcm_path_change *change = &changes[changed];",
        "err = dpcm_be_connect(fe, be, stream, change);",
        "if (err < 0)",
        "dpcm_rollback_path_changes(fe, stream, changes, changed);",
        "return err;",
        "if (change->type == DPCM_PATH_UNCHANGED)",
        "changed++;",
        label="path discovery records and rolls back exact changed links",
    )
    require_order(
        extracted["candidate_dpcm_close"][0],
        "ret = dpcm_fe_dai_shutdown(fe_substream);",
        "dpcm_fe_dai_cleanup(fe_substream);",
        "retry_ret = snd_soc_dpcm_retry_pending_route_update(fe->card);",
        "if (!ret)",
        "ret = retry_ret;",
        "snd_soc_dpcm_mutex_unlock(fe);",
        label="PCM close retries deferred routes under the card mutex",
    )
    disconnect_one = extracted["candidate_dpcm_be_disconnect_one"][0]
    require_order(
        disconnect_one,
        "snd_soc_dpcm_mutex_assert_held(fe);",
        "snd_pcm_stream_lock_irq(substream);",
        "dpcm_be_reparent(fe, dpcm->be, stream);",
        "list_del(&dpcm->list_be);",
        "list_del(&dpcm->list_fe);",
        "snd_pcm_stream_unlock_irq(substream);",
        "dpcm_remove_debugfs_state(dpcm);",
        "kfree(dpcm);",
        label="single-link rollback reparents any shared BE before freeing the FE link",
    )
    fe_open = extracted["candidate_dpcm_fe_dai_open"][0]
    require_order(
        fe_open,
        "ret = dpcm_add_paths(fe, stream, &list);",
        "if (ret < 0)",
        "goto put_path;",
        label="ordinary PCM open propagates path transaction failures",
    )
    compr_open_fe = extracted["candidate_soc_compr_open_fe"][0]
    require_order(
        compr_open_fe,
        "ret = dpcm_add_paths(fe, stream, &list);",
        "if (ret < 0)",
        "goto out;",
        label="compressed PCM open propagates path transaction failures",
    )
    lpass_trigger = extracted["candidate_lpass_trigger"][0]
    lpass_stop = extract_case(
        lpass_trigger,
        "case SNDRV_PCM_TRIGGER_STOP:",
        "default:",
    )
    require_order(
        lpass_stop,
        "if (!dai_data->is_port_started[dai->id])",
        "break;",
        "if (!dai_data->graph[dai->id])",
        "return -EINVAL;",
        "ret = q6apm_graph_stop(dai_data->graph[dai->id]);",
        "if (ret < 0)",
        "break;",
        "dai_data->is_port_started[dai->id] = false;",
        label="LPASS STOP clears its started bit only after a successful graph stop",
    )

    runtime_update = extracted["candidate_dpcm_runtime_update"][0]
    runtime_update_locked = extracted["candidate_dpcm_runtime_update_locked"][0]
    retry_pending_routes = extracted["candidate_retry_pending_routes"][0]
    run_update_shutdown = extracted["candidate_dpcm_run_update_shutdown"][0]
    run_update = extracted["candidate_dpcm_run_update"][0]
    fe_runtime_update = extracted["candidate_soc_dpcm_fe_runtime_update"][0]
    runtime_update_guard = extract_if_block(
        runtime_update_locked, "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))"
    )
    require_order(
        runtime_update_locked,
        "for_each_card_rtds(card, fe)",
        "if (READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "WRITE_ONCE(card->dpcm_route_update_pending, true);",
        "return -EBUSY;",
        "replay = READ_ONCE(card->dpcm_route_update_pending);",
        "ret = soc_dpcm_fe_runtime_update(fe, 0, replay);",
        label="runtime routing checks failed STOP before changing BE paths",
    )
    require_absent(
        runtime_update_guard,
        "soc_dpcm_fe_runtime_update",
        "failed STOP leaves DAPM backend paths untouched",
    )
    require_order(
        run_update_shutdown,
        "err = dpcm_be_dai_trigger(fe, stream, SNDRV_PCM_TRIGGER_STOP);",
        "if (err)",
        "return soc_pcm_ret(fe, err);",
        "dpcm_be_dai_hw_free(fe, stream);",
        label="failed old-route STOP retains backend allocations",
    )
    require_order(
        run_update,
        "ret = dpcm_run_update_shutdown(fe, stream);",
        "dpcm_set_fe_update_state(fe, stream, SND_SOC_DPCM_UPDATE_NO);",
        "/* A later full retry finishes any partially applied route update. */",
        "if (ret < 0)",
        "return ret;",
        "dpcm_clear_pending_state(fe, stream);",
        "dpcm_be_disconnect(fe, stream);",
        label="failed route startup or prune keeps links attached for retry",
    )
    require_order(
        fe_runtime_update,
        "bool replay)",
        "if (!replay && !snd_soc_dai_active(snd_soc_rtd_to_cpu(fe, 0)))",
        "if (replay) {",
        "count = dpcm_prune_paths(fe, stream, NULL, true);",
        "ret = dpcm_run_update(fe, stream, 0);",
        "continue;",
        "if (new) {",
        "count = dpcm_add_paths(fe, stream, &list);",
        "if (count < 0) {",
        "ret = count;",
        "goto out;",
        "count = dpcm_prune_paths(fe, stream, &list, replay);",
        "ret = dpcm_run_update(fe, stream, new);",
        "out:\n\t\tdpcm_path_put(&list);\n\t\tif (ret < 0)\n\t\t\treturn ret;",
        label="FE route update propagates failures and replays every old path",
    )
    require_order(
        runtime_update_locked,
        "replay = READ_ONCE(card->dpcm_route_update_pending);",
        "ret = soc_dpcm_fe_runtime_update(fe, 0, replay);",
        "/* Bring new paths up only after every old path has converged. */",
        "ret = soc_dpcm_fe_runtime_update(fe, 1, false);",
        "WRITE_ONCE(card->dpcm_route_update_pending, false);",
        label="retry forces old-path replay before applying the current DAPM graph",
    )
    require_order(
        retry_pending_routes,
        "snd_soc_dpcm_mutex_assert_held(card);",
        "if (!READ_ONCE(card->dpcm_route_update_pending))",
        "return 0;",
        "snd_soc_dpcm_runtime_update_locked(card);",
        "if (ret && ret != -EBUSY)",
        "return ret;",
        label="deferred route retries stay under the card PCM mutex",
    )
    require_order(
        runtime_update,
        "snd_soc_dpcm_mutex_lock(card);",
        "snd_soc_dpcm_runtime_update_locked(card);",
        "snd_soc_dpcm_mutex_unlock(card);",
        label="public DPCM route update owns the PCM mutex",
    )
    require_order(
        extracted["candidate_dpcm_hw_params"][0],
        "if (ret >= 0 && !READ_ONCE(fe->dpcm[stream].hw_free_failed))",
        "ret = snd_soc_dpcm_retry_pending_route_update(fe->card);",
        "snd_soc_dpcm_mutex_unlock(fe);",
        "return soc_pcm_ret(fe, ret);",
        label="hw_params propagates deferred route retry errors under the PCM lock",
    )
    require_order(
        extracted["candidate_dpcm_close"][0],
        "ret = dpcm_fe_dai_shutdown(fe_substream);",
        "dpcm_fe_dai_cleanup(fe_substream);",
        "retry_ret = snd_soc_dpcm_retry_pending_route_update(fe->card);",
        "if (!ret)",
        "ret = retry_ret;",
        "snd_soc_dpcm_mutex_unlock(fe);",
        "return ret;",
        label="close replays deferred routes under the PCM mutex",
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
	bool start_uncertain;
	int start_count;
};

struct q6apm_graph {
	struct audioreach_graph *ar_graph;
};

enum {
	SNDRV_PCM_TRIGGER_START, SNDRV_PCM_TRIGGER_RESUME,
	SNDRV_PCM_TRIGGER_PAUSE_RELEASE, SNDRV_PCM_TRIGGER_STOP,
	SNDRV_PCM_TRIGGER_SUSPEND, SNDRV_PCM_TRIGGER_PAUSE_PUSH,
	SNDRV_PCM_TRIGGER_DRAIN,
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

struct snd_pcm {
	int nonatomic;
};

struct snd_pcm_hw_params;
struct snd_soc_pcm_runtime;

struct snd_pcm_substream {
	struct snd_pcm_runtime *runtime;
	struct snd_pcm *pcm;
	struct snd_soc_pcm_runtime *fe;
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
	CHECK(shared.start_uncertain);
	CHECK(stop_calls == 1);
	/* Another owner cannot mistake the retained reference for a running DSP. */
	CHECK(candidate_graph_start(&graph) == -EIO);
	CHECK(shared.start_count == 1);
	CHECK(start_calls == 0);
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(!shared.start_uncertain);
	CHECK(stop_calls == 2);

	/* A missing STOP acknowledgement may follow a real DSP STOP. */
	shared.start_count = 1;
	stops[0] = -ETIMEDOUT;
	stops[1] = 0;
	reset_graph_commands(stops, 2, starts, 1);
	dsp_running = 1;
	CHECK(candidate_graph_stop(&graph) == -ETIMEDOUT);
	CHECK(!dsp_running);
	CHECK(candidate_graph_start(&graph) == -EIO);
	CHECK(shared.start_count == 1);
	CHECK(!dsp_running);
	CHECK(candidate_graph_stop(&graph) == 0);
	CHECK(shared.start_count == 0);
	CHECK(!shared.start_uncertain);
	CHECK(candidate_graph_start(&graph) == 0);
	CHECK(shared.start_count == 1);
	CHECK(start_calls == 1);
	CHECK(dsp_running);

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
	while (!stop_entered) {
		int wait_ret = pthread_cond_timedwait(&stop_entered_cond,
						      &control_lock,
						      &deadline);

		CHECK(wait_ret == 0);
	}
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
	CHECK(simulated_component_rollback == 0);
	CHECK(simulated_dai_free_calls == 0);
	CHECK(simulated_be_free_calls == 0);
	CHECK(simulated_whole_clean_calls == 1);

	/* A successful retry releases the stale BE state and clears the guard. */
	simulated_component_result = 0;
	ret = simulate_dpcm_hw_params_recovery(&fe, &substream);
	CHECK(ret == 0);
	CHECK(!fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
	CHECK(fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].state == SND_SOC_DPCM_STATE_HW_FREE);
	CHECK(simulated_component_rollback == 0);
	CHECK(simulated_component_clean_calls == 2);
	CHECK(simulated_dai_free_calls == 1);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_whole_clean_calls == 1);

	/* Close retries teardown and finishes despite another STOP failure. */
	fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = true;
	simulated_component_result = -ETIMEDOUT;
	simulated_component_clean_calls = 0;
	simulated_dai_free_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;
	simulate_dpcm_shutdown_recovery(&fe, &substream);
	CHECK(simulated_component_clean_calls == 1);
	CHECK(simulated_component_rollback == 0);
	CHECK(simulated_dai_free_calls == 1);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_be_shutdown_calls == 1);
	CHECK(!fe.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed);
}

static void check_dpcm_runtime_update_guard(void)
{
	struct device device = {};
	struct snd_soc_dai_link dynamic_link = {
		.name = "dynamic-link",
		.dynamic = true,
	};
	struct snd_soc_pcm_runtime first = {};
	struct snd_soc_pcm_runtime second = {};
	struct snd_soc_pcm_runtime *rtds[] = { &first, &second };
	struct snd_soc_card card = {
		.dev = &device,
		.rtds = rtds,
		.num_rtds = 2,
	};
	struct snd_soc_card other_card = {};
	first.dai_link = &dynamic_link;
	first.dev = &device;
	second.dai_link = &dynamic_link;
	second.dev = &device;

	first.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = false;
	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = true;
	simulated_runtime_update_calls = 0;
	CHECK(snd_soc_dpcm_runtime_update(&card) == -EBUSY);
	CHECK(simulated_runtime_update_calls == 0);
	CHECK(card.dpcm_route_update_pending);

	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].hw_free_failed = false;
	snd_soc_dpcm_mutex_lock(&card);
	CHECK(snd_soc_dpcm_retry_pending_route_update(&card) == 0);
	CHECK(!card.dpcm_route_update_pending);
	CHECK(card.pcm_mutex_locked);
	CHECK(!other_card.pcm_mutex_locked);
	snd_soc_dpcm_mutex_unlock(&card);
	CHECK(simulated_runtime_update_calls == 6);

	card.dpcm_route_update_pending = true;
	second.dpcm[SNDRV_PCM_STREAM_PLAYBACK].hw_free_failed = true;
	simulated_runtime_update_calls = 0;
	CHECK(snd_soc_dpcm_runtime_update(&card) == -EBUSY);
	CHECK(simulated_runtime_update_calls == 0);
	CHECK(card.dpcm_route_update_pending);
	second.dpcm[SNDRV_PCM_STREAM_PLAYBACK].hw_free_failed = false;

	/* A deferred retry must surface a still-busy FE to the PCM caller. */
	card.dpcm_route_update_pending = true;
	second.dpcm[SNDRV_PCM_STREAM_PLAYBACK].hw_free_failed = true;
	snd_soc_dpcm_mutex_lock(&card);
	CHECK(snd_soc_dpcm_retry_pending_route_update(&card) == -EBUSY);
	CHECK(card.dpcm_route_update_pending);
	snd_soc_dpcm_mutex_unlock(&card);
	second.dpcm[SNDRV_PCM_STREAM_PLAYBACK].hw_free_failed = false;

	simulated_runtime_update_calls = 0;
	first.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_connected = true;
	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_wanted = true;
	second.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_connected = false;
	simulated_startup_result = -EIO;
	CHECK(snd_soc_dpcm_runtime_update(&card) == -EIO);
	CHECK(card.dpcm_route_update_pending);
	CHECK(simulated_runtime_update_calls == 6);
	CHECK(simulated_runtime_update_order[0] == 0);
	CHECK(simulated_runtime_update_order[1] == 0);
	CHECK(simulated_runtime_update_order[2] == 0);
	CHECK(simulated_runtime_update_order[3] == 0);
	CHECK(simulated_runtime_update_order[4] == 1);
	CHECK(simulated_runtime_update_order[5] == 1);
	CHECK(!first.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_connected);
	CHECK(second.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_connected);
	simulated_startup_result = 0;
	simulated_runtime_update_calls = 0;
	snd_soc_dpcm_mutex_lock(&card);
	CHECK(snd_soc_dpcm_retry_pending_route_update(&card) == 0);
	CHECK(!card.dpcm_route_update_pending);
	snd_soc_dpcm_mutex_unlock(&card);
	CHECK(simulated_runtime_update_calls == 6);
	CHECK(simulated_runtime_update_order[0] == 0);
	CHECK(simulated_runtime_update_order[1] == 0);
	CHECK(simulated_runtime_update_order[2] == 0);
	CHECK(simulated_runtime_update_order[3] == 0);
	CHECK(simulated_runtime_update_order[4] == 1);
	CHECK(simulated_runtime_update_order[5] == 1);
	CHECK(second.dpcm[SNDRV_PCM_STREAM_CAPTURE].route_connected);

	card.dpcm_route_update_pending = true;
	simulated_path_get_result = -ENOMEM;
	simulated_path_get_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;
	simulated_runtime_update_calls = 0;
	CHECK(snd_soc_dpcm_runtime_update(&card) == -ENOMEM);
	CHECK(card.dpcm_route_update_pending);
	CHECK(simulated_path_get_calls == 1);
	CHECK(simulated_runtime_update_calls == 4);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_be_shutdown_calls == 1);
	simulated_path_get_result = 1;
	simulated_runtime_update_calls = 0;
	snd_soc_dpcm_mutex_lock(&card);
	CHECK(snd_soc_dpcm_retry_pending_route_update(&card) == 0);
	CHECK(!card.dpcm_route_update_pending);
	snd_soc_dpcm_mutex_unlock(&card);
	CHECK(simulated_runtime_update_calls == 6);
}

static void check_dpcm_stop_failure_route_retry(void)
{
	struct device device = {};
	struct snd_soc_dai_link dynamic_link = {
		.name = "dynamic-link",
		.dynamic = true,
	};
	struct snd_soc_pcm_runtime old_fe = {
		.dai_link = &dynamic_link,
		.dev = &device,
	};
	struct snd_soc_pcm_runtime new_fe = {
		.dai_link = &dynamic_link,
		.dev = &device,
	};
	struct snd_soc_pcm_runtime *rtds[] = { &old_fe, &new_fe };
	struct snd_soc_card card = {
		.dev = &device,
		.rtds = rtds,
		.num_rtds = 2,
	};
	struct fake_dpcm_runtime *old_path =
		&old_fe.dpcm[SNDRV_PCM_STREAM_CAPTURE];
	struct fake_dpcm_runtime *new_path =
		&new_fe.dpcm[SNDRV_PCM_STREAM_CAPTURE];

	old_path->route_connected = true;
	new_path->route_wanted = true;
	simulated_be_trigger_result = -ETIMEDOUT;
	simulated_be_trigger_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;
	simulated_clear_pending_calls = 0;
	simulated_disconnect_calls = 0;
	simulated_runtime_update_calls = 0;
	simulated_prune_replay = -1;

	CHECK(snd_soc_dpcm_runtime_update(&card) == -ETIMEDOUT);
	CHECK(card.dpcm_route_update_pending);
	CHECK(old_path->route_connected);
	CHECK(old_path->link_free);
	CHECK(!new_path->route_connected);
	CHECK(simulated_be_trigger_calls == 1);
	CHECK(simulated_be_free_calls == 0);
	CHECK(simulated_be_shutdown_calls == 0);
	CHECK(simulated_clear_pending_calls == 0);
	CHECK(simulated_disconnect_calls == 0);
	CHECK(simulated_prune_replay == 0);
	/* A later FE must not overwrite the first FE's shutdown failure. */
	CHECK(simulated_runtime_update_calls == 1);

	/* Retry prunes the retained old link again, then adds the new path. */
	simulated_be_trigger_result = 0;
	simulated_runtime_update_calls = 0;
	simulated_prune_replay = -1;
	snd_soc_dpcm_mutex_lock(&card);
	CHECK(snd_soc_dpcm_retry_pending_route_update(&card) == 0);
	CHECK(!card.dpcm_route_update_pending);
	snd_soc_dpcm_mutex_unlock(&card);
	CHECK(simulated_runtime_update_calls == 6);
	CHECK(simulated_runtime_update_order[0] == 0);
	CHECK(simulated_runtime_update_order[1] == 0);
	CHECK(simulated_runtime_update_order[2] == 0);
	CHECK(simulated_runtime_update_order[3] == 0);
	CHECK(simulated_runtime_update_order[4] == 1);
	CHECK(simulated_runtime_update_order[5] == 1);
	CHECK(!old_path->route_connected);
	CHECK(!old_path->link_free);
	CHECK(new_path->route_connected);
	CHECK(simulated_be_trigger_calls == 2);
	CHECK(simulated_be_free_calls == 1);
	CHECK(simulated_be_shutdown_calls == 1);
	CHECK(simulated_clear_pending_calls == 2);
	CHECK(simulated_disconnect_calls == 1);
	CHECK(simulated_prune_replay == 1);
}

static void check_dpcm_route_connect_failure_stays_pending(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link = {
		.name = "connect-retry",
		.dynamic = true,
	};
	struct snd_soc_pcm_runtime fe = {
		.dai_link = &link,
		.dev = &device,
	};
	struct snd_soc_pcm_runtime *rtds[] = { &fe };
	struct snd_soc_card card = {
		.dev = &device,
		.rtds = rtds,
		.num_rtds = 1,
	};

	fe.card = &card;
	fe.dpcm[stream].route_wanted = true;
	simulated_active_stream = stream;
	simulated_path_get_result = 1;
	simulated_path_get_calls = 0;
	simulated_runtime_update_calls = 0;
	simulated_add_paths_result = -EIO;
	simulated_startup_result = 0;
	simulated_clear_pending_calls = 0;
	simulated_disconnect_calls = 0;
	simulated_prune_replay = -1;

	CHECK(snd_soc_dpcm_runtime_update(&card) == -EIO);
	CHECK(card.dpcm_route_update_pending);
	CHECK(simulated_path_get_calls == 2);
	CHECK(simulated_runtime_update_calls == 2);
	CHECK(simulated_runtime_update_order[0] == 0);
	CHECK(simulated_runtime_update_order[1] == 1);
	CHECK(!fe.dpcm[stream].route_connected);
	CHECK(simulated_clear_pending_calls == 0);
	CHECK(simulated_disconnect_calls == 0);
	CHECK(simulated_prune_replay == 0);

	simulated_add_paths_result = 1;
	simulated_prune_replay = -1;
	CHECK(snd_soc_dpcm_runtime_update(&card) == 0);
	CHECK(!card.dpcm_route_update_pending);
	CHECK(fe.dpcm[stream].route_connected);
	CHECK(simulated_clear_pending_calls == 1);
	CHECK(simulated_prune_replay == 1);
}

static void check_dpcm_path_connect_changes(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_card card = {
		.dev = &device,
		.component_chaining = true,
		.pcm_mutex_locked = true,
	};
	struct snd_soc_dai_link retained_link_link = { .name = "retained-BE" };
	struct snd_soc_dai_link new_link_link = { .name = "new-BE" };
	struct snd_soc_pcm_runtime fe = { .dev = &device, .card = &card };
	struct snd_soc_pcm_runtime retained = {
		.dev = &device,
		.dai_link = &retained_link_link,
	};
	struct snd_soc_pcm_runtime new_be = {
		.dev = &device,
		.dai_link = &new_link_link,
	};
	struct snd_pcm_substream fe_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &fe,
	};
	struct snd_pcm_substream retained_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &retained,
	};
	struct snd_pcm_substream new_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &new_be,
	};
	struct snd_soc_dpcm retained_dpcm = {
		.be = &retained,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_FREE,
	};
	struct dpcm_path_change change = {};

	fe.be_substreams[stream] = &fe_substream;
	retained.be_substreams[stream] = &retained_substream;
	new_be.be_substreams[stream] = &new_substream;
	fe.be_links[stream][0] = &retained_dpcm;
	fe.num_be_links[stream] = 1;
	fe.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	retained.dpcm[stream].state = SND_SOC_DPCM_STATE_STOP;
	retained.dpcm[stream].be_start = 0;
	retained.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_BE;
	retained.dpcm[stream].start_result = 0;
	simulated_trigger_calls = 0;
	simulated_update_no_calls = 0;

	/* An old link with an unconsumed route STOP marker cannot be reused. */
	retained_dpcm.route_stop_done = true;
	CHECK(candidate_dpcm_be_connect(
		      &fe, &retained, stream, &change) == -EBUSY);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(simulated_trigger_calls == 0);
	retained_dpcm.route_stop_done = false;

	CHECK(candidate_dpcm_be_connect(
		      &fe, &retained, stream, &change) == 0);
	CHECK(change.type == DPCM_PATH_REUSED);
	CHECK(change.dpcm == &retained_dpcm);
	CHECK(change.update == SND_SOC_DPCM_UPDATE_BE);
	CHECK(change.state == SND_SOC_DPCM_STATE_STOP);
	CHECK(change.be_start == 0);
	CHECK(change.hardware_started);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_NEW);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_START);
	CHECK(retained.dpcm[stream].be_start == 1);
	CHECK(retained.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_NO);
	CHECK(simulated_trigger_calls == 1);

	/* A failed restore START leaves the retained link FREE for retry. */
	retained_dpcm.state = SND_SOC_DPCM_LINK_STATE_FREE;
	retained.dpcm[stream].state = SND_SOC_DPCM_STATE_STOP;
	retained.dpcm[stream].be_start = 0;
	retained.dpcm[stream].start_result = -EIO;
	change = (struct dpcm_path_change){};
	simulated_trigger_calls = 0;
	CHECK(candidate_dpcm_be_connect(
		      &fe, &retained, stream, &change) == -EIO);
	CHECK(change.type == DPCM_PATH_REUSED);
	CHECK(!change.hardware_started);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(simulated_trigger_calls == 1);

	/* A stopped FE only reactivates the link and leaves hardware stopped. */
	fe.dpcm[stream].state = SND_SOC_DPCM_STATE_STOP;
	retained.dpcm[stream].start_result = 0;
	change = (struct dpcm_path_change){};
	simulated_trigger_calls = 0;
	CHECK(candidate_dpcm_be_connect(
		      &fe, &retained, stream, &change) == 0);
	CHECK(change.type == DPCM_PATH_REUSED);
	CHECK(!change.hardware_started);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_NEW);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(simulated_trigger_calls == 0);

	/*
	 * Once the marked old link has been disconnected, reconnecting the
	 * same BE allocates a fresh link instead of reviving stale state.
	 */
	fe.num_be_links[stream] = 0;
	retained.dpcm[stream].state = SND_SOC_DPCM_STATE_CLOSE;
	change = (struct dpcm_path_change){};
	CHECK(candidate_dpcm_be_connect(
		      &fe, &retained, stream, &change) == 0);
	CHECK(change.type == DPCM_PATH_NEW);
	CHECK(change.dpcm != NULL);
	CHECK(change.dpcm->be == &retained);
	CHECK(!change.dpcm->route_stop_done);
	free(change.dpcm);

	/* A genuinely new path is recorded with its previous update state. */
	new_be.dpcm[stream].state = SND_SOC_DPCM_STATE_NEW;
	new_be.dpcm[stream].runtime_update = 7;
	change = (struct dpcm_path_change){};
	CHECK(candidate_dpcm_be_connect(
		      &fe, &new_be, stream, &change) == 0);
	CHECK(change.type == DPCM_PATH_NEW);
	CHECK(change.dpcm != NULL);
	CHECK(change.dpcm->be == &new_be);
	CHECK(change.update == 7);
	CHECK(change.dpcm->state == SND_SOC_DPCM_LINK_STATE_NEW);
	free(change.dpcm);
}

static void check_dpcm_path_rollback(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_card card = {
		.dev = &device,
		.component_chaining = true,
		.pcm_mutex_locked = true,
	};
	struct snd_soc_dai_link retained_link = { .name = "retained-BE" };
	struct snd_soc_pcm_runtime fe = { .dev = &device, .card = &card };
	struct snd_soc_pcm_runtime retained = {
		.dev = &device,
		.dai_link = &retained_link,
	};
	struct snd_soc_pcm_runtime new_be_one = {
		.dev = &device,
		.dai_link = &retained_link,
	};
	struct snd_soc_pcm_runtime new_be_two = {
		.dev = &device,
		.dai_link = &retained_link,
	};
	struct snd_pcm_substream retained_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &retained,
	};
	struct snd_soc_dpcm *new_one = calloc(1, sizeof(*new_one));
	struct snd_soc_dpcm *new_two = calloc(1, sizeof(*new_two));
	struct snd_soc_dpcm retained_dpcm = {
		.be = &retained,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};
	struct snd_soc_dpcm unrelated = {
		.state = SND_SOC_DPCM_LINK_STATE_FREE,
	};
	struct dpcm_path_change changes[3];

	CHECK(new_one != NULL);
	CHECK(new_two != NULL);
	new_one->be = &new_be_one;
	new_one->fe = &fe;
	new_one->state = SND_SOC_DPCM_LINK_STATE_NEW;
	new_two->be = &new_be_two;
	new_two->fe = &fe;
	new_two->state = SND_SOC_DPCM_LINK_STATE_NEW;
	retained.be_substreams[stream] = &retained_substream;
	fe.be_links[stream][0] = new_one;
	fe.be_links[stream][1] = &retained_dpcm;
	fe.be_links[stream][2] = new_two;
	fe.be_links[stream][3] = &unrelated;
	fe.num_be_links[stream] = 4;

	retained.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	retained.dpcm[stream].be_start = 1;
	retained.dpcm[stream].be_pause = 0;
	retained.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_NO;
	retained.dpcm[stream].stop_result = 0;
	changes[0] = (struct dpcm_path_change){
		.dpcm = new_one,
		.type = DPCM_PATH_NEW,
		.update = SND_SOC_DPCM_UPDATE_BE,
	};
	changes[1] = (struct dpcm_path_change){
		.dpcm = &retained_dpcm,
		.type = DPCM_PATH_REUSED,
		.update = SND_SOC_DPCM_UPDATE_FE,
		.state = SND_SOC_DPCM_STATE_STOP,
		.be_start = 0,
		.be_pause = 0,
		.hardware_started = true,
	};
	changes[2] = (struct dpcm_path_change){
		.dpcm = new_two,
		.type = DPCM_PATH_NEW,
		.update = SND_SOC_DPCM_UPDATE_NO,
	};
	simulated_disconnect_one_calls = 0;
	simulated_trigger_calls = 0;

	candidate_dpcm_rollback_path_changes(&fe, stream, changes, 3);

	CHECK(simulated_disconnect_one_calls == 2);
	CHECK(simulated_trigger_calls == 1);
	CHECK(fe.num_be_links[stream] == 2);
	CHECK(fe.be_links[stream][0] == &retained_dpcm);
	CHECK(fe.be_links[stream][1] == &unrelated);
	CHECK(unrelated.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(retained.dpcm[stream].be_start == 0);
	CHECK(retained.dpcm[stream].be_pause == 0);
	CHECK(retained.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_FE);
	CHECK(new_be_one.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_BE);
	CHECK(new_be_two.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_NO);

	/*
	 * A rollback STOP failure cannot claim the retained link is free.
	 * Keep it active for the full route retry; the caller's failed
	 * dpcm_add_paths() keeps card->dpcm_route_update_pending set.
	 */
	retained_dpcm.state = SND_SOC_DPCM_LINK_STATE_NEW;
	retained.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	retained.dpcm[stream].be_start = 1;
	retained.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_NO;
	retained.dpcm[stream].stop_result = -EIO;
	fe.be_links[stream][0] = &retained_dpcm;
	fe.num_be_links[stream] = 1;
	changes[0] = (struct dpcm_path_change){
		.dpcm = &retained_dpcm,
		.type = DPCM_PATH_REUSED,
		.update = SND_SOC_DPCM_UPDATE_BE,
		.state = SND_SOC_DPCM_STATE_STOP,
		.hardware_started = true,
	};
	simulated_disconnect_one_calls = 0;
	simulated_trigger_calls = 0;

	candidate_dpcm_rollback_path_changes(&fe, stream, changes, 1);

	CHECK(simulated_disconnect_one_calls == 0);
	CHECK(simulated_trigger_calls == 1);
	CHECK(fe.num_be_links[stream] == 1);
	CHECK(retained_dpcm.state == SND_SOC_DPCM_LINK_STATE_NEW);
	CHECK(retained.dpcm[stream].state == SND_SOC_DPCM_STATE_START);
	CHECK(retained.dpcm[stream].be_start == 1);
	CHECK(retained.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_NO);
}

static void check_dpcm_shared_stop_replay_marker(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link_a = { .name = "shared-BE-A" };
	struct snd_soc_dai_link link_b = { .name = "shared-BE-B" };
	struct snd_soc_card card = {};
	struct snd_soc_pcm_runtime fe = {
		.dev = &device,
		.card = &card,
	};
	struct snd_soc_pcm_runtime be_a = {
		.dev = &device,
		.dai_link = &link_a,
	};
	struct snd_soc_pcm_runtime be_b = {
		.dev = &device,
		.dai_link = &link_b,
	};
	struct snd_pcm_substream be_a_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &be_a,
	};
	struct snd_pcm_substream be_b_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &be_b,
	};
	struct snd_soc_dpcm dpcm_a = {
		.be = &be_a,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};
	struct snd_soc_dpcm dpcm_b = {
		.be = &be_b,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};

	be_a.be_substreams[stream] = &be_a_substream;
	be_b.be_substreams[stream] = &be_b_substream;
	fe.be_links[stream][0] = &dpcm_a;
	fe.be_links[stream][1] = &dpcm_b;
	fe.num_be_links[stream] = 2;
	fe.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_BE;

	be_a.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	be_a.dpcm[stream].be_start = 2;
	be_a.dpcm[stream].users = 2;
	be_a.dpcm[stream].stop_result = 0;
	be_b.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	be_b.dpcm[stream].be_start = 1;
	be_b.dpcm[stream].users = 1;
	be_b.dpcm[stream].stop_result = -EIO;
	simulated_trigger_calls = 0;

	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe, stream, SNDRV_PCM_TRIGGER_STOP) == -EIO);
	CHECK(be_a.dpcm[stream].be_start == 1);
	CHECK(be_a.dpcm[stream].state == SND_SOC_DPCM_STATE_START);
	CHECK(dpcm_a.route_stop_done);
	CHECK(be_a.dpcm[stream].stop_calls == 0);
	CHECK(be_b.dpcm[stream].be_start == 1);
	CHECK(be_b.dpcm[stream].state == SND_SOC_DPCM_STATE_START);
	CHECK(!dpcm_b.route_stop_done);
	CHECK(be_b.dpcm[stream].stop_calls == 1);

	/* The full retry must not consume A's remaining shared reference. */
	be_b.dpcm[stream].stop_result = 0;
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be_a.dpcm[stream].be_start == 1);
	CHECK(dpcm_a.route_stop_done);
	CHECK(be_a.dpcm[stream].stop_calls == 0);
	CHECK(be_b.dpcm[stream].be_start == 0);
	CHECK(be_b.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(dpcm_b.route_stop_done);
	CHECK(be_b.dpcm[stream].stop_calls == 2);
	CHECK(simulated_trigger_calls == 2);
}

static void check_dpcm_ordinary_stop_keeps_marker_clear(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link = { .name = "ordinary-BE" };
	struct snd_soc_card card = {};
	struct snd_soc_pcm_runtime fe = {
		.dev = &device,
		.card = &card,
	};
	struct snd_soc_pcm_runtime be = {
		.dev = &device,
		.dai_link = &link,
	};
	struct snd_pcm_substream be_substream = {
		.pcm = &(struct snd_pcm){ .nonatomic = 1 },
		.stream = stream,
		.fe = &be,
	};
	struct snd_soc_dpcm dpcm = {
		.be = &be,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};

	be.be_substreams[stream] = &be_substream;
	fe.be_links[stream][0] = &dpcm;
	fe.num_be_links[stream] = 1;
	fe.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_FE;
	be.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	be.dpcm[stream].be_start = 1;
	be.dpcm[stream].stop_result = 0;

	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be.dpcm[stream].be_start == 0);
	CHECK(be.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(be.dpcm[stream].stop_calls == 1);
	CHECK(!dpcm.route_stop_done);
}

static void check_dpcm_pending_trigger_guard(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	const int blocked[] = {
		SNDRV_PCM_TRIGGER_START,
		SNDRV_PCM_TRIGGER_RESUME,
		SNDRV_PCM_TRIGGER_PAUSE_RELEASE,
		SNDRV_PCM_TRIGGER_DRAIN,
	};
	struct device device = {};
	struct snd_soc_dai_link link = {
		.name = "guarded-FE",
		.trigger = {
			SND_SOC_DPCM_TRIGGER_PRE,
			SND_SOC_DPCM_TRIGGER_PRE,
		},
	};
	struct snd_soc_card card = { .dev = &device };
	struct snd_soc_pcm_runtime fe = {
		.dev = &device,
		.card = &card,
		.dai_link = &link,
	};
	struct snd_pcm_substream substream = {
		.stream = stream,
		.fe = &fe,
	};

	fe.dpcm[stream].state = SND_SOC_DPCM_STATE_STOP;
	fe.dpcm[stream].hw_free_failed = false;
	card.dpcm_route_update_pending = true;
	simulated_fe_be_trigger_calls = 0;

	for (unsigned int i = 0; i < sizeof(blocked) / sizeof(blocked[0]); i++) {
		CHECK(candidate_dpcm_trigger(&substream, blocked[i]) == -EIO);
		CHECK(fe.dpcm[stream].runtime_update ==
		      SND_SOC_DPCM_UPDATE_NO);
		CHECK(fe.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
		CHECK(simulated_fe_be_trigger_calls == 0);
	}

	/* A failed hw_free keeps the existing START-class guard in force. */
	card.dpcm_route_update_pending = false;
	fe.dpcm[stream].hw_free_failed = true;
	CHECK(candidate_dpcm_trigger(
		      &substream, SNDRV_PCM_TRIGGER_START) == -EIO);
	CHECK(simulated_fe_be_trigger_calls == 0);

	/* With both guards clear, the real candidate reaches FE/BE dispatch. */
	fe.dpcm[stream].hw_free_failed = false;
	CHECK(candidate_dpcm_trigger(
		      &substream, SNDRV_PCM_TRIGGER_SUSPEND) == -EAGAIN);
	CHECK(simulated_fe_be_trigger_calls == 1);
	CHECK(fe.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_NO);
}

static void check_dpcm_shared_be_across_fe_stop(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link = { .name = "shared-across-FE" };
	struct snd_soc_card card = {};
	struct snd_soc_pcm_runtime fe_a = {
		.dev = &device,
		.card = &card,
	};
	struct snd_soc_pcm_runtime fe_b = {
		.dev = &device,
		.card = &card,
	};
	struct snd_soc_pcm_runtime be = {
		.dev = &device,
		.card = &card,
		.dai_link = &link,
	};
	struct snd_pcm_substream be_substream = {
		.stream = stream,
		.fe = &be,
	};
	struct snd_soc_dpcm dpcm_a = {
		.fe = &fe_a,
		.be = &be,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};
	struct snd_soc_dpcm dpcm_b = {
		.fe = &fe_b,
		.be = &be,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};

	be.be_substreams[stream] = &be_substream;
	fe_a.be_links[stream][0] = &dpcm_a;
	fe_a.num_be_links[stream] = 1;
	fe_b.be_links[stream][0] = &dpcm_b;
	fe_b.num_be_links[stream] = 1;
	fe_a.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_BE;
	fe_b.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_BE;
	be.dpcm[stream].state = SND_SOC_DPCM_STATE_START;
	be.dpcm[stream].be_start = 2;
	be.dpcm[stream].users = 2;
	be.dpcm[stream].stop_result = 0;
	simulated_trigger_calls = 0;

	/* FE A consumes one reference without stopping the shared hardware. */
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe_a, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be.dpcm[stream].be_start == 1);
	CHECK(be.dpcm[stream].state == SND_SOC_DPCM_STATE_START);
	CHECK(dpcm_a.route_stop_done);
	CHECK(!dpcm_b.route_stop_done);
	CHECK(be.dpcm[stream].stop_calls == 0);

	/* A failed later FE retries A; its consumed reference stays consumed. */
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe_a, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be.dpcm[stream].be_start == 1);
	CHECK(dpcm_a.route_stop_done);
	CHECK(be.dpcm[stream].stop_calls == 0);

	/* FE B consumes the last reference and performs the sole HW STOP. */
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe_b, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be.dpcm[stream].be_start == 0);
	CHECK(be.dpcm[stream].state == SND_SOC_DPCM_STATE_STOP);
	CHECK(dpcm_b.route_stop_done);
	CHECK(be.dpcm[stream].stop_calls == 1);

	/* Another full replay remains idempotent for both links. */
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe_a, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(candidate_dpcm_be_dai_trigger(
		      &fe_b, stream, SNDRV_PCM_TRIGGER_STOP) == 0);
	CHECK(be.dpcm[stream].be_start == 0);
	CHECK(be.dpcm[stream].stop_calls == 1);
	CHECK(simulated_trigger_calls == 1);
}

static void check_dpcm_real_prune_paths(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link = { .name = "prune-BE" };
	struct snd_soc_card card = {};
	struct snd_soc_pcm_runtime fe = {
		.dev = &device,
		.card = &card,
	};
	struct snd_soc_pcm_runtime be = {
		.dev = &device,
		.dai_link = &link,
	};
	struct snd_pcm_substream be_substream = {
		.stream = stream,
		.fe = &be,
	};
	struct snd_soc_dpcm dpcm = {
		.be = &be,
		.fe = &fe,
		.state = SND_SOC_DPCM_LINK_STATE_NEW,
	};
	struct snd_soc_dapm_widget_list *list = &simulated_path_list;

	be.be_substreams[stream] = &be_substream;
	fe.be_links[stream][0] = &dpcm;
	fe.num_be_links[stream] = 1;
	be.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_NO;

	/* A normal prune still respects the current DAPM graph. */
	simulated_be_is_active = true;
	simulated_be_is_active_calls = 0;
	CHECK(candidate_dpcm_prune_paths(&fe, stream, &list, false) == 0);
	CHECK(simulated_be_is_active_calls == 1);
	CHECK(dpcm.state == SND_SOC_DPCM_LINK_STATE_NEW);

	/* A completed partial STOP converges even if the graph contains it. */
	dpcm.route_stop_done = true;
	simulated_be_is_active_calls = 0;
	CHECK(candidate_dpcm_prune_paths(&fe, stream, &list, false) == 1);
	CHECK(simulated_be_is_active_calls == 0);
	CHECK(dpcm.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(be.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_BE);

	/* A full replay force-prunes every retained link, active or not. */
	dpcm.state = SND_SOC_DPCM_LINK_STATE_NEW;
	dpcm.route_stop_done = false;
	be.dpcm[stream].runtime_update = SND_SOC_DPCM_UPDATE_NO;
	simulated_be_is_active_calls = 0;
	CHECK(candidate_dpcm_prune_paths(&fe, stream, &list, true) == 1);
	CHECK(simulated_be_is_active_calls == 0);
	CHECK(dpcm.state == SND_SOC_DPCM_LINK_STATE_FREE);
	CHECK(be.dpcm[stream].runtime_update == SND_SOC_DPCM_UPDATE_BE);
}

static void check_dpcm_be_disconnect_one_reparents(void)
{
	const int stream = SNDRV_PCM_STREAM_CAPTURE;
	struct device device = {};
	struct snd_soc_dai_link link = { .name = "shared-BE" };
	struct snd_soc_card card = {
		.dev = &device,
		.pcm_mutex_locked = true,
	};
	struct snd_soc_pcm_runtime fe = {
		.dev = &device,
		.dai_link = &link,
		.card = &card,
	};
	struct snd_soc_pcm_runtime be = {
		.dev = &device,
		.dai_link = &link,
		.card = &card,
	};
	struct snd_pcm_substream fe_substream = {
		.stream = stream,
		.fe = &fe,
	};
	struct snd_soc_dpcm *victim = calloc(1, sizeof(*victim));

	CHECK(victim != NULL);
	victim->fe = &fe;
	victim->be = &be;
	fe.be_substreams[stream] = &fe_substream;
	simulated_reparent_calls = 0;

	candidate_dpcm_be_disconnect_one(&fe, stream, victim);

	CHECK(simulated_reparent_calls == 1);
	CHECK(!fe.dpcm[stream].stream_locked);
}

static void check_lpass_trigger_stop_lifecycle(void)
{
	struct device device = {};
	struct q6apm_lpass_dai_data data = {};
	struct snd_soc_dai dai = { .id = 2, .dev = &device };
	struct q6apm_graph graph = {};

	device.drv = &data;
	data.graph[dai.id] = &graph;
	simulated_lpass_graph_start_calls = 0;
	simulated_lpass_graph_stop_calls = 0;

	/* An inactive port must not issue graph STOP. */
	simulated_lpass_graph_stop_result = 0;
	CHECK(candidate_lpass_trigger(NULL, SNDRV_PCM_TRIGGER_STOP, &dai) == 0);
	CHECK(simulated_lpass_graph_stop_calls == 0);

	/* A failed STOP keeps the started bit so the upper layer can retry. */
	data.is_port_started[dai.id] = true;
	simulated_lpass_graph_stop_result = -EIO;
	CHECK(candidate_lpass_trigger(NULL, SNDRV_PCM_TRIGGER_STOP, &dai) == -EIO);
	CHECK(simulated_lpass_graph_stop_calls == 1);
	CHECK(data.is_port_started[dai.id]);

	/* Only a successful graph STOP retires the started bit. */
	simulated_lpass_graph_stop_result = 0;
	CHECK(candidate_lpass_trigger(NULL, SNDRV_PCM_TRIGGER_STOP, &dai) == 0);
	CHECK(simulated_lpass_graph_stop_calls == 2);
	CHECK(!data.is_port_started[dai.id]);
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

static void check_component_recovery_replays_without_mark(void)
{
	struct snd_soc_component_driver driver = {
		.hw_params = simulate_component_hw_params,
		.hw_free = simulate_component_hw_free,
	};
	struct snd_soc_component first = { .driver = &driver };
	struct snd_soc_component failed = { .driver = &driver };
	struct snd_soc_component *components[] = { &first, &failed };
	struct snd_soc_dai_link link = { .name = "mark-FE" };
	struct snd_soc_pcm_runtime fe = {
		.dai_link = &link,
		.components = components,
		.num_components = 2,
	};
	struct snd_pcm_substream playback = {
		.stream = SNDRV_PCM_STREAM_PLAYBACK,
		.fe = &fe,
	};
	struct snd_pcm_substream capture = {
		.stream = SNDRV_PCM_STREAM_CAPTURE,
		.fe = &fe,
	};

	CHECK(snd_soc_pcm_component_hw_params(&capture, NULL) == 0);
	CHECK(first.mark_hw_params == &capture);
	CHECK(failed.mark_hw_params == &capture);

	/* Playback overlap overwrites the single upstream unwind mark. */
	CHECK(snd_soc_pcm_component_hw_params(&playback, NULL) == 0);
	CHECK(first.mark_hw_params == &playback);
	CHECK(failed.mark_hw_params == &playback);

	/*
	 * The first capture cleanup fails after playback overwrote the mark.
	 * It still clears the mark; DPCM state must drive the later recovery.
	 */
	failed.hw_free_result[SNDRV_PCM_STREAM_CAPTURE] = -ETIMEDOUT;
	CHECK(snd_soc_pcm_component_hw_free(&capture, 0) == -ETIMEDOUT);
	CHECK(first.mark_hw_params == NULL);
	CHECK(failed.mark_hw_params == NULL);
	CHECK(first.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 1);
	CHECK(failed.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 1);

	/* Playback cleanup repeats safely even though its mark is already gone. */
	CHECK(snd_soc_pcm_component_hw_free(&playback, 0) == 0);
	CHECK(first.mark_hw_params == NULL);
	CHECK(failed.mark_hw_params == NULL);

	/*
	 * rollback=0 repeats every component even though both marks are gone.
	 * This is the recovery mode used by both delayed DPCM cleanup paths.
	 */
	failed.hw_free_result[SNDRV_PCM_STREAM_CAPTURE] = 0;
	CHECK(snd_soc_pcm_component_hw_free(&capture, 0) == 0);
	CHECK(first.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 2);
	CHECK(failed.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 2);

	/* A marked rollback would skip the absent component, so recovery uses zero. */
	CHECK(snd_soc_pcm_component_hw_free(&capture, 1) == 0);
	CHECK(first.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 2);
	CHECK(failed.hw_free_calls[SNDRV_PCM_STREAM_CAPTURE] == 2);
	CHECK(first.hw_params_calls[SNDRV_PCM_STREAM_CAPTURE] == 1);
	CHECK(failed.hw_params_calls[SNDRV_PCM_STREAM_CAPTURE] == 1);
	CHECK(first.hw_params_calls[SNDRV_PCM_STREAM_PLAYBACK] == 1);
	CHECK(failed.hw_params_calls[SNDRV_PCM_STREAM_PLAYBACK] == 1);
	CHECK(first.hw_free_calls[SNDRV_PCM_STREAM_PLAYBACK] == 1);
	CHECK(failed.hw_free_calls[SNDRV_PCM_STREAM_PLAYBACK] == 1);
}

static void check_dpcm_close_replays_pending_routes(void)
{
	struct device device = {};
	struct snd_soc_dai_link dynamic_link = {
		.name = "close-retry",
		.dynamic = true,
	};
	struct snd_soc_pcm_runtime fe = {
		.dai_link = &dynamic_link,
		.dev = &device,
	};
	struct snd_soc_pcm_runtime retained_fe = {
		.dai_link = &dynamic_link,
		.dev = &device,
	};
	struct snd_soc_pcm_runtime *rtds[] = { &fe, &retained_fe };
	struct snd_soc_card card = {
		.dev = &device,
		.rtds = rtds,
		.num_rtds = 2,
	};
	struct snd_pcm_substream substream = {
		.stream = SNDRV_PCM_STREAM_CAPTURE,
		.fe = &fe,
	};
	struct fake_dpcm_runtime *path =
		&fe.dpcm[SNDRV_PCM_STREAM_CAPTURE];
	struct fake_dpcm_runtime *retained_path =
		&retained_fe.dpcm[SNDRV_PCM_STREAM_CAPTURE];

	fe.card = &card;
	retained_fe.card = &card;
	path->hw_free_failed = true;
	path->route_connected = true;
	path->route_wanted = false;
	retained_path->route_connected = true;
	retained_path->route_wanted = false;
	card.dpcm_route_update_pending = true;

	simulated_component_result = -ETIMEDOUT;
	simulated_be_trigger_result = 0;
	simulated_be_trigger_calls = 0;
	simulated_be_free_calls = 0;
	simulated_be_shutdown_calls = 0;
	simulated_clear_pending_calls = 0;
	simulated_disconnect_calls = 0;
	simulated_dpcm_cleanup_calls = 0;
	simulated_runtime_update_calls = 0;
	simulated_path_get_calls = 0;

	CHECK(dpcm_fe_dai_close(&substream) == -ETIMEDOUT);
	CHECK(simulated_dpcm_cleanup_calls == 1);
	CHECK(!card.dpcm_route_update_pending);
	CHECK(path->state == SND_SOC_DPCM_STATE_CLOSE);
	CHECK(!path->hw_free_failed);
	CHECK(!path->route_connected);
	CHECK(!retained_path->route_connected);
	CHECK(simulated_path_get_calls == 2);
	CHECK(simulated_runtime_update_calls == 6);
	CHECK(simulated_runtime_update_order[0] == 0);
	CHECK(simulated_runtime_update_order[1] == 0);
	CHECK(simulated_runtime_update_order[2] == 0);
	CHECK(simulated_runtime_update_order[3] == 0);
	CHECK(simulated_runtime_update_order[4] == 1);
	CHECK(simulated_runtime_update_order[5] == 1);
	CHECK(simulated_be_trigger_calls == 1);
	CHECK(simulated_be_free_calls == 2);
	CHECK(simulated_be_shutdown_calls == 2);
	CHECK(simulated_clear_pending_calls == 1);
	CHECK(simulated_disconnect_calls == 1);

	/*
	 * Once shutdown succeeds, a deferred -EBUSY belongs to the close caller.
	 * Another FE still recovering keeps the route pending for its next retry.
	 */
	path->hw_free_failed = false;
	retained_path->hw_free_failed = true;
	card.dpcm_route_update_pending = true;
	simulated_component_result = 0;
	simulated_component_clean_calls = 0;
	simulated_dpcm_cleanup_calls = 0;
	simulated_runtime_update_calls = 0;
	simulated_path_get_calls = 0;

	CHECK(dpcm_fe_dai_close(&substream) == -EBUSY);
	CHECK(simulated_dpcm_cleanup_calls == 1);
	CHECK(card.dpcm_route_update_pending);
	CHECK(simulated_component_clean_calls == 0);
	CHECK(simulated_runtime_update_calls == 0);
	CHECK(simulated_path_get_calls == 0);
}

int main(void)
{
	check_start_stop_sequences();
	check_concurrent_start_during_stop();
	check_stop_replies();
	check_unmap_replies();
	check_dpcm_stop_failure_recovery();
	check_dpcm_runtime_update_guard();
	check_dpcm_stop_failure_route_retry();
	check_dpcm_route_connect_failure_stays_pending();
	check_dpcm_path_connect_changes();
	check_dpcm_path_rollback();
	check_dpcm_shared_stop_replay_marker();
	check_dpcm_ordinary_stop_keeps_marker_clear();
	check_dpcm_pending_trigger_guard();
	check_dpcm_shared_be_across_fe_stop();
	check_dpcm_real_prune_paths();
	check_dpcm_be_disconnect_one_reparents();
	check_lpass_trigger_stop_lifecycle();
	check_component_recovery_replays_without_mark();
	check_dpcm_close_replays_pending_routes();
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
	bool dynamic;
	int num_cpus;
	int trigger[2];
};

struct snd_soc_dapm_widget;

struct snd_soc_dapm_widget_list {
	int num_widgets;
	struct snd_soc_dapm_widget *widgets[8];
};

enum snd_soc_dapm_type {
	snd_soc_dapm_dai_in,
	snd_soc_dapm_dai_out,
};

struct snd_soc_dapm_widget {
	enum snd_soc_dapm_type id;
	const char *name;
	struct snd_soc_pcm_runtime *be;
};

struct snd_soc_component_driver {
	int (*hw_params)(struct snd_soc_component *component,
			 struct snd_pcm_substream *substream,
			 void *params);
	int (*hw_free)(struct snd_soc_component *component,
		       struct snd_pcm_substream *substream);
};

struct snd_soc_component {
	const struct snd_soc_component_driver *driver;
	struct snd_pcm_substream *mark_hw_params;
	int hw_params_result[2];
	int hw_free_result[2];
	int hw_params_calls[2];
	int hw_free_calls[2];
};

struct fake_dpcm_runtime {
	bool hw_free_failed;
	int state;
	int runtime_update;
	bool route_connected;
	bool route_wanted;
	bool link_free;
	int pending_state;
	int be_start;
	int be_pause;
	int users;
	bool fe_pause;
	int stop_result;
	int start_result;
	int pause_result;
	int stop_calls;
	int start_calls;
	int pause_calls;
	bool stream_locked;
	struct snd_soc_pcm_runtime *observe_be;
	int observed_be_start;
	int be_clients;
	int fe_clients;
};

struct snd_soc_dpcm;

struct snd_soc_pcm_runtime {
	struct fake_dpcm_runtime dpcm[2];
	struct snd_soc_dai_link *dai_link;
	struct device *dev;
	struct snd_soc_card *card;
	struct snd_soc_component **components;
	int num_components;
	struct snd_soc_dpcm *be_links[2][8];
	int num_be_links[2];
	struct snd_pcm_substream *be_substreams[2];
};

enum dpcm_path_change_type {
	DPCM_PATH_UNCHANGED,
	DPCM_PATH_NEW,
	DPCM_PATH_REUSED,
};

struct dpcm_path_change {
	struct snd_soc_dpcm *dpcm;
	enum dpcm_path_change_type type;
	int update;
	int state;
	int be_start;
	int be_pause;
	bool hardware_started;
};

struct snd_soc_dpcm {
	struct snd_soc_pcm_runtime *fe;
	struct snd_soc_pcm_runtime *be;
	int state;
	bool route_stop_done;
	int list_be;
	int list_fe;
};

#define APM_PORT_MAX 16

struct snd_soc_dai {
	int id;
	struct device *dev;
};

struct q6apm_lpass_dai_data {
	struct q6apm_graph *graph[APM_PORT_MAX];
	bool is_port_started[APM_PORT_MAX];
	bool is_prepared[APM_PORT_MAX];
	bool is_restored[APM_PORT_MAX];
};

struct snd_soc_card {
	struct device *dev;
	struct snd_soc_pcm_runtime **rtds;
	int num_rtds;
	bool dpcm_route_update_pending;
	bool pcm_mutex_locked;
	bool component_chaining;
};

#define SND_SOC_DPCM_STATE_NEW 0
#define SND_SOC_DPCM_STATE_OPEN 6
#define SND_SOC_DPCM_STATE_HW_PARAMS 7
#define SND_SOC_DPCM_UPDATE_FE 1
#define SND_SOC_DPCM_UPDATE_BE 2
#define SND_SOC_DPCM_UPDATE_NO 0
#define SND_SOC_DPCM_STATE_PREPARE 1
#define SND_SOC_DPCM_STATE_START 2
#define SND_SOC_DPCM_STATE_STOP 3
#define SND_SOC_DPCM_STATE_PAUSED 4
#define SND_SOC_DPCM_STATE_SUSPEND 5
#define SND_SOC_DPCM_STATE_HW_FREE 8
#define SND_SOC_DPCM_STATE_CLOSE 9
#define SND_SOC_DPCM_LINK_STATE_NEW 0
#define SND_SOC_DPCM_LINK_STATE_FREE 2
enum snd_soc_dpcm_trigger {
	SND_SOC_DPCM_TRIGGER_PRE,
	SND_SOC_DPCM_TRIGGER_POST,
};
#define SND_SOC_DAPM_STREAM_NOP 0
#define SND_SOC_DAPM_STREAM_STOP 0
#define dev_dbg(dev, ...) do { (void)(dev); if (0) fprintf(stderr, __VA_ARGS__); } while (0)
#define dev_err(dev, ...) do { (void)(dev); if (0) fprintf(stderr, __VA_ARGS__); } while (0)
#define snd_soc_substream_to_rtd(substream) ((substream)->fe)
#define for_each_card_rtds(card, rtd) \
	for (int __i = 0; __i < (card)->num_rtds && \
	     (((rtd) = (card)->rtds[__i]), true); __i++)
#define for_each_pcm_streams(stream) for ((stream) = 0; (stream) < 2; (stream)++)
#define for_each_dapm_widgets(list, i, widget) \
	for ((i) = 0; \
	     (i) < (list)->num_widgets && (((widget) = (list)->widgets[i]), true); \
	     (i)++)
#define for_each_rtd_components(rtd, i, component) \
	for ((i) = 0; (i) < (rtd)->num_components && \
	     (((component) = (rtd)->components[i]), true); (i)++)
#define for_each_dpcm_be(fe, stream, dpcm) \
	for (int __dpcm_i = 0; __dpcm_i < (fe)->num_be_links[stream] && \
	     (((dpcm) = (fe)->be_links[stream][__dpcm_i]), true); __dpcm_i++)
#define snd_soc_dpcm_get_substream(be, stream) ((be)->be_substreams[stream])
#define snd_pcm_stream_lock_irqsave_nested(substream, flags) \
	do { \
		(substream)->fe->dpcm[(substream)->stream].stream_locked = true; \
		(flags) = 0; \
	} while (0)
#define snd_pcm_stream_unlock_irqrestore(substream, flags) \
	do { \
		(substream)->fe->dpcm[(substream)->stream].stream_locked = false; \
		(void)(flags); \
	} while (0)
#define snd_pcm_stream_lock_irq(substream) \
	do { (substream)->fe->dpcm[(substream)->stream].stream_locked = true; } while (0)
#define snd_pcm_stream_unlock_irq(substream) \
	do { (substream)->fe->dpcm[(substream)->stream].stream_locked = false; } while (0)
#define kzalloc_obj(type) calloc(1, sizeof(type))
#define kcalloc(count, size, flags) calloc((count), (size))
#define kfree(ptr) free(ptr)
#define GFP_KERNEL 0
#define list_add(node, head) \
	do { \
		(void)(node); \
		(void)(head); \
	} while (0)
#define list_del(node) do { (void)(node); } while (0)

#define soc_component_mark_push(component, substream, target) \
	((component)->mark_##target = (substream))
#define soc_component_mark_match(component, substream, target) \
	((component)->mark_##target == (substream))
#define soc_component_mark_pop(component, target) \
	((component)->mark_##target = NULL)

static int simulated_component_result;
static int simulated_whole_clean_calls;
static int simulated_component_clean_calls;
static int simulated_component_rollback;
static int simulated_dai_free_calls;
static int simulated_be_free_calls;
static int simulated_be_shutdown_calls;
static int simulated_runtime_update_calls;
static int simulated_runtime_update_order[32];
static int simulated_be_trigger_result;
static int simulated_be_trigger_calls;
static int simulated_clear_pending_calls;
static int simulated_disconnect_calls;
static int simulated_startup_result;
static int simulated_path_get_result = 1;
static int simulated_add_paths_result = 1;
static int simulated_path_get_calls;
static int simulated_active_stream = SNDRV_PCM_STREAM_CAPTURE;
static int simulated_dpcm_cleanup_calls;
static int simulated_trigger_calls;
static int simulated_fe_be_trigger_calls;
static int simulated_update_no_calls;
static int simulated_update_no_state = -1;
static int simulated_disconnect_one_calls;
static int simulated_reparent_calls;
static int simulated_lpass_graph_start_calls;
static int simulated_lpass_graph_stop_calls;
static int simulated_lpass_graph_start_result;
static int simulated_lpass_graph_stop_result;
static bool simulated_be_is_active;
static int simulated_be_is_active_calls;
static int simulated_prune_replay = -1;
static struct snd_soc_dapm_widget_list simulated_path_list;

static int simulate_component_hw_params(struct snd_soc_component *component,
					struct snd_pcm_substream *substream,
					void *params)
{
	int stream = substream->stream;

	(void)params;
	component->hw_params_calls[stream]++;
	return component->hw_params_result[stream];
}

static int simulate_component_hw_free(struct snd_soc_component *component,
				      struct snd_pcm_substream *substream)
{
	int stream = substream->stream;

	component->hw_free_calls[stream]++;
	return component->hw_free_result[stream];
}

static int soc_component_ret(struct snd_soc_component *component, int ret)
{
	(void)component;
	return ret;
}

static void *snd_soc_rtd_to_cpu(struct snd_soc_pcm_runtime *fe, int index)
{
	(void)index;
	return fe;
}

static void *snd_soc_rtd_to_codec(struct snd_soc_pcm_runtime *fe, int index)
{
	(void)index;
	return fe;
}

static bool snd_soc_dai_stream_valid(void *dai, int stream)
{
	(void)dai;
	(void)stream;
	return true;
}

static bool snd_soc_dai_active(void *dai)
{
	(void)dai;
	return true;
}

static bool snd_soc_dai_stream_active(void *dai, int stream)
{
	(void)dai;
	return stream == simulated_active_stream;
}

static __attribute__((unused)) bool snd_soc_dpcm_can_be_update(
	struct snd_soc_pcm_runtime *fe,
	struct snd_soc_pcm_runtime *be,
	int stream)
{
	(void)fe;
	(void)be;
	(void)stream;
	return true;
}

static const char *dpcm_state_string(int state)
{
	(void)state;
	return "not-running";
}

static int soc_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct fake_dpcm_runtime *dpcm = &substream->fe->dpcm[substream->stream];

	CHECK(dpcm->stream_locked);
	simulated_trigger_calls++;
	if (cmd == SNDRV_PCM_TRIGGER_STOP && dpcm->observe_be)
		dpcm->observed_be_start =
			dpcm->observe_be->dpcm[substream->stream].be_start;
	if (cmd == SNDRV_PCM_TRIGGER_STOP) {
		dpcm->stop_calls++;
		return dpcm->stop_result;
	}
	if (cmd == SNDRV_PCM_TRIGGER_START) {
		dpcm->start_calls++;
		return dpcm->start_result;
	}
	if (cmd == SNDRV_PCM_TRIGGER_PAUSE_PUSH ||
	    cmd == SNDRV_PCM_TRIGGER_PAUSE_RELEASE) {
		dpcm->pause_calls++;
		return dpcm->pause_result;
	}
	return 0;
}

static int dpcm_dai_trigger_fe_be(struct snd_pcm_substream *substream,
				  int cmd, bool fe_first)
{
	(void)substream;
	(void)cmd;
	(void)fe_first;
	simulated_fe_be_trigger_calls++;
	return -EAGAIN;
}

static void dpcm_set_be_update_state(struct snd_soc_pcm_runtime *be,
				     int stream, int state)
{
	be->dpcm[stream].runtime_update = state;
	simulated_update_no_state = state;
	if (state == SND_SOC_DPCM_UPDATE_NO)
		simulated_update_no_calls++;
}

static int simulated_lpass_graph_start(struct q6apm_graph *graph)
{
	(void)graph;
	simulated_lpass_graph_start_calls++;
	return simulated_lpass_graph_start_result;
}

static int simulated_lpass_graph_stop(struct q6apm_graph *graph)
{
	(void)graph;
	simulated_lpass_graph_stop_calls++;
	return simulated_lpass_graph_stop_result;
}

static void dpcm_create_debugfs_state(struct snd_soc_dpcm *dpcm, int stream)
{
	(void)dpcm;
	(void)stream;
}

static void simulate_dpcm_be_reparent(struct snd_soc_pcm_runtime *fe,
			     struct snd_soc_pcm_runtime *be, int stream)
{
	(void)fe;
	(void)be;
	(void)stream;
	simulated_reparent_calls++;
}

static void dpcm_remove_debugfs_state(struct snd_soc_dpcm *dpcm)
{
	(void)dpcm;
}

static int dpcm_path_get(struct snd_soc_pcm_runtime *fe, int stream,
			 struct snd_soc_dapm_widget_list **list)
{
	(void)stream;
	*list = &simulated_path_list;
	simulated_path_get_calls++;
	return simulated_path_get_result;
}

static void dpcm_path_put(struct snd_soc_dapm_widget_list **list)
{
	*list = NULL;
}

static bool simulated_dpcm_be_is_active(
	struct snd_soc_dpcm *dpcm,
	int stream,
	struct snd_soc_dapm_widget_list *list)
{
	(void)dpcm;
	(void)stream;
	(void)list;
	simulated_be_is_active_calls++;
	return simulated_be_is_active;
}

static int dpcm_prune_paths(struct snd_soc_pcm_runtime *fe, int stream,
			    struct snd_soc_dapm_widget_list **list, bool replay)
{
	struct fake_dpcm_runtime *dpcm = &fe->dpcm[stream];

	(void)list;
	simulated_prune_replay = replay;
	CHECK(simulated_runtime_update_calls <
	      (int)(sizeof(simulated_runtime_update_order) /
		    sizeof(simulated_runtime_update_order[0])));
	simulated_runtime_update_order[simulated_runtime_update_calls] = 0;
	simulated_runtime_update_calls++;
	if (replay) {
		if (!dpcm->route_connected)
			return 0;
		dpcm->link_free = true;
		return 1;
	}
	if (!dpcm->route_connected || dpcm->route_wanted)
		return 0;
	dpcm->link_free = true;
	return 1;
}

static int dpcm_add_paths(struct snd_soc_pcm_runtime *fe, int stream,
			  struct snd_soc_dapm_widget_list **list)
{
	struct fake_dpcm_runtime *dpcm = &fe->dpcm[stream];

	(void)list;
	CHECK(simulated_runtime_update_calls <
	      (int)(sizeof(simulated_runtime_update_order) /
		    sizeof(simulated_runtime_update_order[0])));
	simulated_runtime_update_order[simulated_runtime_update_calls] = 1;
	simulated_runtime_update_calls++;
	if (simulated_add_paths_result < 0)
		return simulated_add_paths_result;
	if (!dpcm->route_wanted || dpcm->route_connected)
		return 0;
	dpcm->route_connected = true;
	dpcm->link_free = false;
	return 1;
}

static void simulated_dpcm_mutex_lock_card(struct snd_soc_card *card)
{
	assert(!card->pcm_mutex_locked);
	card->pcm_mutex_locked = true;
}

static void simulated_dpcm_mutex_unlock_card(struct snd_soc_card *card)
{
	assert(card->pcm_mutex_locked);
	card->pcm_mutex_locked = false;
}

static void simulated_dpcm_mutex_assert_card(struct snd_soc_card *card)
{
	assert(card->pcm_mutex_locked);
}

static void simulated_dpcm_mutex_lock_rtd(struct snd_soc_pcm_runtime *rtd)
{
	simulated_dpcm_mutex_lock_card(rtd->card);
}

static void simulated_dpcm_mutex_unlock_rtd(struct snd_soc_pcm_runtime *rtd)
{
	simulated_dpcm_mutex_unlock_card(rtd->card);
}

static void simulated_dpcm_mutex_assert_rtd(struct snd_soc_pcm_runtime *rtd)
{
	simulated_dpcm_mutex_assert_card(rtd->card);
}

#define snd_soc_dpcm_mutex_lock(x) _Generic((x), \
	struct snd_soc_card *: simulated_dpcm_mutex_lock_card, \
	struct snd_soc_pcm_runtime *: simulated_dpcm_mutex_lock_rtd)(x)
#define snd_soc_dpcm_mutex_unlock(x) _Generic((x), \
	struct snd_soc_card *: simulated_dpcm_mutex_unlock_card, \
	struct snd_soc_pcm_runtime *: simulated_dpcm_mutex_unlock_rtd)(x)
#define snd_soc_dpcm_mutex_assert_held(x) _Generic((x), \
	struct snd_soc_card *: simulated_dpcm_mutex_assert_card, \
	struct snd_soc_pcm_runtime *: simulated_dpcm_mutex_assert_rtd)(x)

static void dpcm_set_fe_update_state(struct snd_soc_pcm_runtime *fe,
				     int stream, int state)
{
	(void)fe;
	(void)stream;
	fe->dpcm[stream].pending_state = state;
}

static int soc_pcm_hw_clean(struct snd_soc_pcm_runtime *fe,
			    struct snd_pcm_substream *substream,
			    int rollback, bool stop_on_component_error)
{
	(void)fe;
	(void)substream;
	(void)rollback;
	(void)stop_on_component_error;
	simulated_whole_clean_calls++;
	return simulated_component_result;
}

static int soc_pcm_hw_clean_components(struct snd_pcm_substream *substream,
				       int rollback)
{
	(void)substream;
	simulated_component_clean_calls++;
	simulated_component_rollback = rollback;
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

static void __soc_pcm_close(struct snd_soc_pcm_runtime *fe,
			    struct snd_pcm_substream *substream)
{
	(void)fe;
	(void)substream;
}

static int dpcm_be_dai_trigger(struct snd_soc_pcm_runtime *fe, int stream,
			       int cmd)
{
	(void)fe;
	(void)stream;
	(void)cmd;
	simulated_be_trigger_calls++;
	return simulated_be_trigger_result;
}

static int dpcm_run_update_startup(struct snd_soc_pcm_runtime *fe, int stream)
{
	if (simulated_startup_result < 0)
		fe->dpcm[stream].link_free = true;
	return simulated_startup_result;
}

static void dpcm_clear_pending_state(struct snd_soc_pcm_runtime *fe, int stream)
{
	(void)fe;
	(void)stream;
	simulated_clear_pending_calls++;
}

static void dpcm_be_disconnect(struct snd_soc_pcm_runtime *fe, int stream)
{
	if (fe->dpcm[stream].link_free) {
		fe->dpcm[stream].route_connected = false;
		fe->dpcm[stream].link_free = false;
		simulated_disconnect_calls++;
	}
}

static void dpcm_be_disconnect_one(struct snd_soc_pcm_runtime *fe,
				   int stream, struct snd_soc_dpcm *victim)
{
	for (int i = 0; i < fe->num_be_links[stream]; i++) {
		if (fe->be_links[stream][i] != victim)
			continue;

		for (int j = i + 1; j < fe->num_be_links[stream]; j++)
			fe->be_links[stream][j - 1] = fe->be_links[stream][j];
		fe->be_links[stream][--fe->num_be_links[stream]] = NULL;
		simulated_disconnect_one_calls++;
		free(victim);
		return;
	}
	CHECK(!"attempted to disconnect an unrecorded DPCM link");
}

static const char *snd_pcm_direction_name(int stream)
{
	static const char *directions[] = { "playback", "capture" };
	CHECK(stream >= SNDRV_PCM_STREAM_PLAYBACK &&
	      stream <= SNDRV_PCM_STREAM_CAPTURE);
	CHECK(directions[stream] != NULL);
	return "capture";
}

static void dpcm_dapm_stream_event(struct snd_soc_pcm_runtime *fe,
				   int stream, int event)
{
	(void)fe;
	(void)stream;
	(void)event;
}

static int soc_pcm_ret(struct snd_soc_pcm_runtime *fe, int ret)
{
	(void)fe;
	return ret;
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
        "ret = soc_pcm_hw_clean(fe, substream, 0, true);"
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
    runtime_update_locked, _ = extracted["candidate_dpcm_runtime_update_locked"]
    retry_pending_routes, _ = extracted["candidate_retry_pending_routes"]
    route_stop_done, _ = extracted["candidate_dpcm_route_stop_done"]
    prune_paths, _ = extracted["candidate_dpcm_prune_paths"]
    be_dai_trigger, _ = extracted["candidate_dpcm_be_dai_trigger"]
    fe_trigger, _ = extracted["candidate_dpcm_trigger"]
    rollback_path_changes, _ = extracted["candidate_dpcm_rollback_path_changes"]
    lpass_trigger, _ = extracted["candidate_lpass_trigger"]
    close, _ = extracted["candidate_dpcm_close"]
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
        "#define q6apm_graph_start simulated_lpass_graph_start",
        "#define q6apm_graph_stop simulated_lpass_graph_stop",
        "#define q6apm_lpass_dai_trigger candidate_lpass_trigger",
        lpass_trigger,
        "#undef q6apm_lpass_dai_trigger",
        "#undef q6apm_graph_stop",
        "#undef q6apm_graph_start",
        "#define dpcm_route_stop_done candidate_dpcm_route_stop_done",
        route_stop_done,
        "#undef dpcm_route_stop_done",
        "static int snd_soc_dpcm_runtime_update_locked(struct snd_soc_card *card);\n",
        "static int snd_soc_dpcm_retry_pending_route_update(struct snd_soc_card *card);\n",
        extracted["candidate_dpcm_run_update_shutdown"][0],
        extracted["candidate_dpcm_run_update"][0],
        extracted["candidate_soc_dpcm_fe_runtime_update"][0],
        "#define dpcm_be_connect candidate_dpcm_be_connect",
        "#define dpcm_route_stop_done candidate_dpcm_route_stop_done",
        extracted["candidate_dpcm_be_connect"][0],
        "#undef dpcm_route_stop_done",
        "#undef dpcm_be_connect",
        "#define dpcm_prune_paths candidate_dpcm_prune_paths",
        "#define dpcm_be_is_active simulated_dpcm_be_is_active",
        "#define dpcm_route_stop_done candidate_dpcm_route_stop_done",
        prune_paths,
        "#undef dpcm_route_stop_done",
        "#undef dpcm_be_is_active",
        "#undef dpcm_prune_paths",
        "#define dpcm_be_dai_trigger candidate_dpcm_be_dai_trigger",
        be_dai_trigger,
        "#undef dpcm_be_dai_trigger",
        "#define dpcm_fe_dai_do_trigger candidate_dpcm_trigger",
        fe_trigger,
        "#undef dpcm_fe_dai_do_trigger",
        "#define dpcm_rollback_path_changes candidate_dpcm_rollback_path_changes",
        rollback_path_changes,
        "#undef dpcm_rollback_path_changes",
        runtime_update_locked,
        runtime_update,
        retry_pending_routes,
        "#define dpcm_be_disconnect_one candidate_dpcm_be_disconnect_one",
        "#define dpcm_be_reparent simulate_dpcm_be_reparent",
        extracted["candidate_dpcm_be_disconnect_one"][0],
        "#undef dpcm_be_reparent",
        "#undef dpcm_be_disconnect_one",
        extracted["candidate_component_hw_params"][0],
        extracted["candidate_component_hw_free"][0],
        "static int simulate_dpcm_hw_free(struct snd_soc_pcm_runtime *fe, "
        "struct snd_pcm_substream *substream)\n{\n"
        "\tint stream = substream->stream;\n\tint ret;\n"
        + hw_free_core
        + "\nout:\n"
        "\tdpcm_set_fe_update_state(fe, stream, SND_SOC_DPCM_UPDATE_NO);\n"
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
        shutdown,
        "static void simulate_dpcm_fe_dai_cleanup("
        "struct snd_pcm_substream *fe_substream)\n{\n"
        "\tsnd_soc_dpcm_mutex_assert_held(snd_soc_substream_to_rtd(fe_substream));\n"
        "\tfe_substream->fe->dpcm[fe_substream->stream].route_connected = false;\n"
        "\tsimulated_dpcm_cleanup_calls++;\n}\n",
        "#define dpcm_fe_dai_cleanup simulate_dpcm_fe_dai_cleanup",
        close,
        "#undef dpcm_fe_dai_cleanup",
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


def read_source(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


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
        help="candidate Linux kernel source tree",
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

    if not args.kernel_source_tree.is_dir():
        raise SystemExit(
            "candidate must be a Linux kernel source tree; "
            "--baseline may still point to a direct q6apm.c path"
        )

    candidate = resolve_q6apm_c(args.kernel_source_tree, "candidate")
    candidate_header = candidate.with_name(Q6APM_H_REL.name)
    candidate_dai = candidate.with_name(Q6APM_DAI_NAME)
    candidate_lpass = candidate.with_name(Q6APM_LPASS_NAME)
    candidate_soc_pcm = (args.kernel_source_tree / "sound/soc/soc-pcm.c").resolve()
    candidate_soc_dpcm_header = (
        args.kernel_source_tree / SOC_DPCM_H_REL
    ).resolve()
    candidate_soc_component = (
        args.kernel_source_tree / "sound/soc/soc-component.c"
    ).resolve()
    candidate_soc_compress = (
        args.kernel_source_tree / "sound/soc/soc-compress.c"
    ).resolve()
    if not candidate_header.is_file():
        raise SystemExit(f"candidate header not found at {candidate_header}")
    if not candidate_lpass.is_file():
        raise SystemExit(f"candidate LPASS DAI source not found at {candidate_lpass}")
    if not candidate_soc_pcm.is_file():
        raise SystemExit(f"candidate DPCM source not found at {candidate_soc_pcm}")
    if not candidate_soc_dpcm_header.is_file():
        raise SystemExit(
            f"candidate DPCM header not found at {candidate_soc_dpcm_header}"
        )
    if not candidate_soc_component.is_file():
        raise SystemExit(f"candidate component source not found at {candidate_soc_component}")
    if not candidate_soc_compress.is_file():
        raise SystemExit(f"candidate compress source not found at {candidate_soc_compress}")
    baseline = (
        resolve_q6apm_c(args.baseline, "baseline") if args.baseline else None
    )
    candidate_text, candidate_sha = read_source(candidate)
    candidate_header_text, candidate_header_sha = read_source(candidate_header)
    dai_text, candidate_dai_sha = read_source(candidate_dai)
    lpass_text, candidate_lpass_sha = read_source(candidate_lpass)
    soc_pcm_text, candidate_soc_pcm_sha = read_source(candidate_soc_pcm)
    soc_dpcm_header_text, candidate_soc_dpcm_header_sha = read_source(
        candidate_soc_dpcm_header
    )
    soc_component_text, candidate_soc_component_sha = read_source(
        candidate_soc_component
    )
    soc_compress_text, candidate_soc_compress_sha = read_source(
        candidate_soc_compress
    )
    if baseline:
        baseline_text, baseline_sha = read_source(baseline)
    else:
        baseline_text = None
        baseline_sha = None
    extracted = classify_sources(
        candidate_text,
        candidate_header_text,
        dai_text,
        lpass_text,
        soc_pcm_text,
        soc_dpcm_header_text,
        soc_component_text,
        soc_compress_text,
        baseline_text,
    )
    harness = build_harness(extracted)

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source_regression.c"
    binary = (work / "source_regression").resolve()
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
        tested = subprocess.run(
            [str(binary.resolve())], text=True, capture_output=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"AudioReach lifecycle harness timed out: {binary}\n")
        return 1
    sys.stdout.write(tested.stdout)
    sys.stderr.write(tested.stderr)
    if tested.returncode:
        return tested.returncode

    report = {
        "candidate": str(candidate),
        "candidate_sha256": candidate_sha,
        "candidate_header": str(candidate_header),
        "candidate_header_sha256": candidate_header_sha,
        "candidate_soc_pcm": str(candidate_soc_pcm),
        "candidate_soc_pcm_sha256": candidate_soc_pcm_sha,
        "candidate_soc_dpcm_header": str(candidate_soc_dpcm_header),
        "candidate_soc_dpcm_header_sha256": candidate_soc_dpcm_header_sha,
        "candidate_soc_component": str(candidate_soc_component),
        "candidate_soc_component_sha256": candidate_soc_component_sha,
        "candidate_soc_compress": str(candidate_soc_compress),
        "candidate_soc_compress_sha256": candidate_soc_compress_sha,
        "candidate_dai_sha256": candidate_dai_sha,
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
                "baseline_sha256": baseline_sha,
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
