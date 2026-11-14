(() => {
  'use strict';
  const P = globalThis.ResumeProfile;
  const $ = id => document.getElementById(id);
  const file = $('profile-file'), save = $('save'), status = $('status');
  const local = chrome.storage.local;
  const keys = P.storage;
  let pending = null, version = 0;
  function add(parent, tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    parent.append(node);
    return node;
  }
  function preview(parent, profile, empty = '') {
    parent.replaceChildren();
    if (!profile) { parent.textContent = empty; return; }
    const base = add(parent, 'details', '');
    add(base, 'summary', `基本信息：${Object.keys(profile.contact).length} 项`);
    for (const [key, value] of Object.entries(profile.contact)) add(base, 'p', `${P.schema.contact[key]}：${value}`);
    for (const kind of ['education', 'work', 'internships', 'projects']) {
      const list = profile[kind];
      const group = add(parent, 'details', '');
      add(group, 'summary', `${P.kinds[kind]}：${list.length} 段`);
      for (const record of list) {
        add(group, 'h3', P.recordTitle(record));
        for (const [key, label] of Object.entries(P.schema[kind])) {
          if (record[key]) add(group, 'p', `${label}：${record[key]}`);
        }
        if (record.ongoing) add(group, 'p', '进行中');
      }
    }
  }
  async function refresh() {
    const state = await local.get(keys.profile);
    preview($('stored'), state[keys.profile], '尚未保存资料卡');
  }
  file.addEventListener('change', async () => {
    const request = ++version;
    pending = null;
    save.disabled = true;
    status.textContent = '';
    preview($('candidate'), null);
    try {
      const selected = file.files[0];
      if (!selected) return;
      if (selected.size > 2_000_000) throw Error('资料卡文件超过 2 MB。');
      const parsed = P.parse(await selected.text());
      if (request !== version) return;
      pending = parsed;
      preview($('candidate'), parsed);
      save.disabled = false;
    } catch (error) { if (request === version) status.textContent = error.message; }
  });
  save.addEventListener('click', async () => {
    if (!pending) return;
    save.disabled = file.disabled = true;
    try {
      await local.set({[keys.profile]: pending});
      pending = null;
      file.value = '';
      preview($('candidate'), null);
      await refresh();
      status.textContent = '资料卡已保存在本机。';
    } catch { status.textContent = '保存失败，请重试。'; }
    finally { file.disabled = false; save.disabled = !pending; }
  });
  $('clear').onclick = () => { $('clear-panel').hidden = false; };
  $('cancel-clear').onclick = () => { $('clear-panel').hidden = true; };
  $('confirm-clear').onclick = async () => {
    try {
      await local.remove([keys.profile, keys.mappings]);
      version++;
      pending = null;
      save.disabled = true;
      file.value = '';
      preview($('candidate'), null);
      $('clear-panel').hidden = true;
      await refresh();
      status.textContent = '已清除。';
    } catch { status.textContent = '清除失败，请重试。'; }
  };
  refresh().catch(() => { $('stored').textContent = '无法读取本机资料。'; });
})();
