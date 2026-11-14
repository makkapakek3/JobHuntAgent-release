(() => {
  'use strict';
  const P = globalThis.ResumePlan;
  const selector = 'input,textarea,select,[role="combobox"],[role="textbox"],[contenteditable="true"]';
  const controlTags = ['INPUT', 'TEXTAREA', 'SELECT'];
  const text = value => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, 140);
  const groups = {
    contact: /^(?:基本信息|个人信息|个人资料|个人基本信息|联系方式|基本资料|求职者信息|申请人信息|候选人信息|personal(?:information|details)|contact(?:information|details)|basicinformation)$/,
    education: /^(?:教育(?:经历|背景|信息)?|学习经历|学历信息|education(?:history|experience|background|details)?|academichistory)$/,
    work: /^(?:工作(?:经历|经验|履历)?|职业经历|任职经历|work(?:history|experience)|employment(?:history|experience)?)$/,
    internships: /^(?:实习(?:经历|经验|信息)?|internships?|internshipexperience)$/,
    projects: /^(?:项目(?:经历|经验|信息)?|projects?|project(?:experience|history))$/,
    employment: /^(?:工作(?:及|与|或)?实习(?:经历|经验)?|实习(?:及|与|或)?工作(?:经历|经验)?|workandinternshipexperience)$/,
  };
  const noScope = /^(?:紧急联系人|推荐人|证明人|家庭成员|家属|推荐信|第三方|账户登录|用户登录|搜索|emergencycontact|references?|referees?|familymembers|sign(?:in|up)|log(?:in|on)|search)|^(?:登录|注册)$/;
  const fullResume = /^(?:简历(?:编辑|填写|信息)?|(?:编辑|填写|新建|在线)简历|网申(?:表|信息)?|职位申请|岗位申请|求职申请|candidateapplication|(?:job)?application(?:form)?|(?:edit)?(?:resume|cv))$/;
  // Feishu Recruiting uses these component names with different CSS build suffixes.
  const classFamily = name => `.${name},[class^="${name}__"],[class*=" ${name}__"]`;
  const providerSection = ['createFormSection', 'applyFormModuleWrapper'].map(classFamily).join(',');
  const providerTitle = ['createFormSection-text', 'applyFormModuleWrapper-text'].map(classFamily).join(',');
  const providerField = '.atsx-form-item,.ud-formily-item';
  function componentTitle(element) {
    if (!element.matches(providerSection)) return '';
    const title = [...element.querySelectorAll(providerTitle)].find(node => node.closest(providerSection) === element && visible(node));
    return title ? labelText(title) : '';
  }
  function visualTitle(element) {
    const first = element.firstElementChild;
    if (!first?.matches('div,span,p,header') || first.matches(selector) || first.querySelector(`${selector},iframe,form,a`) || !visible(first)) return '';
    const label = labelText(first);
    const key = P.normalize(label);
    return key.length <= 36 && (kindOf(label) || fullResume.test(key) || noScope.test(key)) ? label : '';
  }
  const titleCache = () => {
    const cached = new Map();
    return element => {
      if (!cached.has(element)) {
        const reference = ariaNames(element).join(' ');
        const heading = [...element.children].find(child => child.matches('legend,summary,h1,h2,h3,h4,h5,h6,[role="heading"]'));
        cached.set(element, text(element.getAttribute('aria-label') || reference || componentTitle(element) || (heading && labelText(heading)) || visualTitle(element)));
      }
      return cached.get(element);
    };
  };
  function kindOf(title) {
    const key = P.normalize(title).replace(/(?:第)?[0-9一二三四五六七八九十]+(?:段|项|条)?$/, '');
    return Object.entries(groups).find(([, pattern]) => pattern.test(key))?.[0] ?? '';
  }
  function ariaNames(element) {
    return (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => text(document.getElementById(id)?.textContent)).filter(Boolean);
  }
  function labelText(node) {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('input,select,textarea,button,[role="button"],[role="tooltip"],[hidden],[aria-hidden="true"]').forEach(el => el.remove());
    return text(clone.textContent);
  }
  function visible(el) {
    if (!el.isConnected || el.closest('[hidden],[aria-hidden="true"],[inert]')) return false;
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS' && !parent.open && ![...parent.children].find(c => c.tagName === 'SUMMARY')?.contains(el)) return false;
      if (getComputedStyle(parent).opacity === '0') return false;
    }
    const style = getComputedStyle(el);
    return style.visibility === 'visible' && style.opacity !== '0' && Boolean(el.getClientRects().length);
  }
  function controlType(el) {
    if (el.getAttribute('role') === 'combobox' && el.tagName !== 'SELECT' || el.hasAttribute('list') ||
        el.getAttribute('aria-autocomplete') && el.getAttribute('aria-autocomplete') !== 'none') return 'custom';
    if (el.tagName === 'TEXTAREA') return 'textarea';
    if (el.tagName === 'SELECT') return el.multiple ? 'custom' : 'select';
    if (el.tagName === 'INPUT') return el.type;
    return 'custom';
  }
  function visualLabel(el) {
    for (let node = el, depth = 0; node.parentElement && depth < 3; node = node.parentElement, depth++) {
      const parent = node.parentElement;
      if ([...parent.querySelectorAll(selector)].some(control => control !== el && !el.contains(control))) break;
      const previous = node.previousElementSibling;
      if (!previous) continue;
      if (!previous.matches('label:not([for]),div,span,p') || !visible(previous) ||
          previous.matches(selector) || previous.querySelector(`${selector},button,[role="button"],a,iframe`)) return '';
      const label = labelText(previous);
      return label.length <= 60 ? label : '';
    }
    return '';
  }
  function componentLabel(el) {
    const item = el.closest(providerField);
    if (!item || !item.closest(providerSection) || [...item.querySelectorAll(selector)].some(control => control !== el && !el.contains(control))) return '';
    for (const selector of ['.customResumeForm-fieldName', '.atsx-form-item-label > label', '.ud-formily-item-label-content']) {
      const label = [...item.querySelectorAll(selector)].find(node => node.closest(providerField) === item && visible(node));
      if (label) return labelText(label);
    }
    return '';
  }
  function fieldInfo(el, context) {
    const component = componentLabel(el);
    const direct = component ? [component] : [...(el.labels ?? [])].map(labelText).filter(Boolean);
    const accessible = [text(el.getAttribute('aria-label')), ...ariaNames(el)].filter(Boolean);
    if (!direct.length && !accessible.length) {
      const label = visualLabel(el);
      if (label) direct.push(label);
    }
    const signals = [...new Set([...direct, ...accessible])];
    const weak = [text(el.getAttribute('placeholder')), text(el.getAttribute('name'))].filter(Boolean);
    const label = signals[0] || '';
    const type = controlType(el);
    const autocomplete = (el.getAttribute('autocomplete') || '').trim().toLowerCase().split(/\s+/).at(-1);
    const options = type === 'select' ? [...el.options].filter(o => !o.disabled && !o.hidden && !o.closest('optgroup[disabled]'))
      .map(o => ({value: o.value, label: text(o.label)})) : [];
    const blocked = P.forbidden({label, signals, weak, context, autocomplete});
    const readable = ['text', 'tel', 'email', 'month', 'date', 'textarea', 'select', 'custom'].includes(type);
    const value = !blocked && readable && controlTags.includes(el.tagName) ? el.value : '';
    const marker = JSON.stringify([type, signals, weak, autocomplete]);
    return {label, signals, weak, context, type, autocomplete, options, value, marker,
      domId: text(el.id), maxLength: typeof el.maxLength === 'number' ? el.maxLength : -1, blocked};
  }
  function explicitAutosave(scopes) {
    const enabled = element => ['data-autosave', 'data-auto-save'].some(attr =>
      element.hasAttribute(attr) && !/^(?:false|off|0|no)$/i.test(element.getAttribute(attr)));
    return [...scopes].some(node => enabled(node) || enabled(node.closest('form') ?? node)) ||
      [...document.querySelectorAll('h1,h2,[role="status"],small,p')].slice(0, 120).some(node => visible(node) &&
        /^(?:更改将自动保存|修改后自动保存|输入后自动保存|更改会自动保存|changes (?:are|will be) saved automatically|automatically saves changes)[。.!]?$/i.test(text(node.textContent)));
  }
  function partition(scope, fields, kind, title) {
    if (kind === 'contact') return [{node: scope, fields}];
    const keyOf = entry => P.identify(entry.data, kind);
    const idKey = P.identity[kind];
    const identities = fields.filter(f => keyOf(f) === idKey);
    const cleanShape = entries => {
      const keys = entries.map(keyOf).filter(Boolean);
      return keys.length >= 2 && keys.includes(idKey) && new Set(keys).size === keys.length;
    };
    const rowsIn = parent => [...parent.children].map(node => ({node, fields: fields.filter(f => node.contains(f.node))}))
      .filter(row => row.fields.length);
    function multiplePotentialRows(parent) {
      const rows = rowsIn(parent);
      const semanticRow = row => !row.node.matches(selector) &&
        (row.node.matches('fieldset,section,article,details,[role="group"]') || title(row.node));
      if (rows.filter(semanticRow).length >= 2) return true;
      const plausible = rows.filter(row => row.fields.length >= 2);
      if (plausible.some((row, i) => plausible.slice(i + 1).some(other => row.node.tagName === other.node.tagName &&
          row.fields.length === other.fields.length && row.fields.every((f, j) => f.node.tagName === other.fields[j].node.tagName)))) return true;
      return rows.some(row => row.fields.length >= 2 && multiplePotentialRows(row.node));
    }
    if (identities.length === 1 && cleanShape(fields) && !multiplePotentialRows(scope)) return [{node: scope, fields}];
    if (identities.length >= 2) {
      // Find the deepest common parent of the identities, then check its actual sibling rows.
      let shared = identities[0].node.parentElement;
      while (shared !== scope && !identities.every(f => shared.contains(f.node))) shared = shared.parentElement;
      const rows = rowsIn(shared);
      const keyShape = row => row.fields.map(keyOf).filter(Boolean).sort().join(',');
      if (rows.length === identities.length && rows.reduce((sum, row) => sum + row.fields.length, 0) === fields.length &&
          rows.every(row => cleanShape(row.fields) && !multiplePotentialRows(row.node) && row.node.tagName === rows[0].node.tagName && keyShape(row) === keyShape(rows[0]))) return rows;
    }
    return [{node: scope, fields, ambiguous: true}];
  }
  function collect(trace) {
    const title = titleCache(), typed = new Map(), rejectedForms = new Map();
    const nodes = [...document.querySelectorAll(selector)];
    const tooLarge = nodes.length > 800;
    const count = reason => { if (trace) trace[reason] = (trace[reason] ?? 0) + 1; };
    if (trace) trace.controls = nodes.length;
    const topHeading = [...document.querySelectorAll('h1')].find(visible);
    const resumePage = fullResume.test(P.normalize(topHeading?.textContent));
    for (const el of tooLarge ? [] : nodes) {
      if (!visible(el) || el.matches(':disabled,[readonly],[aria-disabled="true"],[aria-readonly="true"],input[type="hidden"],input[type="submit"],input[type="button"],input[type="reset"],input[type="image"]')) { count('hiddenOrReadOnly'); continue; }
      if (el.parentElement?.closest('[role="combobox"]')) { count('insideCombo'); continue; }
      const form = el.form || el.closest('form');
      if (form && !rejectedForms.has(form)) {
        const editor = form.closest('.resumeEditForm-wrapper') || form.querySelector('.resumeEditForm-wrapper');
        const search = [...form.querySelectorAll('input[type="search"]')].some(input =>
          !(editor && input.closest(providerSection) && input.closest('[role="combobox"]')));
        rejectedForms.set(form, Boolean(search || form.querySelector('input[type="password"]') ||
          /(?:^|\/)(?:login|signin|search)(?:[/?#]|$)/i.test(form.getAttribute('action') ?? '')));
      }
      if (rejectedForms.get(form)) { count('blockedForm'); continue; }
      let scope = null, kind = '', context = '', unsafe = false;
      for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
        const label = title(p);
        if (label) context ||= label;
        if (noScope.test(P.normalize(label))) { unsafe = true; break; }
        const candidate = kindOf(label);
        if (candidate && !scope) { scope = p; kind = candidate; }
      }
      if (unsafe) { count('excludedSection'); continue; }
      let data = fieldInfo(el, context);
      if (!scope && form?.contains(el) && (resumePage || fullResume.test(P.normalize(title(form)))) &&
          (!context || fullResume.test(P.normalize(context))) && P.identify(data, 'contact')) {
        // Bare contact controls are only eligible inside a clearly named resume form.
        scope = form;
        kind = 'contact';
      }
      if (!scope) { count('unscoped'); continue; }
      if (data.blocked) data = {...data, value: ''};
      if (!typed.has(scope)) typed.set(scope, {kind, fields: []});
      typed.get(scope).fields.push({node: el, data});
    }
    const blocks = [];
    for (const [scope, group] of typed) {
      const nested = [...typed].some(([other, child]) => other !== scope && scope.contains(other) && child.kind === group.kind);
      // Controls outside titled sibling rows must never be fused into one of those rows.
      const rows = nested && group.kind !== 'contact' ? [{node: scope, fields: group.fields, ambiguous: true}]
        : partition(scope, group.fields, group.kind, title);
      blocks.push(...rows.map(row => ({...row, kind: group.kind, scopeTitle: title(scope), rowTitle: title(row.node)})));
    }
    blocks.sort((a, b) => a.fields[0].node === b.fields[0].node ? 0
      : a.fields[0].node.compareDocumentPosition(b.fields[0].node) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    const autosave = explicitAutosave(typed.keys());
    const signature = JSON.stringify(blocks.map(block => [block.kind, Boolean(block.ambiguous), block.node.tagName,
      block.fields.map(f => [f.data.marker, f.data.blocked, f.data.maxLength, f.data.options]) ]));
    return {blocks, signature, autosave, tooLarge};
  }
  let snapshot = null;
  function layoutReport(main) {
    const frames = [...document.querySelectorAll('iframe')].filter(visible);
    const accessible = frames.map(frame => frame.contentDocument).filter(Boolean);
    const titles = [...document.querySelectorAll(providerTitle)].filter(visible);
    return {main, frames: {visible: frames.length, accessible: accessible.length,
      withControls: accessible.filter(doc => doc.querySelector(selector)).length},
      components: {sectionCount: document.querySelectorAll(providerSection).length,
        titleKinds: titles.map(title => kindOf(labelText(title)) || 'unknown').slice(0, 20),
        fieldCount: document.querySelectorAll(providerField).length}};
  }
  function scan() {
    const trace = {};
    const data = collect(trace);
    let index = 0;
    data.blocks.forEach((block, i) => {
      block.ref = `b${i}`;
      block.fields.forEach(field => { field.data.ref = `f${index++}`; });
    });
    snapshot = {token: crypto.randomUUID(), href: location.href, data};
    const {signature, autosave, tooLarge} = data;
    return {token: snapshot.token, origin: location.origin, signature, autosave, tooLarge,
      diagnostic: data.blocks.length ? undefined : layoutReport(trace),
      blocks: data.blocks.map(({ref, kind, ambiguous, fields}) => ({ref, kind, ambiguous, fields: fields.map(f => f.data)}))};
  }
  function unchanged(start, touched) {
    if (location.href !== start.href) return false;
    const now = collect(), before = start.data;
    return !now.autosave && now.signature === before.signature && before.blocks.every((block, i) => {
      const match = now.blocks[i];
      return match?.node === block.node && match.scopeTitle === block.scopeTitle && match.rowTitle === block.rowTitle && block.fields.every((entry, j) => {
        const current = match.fields[j];
        return current?.node === entry.node && current.data.context === entry.data.context &&
          (touched.has(entry.node) || current.data.value === entry.data.value);
      });
    });
  }
  async function fill(token, selected) {
    const start = snapshot;
    snapshot = null;
    if (!start || start.token !== token || start.data.autosave) return {stale: true, results: []};
    const touched = new Set();
    if (!unchanged(start, touched)) return {stale: true, results: []};
    const requested = new Map(selected.map(item => [item.controlRef, item]));
    const results = [], attempts = [];
    const pause = () => new Promise(resolve => setTimeout(resolve, 0));
    let interrupted = false;
    for (const block of start.data.blocks) {
      for (const {node, data} of block.fields) {
        const item = requested.get(data.ref);
        if (!item) continue;
        const result = {ref: data.ref, status: 'unprocessed'};
        results.push(result);
        if (interrupted || !unchanged(start, touched)) { interrupted = true; continue; }
        if (block.ref !== item.blockRef || block.kind !== item.sectionKind || block.ambiguous || data.value || node.value ||
            item.observedValue !== '' || data.blocked || !Object.hasOwn(P.allowed(block.kind), item.fieldKey) ||
            !P.typeOK(data.type, item.fieldKey) || typeof item.targetValue !== 'string' || !item.targetValue) continue;
        if (data.maxLength >= 0 && item.targetValue.length > data.maxLength) { result.status = 'failed'; continue; }
        if (data.type === 'select' && !data.options.some(o => o.value === item.targetValue)) { result.status = 'failed'; continue; }
        const prototype = {INPUT: HTMLInputElement, TEXTAREA: HTMLTextAreaElement, SELECT: HTMLSelectElement}[node.tagName].prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, item.targetValue);
        touched.add(node);
        node.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
        if (node.isConnected) node.dispatchEvent(new Event('change', {bubbles: true}));
        await pause();
        attempts.push({node, item, result});
        result.status = node.isConnected && node.value === item.targetValue && node.validity.valid ? 'filled' : 'failed';
      }
      for (const {node, item, result} of attempts) {
        if (!node.isConnected || node.value !== item.targetValue || !node.validity.valid) result.status = 'failed';
      }
    }
    return {interrupted, results};
  }
  globalThis.ResumePage = {scan, fill};
})();
