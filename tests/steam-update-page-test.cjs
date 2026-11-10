const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require('playwright');

const PAGE_JS_PATH = path.resolve(__dirname, '../system_files/usr/share/armada/steam-updates/page.js');
const NATIVE_JS_PATH = path.resolve(__dirname, '../system_files/usr/share/armada/steam-updates/native-client.js');

let globalBrowser = null;

test.before(async () => {
  globalBrowser = await chromium.launch({ headless: true });
});

test.after(async () => {
  if (globalBrowser) {
    await globalBrowser.close();
  }
});

async function createTestPage(t, locale = 'en-US') {
  const context = await globalBrowser.newContext({
    locale: locale,
    viewport: { width: 1280, height: 800 }
  });
  t.after(async () => {
    await context.close();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.setDefaultNavigationTimeout(5000);
  return { context, page };
}

async function setupSyntheticSteamDom(page, options = {}) {
  const { headingClass = 'RCuL60vD-fW2zF5PlEKHx', duplicateHeadings = false, duplicateSections = false, badClass = false } = options;

  let classAttr = badClass ? 'UnsupportedSectionClass' : headingClass;
  let sectionContent = `
    <div class="DialogSettingsSection" role="region">
      <div class="SettingsDialogSubHeader">
        <div class="${classAttr}">Software Updates</div>
        ${duplicateHeadings ? `<div class="${classAttr}">Software Updates Duplicate</div>` : ''}
      </div>
      <div id="original" style="display:flex!important">Original update controls</div>
    </div>
    ${duplicateSections ? `
    <div class="DialogSettingsSection" role="region">
      <div class="SettingsDialogSubHeader">
        <div class="${classAttr}">Software Updates 2</div>
      </div>
      <div id="original-2" style="display:flex!important">Original controls 2</div>
    </div>` : ''}
    <div id="native-channel" style="display:block">Native Channel Selector Untouched</div>
  `;

  await page.setContent(`<!DOCTYPE html>
<html>
<head><title>Synthetic Steam Environment</title></head>
<body style="background:#1a1f26;color:#fff;">
  ${sectionContent}
</body>
</html>`);

  await page.evaluate(() => {
    const state = {
      updaterState: 2,
      checkResult: {
        type: 1,
        eresult: 1,
        available: false,
        rtime_checked: Math.floor(Date.now() / 1000)
      },
      applyResult: {
        type: 1,
        eresult: 1,
        requires_client_restart: false,
        requires_system_restart: false
      },
      progressPercent: 0,
      checkForUpdatesResult: { result: 1 },
      systemInfo: { nSteamVersion: 12345 },
      restartPCCalls: 0,
      nativeCheckCalls: 0,
      startRestartCalls: [],
      bridgeCalls: [],
      heldRequests: {},
      holdMethods: {},
      failedMethods: {},
      autoReply: true,
      status: {
        schemaVersion: 1,
        components: {
          system: {
            busy: false,
            jobId: null,
            operation: null,
            result: {
              component: 'system',
              state: 'update_available',
              requiresReboot: false,
              currentVersion: '3.5.0',
              targetVersion: '3.5.1',
              channel: 'stable'
            }
          },
          decky: {
            busy: false,
            jobId: null,
            operation: null,
            result: {
              component: 'decky',
              state: 'repair_required',
              requiresReboot: false,
              currentVersion: '2.0.0',
              targetVersion: '2.0.1',
              channel: 'prerelease'
            }
          }
        }
      }
    };

    window.fixture = {
      state: state,
      replyOne: function(method, payload, ok = true) {
        const queue = state.heldRequests[method];
        if (!queue || queue.length === 0) return false;
        const item = queue.shift();
        const msg = Object.assign({ id: item.id, ok: ok }, ok ? { result: payload } : { error: payload });
        if (window.ArmadaUpdatesPage && typeof window.ArmadaUpdatesPage.receive === 'function') {
          window.ArmadaUpdatesPage.receive(msg);
        }
        return true;
      },
      setBackendComponent: function(comp, updates) {
        const current = state.status.components[comp];
        if (current) {
          const mergedResult = updates.result
            ? Object.assign({}, current.result, updates.result)
            : current.result;
          Object.assign(current, updates);
          current.result = mergedResult;
        }
      }
    };

    window.UpdateStore = {
      m_updater: {
        m_updateState: { state: 2 },
        GetCheckResult: function(type) {
          return state.checkResult;
        },
        GetApplyResult: function(type) {
          return state.applyResult;
        },
        ApplyUpdate: function(arg1, arg2, arg3) {
          return Promise.resolve({ result: 1 });
        },
        GetProgressPercent: function() {
          return state.progressPercent;
        }
      }
    };

    window.SteamClient = {
      Updates: {
        CheckForUpdates: function() {
          state.nativeCheckCalls++;
          state.checkResult.rtime_checked = Math.max(
            state.checkResult.rtime_checked + 1,
            Math.floor(Date.now() / 1000) + 1
          );
          return Promise.resolve(state.checkForUpdatesResult);
        }
      },
      System: {
        GetSystemInfo: function() {
          return Promise.resolve(state.systemInfo);
        },
        RestartPC: function() {
          state.restartPCCalls++;
          return Promise.resolve();
        }
      },
      User: {
        StartRestart: function(arg) {
          state.startRestartCalls.push(arg);
          return Promise.resolve();
        }
      }
    };

    window.__armadaUpdatesRequest = function(raw) {
      const req = JSON.parse(raw);
      state.bridgeCalls.push(req);

      const failureKey = req.method + ':' + req.argument;
      const failure = state.failedMethods[failureKey] || state.failedMethods[req.method];
      if (failure) {
        setTimeout(() => {
          if (window.ArmadaUpdatesPage) {
            window.ArmadaUpdatesPage.receive({
              id: req.id,
              ok: false,
              error: failure
            });
          }
        }, 5);
        return;
      }

      if (!state.autoReply || state.holdMethods[req.method]) {
        if (!state.heldRequests[req.method]) {
          state.heldRequests[req.method] = [];
        }
        state.heldRequests[req.method].push(req);
        return;
      }

      setTimeout(() => {
        if (!window.ArmadaUpdatesPage) return;
        if (req.method === 'GetStatus') {
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: JSON.parse(JSON.stringify(state.status))
          });
        } else if (req.method === 'RefreshStatus' || req.method === 'StartCheck') {
          const comp = req.argument === 'decky' ? 'decky' : 'system';
          const jid = 'job_chk_' + Math.random().toString(36).slice(2);
          const component = state.status.components[comp];
          component.busy = true;
          component.jobId = jid;
          component.operation = 'check';
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: { jobId: jid }
          });
          setTimeout(() => {
            component.busy = false;
            component.operation = null;
            component.result = comp === 'system'
              ? {
                  component: 'system',
                  state: 'update_available',
                  requiresReboot: false,
                  currentVersion: '3.5.0',
                  targetVersion: '3.5.1',
                  channel: component.result.channel
                }
              : {
                  component: 'decky',
                  state: 'repair_required',
                  requiresReboot: false,
                  currentVersion: '2.0.0',
                  targetVersion: '2.0.1',
                  channel: 'prerelease'
                };
          }, 20);
        } else if (req.method === 'StartSystemUpdate') {
          const jid = 'job_sys_' + Math.random().toString(36).slice(2);
          state.status.components.system.busy = true;
          state.status.components.system.jobId = jid;
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: { jobId: jid }
          });
        } else if (req.method === 'StartDeckyAction') {
          const jid = 'job_dky_' + Math.random().toString(36).slice(2);
          state.status.components.decky.busy = true;
          state.status.components.decky.jobId = jid;
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: { jobId: jid }
          });
        } else if (req.method === 'SetSystemChannel') {
          const jid = 'job_ch_' + Math.random().toString(36).slice(2);
          state.status.components.system.busy = true;
          state.status.components.system.jobId = jid;
          state.status.components.system.operation = 'channel';
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: { jobId: jid }
          });
        } else {
          window.ArmadaUpdatesPage.receive({
            id: req.id,
            ok: true,
            result: {}
          });
        }
      }, 5);
    };
  });

  await page.addScriptTag({ path: NATIVE_JS_PATH });
  await page.addScriptTag({ path: PAGE_JS_PATH });

  if (duplicateHeadings || duplicateSections || badClass) return;
  await page.waitForFunction(() => {
    const rows = document.querySelectorAll('.armada-row');
    const systemRow = document.querySelector('.armada-row[data-comp="system"]');
    const clientRow = document.querySelector('.armada-row[data-comp="client"]');
    const btn = systemRow ? systemRow.querySelector('button') : null;
    return rows.length === 3 && systemRow && clientRow &&
      systemRow.textContent.includes('3.5.0') &&
      clientRow.textContent.includes('12345') &&
      !!btn && btn.offsetParent !== null && !btn.disabled;
  }, null, { timeout: 5000 });
}

