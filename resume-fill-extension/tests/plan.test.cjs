const test = require('node:test');
const assert = require('node:assert/strict');
const profile = require('../profile.js');
const plan = require('../plan.js');

const card = {schemaVersion: 1, contact: {fullName: '示例同学', phone: '00000000000'},
  education: [{id: 'e', school: '示例大学', startMonth: '2022-09'}],
  internships: ['甲', '乙', '丙', '丁'].map((company, i) => ({id: `r${i}`, company,
    title: '实习生', startMonth: `2025-0${i + 1}`, description: `仅属于${company}的描述`}))};

function field(ref, label, value = '', type = 'text') {
  return {ref, label, value, type, marker: label};
}
function block(ref, kind, fields) { return {ref, kind, fields}; }
const contact = block('basic', 'contact', [field('person', '姓名')]);
const education = block('edu', 'education', [field('school', '学校'), field('estart', '开始时间')]);
function internships(n = 4, prefill = {}) {
  return Array.from({length: n}, (_, i) => block(`b${i}`, 'internships', [
    field(`c${i}`, '公司名称', prefill[i]), field(`s${i}`, '开始时间'),
    field(`d${i}`, '实习描述', '', 'textarea'),
  ]));
}
const get = (draft, ref) => draft.rows.find(row => row.controlRef === ref);

test('资料卡仅保留允许的值，拒绝错误版本、月份和重复 ID', () => {
  const clean = profile.parse(JSON.stringify({...card, contact: {...card.contact, idNumber: 'ignored'}}));
  assert.equal(clean.contact.idNumber, undefined);
  assert.equal(clean.internships.length, 4);
  assert.throws(() => profile.parse('{'));
  assert.throws(() => profile.parse(JSON.stringify({...card, schemaVersion: 2})), /版本/);
  assert.throws(() => profile.parse(JSON.stringify({...card, internships: [card.internships[0], card.internships[0]]})), /重复/);
  assert.throws(() => profile.parse(JSON.stringify({...card, work: [{id: 'x', startMonth: '2024-99'}]})), /YYYY-MM/);
});

test('四段描述分别绑定；同名日期按区块匹配', () => {
  const draft = plan.build([education, ...internships()], card);
  assert.equal(get(draft, 'estart').targetValue, '2022-09');
  assert.equal(get(draft, 's3').targetValue, '2025-04');
  assert.deepEqual(draft.rows.filter(r => r.fieldKey === 'description').map(r => [r.blockRef, r.targetValue]),
    card.internships.map((r, i) => [`b${i}`, r.description]));
});

test('可交换条目，重复手动分配不产生跨区块写入', () => {
  const swapped = plan.build(internships(), card, {assignments: {b1: 'internships:r3', b3: 'internships:r1'}});
  assert.equal(get(swapped, 'c1').targetValue, '丁');
  assert.equal(get(swapped, 'c3').targetValue, '乙');
  const repeated = plan.build(internships(), card, {assignments: {b1: 'internships:r3', b3: 'internships:r3'}});
  assert.equal(get(repeated, 'd1').status, 'manual');
  assert.equal(get(repeated, 'd3').status, 'manual');
});

test('三个或五个区块均暂停该类，不拦截独立基本信息', () => {
  for (const n of [3, 5]) {
    const draft = plan.build([contact, ...internships(n)], card);
    assert.deepEqual(draft.rows.filter(r => r.status === 'ready').map(r => r.controlRef), ['person']);
    assert.equal(draft.groups.find(g => g.kind === 'internships').actual, n);
  }
});

test('预填唯一匹配优先；冲突区块不补，已有值不覆盖', () => {
  const draft = plan.build(internships(4, {0: '丁', 2: '资料外公司'}), card);
  assert.equal(get(draft, 'c0').status, 'kept');
  assert.equal(get(draft, 'd0').targetValue, '仅属于丁的描述');
  assert.equal(get(draft, 'd2').status, 'manual');
  assert.equal(get(draft, 'd3').status, 'manual');
  assert.equal(get(draft, 'd1').targetValue, '仅属于乙的描述');
});

test('纠正限于区块字段；敏感字段、完整日期和歧义下拉不会写入', () => {
  const basic = block('basic', 'contact', [field('custom', '如何联系'), field('secret', '身份证号码'),
    {...field('otp', 'Code'), autocomplete: 'one-time-code'},
    {...field('city', '现居城市', '', 'select'), options: [{value: 'a', label: '示例城'}, {value: 'b', label: '示例城'}]}]);
  const edu = block('edu', 'education', [field('date', '入学时间', '', 'date')]);
  const draft = plan.build([basic, edu], {...card, contact: {...card.contact, currentCity: '示例城'}},
    {overrides: {custom: 'phone', secret: 'phone', otp: 'phone'}});
  assert.equal(get(draft, 'custom').targetValue, '00000000000');
  for (const ref of ['secret', 'otp', 'city', 'date']) assert.notEqual(get(draft, ref).status, 'ready');
});

test('完整日期只用于比对月份，冲突时暂停该段', () => {
  const edu = (date) => block('edu', 'education', [field('school', '学校', '示例大学'),
    field('date', '开始时间', date, 'date'), field('major', '专业')]);
  const source = {...card, education: [{...card.education[0], major: '电子工程'}]};
  assert.equal(get(plan.build([edu('2022-09-17')], source), 'major').targetValue, '电子工程');
  assert.equal(get(plan.build([edu('2022-10-17')], source), 'major').status, 'manual');
});
