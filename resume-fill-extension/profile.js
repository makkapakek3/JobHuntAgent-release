(() => {
  'use strict';
  const schema = {
    contact: {fullName: '姓名', phone: '手机号码', email: '邮箱', currentCity: '现居城市'},
    education: {school: '学校', major: '专业', degree: '学历或学位', startMonth: '入学月份', endMonth: '毕业月份'},
    work: {company: '公司', title: '职位', startMonth: '开始月份', endMonth: '结束月份', description: '工作描述'},
    internships: {company: '公司', title: '职位', startMonth: '开始月份', endMonth: '结束月份', description: '实习描述'},
    projects: {name: '项目名称', role: '项目角色', startMonth: '开始月份', endMonth: '结束月份', description: '项目描述'},
  };
  const kinds = {contact: '基本信息', education: '教育经历', work: '工作经历', internships: '实习经历',
    projects: '项目经历', employment: '工作/实习经历'};
  const storage = {profile: 'resumeProfileV1', mappings: 'resumeMappingsV1'};
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  function cleanFields(source, spec) {
    const result = {};
    for (const [key, label] of Object.entries(spec)) {
      const value = source[key];
      if (value === undefined || value === null || value === '') continue;
      if (typeof value !== 'string' || value.length > 16000) throw Error(`${label}必须是有效文本。`);
      const trimmed = value.trim();
      if (!trimmed) continue;
      if (key.endsWith('Month') && !/^\d{4}-(0[1-9]|1[0-2])$/.test(trimmed)) throw Error(`${label}必须使用 YYYY-MM。`);
      result[key] = trimmed;
    }
    return result;
  }
  function parse(input) {
    let data;
    try { data = JSON.parse(input.replace(/^\uFEFF/, '')); }
    catch { throw Error('无法读取 JSON，请选择有效的资料卡。'); }
    if (!object(data) || data.schemaVersion !== 1) throw Error('资料卡版本必须为 1。');
    if (data.contact !== undefined && !object(data.contact)) throw Error('基本信息格式不正确。');
    const result = {schemaVersion: 1, contact: cleanFields(data.contact ?? {}, schema.contact)};
    for (const kind of Object.keys(schema).filter(key => key !== 'contact')) {
      const list = data[kind] === undefined ? [] : data[kind];
      if (!Array.isArray(list) || list.length > 100) throw Error(`${kinds[kind]}必须是长度不超过 100 的数组。`);
      const ids = new Set();
      result[kind] = list.map(record => {
        if (!object(record) || typeof record.id !== 'string' || !/^[\w-]{1,64}$/.test(record.id)) throw Error(`${kinds[kind]}的条目缺少有效 ID。`);
        if (ids.has(record.id)) throw Error(`${kinds[kind]}有重复条目 ID。`);
        ids.add(record.id);
        if (record.ongoing !== undefined && typeof record.ongoing !== 'boolean') throw Error('ongoing 必须为 true 或 false。');
        const values = cleanFields(record, schema[kind]);
        if (record.ongoing && values.endMonth) throw Error('进行中的经历不能同时填写结束月份。');
        return {id: record.id, ...values, ...(record.ongoing ? {ongoing: true} : {})};
      });
    }
    return result;
  }
  function recordTitle(record) {
    return [record.school || record.company || record.name, record.major || record.title || record.role]
      .filter(Boolean).join(' · ') || '未命名经历';
  }
  const api = {schema, kinds, storage, parse, recordTitle};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.ResumeProfile = api;
})();