async function captureScreenshotIfEnv(page, name) {
  const dir = process.env.ARMADA_SCREENSHOT_DIR;
  if (dir) {
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    await page.screenshot({ path: path.join(dir, `${name}.png`) });
  }
}

test('1: Mount zh/en three separate rows, current client version 12345, nativechannel untouched, no auto actions', async (t) => {
  for (const locale of ['zh-CN', 'en-US']) {
    const { page } = await createTestPage(t, locale);
    await setupSyntheticSteamDom(page);

    await page.waitForFunction(() => document.querySelectorAll('.armada-row').length === 3, null, { timeout: 5000 });
    await page.waitForFunction(() => {
      const val = document.querySelector('.armada-row[data-comp="client"] .armada-meta-val');
      return val && val.textContent.trim() === '12345';
    }, null, { timeout: 5000 });

    const nativeChannelDisplay = await page.$eval('#native-channel', el => window.getComputedStyle(el).display);
    assert.notEqual(nativeChannelDisplay, 'none');

    const bridgeCalls = await page.evaluate(() => window.fixture.state.bridgeCalls.map(c => c.method));
    assert.equal(bridgeCalls.includes('StartCheck'), false);
    assert.equal(bridgeCalls.includes('StartSystemUpdate'), false);
    assert.equal(bridgeCalls.includes('StartDeckyAction'), false);
    const restartCalls = await page.evaluate(() => window.fixture.state.restartPCCalls);
    assert.equal(restartCalls, 0);

    const langPrefix = locale.startsWith('zh') ? 'zh' : 'en';
    await captureScreenshotIfEnv(page, `test1_mount_${langPrefix}_wide_1280`);
    await page.setViewportSize({ width: 800, height: 800 });
    await captureScreenshotIfEnv(page, `test1_mount_${langPrefix}_narrow_800`);
  }
});

