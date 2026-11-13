(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ArmadaSteamClientUpdates = factory();
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  function createAdapter(host) {
    var currentVersion = null;
    var operation = null;
    var local = null;

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
      var raw = rawApplyResult();
      if (raw && raw.eresult !== 1)
        failed('apply', 'Client apply failed (' + raw.eresult + ')');
      else if (raw && raw.eresult === 1 &&
        typeof raw.requires_client_restart === 'boolean' &&
        typeof raw.requires_system_restart === 'boolean' && raw.requires_client_restart)
        local = { state: 'restart_required', phase: 'apply', error: null, restart: true };
    }
    function pendingRestart() {
      var result = applyResult();
      return !!(result && result.requires_client_restart === true);
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
      var busy = !!operation || (available() && nativeBusy());
      if (!available()) state = 'adapter_unavailable';
      else if (operation === 'check') { state = 'checking'; phase = 'check'; }
      else if (operation === 'apply') { state = 'applying'; phase = 'apply'; }
      else if (operation === 'restart') { state = 'restart_required'; phase = 'restart'; restart = true; }
      else if (busy) { state = 'native_busy'; phase = 'native'; }
      else if (local) {
        state = local.state; phase = local.phase; error = local.error || null;
        restart = !!local.restart;
      } else {
        var apply = applyResult(), check = checkResult();
        if (apply && apply.requires_client_restart) { state = 'restart_required'; restart = true; }
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
        else if (!operation) failed('refresh', 'Malformed Steam version in system info');
      } catch (error) {
        if (!operation) failed('refresh', error);
      }
      return snapshot();
    }
    async function check() {
      guard();
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
      if (pendingRestart()) throw new Error('A pending client restart blocks new client applies');
      var check = checkResult();
      if (!check || !check.available || (local && local.state !== 'available'))
        throw new Error('A successful client check with an available update is required');
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
          var result = applyResult();
          if (!result) failed('apply', 'Client apply outcome is uncertain');
          else if (result.requires_client_restart)
            local = { state: 'restart_required', phase: 'apply', error: null, restart: true };
          else local = { state: 'awaiting_status', phase: 'apply', error: null, restart: false };
        }
      } catch (error) { failed('apply', error); }
      finally { operation = null; }
      return snapshot();
    }
    async function restartClient() {
      guard();
      syncLateApply();
      var result = applyResult();
      var permitted = (local && local.state === 'restart_required' && local.restart) ||
        (!local && result && result.requires_client_restart);
      if (!permitted || !result || !result.requires_client_restart)
        throw new Error('Client-specific restart evidence is required');
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
