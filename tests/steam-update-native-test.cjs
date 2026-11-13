'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createAdapter } = require('../system_files/usr/share/armada/steam-updates/native-client.js');

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function host(overrides = {}) {
  const calls = { apply: [], restart: [], check: 0 };
  const data = {
    state: 2,
    check: { type: 1, eresult: 1, available: true, rtime_checked: 1893456000 },
    apply: { type: 1, eresult: 1, requires_client_restart: false, requires_system_restart: false },
    applyReturn: { result: 1, message: '' },
    applyImpl: null,
    checkImpl: null,
    checkReturn: { result: 1, message: '' },
    info: { nSteamVersion: 12345, sSteamBuildDate: 'today', other: 'ignore' },
    infoError: null,
    calls
  };
  const h = {
    UpdateStore: { m_updater: {
      m_updateState: { state: data.state },
      GetCheckResult(type) { return type === 1 ? data.check : { type, eresult: 2 }; },
      GetApplyResult(type) { return type === 1 ? data.apply : { type, eresult: 2 }; },
      async ApplyUpdate(types, recheck, callback) {
        calls.apply.push([types, recheck, callback]);
        if (data.applyImpl) return data.applyImpl();
        return data.applyReturn;
      },
      GetProgressPercent() { return 42; }
    } },
    SteamClient: {
      Updates: { async CheckForUpdates() { calls.check++; if (data.checkImpl) return data.checkImpl(); return data.checkReturn; } },
      System: { async GetSystemInfo() {
        if (data.infoError) throw data.infoError;
        return data.info;
      } },
      User: { async StartRestart(value) { calls.restart.push(value); } }
    }
  };
  return Object.assign(h, overrides, { data });
}

test('missing capabilities are inert and do not patch supplied globals', async () => {
  const h = { SteamClient: {} };
  const original = JSON.stringify(h);
  const adapter = createAdapter(h);
  assert.equal(adapter.snapshot().state, 'adapter_unavailable');
  await assert.rejects(adapter.check(), /unavailable/);
  assert.equal(JSON.stringify(h), original);
});

test('refresh caches current integer build and never invents target', async () => {
  const adapter = createAdapter(host());
  const snap = await adapter.refresh();
  assert.equal(snap.currentVersion, '12345');
  assert.equal(snap.targetVersion, null);
});

test('absent or malformed client checks are never up to date', async () => {
  for (const value of [null, { type: 2, eresult: 1, available: false },
    { type: 1, eresult: 2, available: false }, { type: 1, eresult: 1 }]) {
    const h = host();
    h.data.check = value;
    const snap = await createAdapter(h).check();
    assert.equal(snap.state, 'failed');
    assert.notEqual(snap.state, 'up_to_date');
    assert.match(snap.error, /client-specific/);
  }
});

test('fresh client success during call overrides aggregate OS failure', async () => {
  const h = host();
  h.data.checkImpl = () => {
    h.data.check = { type: 1, eresult: 1, available: false, rtime_checked: h.data.check.rtime_checked + 1 };
    return { result: 2, message: 'OS failed' };
  };
  const snap = await createAdapter(h).check();
  assert.equal(snap.state, 'up_to_date');
  assert.equal(snap.error, null);
});

test('stale client result cannot mask aggregate OS failure', async () => {
  const h = host();
  h.data.checkReturn = { result: 0, message: 'OS failed' };
  const snap = await createAdapter(h).check();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /OS failed/);
});

test('apply is client-only with empty recheck and no callback', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyImpl = () => {
    h.data.apply.requires_client_restart = true;
    return h.data.applyReturn;
  };
  const snap = await adapter.apply();
  assert.deepEqual(h.data.calls.apply, [[[1], [], null]]);
  assert.equal(snap.state, 'restart_required');
  assert.equal(snap.requiresClientRestart, true);
  assert.equal(snap.requiresReboot, false);
});

test('failed apply is not masked by stale available check', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyImpl = async () => { throw new Error('apply exploded'); };
  const snap = await adapter.apply();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /apply exploded/);
  assert.equal(snap.busy, false);
});

test('numeric apply failure with stale restart evidence is failed', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyReturn = { result: 2, message: 'disk full' };
  h.data.applyImpl = () => {
    h.data.apply.requires_client_restart = true;
    return h.data.applyReturn;
  };
  const snap = await adapter.apply();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /disk full/);
  assert.equal(snap.requiresClientRestart, false);
});