test('2: checkAll fails only system; Decky repair and native update stay actionable; system error survives two refreshes', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);

  await page.evaluate(() => {
    window.fixture.state.failedMethods['StartCheck:system'] = 'Simulated system check failure';
    window.fixture.state.checkResult.available = true;
  });
  await page.click('.armada-header-row button');

  const sysErrLocator = page.locator('.armada-row[data-comp="system"] .armada-error-box');
  await sysErrLocator.waitFor({ state: 'visible' });
  assert.match(await sysErrLocator.textContent(), /Simulated system check failure/);

  const deckyBtn = page.locator('.armada-row[data-comp="decky"] .armada-btn-primary');
  await deckyBtn.waitFor({ state: 'visible' });
  assert.equal(await deckyBtn.isEnabled(), true);
  assert.match(await deckyBtn.textContent(), /Repair/);
  const clientBtn = page.locator('.armada-row[data-comp="client"] .armada-btn-primary');
  await clientBtn.waitFor({ state: 'visible' });
  assert.equal(await clientBtn.isEnabled(), true);
  assert.equal(await page.evaluate(() => window.fixture.state.nativeCheckCalls), 1);

  for (const expected of [2, 3]) {
    await page.evaluate(() => window.ArmadaUpdatesPage.refresh());
    await page.waitForFunction((count) =>
      window.fixture.state.bridgeCalls.filter(c => c.method === 'GetStatus').length >= count,
    expected, { timeout: 5000 });
    assert.equal(await sysErrLocator.isVisible(), true);
    assert.match(await sysErrLocator.textContent(), /Simulated system check failure/);
  }
});

