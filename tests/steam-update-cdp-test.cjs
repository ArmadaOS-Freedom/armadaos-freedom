const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

const REPO_ROOT = path.resolve(__dirname, '..');
const ACTUAL_JS_BASE = path.join(REPO_ROOT, 'system_files/usr/share/armada/steam-updates');

const FIXTURE_HTML = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>SP</title></head>
<body>
<div class="DialogSettingsSection" role="region">
 <div class="SettingsDialogSubHeader"><div class="RCuL60vD-fW2zF5PlEKHx">Software Updates</div></div>
 <div id="original-controls">Original controls</div>
</div>
<div id="native-channel">Client channel</div>
<script>
window.__nativeCheckCount = 0;
window.__nativeRestartCount = 0;
window.__nativeApplyCount = 0;
window.__nativeTime = Math.floor(Date.now() / 1000);
window.UpdateStore = {
  m_updater: {
    m_updateState: { state: 2 },
    GetCheckResult: function(type) { return { type: 1, eresult: 1, available: false, rtime_checked: window.__nativeTime }; },
    GetApplyResult: function(type) { return { type: 1, eresult: 1, requires_client_restart: false, requires_system_restart: false }; },
    ApplyUpdate: function() { window.__nativeApplyCount++; return Promise.resolve({ result: 1 }); },
    GetProgressPercent: function() { return 0; }
  }
};
window.SteamClient = {
  Updates: {
    CheckForUpdates: function() {
      window.__nativeCheckCount++;
      window.__nativeTime++;
      return Promise.resolve({ result: 1 });
    }
  },
  System: {
    GetSystemInfo: function() { return Promise.resolve({ nSteamVersion: 12345 }); },
    RestartPC: function() { window.__nativeRestartCount++; }
  },
  User: {
    StartRestart: function() { window.__nativeRestartCount++; }
  }
};
</script>
</body>
</html>`;

const PYTHON_HELPER = String.raw`
import sys, os, signal, threading, json, copy
from pathlib import Path

repo_root = Path(sys.argv[1])
sys.path.insert(0, str(repo_root / "system_files" / "usr" / "lib" / "armada"))
from updates import steam_bridge as sb

class FakeClient:
    def __init__(self):
        self.seq = 0
        self.job_counter = 0
        self.records = {
            component: {
                "busy": False,
                "jobId": None,
                "operation": None,
                "result": {
                    "component": component,
                    "state": state,
                    "phase": "status",
                    "currentVersion": "0.8.0",
                    "targetVersion": None,
                    "requiresReboot": False,
                    "channel": "stable"
                }
            }
            for component, state in (("system", "up_to_date"), ("decky", "repair_required"))
        }

    def snapshot(self):
        self.seq += 1
        sys.stdout.write(json.dumps({"type": "snapshot", "seq": self.seq}) + "\n")
        sys.stdout.flush()
        return {"schemaVersion": 1, "components": copy.deepcopy(self.records)}

    def call(self, method, argument):
        self.job_counter += 1
        job_id = f"job-{self.job_counter}"
        sys.stdout.write(json.dumps({"type": "call", "method": method, "argument": argument}) + "\n")
        sys.stdout.flush()
        if method == "StartCheck":
            self.records[argument]["jobId"] = job_id
            self.records[argument]["busy"] = False
            self.records[argument]["result"]["phase"] = "check"
        return job_id

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda s, f: stop.set())
signal.signal(signal.SIGINT, lambda s, f: stop.set())