test('missing apply return object is failed, not success', async () => {
  const h = host();
  h.data.applyReturn = undefined;
  const adapter = createAdapter(h);
  await adapter.check();
  const snap = await adapter.apply();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /uncertain/i);
});

test('pending client restart blocks new adapter check and apply but allows restart', async () => {
  const h = host();
  h.data.apply.requires_client_restart = true;
  const adapter = createAdapter(h);
  await assert.rejects(adapter.check(), /pending client restart/);
  await assert.rejects(adapter.apply(), /pending client restart/);
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('pending restart after prior apply success blocks reapply but allows restart', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyImpl = () => {
    h.data.apply.requires_client_restart = true;
    return h.data.applyReturn;
  };
  await adapter.apply();
  await assert.rejects(adapter.check(), /pending client restart/);
  await assert.rejects(adapter.apply(), /pending client restart/);
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('malformed nSteamVersion is a refresh failure', async () => {
  const h = host();
  h.data.info = { nSteamVersion: -3 };
  const snap = await createAdapter(h).refresh();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /Malformed Steam version/);
});

test('OS restart flags and global restart state never authorize client restart', async () => {
  const h = host();
  h.data.apply.requires_system_restart = true;
  h.UpdateStore.m_updater.m_updateState.state = 7;
  const adapter = createAdapter(h);
  assert.equal(adapter.snapshot().requiresClientRestart, false);
  await assert.rejects(adapter.restartClient(), /restart evidence/);
  assert.deepEqual(h.data.calls.restart, []);
});

test('successful apply without client restart evidence awaits status and never auto restarts', async () => {
  const h = host();
  h.data.apply.requires_system_restart = true;
  const adapter = createAdapter(h);
  await adapter.check();
  const snap = await adapter.apply();
  assert.equal(snap.state, 'awaiting_status');
  assert.equal(snap.requiresClientRestart, false);
  assert.deepEqual(h.data.calls.restart, []);
});

test('deferred apply excludes concurrent apply and check and reports busy/progress', async () => {
  const h = host();
  const gate = deferred();
  h.data.applyImpl = () => gate.promise;
  const adapter = createAdapter(h);
  await adapter.check();
  const pending = adapter.apply();
  const busy = adapter.snapshot();
  assert.equal(busy.busy, true);
  assert.equal(busy.state, 'applying');
  assert.equal(busy.progressPercent, 42);
  await assert.rejects(adapter.apply(), /active/);
  await assert.rejects(adapter.check(), /active/);
  assert.equal(h.data.calls.apply.length, 1);
  assert.equal(h.data.calls.check, 1);
  gate.resolve();
  await pending;
  assert.equal(adapter.snapshot().progressPercent, null);
});

test('refresh rejection is failed, never up to date', async () => {
  const h = host();
  h.data.check.available = false;
  h.data.infoError = new Error('info unavailable');
  const snap = await createAdapter(h).refresh();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /info unavailable/);
});

test('initial refresh failure keeps pending client restart evidence and its error', async () => {
  const h = host();
  h.data.apply.requires_client_restart = true;
  h.data.infoError = new Error('system info unavailable');
  const adapter = createAdapter(h);
  const snap = await adapter.refresh();
  assert.equal(snap.state, 'restart_required');
  assert.equal(snap.requiresClientRestart, true);
  assert.equal(snap.busy, false);
  assert.match(snap.error, /system info unavailable/);

  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
  const restarted = adapter.snapshot();
  assert.equal(restarted.state, 'restart_required');
  assert.equal(restarted.requiresClientRestart, true);
  assert.equal(restarted.error, null);
});

test('explicit successful check recovers a failed idle adapter', async () => {
  const h = host();
  h.data.check = null;
  const adapter = createAdapter(h);
  assert.equal((await adapter.check()).state, 'failed');
  h.data.check = { type: 1, eresult: 1, available: false, rtime_checked: 1893456001 };
  const snap = await adapter.check();
  assert.equal(snap.state, 'up_to_date');
  assert.equal(snap.error, null);
  assert.equal(snap.busy, false);
});

test('explicit restart uses only StartRestart(false)', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyImpl = () => {
    h.data.apply.requires_client_restart = true;
    return h.data.applyReturn;
  };
  await adapter.apply();
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('refused restart keeps client restart evidence, shows the error and allows a retry', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyImpl = () => {
    h.data.apply.requires_client_restart = true;
    return h.data.applyReturn;
  };
  await adapter.apply();
  const attempts = [];
  h.SteamClient.User.StartRestart = async (value) => {
    attempts.push(value);
    if (attempts.length === 1) throw new Error('restart refused');
  };

  const refused = await adapter.restartClient();
  assert.equal(refused.state, 'restart_required');
  assert.equal(refused.requiresClientRestart, true);
  assert.equal(refused.busy, false);
  assert.match(refused.error, /restart refused/);

  const retried = await adapter.restartClient();
  assert.deepEqual(attempts, [false, false]);
  assert.equal(retried.state, 'restart_required');
  assert.equal(retried.requiresClientRestart, true);
  assert.equal(retried.error, null);
});

test('late client restart evidence after awaiting status authorizes explicit restart', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  assert.equal((await adapter.apply()).state, 'awaiting_status');
  assert.deepEqual(h.data.calls.restart, []);
  h.data.apply.requires_client_restart = true;
  assert.equal(adapter.snapshot().state, 'restart_required');
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('restartClient directly reconciles late client restart evidence', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  await adapter.apply();
  h.data.apply.requires_client_restart = true;
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('late client apply failure after awaiting status becomes failed', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  await adapter.apply();
  h.data.apply = { type: 1, eresult: 2, requires_client_restart: false, requires_system_restart: false };
  const snap = adapter.snapshot();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /Client apply failed \(2\)/);
  assert.equal(snap.requiresClientRestart, false);
});

test('accepted apply with unpublished status awaits and later restart evidence surfaces', async () => {
  const h = host();
  h.data.apply = null;
  const adapter = createAdapter(h);
  await adapter.check();
  const snap = await adapter.apply();
  assert.equal(snap.state, 'awaiting_status');
  assert.equal(snap.error, null);
  assert.equal(snap.busy, false);
  assert.equal(snap.requiresClientRestart, false);
  assert.equal(snap.requiresReboot, false);
  assert.deepEqual(h.data.calls.restart, []);
  await assert.rejects(adapter.apply(), /required/);
  assert.equal(h.data.calls.apply.length, 1);

  h.data.apply = { type: 1, eresult: 1, requires_client_restart: true, requires_system_restart: false };
  assert.equal(adapter.snapshot().state, 'restart_required');
  await adapter.restartClient();
  assert.deepEqual(h.data.calls.restart, [false]);
});

test('accepted apply with unpublished status awaits, so a late rejection is still shown', async () => {
  const h = host();
  h.data.apply = null;
  const adapter = createAdapter(h);
  await adapter.check();
  assert.equal((await adapter.apply()).state, 'awaiting_status');
  assert.deepEqual(h.data.calls.restart, []);

  h.data.apply = { type: 1, eresult: 2, requires_client_restart: false, requires_system_restart: false };
  const snap = adapter.snapshot();
  assert.equal(snap.state, 'failed');
  assert.match(snap.error, /Client apply failed \(2\)/);
  assert.equal(snap.requiresClientRestart, false);
  assert.equal(snap.requiresReboot, false);
  assert.deepEqual(h.data.calls.restart, []);
});

test('explicit native apply failure ignores later successful client restart evidence', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  h.data.applyReturn = { result: 2, message: 'disk full' };
  assert.equal((await adapter.apply()).state, 'failed');
  h.data.apply.requires_client_restart = true;
  assert.equal(adapter.snapshot().state, 'failed');
  await assert.rejects(adapter.restartClient(), /restart evidence/);
  assert.deepEqual(h.data.calls.restart, []);
});

test('late OS-only restart evidence leaves awaiting status unchanged', async () => {
  const h = host();
  const adapter = createAdapter(h);
  await adapter.check();
  await adapter.apply();
  h.data.apply.requires_system_restart = true;
  const snap = adapter.snapshot();
  assert.equal(snap.state, 'awaiting_status');
  assert.equal(snap.requiresReboot, false);
  await assert.rejects(adapter.restartClient(), /restart evidence/);
  assert.deepEqual(h.data.calls.restart, []);
});

test('malformed check timestamps cannot authorize snapshot or apply', async () => {
  for (const timestamp of [undefined, 0, -1, NaN]) {
    const h = host();
    if (timestamp === undefined) {
      delete h.data.check.rtime_checked;
    } else {
      h.data.check.rtime_checked = timestamp;
    }
    const adapter = createAdapter(h);
    const snap = adapter.snapshot();
    assert.notEqual(snap.state, 'up_to_date');
    assert.notEqual(snap.state, 'available');
    await assert.rejects(adapter.apply(), /required/);
    assert.equal(h.data.calls.apply.length, 0);
  }
});