test('3: Duplicate update confirmations send once; stale idle cannot unlock, matching terminal available can', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  const sysBtn = page.locator('.armada-row[data-comp="system"] .armada-btn-primary');
  await sysBtn.click();
  await page.locator('.armada-row[data-comp="system"] .armada-btn-danger').waitFor({ state: 'visible' });

  await page.evaluate(() => {
    window.fixture.state.holdMethods.StartSystemUpdate = true;
    const btn = document.querySelector('.armada-row[data-comp="system"] .armada-btn-danger');
    for (let i = 0; i < 4; i++) btn.click();
  });
  assert.equal(await page.evaluate(() =>
    window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length), 1);

  await page.evaluate(() => {
    window.fixture.replyOne('StartSystemUpdate', { jobId: 'job_sys_ack_1' }, true);
    window.fixture.setBackendComponent('system', {
      busy: false,
      jobId: null,
      operation: null,
      result: { state: 'update_available' }
    });
  });

  for (const expected of [2, 3]) {
    await page.evaluate(() => window.ArmadaUpdatesPage.refresh());
    await page.waitForFunction((count) =>
      window.fixture.state.bridgeCalls.filter(c => c.method === 'GetStatus').length >= count,
    expected, { timeout: 5000 });
  }
  await page.waitForFunction(() => {
    const btn = document.querySelector('.armada-row[data-comp="system"] .armada-btn-primary');
    return btn && btn.disabled;
  }, null, { timeout: 5000 });

  await page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      busy: true,
      jobId: 'job_sys_ack_1',
      operation: 'update'
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  await page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      busy: false,
      jobId: 'job_sys_ack_1',
      operation: null,
      result: { state: 'update_available', requiresReboot: false }
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  await page.waitForFunction(() => {
    const btn = document.querySelector('.armada-row[data-comp="system"] .armada-btn-primary');
    return btn && !btn.disabled;
  }, null, { timeout: 5000 });
  assert.equal(await page.evaluate(() =>
    window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length), 1);
});

test('4: Bad status validation: schemaVersion fails both; wrong component or busy string fails only system', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.waitForFunction(() => document.querySelectorAll('.armada-row').length === 3, null, { timeout: 5000 });

  // Wrong component / busy: "false" string validation test
  await page.evaluate(() => {
    window.fixture.state.status.components.system.busy = 'false'; // invalid boolean
  });

  await page.evaluate(() => window.ArmadaUpdatesPage.refresh());

  const sysErrLocator = page.locator('.armada-row[data-comp="system"] .armada-error-box');
  await sysErrLocator.waitFor({ state: 'visible', timeout: 5000 });
  const deckyErrLocator = page.locator('.armada-row[data-comp="decky"] .armada-error-box');
  assert.equal(await deckyErrLocator.isVisible(), false);

  // SchemaVersion bad fails both
  await page.evaluate(() => {
    window.fixture.state.status.components.system.busy = false;
    window.fixture.state.status.schemaVersion = 2;
  });

  await page.evaluate(() => window.ArmadaUpdatesPage.refresh());
  await deckyErrLocator.waitFor({ state: 'visible', timeout: 5000 });
  await sysErrLocator.waitFor({ state: 'visible', timeout: 5000 });
});

