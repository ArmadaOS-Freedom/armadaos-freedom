(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ArmadaSteamClientUpdates = factory();
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  function createAdapter(host) {
    var currentVersion = null;
    var operation = null;
    var local = null;
    var applyEvidenceToken = null;
    var applyEvidenceAbsent = false;

    function parts() {
      var steam = host && host.SteamClient;
      var updater = host && host.UpdateStore && host.UpdateStore.m_updater;
      return { steam: steam, updater: updater };
    }
    function available() {
      var p = parts(), s = p.steam, u = p.updater;
      return !!(u && s && s.Updates && s.System && s.User &&
        typeof u.GetCheckResult === 'function' &&
        typeof u.GetApplyResult === 'function' &&
        typeof u.ApplyUpdate === 'function' &&
        typeof u.GetProgressPercent === 'function' &&
        typeof s.Updates.CheckForUpdates === 'function' &&
        typeof s.System.GetSystemInfo === 'function' &&
        typeof s.User.StartRestart === 'function');
    }
    function errorText(error) {
      return error && error.message ? String(error.message) : String(error || 'Unknown native error');
    }
    function checkResult() {
      try {
        var r = parts().updater.GetCheckResult(1);
        return r && r.type === 1 && r.eresult === 1 && typeof r.available === 'boolean' &&
          Number.isSafeInteger(r.rtime_checked) && r.rtime_checked > 0 ? r : null;
      } catch (_) { return null; }
    }
    function rawApplyResult() {
      try {
        var r = parts().updater.GetApplyResult(1);
        return r && r.type === 1 && typeof r.eresult === 'number' ? r : null;
      } catch (_) { return null; }
    }
    function applyResult() {
      var r = rawApplyResult();
      return r && r.eresult === 1 &&
        typeof r.requires_client_restart === 'boolean' &&
        typeof r.requires_system_restart === 'boolean' ? r : null;
    }
    function applyResultToken(r) {
      return r ? r.eresult + ':' + String(r.requires_client_restart) + ':' +
        String(r.requires_system_restart) : null;
    }
    // GetApplyResult may hand back a fresh object on every read carrying the
    // same flags, so evidence is the semantic token, never object identity.
    // An actually observed gap (no result) is remembered so identical values
    // republished after the gap count as new evidence.
    function recordApplyBaseline() {
      applyEvidenceToken = applyResultToken(rawApplyResult());
      applyEvidenceAbsent = !applyEvidenceToken;
    }
    function observeApplyResult() {
      var raw = rawApplyResult();
      var token = applyResultToken(raw);
      if (!token) { applyEvidenceAbsent = true; return null; }
      if (!applyEvidenceAbsent && token === applyEvidenceToken) return null;
      applyEvidenceToken = token;
      applyEvidenceAbsent = false;
      return raw;
    }
    function awaitingApply() {
      return !!(local && local.state === 'awaiting_status');
    }
    function nativeBusy() {
      try {
        var state = parts().updater.m_updateState.state;
        return state === 3 || state === 5;
      } catch (_) { return false; }
    }
    function failed(phase, error) {
      local = { state: 'failed', phase: phase, error: errorText(error), restart: false };
    }
    function syncLateApply() {
      if (operation || !local || local.state !== 'awaiting_status') return;
      var priorError = local.error || null;
      var raw = observeApplyResult();
      if (!raw) return;
      if (raw.eresult !== 1)
        failed('apply', 'Client apply failed (' + raw.eresult + ')');
      else if (typeof raw.requires_client_restart === 'boolean' &&
        typeof raw.requires_system_restart === 'boolean') {
        if (raw.requires_client_restart)
          local = { state: 'restart_required', phase: 'apply', error: priorError, restart: true };
        else if (!raw.requires_system_restart)
          // A completed client-only apply needs no restart. Settle it so an
          // unresolved awaiting status cannot keep checks/applies blocked. An
          // OS-only restart requirement is not a completed client apply, so it
          // leaves awaiting status (and its busy lock) in place.
          local = { state: 'up_to_date', phase: 'apply', error: priorError, restart: false };
      }
    }
    function refreshFailure(message) {
      // A failed version read says nothing about an already accepted client
      // apply, so keep awaiting status busy/blocking and only surface the
      // error. A pending client restart likewise stays reachable.
      if (local && local.state === 'awaiting_status')
        local = {
          state: 'awaiting_status', phase: local.phase || 'apply',
          error: message, restart: false
        };
      else if (local && local.state === 'restart_required' && local.restart)
        local = { state: 'restart_required', phase: 'refresh', error: message, restart: true };
      else if (!local && pendingRestart())
        local = { state: 'restart_required', phase: 'refresh', error: message, restart: true };
      else failed('refresh', message);
    }
    // One evidence rule for every reader: a native "client restart required"
    // flag is authoritative only while no local outcome exists, or while the
    // local outcome itself is a restart recorded from fresh evidence. Once
    // fresh evidence has promoted local status to restart_required, that
    // requirement is durable: a later missing or throwing native read cannot
    // erase it. A flag left behind by an explicit failure or a newer check is
    // stale and must not keep blocking checks, applies or authorize a restart.
    function restartEvidence() {
      if (local) {
        // Only a restart recorded from fresh evidence may stand without a
        // current native read; failed, awaiting and completed outcomes must
        // never fall back to a stale native flag.
        return local.state === 'restart_required' && local.restart ? true : null;
      }
      var result = applyResult();
      return result && result.requires_client_restart === true ? result : null;
    }
    function pendingRestart() {
      return restartEvidence() !== null;
    }
    function guard() {
      if (!available()) throw new Error('Steam client update adapter is unavailable');
      if (operation) throw new Error('Another client update operation is active');
      if (nativeBusy()) throw new Error('Steam is already checking or applying updates');
    }
    function progress() {
      if (operation !== 'apply') return null;
      try {
        var value = parts().updater.GetProgressPercent();
        return typeof value === 'number' && isFinite(value) ? value : null;
      } catch (_) { return null; }
    }
    function snapshot() {
      syncLateApply();
      var state = 'unknown', phase = 'idle', error = null, restart = false;
      var nativeActive = available() && nativeBusy();
      var busy = !!operation || nativeActive || awaitingApply();
      if (!available()) state = 'adapter_unavailable';
      else if (operation === 'check') { state = 'checking'; phase = 'check'; }
      else if (operation === 'apply') { state = 'applying'; phase = 'apply'; }
      else if (operation === 'restart') { state = 'restart_required'; phase = 'restart'; restart = true; }
      else if (nativeActive) { state = 'native_busy'; phase = 'native'; }
      else if (local) {
        state = local.state; phase = local.phase; error = local.error || null;
        restart = !!local.restart;
      } else {
        var apply = restartEvidence(), check = checkResult();
        if (apply) { state = 'restart_required'; restart = true; }
        else if (check) state = check.available ? 'available' : 'up_to_date';
      }
      return {
        component: 'client', state: state, phase: phase,
        currentVersion: currentVersion, targetVersion: null,
        requiresClientRestart: restart, requiresReboot: false,
        busy: busy, error: error, progressPercent: progress()
      };
    }

    async function refresh() {
      if (!available()) throw new Error('Steam client update adapter is unavailable');
      try {
        var info = await parts().steam.System.GetSystemInfo();
        if (info && Number.isSafeInteger(info.nSteamVersion) && info.nSteamVersion > 0)
          currentVersion = String(info.nSteamVersion);
        else if (!operation) refreshFailure('Malformed Steam version in system info');
      } catch (error) {
        if (!operation) refreshFailure(errorText(error));
      }
      return snapshot();
    }
    async function check() {
      guard();
      syncLateApply();
      if (awaitingApply()) throw new Error('A client apply is awaiting its native result');
      if (pendingRestart()) throw new Error('A pending client restart blocks new client update checks');
      var previous = checkResult();
      var oldTime = previous && Number.isInteger(previous.rtime_checked) &&
        previous.rtime_checked > 0 ? previous.rtime_checked : null;
      var startUnix = Math.floor(Date.now() / 1000);
      operation = 'check';
      try {
        var returned = await parts().steam.Updates.CheckForUpdates();
        var result = checkResult();
        var valid = !!(result && Number.isInteger(result.rtime_checked) && result.rtime_checked > 0);
        var ok = !!(returned && typeof returned === 'object' && returned.result === 1);
        var fresh = valid && (oldTime != null ? result.rtime_checked > oldTime :
          result.rtime_checked >= startUnix);
        if (valid && (ok || fresh))
          local = { state: result.available ? 'available' : 'up_to_date', phase: 'check', error: null, restart: false };
        else {
          var msg = returned && typeof returned === 'object' && returned.message != null ?
            String(returned.message).trim() : '';
          var detail = msg || 'No fresh valid client-specific update check result';
          failed('check', detail);
        }
      } catch (error) { failed('check', error); }
      finally { operation = null; }
      return snapshot();
    }
    async function apply() {
      guard();
      syncLateApply();
      if (awaitingApply()) throw new Error('A client apply is awaiting its native result');
      if (pendingRestart()) throw new Error('A pending client restart blocks new client applies');
      var check = checkResult();
      if (!check || !check.available || (local && local.state !== 'available'))
        throw new Error('A successful client check with an available update is required');
      recordApplyBaseline();
      operation = 'apply';
      local = { state: 'applying', phase: 'apply', error: null, restart: false };
      try {
        var returned = await parts().updater.ApplyUpdate([1], [], null);
        var plain = !!(returned && typeof returned === 'object' && !Array.isArray(returned));
        var ok = plain && returned.result === 1;
        var failureMessage = null;
        if (!ok && plain && typeof returned.result === 'number')
          failureMessage = returned.message != null ? String(returned.message) :
            'Native client apply failed (' + returned.result + ')';
        if (!ok) failed('apply', failureMessage || 'Client apply outcome is uncertain');
        else {
          // The native call only reports that the apply was accepted; its
          // status may not be published yet, so GetApplyResult can still be
          // null or unchanged here. Await new semantic evidence rather than
          // inventing a failure, and never claim success or a restart from a
          // reboxed or pre-existing result: syncLateApply() promotes a later
          // rejection, completion or client restart requirement, including
          // identical values republished after an observed gap. The snapshot
          // below samples the ephemeral result once, for reconciliation only.
          local = { state: 'awaiting_status', phase: 'apply', error: null, restart: false };
        }
      } catch (error) { failed('apply', error); }
      finally { operation = null; }
      return snapshot();
    }
    async function restartClient() {
      guard();
      syncLateApply();
      var result = restartEvidence();
      if (!result) throw new Error('Client-specific restart evidence is required');
      operation = 'restart';
      try {
        await parts().steam.User.StartRestart(false);
        local = { state: 'restart_required', phase: 'restart', error: null, restart: true };
      } catch (error) {
        // A refused restart keeps the client-specific evidence so the user can
        // see why it failed and try again.
        local = { state: 'restart_required', phase: 'restart', error: errorText(error), restart: true };
      }
      finally { operation = null; }
      return snapshot();
    }
    return { snapshot: snapshot, refresh: refresh, check: check, apply: apply, restartClient: restartClient };
  }
  return { createAdapter: createAdapter };
});
