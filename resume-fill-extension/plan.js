(() => {
  'use strict';
  const {schema, recordTitle} = typeof module !== 'undefined' && module.exports
    ? require('./profile.js') : globalThis.ResumeProfile;
  const normalize = value => String(value ?? '').normalize('NFKC').toLowerCase().replace(/[\s\p{P}*]+/gu, '');
  const dateStart = ['开始时间', '开始日期', '开始月份', '起始时间', '起始日期', '起始月份', '起始年月', '开始年月', 'start date', 'start month', 'from'];
  const dateEnd = ['结束时间', '结束日期', '结束月份', '截止时间', '结束年月', 'end date', 'end month', 'to'];
  const employment = {company: ['公司', '公司名称', '企业名称', '单位名称', '实习单位', '工作单位', '所在公司', '雇主', 'company', 'employer', 'organization'],
    title: ['职位', '岗位', '职务', '岗位名称', '职位名称', '实习岗位', '工作岗位', 'job title', 'position', 'title'],
    startMonth: [...dateStart, '入职时间', '入职日期'], endMonth: [...dateEnd, '离职时间', '离职日期'],
    description: ['经历描述', '工作内容', '工作职责', '岗位职责', '工作描述', '工作业绩', '实习内容', '实习描述', '实习职责', '描述', 'responsibilities', 'description']};
  const alias = {
    contact: {fullName: ['姓名', '真实姓名', '中文姓名', 'name', 'full name'], phone: ['手机号', '手机号码', '手机', '电话', '电话号码', '联系电话', '联系方式手机', '移动电话', 'mobile', 'mobile number', 'phone', 'phone number'],
      email: ['邮箱', '电子邮箱', '邮件地址', '电子邮件', '电子邮件地址', '联系邮箱', 'email', 'email address'],
      currentCity: ['现居城市', '当前城市', '居住城市', '现居住地', '现居地', 'current city']},
    education: {school: ['学校', '院校', '学校名称', '就读院校', '毕业院校', '毕业学校', 'school', 'university', 'institution'],
      major: ['专业', '所学专业', '专业名称', 'major', 'field of study'], degree: ['学历', '学位', '学历学位', '教育程度', 'degree', 'qualification'],
      startMonth: [...dateStart, '入学时间', '入学日期', '入学月份', '入学年月'], endMonth: [...dateEnd, '毕业时间', '毕业日期', '毕业月份', '毕业年月']},
    work: employment, internships: employment, employment,
    projects: {name: ['项目', '项目名称', '项目名', 'project', 'project name'], role: ['项目角色', '担任角色', '角色', '担任职务', 'role', 'project role'],
      startMonth: [...dateStart, '项目开始时间'], endMonth: [...dateEnd, '项目结束时间'],
      description: ['项目描述', '项目内容', '项目职责', '项目简介', '项目介绍', '描述', 'description', 'project description']},
  };
  const maps = Object.fromEntries(Object.entries(alias).map(([kind, keys]) => [kind,
    new Map(Object.entries(keys).flatMap(([key, labels]) => labels.map(label => [normalize(label), key])))]));
  const allowed = kind => schema[kind === 'employment' ? 'work' : kind] ?? {};
  const typeOK = (type, key) => {
    if (type === 'month') return key.endsWith('Month');
    if (type === 'tel') return key === 'phone';
    if (type === 'email') return key === 'email';
    if (type === 'textarea') return key === 'description';
    if (type === 'select') return !['fullName', 'phone', 'email', 'description'].includes(key);
    return type === 'text';
  };
  function forbidden(field) {
    if (/^(?:one-time-code|current-password|new-password|webauthn|cc-.+)$/.test(field.autocomplete ?? '')) return true;
    return /密码|口令|验证码|证件|身份证|护照|银行卡|薪资|薪酬|期望待遇|隐私|声明|协议|紧急联系人|推荐人|推荐信|证明人|家庭成员|家属|password|passcode|verification|passport|idnumber|nationalid|socialsecurity|salary|consent|privacy|referenceperson|emergencycontact|referee|referral|otp/.test(
      normalize([field.label, ...(field.signals ?? []), ...(field.weak ?? []), field.context].join(' ')));
  }
  function identify(field, kind) {
    if (forbidden(field) || !maps[kind]) return '';
    const auto = kind === 'contact' ? {name: 'fullName', tel: 'phone', email: 'email'}[field.autocomplete] : '';
    const signals = new Set([field.label, ...(field.signals ?? [])].map(s => maps[kind].get(normalize(s))).filter(Boolean));
    if (auto) signals.add(auto);
    // Name/placeholder can contradict stronger evidence, but never authorize autofill by themselves.
    for (const s of field.weak ?? []) {
      const hint = maps[kind].get(normalize(s));
      if (hint && signals.size && !signals.has(hint)) return '';
    }
    return signals.size === 1 ? [...signals][0] : '';
  }
  const identity = {education: 'school', internships: 'company', work: 'company', employment: 'company', projects: 'name'};
  function records(profile, kind) {
    const kinds = kind === 'employment' ? ['work', 'internships'] : [kind];
    return kinds.flatMap(key => (profile[key] ?? []).map(item => ({...item, token: `${key}:${item.id}`, kind: key, label: recordTitle(item)})));
  }
  function valueFor(field, key, raw) {
    if (!typeOK(field.type, key) || !raw) return '';
    if (field.type !== 'select') return raw;
    const options = (field.options ?? []).filter(o => o.value && (normalize(o.label) === normalize(raw) || normalize(o.value) === normalize(raw)));
    return options.length === 1 ? options[0].value : '';
  }
  function matches(field, key, raw) {
    if (field.type === 'date' && key.endsWith('Month')) return field.value.slice(0, 7) === raw;
    const comparable = value => value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase();
    return comparable(field.value) === comparable(valueFor(field, key, raw) || raw);
  }
  function build(blocks, profile, {assignments = {}, overrides = {}} = {}) {
    const groups = [], rows = [], bindings = [];
    const byKind = Map.groupBy(blocks, b => b.kind);
    const mixed = byKind.has('employment') && (byKind.has('work') || byKind.has('internships'));
    const reservedAcrossKinds = new Set();
    for (const [kind, current] of byKind) {
      const list = kind === 'contact' ? [{...profile.contact, token: 'contact', kind: 'contact'}] : records(profile, kind);
      const countOK = list.length === current.length && !(mixed && ['employment', 'work', 'internships'].includes(kind)) && !current.some(b => b.ambiguous);
      const reason = mixed && ['employment', 'work', 'internships'].includes(kind) ? '混合经历与单独工作/实习区块同时出现，请手填。'
        : current.some(b => b.ambiguous) ? '无法确认独立区块，请手填或展开后重扫。'
        : countOK ? '' : `需要 ${list.length} 个，识别到 ${current.length} 个。请调整区块数量后重新识别。`;
      groups.push({kind, expected: list.length, actual: current.length, reason});
      const decisions = current.map((block, index) => {
        const keyed = block.fields.map(f => {
          const auto = identify(f, kind), manual = overrides[f.ref];
          const key = !forbidden(f) && Object.hasOwn(allowed(kind), manual) ? manual : auto;
          return {f, key};
        });
        const filled = keyed.filter(({f}) => f.value);
        // A populated row needs a verifiable identity, or an explicit user assignment.
        const anchors = filled.filter(({key}) => [identity[kind], 'title', 'role', 'major', 'startMonth', 'endMonth'].includes(key) && key);
        const valid = item => anchors.every(({key, f}) => !item[key] || matches(f, key, item[key]));
        const exact = anchors.filter(({key}) => key === identity[kind]);
        const candidates = list.filter(valid);
        const matched = exact.length ? candidates.filter(item => exact.every(({key, f}) => item[key] && matches(f, key, item[key]))) : [];
        const chosen = assignments[block.ref];
        const explicit = chosen ? candidates.find(item => item.token === chosen) : null;
        const prefilled = kind !== 'contact' && filled.length > 0;
        const record = !countOK ? null : chosen ? explicit : prefilled ? (matched.length === 1 ? matched[0] : null) : list[index];
        return {block, index, keyed, record, candidates, priority: chosen || prefilled ? 1 : 0, prefilled};
      });
      const claimed = Map.groupBy(decisions.filter(d => d.record), d => d.record.token);
      for (const d of decisions) {
        const {block, index, keyed} = d;
        const same = d.record ? claimed.get(d.record.token) : [];
        const maxPriority = Math.max(0, ...same.map(e => e.priority));
        const usable = d.record && !reservedAcrossKinds.has(d.record.token) && d.priority === maxPriority && same.filter(e => e.priority === maxPriority).length === 1;
        const record = usable ? d.record : null;
        if (record) reservedAcrossKinds.add(record.token);
        const blockReason = reason || (!record ? d.prefilled ? '已有内容未能唯一核对，请手动选对应条目或保留现状。' : '请为区块选择尚未使用的资料条目。' : '');
        bindings.push({blockRef: block.ref, kind, index, reason: blockReason, token: record?.token ?? '',
          choices: countOK ? d.candidates.map(item => ({token: item.token, label: item.label, kind: item.kind})) : []});
        const byKey = Map.groupBy(keyed.filter(e => e.key), e => e.key);
        for (const {f, key} of keyed) {
          const row = {sectionKind: kind, blockRef: block.ref, recordId: record?.id ?? '', controlRef: f.ref,
            label: f.label || '未命名字段', fieldKey: key, observedValue: f.value, marker: f.marker, status: 'manual'};
          const raw = key && record?.[key];
          const target = raw ? valueFor(f, key, raw) : '';
          row.canCorrect = !forbidden(f) && !f.value && Object.keys(allowed(kind)).some(k => typeOK(f.type, k));
          if (f.value) { row.status = 'kept'; row.reason = '保留页面已有值'; }
          else if (forbidden(f)) row.reason = '由本人填写';
          else if (blockReason) row.reason = blockReason;
          else if (!key) row.reason = '字段含义未确认';
          else if (byKey.get(key).length !== 1) row.reason = '同区块存在重复字段，请手填';
          else if (!raw) row.reason = record.ongoing && key === 'endMonth' ? '经历进行中' : '资料卡无值';
          else if (!target) row.reason = '控件或选项无法可靠匹配';
          else { row.status = 'ready'; row.targetValue = target; row.displayValue = raw; }
          rows.push(row);
        }
      }
    }
    return {groups, rows, bindings};
  }
  const api = {build, identify, identity, normalize, allowed, forbidden, typeOK};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.ResumePlan = api;
})();