test('5: Channel mutation sends once, stays locked until matching beta terminal, then allows arrow keys', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  const selectLocator = page.locator('.armada-channel-sel');
  await page.evaluate(() => {
    window.fixture.state.holdMethods.SetSystemChannel = true;
  });
  await selectLocator.selectOption('beta');
  await page.waitForFunction(() =>
    window.fixture.state.bridgeCalls.some(c => c.method === 'SetSystemChannel' && c.argument === 'beta'),
  null, { timeout: 5000 });

  const mutationCounts = await page.evaluate(() => ({
    set: window.fixture.state.bridgeCalls.filter(c => c.method === 'SetSystemChannel').length,
    update: window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length,
    refresh: window.fixture.state.bridgeCalls.filter(c => c.method === 'RefreshStatus').length
  }));
  assert.deepEqual(mutationCounts, { set: 1, update: 0, refresh: 0 });
  assert.equal(await selectLocator.isDisabled(), true);

  await page.evaluate(() => {
    window.fixture.replyOne('SetSystemChannel', { jobId: 'job_channel_1' }, true);
    window.fixture.setBackendComponent('system', {
      busy: true,
      jobId: 'job_channel_1',
      operation: 'channel'
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  await page.waitForFunction(() => document.querySelector('.armada-channel-sel').disabled,
    null, { timeout: 5000 });

  await page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      busy: false,
      jobId: 'job_channel_1',
      operation: null,
      result: { state: 'update_available', channel: 'beta', requiresReboot: false }
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  await page.waitForFunction(() => {
    const select = document.querySelector('.armada-channel-sel');
    return select && !select.disabled && select.value === 'beta';
  }, null, { timeout: 5000 });

  await selectLocator.focus();
  const arrowPrevented = await page.evaluate(() => {
    const el = document.querySelector('.armada-channel-sel');
    const ev = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
    el.dispatchEvent(ev);
    return ev.defaultPrevented;
  });
  assert.equal(arrowPrevented, false);
});

test('6: Busy snapshot rejects system restart; fresh snapshot restarts once; native client restart remains unchanged', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      busy: false,
      result: { state: 'awaiting_reboot', requiresReboot: true }
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  const sysBtn = page.locator('.armada-row[data-comp="system"] .armada-btn-primary');
  await sysBtn.waitFor({ state: 'visible' });
  assert.match(await sysBtn.textContent(), /Restart system/);
  await page.evaluate(() => {
    window.fixture.state.status.components.system.busy = true;
    const row = document.querySelector('.armada-row[data-comp="system"]');
    row.querySelector('.armada-btn-primary').click();
    row.querySelector('.armada-btn-danger').click();
  });
  const sysErr = page.locator('.armada-row[data-comp="system"] .armada-error-box');
  await sysErr.waitFor({ state: 'visible' });
  assert.equal(await page.evaluate(() => window.fixture.state.restartPCCalls), 0);

  const fresh = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(fresh.page);
  await fresh.page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      busy: false,
      result: { state: 'awaiting_reboot', requiresReboot: true }
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  await fresh.page.locator('.armada-row[data-comp="system"] .armada-btn-primary').click();
  await fresh.page.locator('.armada-row[data-comp="system"] .armada-btn-danger').click();
  await fresh.page.waitForFunction(() => window.fixture.state.restartPCCalls === 1,
    null, { timeout: 5000 });
  assert.equal(await fresh.page.evaluate(() =>
    window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length), 0);

  await page.evaluate(() => {
    window.fixture.state.applyResult.requires_client_restart = true;
    window.fixture.state.applyResult.eresult = 1;
    window.fixture.state.checkResult.available = true;
    return window.ArmadaUpdatesPage.refresh();
  });
  const clientBtn = page.locator('.armada-row[data-comp="client"] .armada-btn-primary');
  await clientBtn.waitFor({ state: 'visible' });
  assert.match(await clientBtn.textContent(), /Restart Steam/);
  await clientBtn.click();
  await page.locator('.armada-row[data-comp="client"] .armada-btn-danger').click();
  await page.waitForFunction(() => window.fixture.state.startRestartCalls.length === 1,
    null, { timeout: 5000 });
  assert.equal(await page.evaluate(() => window.fixture.state.startRestartCalls[0]), false);
  assert.equal(await page.evaluate(() => window.fixture.state.restartPCCalls), 0);
});

test('7: Failed result preserves requiresReboot and renders opened error details as safe literal text', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => {
    window.fixture.setBackendComponent('system', {
      result: {
        state: 'failed',
        requiresReboot: true,
        error: '<b>Dangerous HTML Alert</b>'
      }
    });
    return window.ArmadaUpdatesPage.refresh();
  });
  assert.equal(await page.evaluate(() =>
    window.fixture.state.status.components.system.result.requiresReboot), true);

  const details = page.locator('.armada-row[data-comp="system"] details');
  await details.waitFor({ state: 'attached' });
  assert.equal(await details.getAttribute('open'), null);
  await details.locator('summary').click();
  assert.equal(await details.getAttribute('open'), '');
  assert.equal(await details.locator('pre').textContent(), '<b>Dangerous HTML Alert</b>');
  assert.equal(await details.locator('b').count(), 0);
});

