(function(root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.ArmadaUpdatesPageFactory = factory();
    if (typeof window !== 'undefined') {
      if (window.ArmadaUpdatesPage && typeof window.ArmadaUpdatesPage.dispose === 'function') {
        try { window.ArmadaUpdatesPage.dispose(); } catch (_) {}
      }
      window.ArmadaUpdatesPage = factory().createPage(window);
      window.ArmadaUpdatesPage.start();
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';

  var I18N = {
    zh: {
      checkAll: '检查更新',
      checking: '正在检查...',
      update: '更新',
      restartSteam: '重启 Steam',
      restartDevice: '重启设备',
      repair: '修复组件',
      cancel: '取消',
      confirm: '确认',
      client: 'Steam 客户端',
      system: '系统更新',
      decky: 'Decky 插件平台',
      current: '当前版本',
      target: '可用版本',
      channel: '更新通道',
      stable: '稳定版',
      beta: '测试版',
      testing: '预览版',
      deckyChannel: '官方预览版',
      notChecked: '未检查',
      upToDate: '已是最新版本',
      available: '发现新版本',
      applying: '正在应用更新...',
      rebootRequired: '需要重启',
      failed: '操作失败',
      completed: '操作已完成',
      installed: '已安装',
      needsRepair: '需要修复',
      awaitConfirmation: '等待确认',
      notConfigured: '尚未配置',
      checkFailed: '检查失败',
      unsupported: '不受支持',
      uncertain: '状态不确定',
      pendingConflict: '存在待处理冲突',
      unknown: '未知状态',
      errorLabel: '错误详情'
    },
    en: {
      checkAll: 'Check for updates',
      checking: 'Checking...',
      update: 'Update',
      restartSteam: 'Restart Steam',
      restartDevice: 'Restart system',
      repair: 'Repair platform',
      cancel: 'Cancel',
      confirm: 'Confirm',
      client: 'Steam Client',
      system: 'System Updates',
      decky: 'Decky Loader',
      current: 'Current',
      target: 'Available',
      channel: 'Channel',
      stable: 'Stable',
      beta: 'Beta',
      testing: 'Preview',
      deckyChannel: 'Official prerelease',
      notChecked: 'Not checked',
      upToDate: 'Up to date',
      available: 'Update available',
      applying: 'Applying update...',
      rebootRequired: 'Restart required',
      failed: 'Action failed',
      completed: 'Operation completed',
      installed: 'Installed',
      needsRepair: 'Repair required',
      awaitConfirmation: 'Awaiting confirmation',
      notConfigured: 'Not configured',
      checkFailed: 'Check failed',
      unsupported: 'Unsupported',
      uncertain: 'Status uncertain',
      pendingConflict: 'Pending conflict',
      unknown: 'Unknown status',
      errorLabel: 'Error details'
    }
  };

  function createPage(hostWindow) {
    var win = hostWindow || window;
    var doc = win.document;
    var isZh = !!(win.navigator && win.navigator.language && win.navigator.language.toLowerCase().indexOf('zh') === 0);
    var t = isZh ? I18N.zh : I18N.en;

    var started = false;
    var disposed = false;
    var adapter = null;
    var mountedRoot = null;
    var nativeSection = null;
    var hiddenNodes = [];
    var observer = null;
    var observerDebounce = null;
    var pollTimer = null;
    var isPolling = false;
    var warnOnce = false;
    var nextReqId = 1;
    var requestPrefix = 'armada_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2) + '_';
    var pendingRequests = Object.create(null);
    var readOnlyRefreshed = { system: false, decky: false };
    var nativeRefreshed = false;
    var pendingConfirm = null; // { component, action }
    var pendingAction = { client: null, system: null, decky: null };
    var pendingJob = { system: null, decky: null };

    var state = {
      client: null,
      system: null,
      decky: null
    };

    var dom = {
      style: null,
      panel: null,
      checkAllBtn: null,
      rows: {}
    };

    function makeId() {
      return requestPrefix + (nextReqId++);
    }

    function sendRequest(method, argument) {
      return new Promise(function(resolve, reject) {
        if (disposed) return reject(new Error('Page disposed'));
        if (typeof win.__armadaUpdatesRequest !== 'function') {
          return reject(new Error('CDP bridge binding __armadaUpdatesRequest unavailable'));
        }
        if (Object.keys(pendingRequests).length >= 32) {
          return reject(new Error('Too many pending requests'));
        }
        var id = makeId();
        var payload = { id: id, method: method };
        if (argument !== undefined) payload.argument = argument;
        var raw;
        try {
          raw = JSON.stringify(payload);
        } catch (e) { return reject(e); }
        if (raw.length > 8192) return reject(new Error('Payload exceeds limit'));

        var timer = win.setTimeout(function() {
          delete pendingRequests[id];
          reject(new Error('Request timed out: ' + method));
        }, 15000);

        pendingRequests[id] = { resolve: resolve, reject: reject, timer: timer };
        try {
          win.__armadaUpdatesRequest(raw);
        } catch (err) {
          win.clearTimeout(timer);
          delete pendingRequests[id];
          reject(err);
        }
      });
    }

    function receive(msg) {
      if (!msg || typeof msg.id !== 'string') return;
      var entry = pendingRequests[msg.id];
      if (!entry) return;
      delete pendingRequests[msg.id];
      win.clearTimeout(entry.timer);
      if (msg.ok === true) entry.resolve(msg.result);
      else if (msg.ok === false) entry.reject(new Error(typeof msg.error === 'string' ? msg.error : 'Request failed'));
      else entry.reject(new Error('Malformed response: ok must be boolean'));
    }

    function findUpdateSection() {
      var headings = doc.querySelectorAll('.RCuL60vD-fW2zF5PlEKHx, [class*="SoftwareUpdateSection"]');
      if (headings.length !== 1) return null;
      var section = headings[0].closest('.DialogSettingsSection[role="region"]');
      return section || null;
    }

    function ensureAdapter() {
      if (!adapter && win.ArmadaSteamClientUpdates && typeof win.ArmadaSteamClientUpdates.createAdapter === 'function') {
        try {
          adapter = win.ArmadaSteamClientUpdates.createAdapter(win);
        } catch (e) { adapter = null; }
      }
      if (adapter && typeof adapter.snapshot === 'function') {
        var snap = adapter.snapshot();
        if (snap && snap.state !== 'adapter_unavailable') return adapter;
      }
      return null;
    }

    function buildCss() {
      var css = doc.createElement('style');
      css.textContent = [
        '.armada-updates-panel { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; width: 100%; max-width: 100%; box-sizing: border-box; overflow-wrap: break-word; }',
        '.armada-header-row { display: flex; justify-content: flex-end; margin-bottom: 4px; flex-wrap: wrap; }',
        '.armada-btn { min-height: 44px; min-width: 90px; padding: 8px 16px; font-size: 16px; border-radius: 4px; border: none; cursor: pointer; color: #fff; background: #3d4450; transition: background .15s; box-sizing: border-box; }',
        '.armada-btn:hover:not(:disabled) { background: #4b5463; }',
        '.armada-btn:disabled { opacity: .45; cursor: default; }',
        '.armada-btn-primary { background: #1a9fff; }',
        '.armada-btn-primary:hover:not(:disabled) { background: #0080ff; }',
        '.armada-btn-cancel { background: #555; }',
        '.armada-btn-danger { background: #d94126; }',
        '.armada-row { background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box; }',
        '.armada-row-main { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }',
        '.armada-row-title { font-size: 18px; font-weight: 600; color: #e1e7ed; min-width: 140px; }',
        '.armada-row-meta { display: flex; flex-wrap: wrap; gap: 12px; font-size: 16px; color: #8f98a0; align-items: center; }',
        '.armada-meta-item { display: inline-flex; align-items: center; font-size: 16px; }',
        '.armada-meta-item span:first-child { margin-right: 4px; font-size: 16px; }',
        '.armada-meta-val { color: #dcdedf; font-size: 16px; }',
        '.armada-status-pill { font-size: 16px; padding: 2px 6px; border-radius: 3px; background: rgba(255,255,255,0.08); color: #c6d4df; }',
        '.armada-channel-sel { min-height: 44px; font-size: 16px; background: #23262e; color: #dfe3e6; border: 1px solid #3c404a; border-radius: 4px; padding: 0 8px; box-sizing: border-box; }',
        '.armada-progress-bar { width: 100%; height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden; }',
        '.armada-progress-fill { height: 100%; background: #1a9fff; transition: width .2s; }',
        '.armada-error-box { font-size: 16px; color: #ff7360; background: rgba(217,65,38,0.1); border: 1px solid rgba(217,65,38,0.25); border-radius: 4px; padding: 6px 8px; margin-top: 4px; }',
        '.armada-error-box summary { font-size: 16px; font-weight: 600; cursor: pointer; outline: none; }',
        '.armada-error-box summary:focus-visible { outline: 2px solid #1a9fff; }',
        '.armada-error-text { margin: 6px 0 0 0; font-size: 16px; font-family: inherit; max-height: 120px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }'
      ].join(' ');
      return css;
    }

    function buildRow(compKey, titleText) {
      var row = doc.createElement('div');
      row.className = 'armada-row';
      row.setAttribute('data-comp', compKey);

      var main = doc.createElement('div');
      main.className = 'armada-row-main';

      var title = doc.createElement('div');
      title.className = 'armada-row-title';
      title.textContent = titleText;
      main.appendChild(title);

      var meta = doc.createElement('div');
      meta.className = 'armada-row-meta';

      var curWrap = doc.createElement('div');
      curWrap.className = 'armada-meta-item';
      var curLbl = doc.createElement('span');
      curLbl.textContent = t.current + ':';
      var curVal = doc.createElement('span');
      curVal.className = 'armada-meta-val';
      curVal.textContent = '—';
      curWrap.appendChild(curLbl);
      curWrap.appendChild(curVal);
      meta.appendChild(curWrap);

      var tgtWrap = doc.createElement('div');
      tgtWrap.className = 'armada-meta-item';
      var tgtLbl = doc.createElement('span');
      tgtLbl.textContent = t.target + ':';
      var tgtVal = doc.createElement('span');
      tgtVal.className = 'armada-meta-val';
      tgtVal.textContent = '—';
      tgtWrap.appendChild(tgtLbl);
      tgtWrap.appendChild(tgtVal);
      meta.appendChild(tgtWrap);

      var chWrap = doc.createElement('div');
      chWrap.className = 'armada-meta-item';
      var chSelect = null;
      var chStatic = null;
      if (compKey === 'system') {
        var chLbl = doc.createElement('span');
        chLbl.textContent = t.channel + ':';
        chSelect = doc.createElement('select');
        chSelect.className = 'armada-channel-sel';
        var opts = [
          { v: '', label: '—' },
          { v: 'stable', label: t.stable },
          { v: 'beta', label: t.beta },
          { v: 'testing', label: t.testing }
        ];
        for (var o = 0; o < opts.length; o++) {
          var optEl = doc.createElement('option');
          optEl.value = opts[o].v;
          optEl.textContent = opts[o].label;
          chSelect.appendChild(optEl);
        }
        chWrap.appendChild(chLbl);
        chWrap.appendChild(chSelect);
      } else if (compKey === 'decky') {
        var dLbl = doc.createElement('span');
        dLbl.textContent = t.channel + ':';
        chStatic = doc.createElement('span');
        chStatic.className = 'armada-meta-val';
        chStatic.textContent = t.deckyChannel;
        chWrap.appendChild(dLbl);
        chWrap.appendChild(chStatic);
      }
      if (compKey !== 'client') meta.appendChild(chWrap);

      var statusPill = doc.createElement('span');
      statusPill.className = 'armada-status-pill';
      statusPill.textContent = t.notChecked;
      meta.appendChild(statusPill);

      main.appendChild(meta);

      var actions = doc.createElement('div');
      actions.className = 'armada-actions-wrap';
      actions.style.display = 'flex';
      actions.style.gap = '8px';

      var opBtn = doc.createElement('button');
      opBtn.className = 'armada-btn armada-btn-primary';
      opBtn.style.display = 'none';

      var cancelBtn = doc.createElement('button');
      cancelBtn.className = 'armada-btn armada-btn-cancel';
      cancelBtn.textContent = t.cancel;
      cancelBtn.style.display = 'none';

      actions.appendChild(opBtn);
      actions.appendChild(cancelBtn);
      main.appendChild(actions);
      row.appendChild(main);

      var prog = doc.createElement('div');
      prog.className = 'armada-progress-bar';
      prog.style.display = 'none';
      var progFill = doc.createElement('div');
      progFill.className = 'armada-progress-fill';
      progFill.style.width = '0%';
      prog.appendChild(progFill);
      row.appendChild(prog);

      var errBox = doc.createElement('details');
      errBox.className = 'armada-error-box';
      errBox.style.display = 'none';
      var errSummary = doc.createElement('summary');
      errSummary.textContent = t.errorLabel;
      var errText = doc.createElement('pre');
      errText.className = 'armada-error-text';
      errBox.appendChild(errSummary);
      errBox.appendChild(errText);
      row.appendChild(errBox);

      return {
        container: row,
        curVal: curVal,
        tgtVal: tgtVal,
        tgtWrap: tgtWrap,
        chSelect: chSelect,
        statusPill: statusPill,
        opBtn: opBtn,
        cancelBtn: cancelBtn,
        prog: prog,
        progFill: progFill,
        errBox: errBox,
        errText: errText
      };
    }

    function hideNativeChild(c) {
      if (!c || c === mountedRoot || c.classList.contains('SettingsDialogSubHeader')) return;
      for (var j = 0; j < hiddenNodes.length; j++) {
        if (hiddenNodes[j].node === c) return;
      }
      var prevVal = c.style.getPropertyValue('display');
      var prevPri = c.style.getPropertyPriority('display');
      hiddenNodes.push({ node: c, prevValue: prevVal, prevPriority: prevPri });
      c.style.setProperty('display', 'none', 'important');
    }

    function mount() {
      if (disposed) return;
      var sec = findUpdateSection();
      var adp = ensureAdapter();
      if (!sec || !adp) {
        if (sec && !adp && !warnOnce) {
          console.warn('[ArmadaUpdates] Update section found but native adapter capability missing.');
          warnOnce = true;
        }
        if (mountedRoot) unmount();
        return;
      }

      if (mountedRoot && mountedRoot.isConnected && mountedRoot.parentNode === sec && nativeSection === sec) {
        var curChildren = Array.prototype.slice.call(sec.children);
        for (var k = 0; k < curChildren.length; k++) {
          hideNativeChild(curChildren[k]);
        }
        return;
      }

      unmount();

      nativeSection = sec;
      var children = Array.prototype.slice.call(sec.children);
      hiddenNodes = [];
      for (var i = 0; i < children.length; i++) {
        hideNativeChild(children[i]);
      }

      dom.style = buildCss();
      doc.head.appendChild(dom.style);

      var panel = doc.createElement('div');
      panel.className = 'armada-updates-panel';
      panel.setAttribute('tabindex', '-1');
      dom.panel = panel;

      var header = doc.createElement('div');
      header.className = 'armada-header-row';
      var checkBtn = doc.createElement('button');
      checkBtn.className = 'armada-btn armada-btn-primary';
      checkBtn.textContent = t.checkAll;
      checkBtn.addEventListener('click', checkAll);
      header.appendChild(checkBtn);
      panel.appendChild(header);
      dom.checkAllBtn = checkBtn;

      dom.rows.client = buildRow('client', t.client);
      dom.rows.system = buildRow('system', t.system);
      dom.rows.decky = buildRow('decky', t.decky);

      panel.appendChild(dom.rows.client.container);
      panel.appendChild(dom.rows.system.container);
      panel.appendChild(dom.rows.decky.container);

      panel.addEventListener('keydown', onPanelKeyDown);
      attachRowEvents();

      sec.appendChild(panel);
      mountedRoot = panel;

      syncClientSnapshot();
      if (!nativeRefreshed && typeof adp.refresh === 'function') {
        nativeRefreshed = true;
        Promise.resolve().then(function() { return adp.refresh(); }).catch(function(err) {
          markLocalError('client', err);
        }).finally(function() {
          if (disposed) return;
          syncClientSnapshot();
          render();
        });
      }
      render();
      poll();
    }

    function unmount() {
      if (dom.panel && dom.panel.parentNode) dom.panel.parentNode.removeChild(dom.panel);
      if (mountedRoot && mountedRoot.parentNode) mountedRoot.parentNode.removeChild(mountedRoot);
      if (dom.style && dom.style.parentNode) dom.style.parentNode.removeChild(dom.style);
      for (var i = 0; i < hiddenNodes.length; i++) {
        var h = hiddenNodes[i];
        if (h.node) {
          if (h.prevValue) {
            h.node.style.setProperty('display', h.prevValue, h.prevPriority || '');
          } else {
            h.node.style.removeProperty('display');
          }
        }
      }
      hiddenNodes = [];
      mountedRoot = null;
      dom.panel = null;
      nativeSection = null;
    }

    function onPanelKeyDown(e) {
      if (!mountedRoot) return;
      if (doc.activeElement && doc.activeElement.tagName === 'SELECT') return;
      if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].indexOf(e.key) === -1) return;
      var focusable = mountedRoot.querySelectorAll('button:not(:disabled), select:not(:disabled), summary, [tabindex="0"]');
      var list = Array.prototype.filter.call(focusable, function(el) { return el.offsetParent !== null; });
      var idx = list.indexOf(doc.activeElement);
      if (idx === -1) return;
      e.stopPropagation();
      e.preventDefault();
      var next = idx;
      if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (idx + 1) % list.length;
      else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (idx - 1 + list.length) % list.length;
      if (list[next]) list[next].focus();
    }

    function attachRowEvents() {
      var rSys = dom.rows.system;
      if (rSys && rSys.chSelect) {
        rSys.chSelect.addEventListener('change', function() {
          var val = rSys.chSelect.value;
          if (!val || isActionLocked('system') || isMutationLocked('system')) {
            render();
            return;
          }
          startJob('system', 'SetSystemChannel', val);
        });
      }

      ['client', 'system', 'decky'].forEach(function(comp) {
        var row = dom.rows[comp];
        if (!row) return;
        row.cancelBtn.addEventListener('click', function() {
          if (pendingConfirm && pendingConfirm.component === comp) {
            pendingConfirm = null;
            render();
          }
        });
        row.opBtn.addEventListener('click', function() {
          handleActionClick(comp);
        });
      });
    }

    function errorText(err) {
      return String(err && err.message ? err.message : err).slice(0, 2000);
    }

    function isRetryableAdapterError(err) {
      return !!(err && err.retryable === true &&
        (err.code === 'apply_baseline_unknown' || err.code === 'restart_predispatch_busy'));
    }

    function isRestartPredispatchError(err) {
      return !!(err && err.code === 'restart_predispatch_busy');
    }

    function clearRetryableClientError() {
      if (!state.client || !state.client.retryableError) return;
      state.client.retryableError = null;
      state.client.retryableCode = null;
      state.client.error = null;
    }

    function markLocalError(comp, err) {
      var previous = state[comp] || { component: comp };
      previous.state = 'failed';
      previous.error = errorText(err);
      previous.localError = true;
      state[comp] = previous;
      render();
    }

    function markPollError(comp, err) {
      if (state[comp] && state[comp].localError) return;
      var previous = state[comp] || { component: comp };
      previous.state = 'failed';
      previous.error = errorText(err);
      state[comp] = previous;
    }

    function syncClientSnapshot() {
      if (!adapter) return;
      var previous = state.client;
      var snap = adapter.snapshot();
      if (snap && previous && previous.localError) {
        snap.state = 'failed';
        snap.error = previous.error;
        snap.localError = true;
      } else if (snap && previous && previous.retryableError && !snap.error) {
        if (previous.retryableCode === 'restart_predispatch_busy') {
          // The refusal dispatched nothing, so the adapter's restart evidence
          // is still real. Keep the requirement and its reason visible; while
          // Steam is busy the row is shown but disabled, and it becomes
          // directly retryable once Steam turns idle again.
          if (snap.state === 'restart_required' && snap.requiresClientRestart === true) {
            snap.error = previous.retryableError;
            snap.retryableError = previous.retryableError;
            snap.retryableCode = previous.retryableCode;
          } else if (snap.state === 'native_busy') {
            snap.state = 'restart_required';
            snap.requiresClientRestart = true;
            snap.busy = true;
            snap.error = previous.retryableError;
            snap.retryableError = previous.retryableError;
            snap.retryableCode = previous.retryableCode;
          }
        } else if (snap.state === 'available') {
          // A retryable pre-dispatch refusal leaves the accepted check and its
          // available update intact. Keep the real reason visible without
          // locking the row or asking the user for an unrelated header check.
          snap.error = previous.retryableError;
          snap.retryableError = previous.retryableError;
        }
      }
      state.client = snap;
    }

    function isBusy(comp) {
      return !!(pendingAction[comp] || (comp !== 'client' && pendingJob[comp]) || (state[comp] && state[comp].busy === true));
    }

    function isMutationLocked(comp) {
      if (comp !== 'system' && comp !== 'decky') return false;
      var other = comp === 'system' ? 'decky' : 'system';
      return isBusy(comp) || isBusy(other);
    }

    function isActionLocked(comp) {
      return isBusy(comp) || !!(state[comp] && state[comp].localError) || isMutationLocked(comp);
    }

    function clientRestartPending() {
      var snap = null;
      if (adapter && typeof adapter.snapshot === 'function') {
        try { snap = adapter.snapshot(); } catch (e) { snap = null; }
      }
      if (!snap) snap = state.client;
      return !!(snap && snap.state === 'restart_required' && snap.requiresClientRestart === true);
    }

    function startJob(comp, method, arg) {
      var isRead = method === 'StartCheck' || method === 'RefreshStatus';
      if ((comp !== 'system' && comp !== 'decky') || isBusy(comp) || (!isRead && isMutationLocked(comp))) {
        return Promise.resolve(false);
      }
      if (isRead) {
        state[comp] = state[comp] || { component: comp };
        state[comp].localError = false;
        state[comp].error = null;
      }
      pendingAction[comp] = { method: method };
      if (pendingConfirm && pendingConfirm.component === comp) pendingConfirm = null;
      render();
      return sendRequest(method, arg).then(function(res) {
        if (!res || typeof res.jobId !== 'string' || res.jobId.length === 0) {
          throw new Error('Malformed ' + method + ' response: jobId must be a nonempty string');
        }
        pendingJob[comp] = { id: res.jobId, method: method };
        return poll().then(function() { return true; });
      }).catch(function(err) {
        markLocalError(comp, err);
        return false;
      }).finally(function() {
        pendingAction[comp] = null;
        render();
      });
    }

    function handleActionClick(comp) {
      var s = state[comp];
      if (!s || isActionLocked(comp)) return;
      var action = determineAction(comp, s);
      if (!action) return;

      if (!pendingConfirm || pendingConfirm.component !== comp || pendingConfirm.action !== action) {
        pendingConfirm = { component: comp, action: action };
        render();
        return;
      }

      pendingConfirm = null;
      executeAction(comp, action);
    }

    function determineAction(comp, s) {
      if (!s || s.localError) return null;
      if (comp === 'client') {
        if (s.state === 'available') return s.busy === true ? null : 'update_client';
        // An accepted client restart requirement stays visible while Steam is
        // busy; isActionLocked() disables the button until it is idle again.
        if (s.state === 'restart_required' && s.requiresClientRestart === true) return 'restart_steam';
        return null;
      }
      if (s.busy === true) return null;
      if (comp === 'system') {
        if (s.state === 'update_available') return 'update_system';
        if (s.state === 'awaiting_reboot' && s.requiresReboot === true) return 'restart_device';
      } else if (comp === 'decky') {
        if (s.state === 'update_available') return 'update_decky';
        if (s.state === 'repair_required' || s.state === 'inactive') return 'repair_decky';
      }
      return null;
    }

    function executeNativeAction(method) {
      if (disposed || !adapter || typeof adapter[method] !== 'function' || isActionLocked('client')) return;
      clearRetryableClientError();
      pendingAction.client = { method: method };
      render();
      Promise.resolve().then(function() {
        if (disposed) return;
        return adapter[method]();
      }).catch(function(err) {
        if (disposed) return;
        if (isRetryableAdapterError(err)) {
          var snap = adapter.snapshot();
          var restartRefusal = isRestartPredispatchError(err) && snap && !snap.error &&
            (snap.state === 'native_busy' ||
              (snap.state === 'restart_required' && snap.requiresClientRestart === true));
          if (restartRefusal) {
            // The adapter refused the restart because Steam became busy; no
            // native restart was requested, so its restart evidence is still
            // real. Keep that evidence and the reason, and let the finally
            // block render it; the row stays disabled until Steam is idle.
            var previous = state.client || {};
            previous.error = errorText(err);
            previous.retryableError = previous.error;
            previous.retryableCode = err.code;
            previous.localError = false;
            state.client = previous;
            return;
          }
          if (!isRestartPredispatchError(err) && snap &&
              snap.state === 'available' && !snap.error) {
            // Nothing was dispatched and the adapter's previous check is still
            // accepted, so the row must stay actionable for a direct retry.
            snap.error = errorText(err);
            snap.retryableError = snap.error;
            snap.localError = false;
            state.client = snap;
            render();
            return;
          }
        }
        markLocalError('client', err);
      }).finally(function() {
        if (disposed) return;
        pendingAction.client = null;
        syncClientSnapshot();
        render();
      });
    }

    function executeAction(comp, action) {
      if (disposed || isActionLocked(comp)) return;
      if (action === 'update_client') {
        executeNativeAction('apply');
      } else if (action === 'restart_steam') {
        executeNativeAction('restartClient');
      } else if (action === 'update_system') {
        startJob('system', 'StartSystemUpdate');
      } else if (action === 'update_decky' || action === 'repair_decky') {
        startJob('decky', 'StartDeckyAction', action === 'update_decky' ? 'update' : 'repair');
      } else if (action === 'restart_device') {
        pendingAction.system = { method: 'RestartPC' };
        render();
        sendRequest('GetStatus').then(function(res) {
          if (disposed) return;
          var components = validateStatus(res);
          var sys = validateComponent('system', components.system);
          var decky = validateComponent('decky', components.decky);
          if (decky.busy !== false) {
            throw new Error('Restart verification rejected: Decky is not idle');
          }
          if (sys.busy !== false || sys.result.component !== 'system' || sys.result.state !== 'awaiting_reboot' || sys.result.requiresReboot !== true) {
            throw new Error('Restart verification rejected: system is not freshly awaiting reboot');
          }
          if (!win.SteamClient || !win.SteamClient.System || typeof win.SteamClient.System.RestartPC !== 'function') {
            throw new Error('RestartPC is unavailable');
          }
          return win.SteamClient.System.RestartPC();
        }).catch(function(err) {
          if (disposed) return;
          markLocalError('system', err);
        }).finally(function() {
          if (disposed) return;
          pendingAction.system = null;
          render();
        });
      }
    }

    function checkComponent(comp) {
      if (disposed || pendingAction[comp]) return Promise.resolve(false);
      if (!pendingJob[comp] && !isBusy(comp)) return startJob(comp, 'StartCheck', comp);
      var token = { method: 'check' };
      var job = pendingJob[comp];
      pendingAction[comp] = token;
      render();
      return sendRequest('GetStatus').then(function(res) {
        if (disposed) return false;
        var components = validateStatus(res);
        var record = validateComponent(comp, components[comp]);
        if (pendingAction[comp] !== token || pendingJob[comp] !== job) return false;
        if (record.busy) {
          pendingAction[comp] = null;
          render();
          return false;
        }
        pendingJob[comp] = null;
        if (state[comp]) {
          state[comp].localError = false;
          state[comp].error = null;
        }
        pendingAction[comp] = null;
        handleBackendComponent(comp, record);
        return startJob(comp, 'StartCheck', comp);
      }).catch(function(err) {
        if (disposed) return false;
        markLocalError(comp, err);
        return false;
      }).finally(function() {
        if (disposed) return;
        if (pendingAction[comp] === token) pendingAction[comp] = null;
        render();
      });
    }

    function checkAll() {
      if (disposed) return;
      if (dom.checkAllBtn) dom.checkAllBtn.disabled = true;
      var checks = [];
      if (adapter && typeof adapter.check === 'function' && !isBusy('client')) {
        if (clientRestartPending()) {
          // The native adapter refuses new checks while its own restart
          // evidence is pending. Keep that evidence and its error visible
          // instead of reporting a synthetic client failure, and still check
          // the system and Decky rows.
          syncClientSnapshot();
          render();
        } else {
          pendingAction.client = { method: 'check' };
          state.client = adapter.snapshot();
          if (state.client) {
            state.client.localError = false;
            state.client.retryableError = null;
            state.client.error = null;
          }
          render();
          checks.push(Promise.resolve().then(function() {
            if (disposed) return;
            return adapter.check();
          }).catch(function(err) {
            if (disposed) return;
            markLocalError('client', err);
          }).finally(function() {
            if (disposed) return;
            pendingAction.client = null;
            syncClientSnapshot();
            render();
          }));
        }
      }
      checks.push(checkComponent('system'));
      checks.push(checkComponent('decky'));
      Promise.all(checks).then(function() {
        if (disposed) return;
        return poll();
      });
    }

    function poll() {
      if (disposed || isPolling || !mountedRoot) return Promise.resolve();
      isPolling = true;

      var adp = ensureAdapter();
      if (!adp) {
        unmount();
        isPolling = false;
        return Promise.resolve();
      }

      syncClientSnapshot();

      return sendRequest('GetStatus').then(function(res) {
        if (disposed) return;
        var components = validateStatus(res);
        handleBackendComponent('system', components.system);
        handleBackendComponent('decky', components.decky);
      }).catch(function(err) {
        if (disposed) return;
        markPollError('system', err);
        markPollError('decky', err);
      }).finally(function() {
        isPolling = false;
        if (disposed) return;
        render();
        checkReadOnlyInitial();
      });
    }

    function validateStatus(res) {
      if (!res || typeof res !== 'object' || res.schemaVersion !== 1 || !res.components || typeof res.components !== 'object' || Array.isArray(res.components)) {
        throw new Error('Malformed GetStatus response');
      }
      return res.components;
    }

    function validateComponent(comp, compData) {
      if (!compData || typeof compData !== 'object' || typeof compData.busy !== 'boolean' || !compData.result || typeof compData.result !== 'object' || Array.isArray(compData.result)) {
        throw new Error('Malformed ' + comp + ' status record');
      }
      var res = compData.result;
      if (res.component !== comp || typeof res.state !== 'string' || res.state.length === 0 || typeof res.requiresReboot !== 'boolean') {
        throw new Error('Malformed ' + comp + ' status result');
      }
      return compData;
    }

    function completedResult(comp, compData, jobId) {
      var completed = compData.completed;
      if (!completed || typeof completed !== 'object' || Array.isArray(completed)) return null;
      var result = completed[jobId];
      if (!result || typeof result !== 'object' || Array.isArray(result) ||
          result.component !== comp || typeof result.state !== 'string' || result.state.length === 0) {
        return null;
      }
      return result;
    }

    function setComponentState(comp, compData, result) {
      state[comp] = {
        component: comp,
        busy: compData.busy === true,
        jobId: compData.jobId,
        operation: compData.operation,
        state: result.state,
        phase: result.phase,
        currentVersion: result.currentVersion,
        targetVersion: result.targetVersion,
        requiresReboot: result.requiresReboot,
        channel: result.channel,
        error: result.error,
        progressPercent: result.progressPercent,
        localError: false
      };
    }

    function handleBackendComponent(comp, compData) {
      try {
        validateComponent(comp, compData);
      } catch (err) {
        markPollError(comp, err);
        return;
      }
      var expected = pendingJob[comp];
      if (!expected) {
        if (state[comp] && state[comp].localError) return;
        if (pendingAction[comp]) return;
      } else if (compData.jobId !== expected.id) {
        // The expected job is no longer current. Its own outcome can only be
        // read from the retained history, never from the job that replaced it.
        // Consume it as soon as it appears, even while a newer job is running,
        // so evicting it from history later cannot leave it pending forever.
        var finished = completedResult(comp, compData, expected.id);
        if (finished) pendingJob[comp] = null;
        if (compData.busy === true) {
          // A newer job owns the component now: keep it visibly busy without
          // presenting its result as the expected job's outcome.
          setComponentState(comp, compData, {
            component: comp, state: 'running', phase: compData.operation, requiresReboot: false
          });
          return;
        }
        if (!finished) return;
        setComponentState(comp, compData, finished);
        return;
      } else if (compData.busy === false) {
        pendingJob[comp] = null;
      }
      setComponentState(comp, compData, compData.result);
    }

    function checkReadOnlyInitial() {
      ['system', 'decky'].forEach(function(comp) {
        if (readOnlyRefreshed[comp]) return;
        if (isBusy(comp) || pendingJob[comp]) return;
        if (!(state[comp] && (state[comp].state === 'not_checked' || state[comp].state === undefined))) return;
        var launched = startJob(comp, 'RefreshStatus', comp);
        if (launched) readOnlyRefreshed[comp] = true;
      });
    }

    function formatStateText(s) {
      if (!s) return t.notChecked;
      if (s.state === 'failed' || s.localError) return t.failed;
      var work = s.phase || s.operation;
      if (s.busy === true || s.state === 'checking' || s.state === 'applying') {
        if (work === 'check' || work === 'status' || s.state === 'checking') return t.checking;
        if (work === 'update' || work === 'repair' || s.state === 'applying') return t.applying;
        return t.applying;
      }
      if (s.state === 'available' || s.state === 'update_available') return t.available;
      if (s.state === 'up_to_date') return t.upToDate;
      if (s.state === 'completed') return t.completed;
      if (s.state === 'installed') return t.installed;
      if (s.state === 'inactive' || s.state === 'repair_required') return t.needsRepair;
      if (s.state === 'awaiting_confirmation') return t.awaitConfirmation;
      if (s.state === 'awaiting_status') return t.checking;
      if (s.state === 'not_configured') return t.notConfigured;
      if (s.state === 'check_failed') return t.checkFailed;
      if (s.state === 'unsupported') return t.unsupported;
      if (s.state === 'uncertain') return t.uncertain;
      if (s.state === 'pending_conflict') return t.pendingConflict;
      if (s.state === 'awaiting_reboot' && s.requiresReboot === true) return t.rebootRequired;
      if (s.state === 'restart_required' && s.requiresClientRestart === true) return t.rebootRequired;
      if (s.state === 'not_checked') return t.notChecked;
      return t.unknown;
    }

    function render() {
      if (!mountedRoot) return;
      var clientBusy = isBusy('client') || !!(state.client && (state.client.state === 'checking' || state.client.state === 'applying'));
      var sysBusy = isBusy('system');
      var dkyBusy = isBusy('decky');
      var mutationLock = sysBusy || dkyBusy;

      if (dom.checkAllBtn) {
        dom.checkAllBtn.disabled = !!(clientBusy && sysBusy && dkyBusy);
      }

      ['client', 'system', 'decky'].forEach(function(comp) {
        var row = dom.rows[comp];
        var s = state[comp];
        if (!row) return;

        var curV = (s && s.currentVersion) ? String(s.currentVersion) : '—';
        var tgtV = (s && s.targetVersion) ? String(s.targetVersion) : '—';
        if (row.curVal.textContent !== curV) row.curVal.textContent = curV;
        if (row.tgtVal.textContent !== tgtV) row.tgtVal.textContent = tgtV;

        var statusStr = formatStateText(s);
        if (pendingAction[comp] && !(s && (s.localError || s.state === 'failed'))) {
          var m = pendingAction[comp].method;
          statusStr = (m === 'check' || m === 'StartCheck' || m === 'RefreshStatus') ? t.checking : t.applying;
        }
        if (row.statusPill.textContent !== statusStr) row.statusPill.textContent = statusStr;

        if (row.chSelect) {
          var actualCh = (s && s.channel) || '';
          if (row.chSelect.value !== actualCh) row.chSelect.value = actualCh;
          row.chSelect.disabled = isActionLocked('system');
        }

        var prog = (s && typeof s.progressPercent === 'number' && s.progressPercent >= 0) ? s.progressPercent : null;
        if (prog !== null && (s.busy || s.state === 'applying')) {
          row.prog.style.display = 'block';
          row.progFill.style.width = Math.min(100, Math.max(0, prog)) + '%';
        } else {
          row.prog.style.display = 'none';
        }

        var err = (s && s.error) ? String(s.error).slice(0, 2000) : '';
        if (err) {
          if (row.errText && row.errText.textContent !== err) {
            row.errText.textContent = err;
          }
          row.errBox.style.display = 'block';
        } else {
          if (row.errText) row.errText.textContent = '';
          row.errBox.open = false;
          row.errBox.style.display = 'none';
        }

        var action = s ? determineAction(comp, s) : null;
        var isConfirming = pendingConfirm && pendingConfirm.component === comp && pendingConfirm.action === action;

        if (action) {
          row.opBtn.style.display = 'inline-block';
          var baseLabel = t.update;
          if (action === 'restart_steam') baseLabel = t.restartSteam;
          else if (action === 'restart_device') baseLabel = t.restartDevice;
          else if (action === 'repair_decky') baseLabel = t.repair;

          var label = isConfirming ? (t.confirm + ' ' + baseLabel + '?') : baseLabel;
          if (row.opBtn.textContent !== label) row.opBtn.textContent = label;

          row.opBtn.disabled = !!isActionLocked(comp);

          if (isConfirming) {
            row.opBtn.className = 'armada-btn armada-btn-danger';
            row.cancelBtn.style.display = 'inline-block';
          } else {
            row.opBtn.className = 'armada-btn armada-btn-primary';
            row.cancelBtn.style.display = 'none';
          }
        } else {
          row.opBtn.style.display = 'none';
          row.cancelBtn.style.display = 'none';
        }
      });
    }

    function start() {
      if (disposed || started) return;
      started = true;
      observer = new win.MutationObserver(function(mutations) {
        if (disposed) return;
        var relevant = false;
        for (var i = 0; i < mutations.length; i++) {
          var m = mutations[i];
          if (mountedRoot && (m.target === mountedRoot || mountedRoot.contains(m.target))) {
            continue;
          }
          relevant = true;
          break;
        }
        if (!relevant) return;
        if (observerDebounce) win.clearTimeout(observerDebounce);
        observerDebounce = win.setTimeout(function() {
          observerDebounce = null;
          if (!disposed) mount();
        }, 50);
      });
      observer.observe(doc.body || doc.documentElement, { childList: true, subtree: true });
      mount();
      pollTimer = win.setInterval(function() {
        if (disposed) return;
        mount();
        if (mountedRoot) poll();
      }, 1000);
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      started = false;
      if (observerDebounce) { win.clearTimeout(observerDebounce); observerDebounce = null; }
      if (observer) { observer.disconnect(); observer = null; }
      if (pollTimer) { win.clearInterval(pollTimer); pollTimer = null; }
      Object.keys(pendingRequests).forEach(function(k) {
        var p = pendingRequests[k];
        win.clearTimeout(p.timer);
        p.reject(new Error('Page disposed'));
      });
      pendingRequests = Object.create(null);
      unmount();
      adapter = null;
      nativeRefreshed = false;
      warnOnce = false;
    }

    return {
      receive: receive,
      start: start,
      dispose: dispose,
      refresh: poll,
      checkAll: checkAll
    };
  }

  return { createPage: createPage };
});