target_id, ws_url = sb.discover_target()
client = FakeClient()
sources = sb.load_js_sources(sys.argv[2])
conn = sb.SteamConnection(sb._default_ws_factory(ws_url), client, sources, stop)
sys.stdout.write(json.dumps({"type": "ready"}) + "\n")
sys.stdout.flush()
conn.run()
`;

async function waitForCondition(predicate, timeoutMs = 5000, intervalMs = 50) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`Condition timed out after ${timeoutMs}ms`);
}

test('Armada Steam CDP bridge integration test', { timeout: 30000 }, async (t) => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--remote-debugging-port=8080', '--remote-debugging-address=127.0.0.1'],
  });
  let context = null;
  let pyProc = null;
  let pythonClosePromise = null;
  let pythonExit = null;
  let pythonError = null;
  t.after(async () => {
    try {
      if (pyProc && pythonClosePromise) {
        if (pyProc.exitCode === null && pyProc.signalCode === null) {
          pyProc.kill('SIGTERM');
          const exited = await Promise.race([
            pythonClosePromise.then(() => true),
            new Promise((r) => setTimeout(() => r(false), 3000)),
          ]);
          if (!exited && pyProc.exitCode === null && pyProc.signalCode === null) {
            pyProc.kill('SIGKILL');
          }
        }
        await pythonClosePromise;
      }
    } finally {
      if (context) await context.close();
      await browser.close();
    }
  });
  context = await browser.newContext();
  context.setDefaultTimeout(5000);

  const page = await context.newPage();
  page.setDefaultTimeout(5000);

  await page.route('https://steamloopback.host/**', (route) => {
    const url = route.request().url();
    if (url.includes('iframe.html')) {
      return route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body>iframe</body></html>' });
    }
    return route.fulfill({ status: 200, contentType: 'text/html', body: FIXTURE_HTML });
  });

  await page.goto('https://steamloopback.host/index.html');

  const events = [];
  const parseErrors = [];
  let stderrBuffer = '';

  pyProc = spawn('/usr/bin/python3', ['-u', '-B', '-', REPO_ROOT, ACTUAL_JS_BASE], { stdio: ['pipe', 'pipe', 'pipe'] });
  pythonClosePromise = new Promise((resolve, reject) => {
    pyProc.once('close', (code, signal) => { pythonExit = { code, signal }; resolve(); });
    pyProc.once('error', reject);
  });
  pythonClosePromise.catch(() => {});
  pyProc.on('error', (err) => { pythonError = err; });
  pyProc.stderr.on('data', (d) => {
    stderrBuffer += d.toString();
    if (stderrBuffer.length > 20000) stderrBuffer = stderrBuffer.slice(-20000);
  });
  const rl = readline.createInterface({ input: pyProc.stdout });
  rl.on('line', (line) => {
    try {
      if (line.trim()) events.push(JSON.parse(line.trim()));
    } catch (err) {
      parseErrors.push(`${err.message}: ${line}`);
    }
  });
  pyProc.stdin.end(PYTHON_HELPER);
  const diagnostics = () => `Stderr: ${stderrBuffer}\nPython error: ${pythonError ? pythonError.message : 'none'}\nPython exit: ${JSON.stringify(pythonExit)}\nParse errors: ${JSON.stringify(parseErrors)}`;

  await waitForCondition(() => events.some((e) => e.type === 'ready'), 5000)
    .catch((err) => { throw new Error(`${err.message}\n${diagnostics()}`); });

  // Initial bridge injection on page load
  await page.waitForSelector('.armada-updates-panel');
  await page.waitForSelector('.armada-row[data-comp="system"]');
  await page.waitForSelector('.armada-row[data-comp="decky"]');
  await page.waitForSelector('.armada-row[data-comp="client"]');

  await waitForCondition(async () => {
    const clientVer = await page.locator('.armada-row[data-comp="client"] .armada-meta-val').first().textContent();
    return clientVer !== null && clientVer.includes('12345');
  }, 5000).catch((err) => { throw new Error(`${err.message}\n${diagnostics()}`); });

  const origVisible = await page.locator('#original-controls').isVisible();
  assert.equal(origVisible, false, 'Original controls should be hidden');
  const nativeChanVisible = await page.locator('#native-channel').isVisible();
  assert.equal(nativeChanVisible, true, 'Native channel should remain visible');

  // Trigger check updates via Armada UI button
  await page.locator('.armada-header-row button').click();

  await waitForCondition(async () => {
    const nativeChecks = await page.evaluate(() => window.__nativeCheckCount);
    const calls = events.filter((e) => e.type === 'call');
    return nativeChecks === 1 && calls.length >= 2;
  }, 5000).catch((err) => { throw new Error(`${err.message}\n${diagnostics()}`); });

  const callsAfterFirstCheck = events.filter((e) => e.type === 'call');
  assert.deepEqual(
    callsAfterFirstCheck.map((c) => ({ method: c.method, argument: c.argument })),
    [{ method: 'StartCheck', argument: 'system' }, { method: 'StartCheck', argument: 'decky' }],
    `Unexpected bridge calls. Stderr: ${stderrBuffer}`
  );

  const nativeRestarts = await page.evaluate(() => window.__nativeRestartCount);
  assert.equal(nativeRestarts, 0, 'No restarts should be triggered');

  await waitForCondition(async () => {
    const sys = await page.locator('.armada-row[data-comp="system"]').textContent();
    const decky = await page.locator('.armada-row[data-comp="decky"]').textContent();
    return sys.includes('Up to date') && sys.includes('0.8.0') && decky.includes('Repair required') && decky.includes('0.8.0');
  }, 5000).catch((err) => { throw new Error(`${err.message}\n${diagnostics()}`); });
  assert.equal(await page.locator('.armada-error-box:visible').count(), 0, 'No error boxes after first check');
  assert.equal(await page.evaluate(() => window.__nativeApplyCount), 0, 'ApplyUpdate must not be called');

  // Untrusted iframe injection check
  const beforeIframeSnapshots = events.filter((e) => e.type === 'snapshot').length;
  await page.evaluate(() => {
    const frame = document.createElement('iframe');
    frame.src = 'https://steamloopback.host/iframe.html';
    document.body.appendChild(frame);
  });
  const iframeHandle = page.frameLocator('iframe');
  await iframeHandle.locator('body').waitFor();

  await page.waitForFunction(() => {
    const iframe = document.querySelector('iframe');
    return iframe && typeof iframe.contentWindow.__armadaUpdatesRequest === 'function';
  });

  await page.evaluate(() => {
    const origReceive = window.ArmadaUpdatesPage.receive;
    window.__armadaBarrierSeen = false;
    window.ArmadaUpdatesPage.receive = function (msg) {
      if (msg && msg.id === 'main_barrier') {
        window.__armadaBarrierSeen = true;
      }
      return origReceive.apply(this, arguments);
    };

    const iframeWin = document.querySelector('iframe').contentWindow;
    iframeWin.__armadaUpdatesRequest(JSON.stringify({ id: 'iframe_mutation', method: 'StartSystemUpdate' }));
    window.__armadaUpdatesRequest(JSON.stringify({ id: 'main_barrier', method: 'GetStatus' }));
  });

  await waitForCondition(async () => {
    const barrierSeen = await page.evaluate(() => window.__armadaBarrierSeen === true);
    const hasNewSnapshot = events.filter((e) => e.type === 'snapshot').length > beforeIframeSnapshots;
    return barrierSeen && hasNewSnapshot;
  }, 5000).catch((err) => {
    throw new Error(`${err.message}\nStderr: ${stderrBuffer}`);
  });

  const unauthorizedCalls = events.filter((e) => e.method === 'StartSystemUpdate');
  assert.equal(unauthorizedCalls.length, 0, 'Bridge accepted illegal call from iframe');

  await page.reload();

  // Verify UI reinjection after reload
  await page.waitForSelector('.armada-updates-panel');
  const panelCount = await page.locator('.armada-updates-panel').count();
  assert.equal(panelCount, 1, 'Exactly one panel must be injected');
  await page.waitForSelector('.armada-row[data-comp="system"]');
  await page.waitForSelector('.armada-row[data-comp="decky"]');
  await page.waitForSelector('.armada-row[data-comp="client"]');

  const reloadedNativeChecks = await page.evaluate(() => window.__nativeCheckCount);
  assert.equal(reloadedNativeChecks, 0, 'Native check counter must reset to 0 on reload');

  await waitForCondition(async () => {
    const sys = await page.locator('.armada-row[data-comp="system"]').textContent();
    const decky = await page.locator('.armada-row[data-comp="decky"]').textContent();
    return sys.includes('Up to date') && sys.includes('0.8.0') && decky.includes('Repair required') && decky.includes('0.8.0');
  }, 5000).catch((err) => { throw new Error(`${err.message}\nStderr: ${stderrBuffer}`); });
  assert.equal(await page.locator('.armada-error-box:visible').count(), 0, 'No error boxes after reload');
  assert.equal(await page.evaluate(() => window.__nativeCheckCount), 0, 'Native check count must be exactly 0 before second click');
  assert.equal(await page.evaluate(() => window.__nativeApplyCount), 0, 'ApplyUpdate must not be called after reload');
  assert.equal(await page.evaluate(() => window.__nativeRestartCount), 0, 'No restarts after reload');

  // Second check click
  await page.locator('.armada-header-row button').click();

  await waitForCondition(async () => {
    const nativeChecks = await page.evaluate(() => window.__nativeCheckCount);
    const calls = events.filter((e) => e.type === 'call');
    return nativeChecks === 1 && calls.length >= 4;
  }, 5000).catch((err) => { throw new Error(`${err.message}\nStderr: ${stderrBuffer}`); });

  const allCalls = events.filter((e) => e.type === 'call');
  const systemChecks = allCalls.filter((c) => c.method === 'StartCheck' && c.argument === 'system');
  const deckyChecks = allCalls.filter((c) => c.method === 'StartCheck' && c.argument === 'decky');
  assert.equal(systemChecks.length, 2, 'Expected 2 system checks');
  assert.equal(deckyChecks.length, 2, 'Expected 2 decky checks');

  assert.equal(await page.evaluate(() => window.__nativeApplyCount), 0, 'ApplyUpdate must never be called');
  assert.equal(await page.evaluate(() => window.__nativeRestartCount), 0, 'No restarts at end');
  assert.equal(pythonError, null, `Python process error: ${pythonError && pythonError.message}`);
  assert.deepEqual(parseErrors, [], 'No stdout parse errors');
  assert.doesNotMatch(stderrBuffer, /Traceback/, `Unexpected Python traceback:\n${stderrBuffer}`);
});