test('8: Dispose restores inline display priority, ignores held GetStatus, and stays disposed without data attributes', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => {
    window.fixture.state.holdMethods.GetStatus = true;
    window.ArmadaUpdatesPage.refresh();
  });
  await page.waitForFunction(() =>
    (window.fixture.state.heldRequests.GetStatus || []).length === 1,
  null, { timeout: 5000 });

  await page.evaluate(() => window.ArmadaUpdatesPage.dispose());
  const callsAtDispose = await page.evaluate(() => window.fixture.state.bridgeCalls.length);
  await page.evaluate(() => {
    window.fixture.replyOne('GetStatus', window.fixture.state.status, true);
    return new Promise(resolve => setTimeout(resolve, 0));
  });
  await page.waitForTimeout(50);

  const original = await page.$eval('#original', el => ({
    display: getComputedStyle(el).display,
    inlineDisplay: el.style.display,
    priority: el.style.getPropertyPriority('display'),
    hidden: el.getAttribute('hidden'),
    dataAttributes: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name)
  }));
  assert.deepEqual(original, {
    display: 'flex', inlineDisplay: 'flex', priority: 'important', hidden: null, dataAttributes: []
  });
  assert.equal(await page.locator('.armada-updates-panel').count(), 0);
  assert.equal(await page.evaluate(() => window.fixture.state.bridgeCalls.length), callsAtDispose);
});

test('9: Capability recovery needs no start; panel replacement stays singular; added native child hides and restores', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => {
    delete window.SteamClient.Updates;
  });
  await page.evaluate(() => window.ArmadaUpdatesPage.refresh());
  await page.waitForFunction(() => document.querySelectorAll('.armada-updates-panel').length === 0,
    null, { timeout: 5000 });
  assert.equal(await page.$eval('#original', el => getComputedStyle(el).display), 'flex');

  await page.evaluate(() => {
    window.SteamClient.Updates = {
      CheckForUpdates: function() { return Promise.resolve({ result: 1 }); }
    };
  });
  await page.waitForFunction(() => document.querySelectorAll('.armada-updates-panel').length === 1,
    null, { timeout: 5000 });

  await page.evaluate(() => document.querySelector('.armada-updates-panel').remove());
  await page.waitForFunction(() => document.querySelectorAll('.armada-updates-panel').length === 1,
    null, { timeout: 5000 });
  assert.equal(await page.locator('.armada-updates-panel').count(), 1);

  await page.evaluate(() => {
    const child = document.createElement('div');
    child.id = 'late-native-child';
    child.style.display = 'block';
    child.textContent = 'Late native controls';
    document.querySelector('.DialogSettingsSection').appendChild(child);
  });
  await page.waitForFunction(() =>
    getComputedStyle(document.querySelector('#late-native-child')).display === 'none',
  null, { timeout: 5000 });
  await page.evaluate(() => window.ArmadaUpdatesPage.dispose());
  assert.equal(await page.$eval('#late-native-child', el => getComputedStyle(el).display), 'block');
});

