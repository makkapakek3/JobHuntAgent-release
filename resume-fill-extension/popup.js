(() => {
  'use strict';
  const Profile = globalThis.ResumeProfile, Plan = globalThis.ResumePlan;
  const $ = id => document.getElementById(id);
  const local = chrome.storage.local, keys = Profile.storage;
  const preview = $('preview'), notice = $('notice'), status = $('status'), results = $('results');
  let state = null, busy = false;
  const add = (parent, tag, text, className = '') => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    parent.append(node);
    return node;
  };
  const option = (select, text, value) => { add(select, 'option', text).value = value; };
  const button = (parent, label, className, handler) => {
    const node = add(parent, 'button', label, className);
    node.type = 'button';
    node.onclick = handler;
    return node;
  };
  const names = {ready: '拟填', manual: '需要手填', kept: '已有值保留', failed: '写入未通过校验', filled: '已填', unprocessed: '未处理'};
  const labels = kind => Profile.schema[kind === 'employment' ? 'work' : kind];
  function mapKey(field, block) { return JSON.stringify([block.kind, field.marker]); }
  function digest(value) {
    return crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)).then(buffer =>
      Array.from(new Uint8Array(buffer), n => n.toString(16).padStart(2, '0')).join(''));
  }
  function controlLabel(row) { return row.label === '未命名字段' && row.fieldKey ? labels(row.sectionKind)[row.fieldKey] : row.label; }
  function setBusy(value) {
    busy = value;
    $('scan').disabled = value;
    refreshFillButton();
  }
  function refreshFillButton() {
    $('fill').disabled = busy || !state || state.used || state.scan.autosave ||
      !state.plan?.rows.some(r => r.status === 'ready' && !state.excluded.has(r.controlRef));
  }
  function setRecord(binding, token) {
    const {assignments, plan} = state;
    const other = plan.bindings.find(b => b.blockRef !== binding.blockRef && b.kind === binding.kind && b.token === token);
    if (other && binding.token && other.choices.some(c => c.token === binding.token)) assignments[other.blockRef] = binding.token;
    assignments[binding.blockRef] = token || 'none';
    render();
  }
  async function remember(field, key) {
    if (!state || !key || state.used) return;
    const hash = state.fieldHashes.get(field.ref);
    if (!hash) return;
    try {
      const saved = (await local.get(keys.mappings))[keys.mappings] ?? {};
      const all = Object.entries(saved);
      if (all.length >= 800 && !Object.hasOwn(saved, hash)) delete saved[all[0][0]];
      saved[hash] = key;
      await local.set({[keys.mappings]: saved});
      notice.textContent = '已记住这个字段的对应关系。';
    } catch { notice.textContent = '无法记住映射，本次选择仍有效。'; }
  }
  function showField(parent, row, field) {
    const item = add(parent, 'div', '', 'field');
    item.dataset.markerId = field.domId;
    const title = add(item, 'div', '', 'field-title');
    if (row.status === 'ready' && !state.scan.autosave) {
      const toggle = document.createElement('input');
      toggle.type = 'checkbox';
      toggle.checked = !state.excluded.has(row.controlRef);
      toggle.setAttribute('aria-label', `选择填写 ${controlLabel(row)}`);
      toggle.onchange = () => {
        if (toggle.checked) state.excluded.delete(row.controlRef);
        else state.excluded.add(row.controlRef);
        refreshFillButton();
      };
      title.append(toggle);
    }
    add(title, 'strong', controlLabel(row));
    add(title, 'span', names[row.status], `badge ${row.status}`);
    if (row.status === 'ready') {
      add(item, 'div', row.displayValue, 'value');
      if (state.scan.autosave) button(item, '复制', 'secondary small', async () => {
        try { await navigator.clipboard.writeText(row.displayValue); notice.textContent = '已复制。'; }
        catch { notice.textContent = '无法复制，请手动选择文本。'; }
      });
    } else add(item, 'div', row.reason, 'muted');
    if (!row.canCorrect || field.blocked) return;
    const actions = add(item, 'div', '', 'correction-row');
    const correct = add(actions, 'select', '', 'correction');
    correct.setAttribute('aria-label', `纠正字段 ${controlLabel(row)}`);
    option(correct, '选择资料字段', '');
    for (const [key, label] of Object.entries(labels(row.sectionKind))) if (Plan.typeOK(field.type, key)) option(correct, label, key);
    correct.value = row.fieldKey;
    correct.onchange = () => {
      if (correct.value) state.overrides[row.controlRef] = correct.value;
      else delete state.overrides[row.controlRef];
      render();
    };
    if (state.fieldHashes.has(row.controlRef) && state.overrides[row.controlRef]) {
      button(actions, '记住映射', 'secondary small remember', () => remember(field, state.overrides[row.controlRef]));
    }
  }
  function render() {
    preview.replaceChildren();
    if (!state) return;
    state.plan = Plan.build(state.scan.blocks, state.profile, state);
    if (state.scan.autosave) add(preview, 'p', '检测到网页会自动保存。这里只提供预览和复制。', 'warning');
    else add(preview, 'p', '请核对每段经历再填写。网站可能自行保存输入；扩展不会点击保存或提交。', 'warning');
    for (const group of state.plan.groups) {
      const area = add(preview, 'section', '');
      area.dataset.kind = group.kind;
      add(area, 'h2', `${Profile.kinds[group.kind]} · ${group.actual} 个区块 / ${group.expected} 条资料`);
      if (group.reason) add(area, 'p', group.reason, 'warning');
      for (const binding of state.plan.bindings.filter(b => b.kind === group.kind)) {
        const container = add(area, 'div', '', 'block');
        const block = state.scan.blocks.find(b => b.ref === binding.blockRef);
        if (group.kind !== 'contact') {
          add(container, 'h3', `区块 ${binding.index + 1}`);
          if (binding.choices.length) {
            const select = add(container, 'select', '', 'assignment');
            select.setAttribute('aria-label', `区块 ${binding.index + 1} 对应的经历`);
            option(select, '手动选择 / 跳过', '');
            for (const choice of binding.choices) option(select, `${Profile.kinds[choice.kind]} · ${choice.label}`, choice.token);
            select.value = binding.token;
            select.onchange = () => setRecord(binding, select.value);
          }
          if (binding.reason && !group.reason) add(container, 'p', binding.reason, 'muted');
        }
        for (const row of state.plan.rows.filter(r => r.blockRef === binding.blockRef)) {
          showField(container, row, block.fields.find(f => f.ref === row.controlRef));
        }
      }
    }
    refreshFillButton();
  }
  $('options').onclick = () => chrome.runtime.openOptionsPage();
  function showDiagnostic(report) {
    $('diagnostic').hidden = !report;
    const field = $('diagnostic-text');
    field.hidden = true;
    field.value = report ? JSON.stringify({version: chrome.runtime.getManifest().version, ...report}, null, 2) : '';
    $('diagnose').onclick = async () => {
      field.hidden = false;
      try {
        await navigator.clipboard.writeText(field.value);
        notice.textContent = '排查信息已复制，可粘贴到对话中。';
      } catch {
        field.select();
        notice.textContent = '请手动复制下方排查信息。';
      }
    };
  }
  $('scan').onclick = async () => {
    if (busy) return;
    state = null;
    preview.replaceChildren();
    results.replaceChildren();
    showDiagnostic(null);
    notice.textContent = '';
    status.textContent = '正在识别当前页面…';
    setBusy(true);
    try {
      const stored = await local.get([keys.profile, keys.mappings]);
      const profile = stored[keys.profile];
      if (!profile) { status.textContent = '请先在「资料卡设置」导入资料。'; return; }
      const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
      if (tab?.id === undefined) throw Error();
      await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['profile.js', 'plan.js', 'page.js']});
      const [injection] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => globalThis.ResumePage.scan()});
      const scan = injection.result;
      if (!scan?.blocks.length) {
        status.textContent = scan?.tooLarge ? '页面控件过多。请切到单独的简历编辑页。' :
          '没有识别到可填写的简历区块。请确认已进入编辑状态并展开表单；如果已能看到输入框，这种布局可能暂不支持。';
        if (scan?.diagnostic) showDiagnostic(scan.diagnostic);
        return;
      }
      const fieldHashes = new Map();
      const overrides = {};
      const scopeHash = await digest(JSON.stringify([scan.origin, scan.signature]));
      for (const block of scan.blocks) {
        const markers = Map.groupBy(block.fields, f => f.marker);
        for (const field of block.fields) {
          if (!field.label || field.blocked || markers.get(field.marker).length !== 1) continue;
          const hash = await digest(JSON.stringify([scopeHash, mapKey(field, block)]));
          fieldHashes.set(field.ref, hash);
          if (stored[keys.mappings]?.[hash]) overrides[field.ref] = stored[keys.mappings][hash];
        }
      }
      state = {scan, tabId: tab.id, documentId: injection.documentId, profile, assignments: {}, overrides, fieldHashes, excluded: new Set()};
      render();
      status.textContent = `识别完成 · ${scan.blocks.length} 个区块`;
    } catch {
      status.textContent = '无法读取当前标签页。请打开招聘表单后从工具栏重新点开扩展。';
    } finally { setBusy(false); }
  };
  $('fill').onclick = async () => {
    if (!state || busy || state.used || state.scan.autosave) return;
    const selected = state.plan.rows.filter(row => row.status === 'ready' && !state.excluded.has(row.controlRef));
    if (!selected.length) return;
    state.used = true;
    setBusy(true);
    preview.querySelectorAll('input,select,button').forEach(node => { node.disabled = true; });
    notice.textContent = '';
    status.textContent = '正在填写并核对…';
    try {
      const target = {tabId: state.tabId, documentIds: [state.documentId]};
      const writes = selected.map(({blockRef, sectionKind, controlRef, fieldKey, observedValue, targetValue}) =>
        ({blockRef, sectionKind, controlRef, fieldKey, observedValue, targetValue}));
      const [answer] = await chrome.scripting.executeScript({target, args: [state.scan.token, writes],
        func: (token, data) => globalThis.ResumePage.fill(token, data)});
      const report = answer.result;
      if (report.stale) { add(results, 'p', '网页或已有值已变化。本次没有写入，请重新识别。', 'warning'); return; }
      if (report.interrupted) add(results, 'p', '页面在填写过程中发生变化，已停止后续填写。', 'warning');
      const actual = new Map(report.results.map(item => [item.ref, item.status]));
      const items = state.plan.rows.map(row => ({...row, outcome: actual.get(row.controlRef) ?? (row.status === 'ready' ? 'unprocessed' : row.status)}));
      const counts = Map.groupBy(items, item => item.outcome);
      add(results, 'p', [...counts].map(([key, rows]) => `${names[key]} ${rows.length} 项`).join(' · '));
      const details = add(results, 'details', '');
      details.open = true;
      add(details, 'summary', '查看逐项结果');
      for (const row of items) {
        const binding = state.plan.bindings.find(b => b.blockRef === row.blockRef);
        add(details, 'p', `${Profile.kinds[row.sectionKind]}${row.sectionKind === 'contact' ? '' : ` ${binding.index + 1}`} / ${controlLabel(row)}：${names[row.outcome]}`);
      }
    } catch {
      add(results, 'p', '网页已离开或无法继续核对。请检查页面上实际保留的内容，再重新识别。', 'warning');
    } finally {
      status.textContent = '填写结束 · 请在网页核对。';
      setBusy(false);
    }
  };
})();