test('10: Ambiguous 2 headings, 2 sections, or unknown class retains native controls without hooking', async (t) => {
  const scenarios = [
    { duplicateHeadings: true },
    { duplicateSections: true },
    { badClass: true }
  ];

  for (const scenario of scenarios) {
    const { page } = await createTestPage(t, 'en-US');
    await setupSyntheticSteamDom(page, scenario);

    // Wait briefly and verify panel never mounts
    await page.waitForTimeout(100);
    const panels = await page.locator('.armada-updates-panel').count();
    assert.equal(panels, 0, `Scenario ${JSON.stringify(scenario)} must not mount panel`);

    const originalDisplay = await page.$eval('#original', el => window.getComputedStyle(el).display);
    assert.equal(originalDisplay, 'flex');
  }
});

test('11: Reinjection ignores held old job reply after fresh available GetStatus restores actionable system row', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => {
    window.fixture.state.holdMethods.StartCheck = true;
    window.ArmadaUpdatesPage.checkAll();
  });
  await page.waitForFunction(() => (window.fixture.state.heldRequests.StartCheck || []).length > 0,
    null, { timeout: 5000 });
  const oldRequestId = await page.evaluate(() => window.fixture.state.heldRequests.StartCheck[0].id);
  await page.addScriptTag({ path: PAGE_JS_PATH });
  await page.waitForFunction(() => {
    const rows = document.querySelectorAll('.armada-row');
    const btn = document.querySelector('.armada-row[data-comp="system"] .armada-btn-primary');
    return rows.length === 3 && btn && !btn.disabled && btn.style.display !== 'none';
  }, null, { timeout: 5000 });

  await page.evaluate((reqId) => {
    window.ArmadaUpdatesPage.receive({
      id: reqId,
      ok: true,
      result: { jobId: 'stale_job_id' }
    });
  }, oldRequestId);
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
  assert.equal(await page.locator('.armada-row').count(), 3);
  const systemButton = page.locator('.armada-row[data-comp="system"] .armada-btn-primary');
  assert.equal(await systemButton.isVisible(), true);
  assert.equal(await systemButton.isEnabled(), true);
  assert.equal(await page.evaluate(() =>
    window.fixture.state.status.components.system.jobId === 'stale_job_id'), false);
});

test('12: Held StartSystemUpdate times out once with visible error and remains unsent after another 16 seconds', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.clock.install();
  await page.evaluate(() => {
    window.fixture.state.holdMethods.StartSystemUpdate = true;
    window.fixture.state.bridgeCalls = [];
  });
  await page.locator('.armada-row[data-comp="system"] .armada-btn-primary').click();
  await page.locator('.armada-row[data-comp="system"] .armada-btn-danger').click();
  await page.clock.fastForward(16000);

  const sysErr = page.locator('.armada-row[data-comp="system"] .armada-error-box');
  await sysErr.waitFor({ state: 'visible' });
  assert.match(await sysErr.textContent(), /timed out|timeout/i);
  const updateCalls = () => page.evaluate(() =>
    window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length);
  assert.equal(await updateCalls(), 1);
  const sysBtn = page.locator('.armada-row[data-comp="system"] .armada-btn-primary');
  assert.equal(await sysBtn.isVisible() && await sysBtn.isEnabled(), false);
  await page.clock.fastForward(16000);
  assert.equal(await updateCalls(), 1);
});

test('U367 - explicit read-only "Check for updates" recovers system status after service coordinator restart without auto-resend of StartSystemUpdate', { timeout: 15000 }, async t => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);

  const row = page.locator('.armada-row[data-comp="system"]');
  await row.locator('.armada-btn-primary').first().click();
  await row.locator('.armada-btn-danger').click();

  await page.waitForFunction(() =>
    window.fixture.state.bridgeCalls.filter(c => c.method === 'StartSystemUpdate').length === 1,
    null, { timeout: 5000 });

  await page.waitForFunction(() => {
    const s = window.fixture.state.status;
    return s.schemaVersion === 1 && s.components && s.components.system &&
           s.components.system.busy === true && s.components.system.jobId != null;
  }, null, { timeout: 5000 });

  await page.evaluate(() => {
    const sys = window.fixture.state.status.components.system;
    sys.busy = false;
    sys.jobId = null;
    sys.operation = null;
    sys.result = {
      component: 'system', state: 'not_checked', phase: 'status',
      currentVersion: null, targetVersion: null,
      requiresReboot: false, channel: 'stable'
    };
  });

  const callsBeforeCheck = await page.evaluate(() => window.fixture.state.bridgeCalls.length);

  await page.evaluate(() => window.ArmadaUpdatesPage.refresh().catch(() => {}));
  await page.locator('.armada-header-row button').first().click();

  await page.waitForFunction(() =>
    window.fixture.state.bridgeCalls.some(c =>
      c.method === 'StartCheck' && c.argument === 'system'),
    null, { timeout: 5000 });

  await page.waitForFunction(() => {
    const btn = document.querySelector('.armada-row[data-comp="system"] .armada-btn-primary');
    const r = document.querySelector('.armada-row[data-comp="system"]');
    return btn && !btn.disabled &&
           r && r.textContent.includes('3.5.0') &&
           /update/i.test(r.textContent) && /avail/i.test(r.textContent);
  }, null, { timeout: 5000 });

  const calls = await page.evaluate(() => window.fixture.state.bridgeCalls);
  const sysUpdates = calls.filter(c => c.method === 'StartSystemUpdate');
  assert.equal(sysUpdates.length, 1);
  assert.equal(calls.filter(c => c.method === 'StartDeckyAction').length, 0);

  const rebootCount = await page.evaluate(() => window.fixture.state.restartPCCalls);
  assert.equal(rebootCount, 0);

  const postCheckMethods = new Set(calls.slice(callsBeforeCheck).map(c => c.method));
  const readOnly = new Set(['GetStatus', 'StartCheck', 'RefreshStatus']);
  for (const m of postCheckMethods) {
    assert.ok(readOnly.has(m), `expected read-only call, got ${m}`);
  }
});


test('poll transport errors recover automatically without clearing action failures', async (t) => {
  const { page } = await createTestPage(t, 'en-US');
  await setupSyntheticSteamDom(page);
  await page.evaluate(() => { window.fixture.state.failedMethods.GetStatus = 'Temporary polling failure'; window.ArmadaUpdatesPage.refresh(); });
  const error = page.locator('.armada-row[data-comp="system"] .armada-error-box');
  await page.waitForFunction(() => document.querySelector('.armada-row[data-comp="system"] .armada-error-box').textContent.includes('Temporary polling failure'));
  await page.evaluate(() => { delete window.fixture.state.failedMethods.GetStatus; window.ArmadaUpdatesPage.refresh(); });
  await page.waitForFunction(() => !document.querySelector('.armada-row[data-comp="system"] .armada-error-box').textContent.includes('Temporary polling failure'));
  assert.equal(await page.locator('.armada-row[data-comp="system"] .armada-btn-primary').isEnabled(), true);
  await page.evaluate(() => { window.fixture.state.failedMethods['StartCheck:system'] = 'Action failure'; });
  await page.click('.armada-header-row button');
  await page.waitForFunction(() => document.querySelector('.armada-row[data-comp="system"] .armada-error-box').textContent.includes('Action failure'));
  await page.evaluate(() => { window.fixture.state.failedMethods.GetStatus = 'Polling must not replace action'; window.ArmadaUpdatesPage.refresh(); });
  await page.waitForTimeout(150);
  await page.evaluate(() => { delete window.fixture.state.failedMethods.GetStatus; window.ArmadaUpdatesPage.refresh(); });
  await page.waitForTimeout(150);
  assert.match(await error.textContent(), /Action failure/);
});
