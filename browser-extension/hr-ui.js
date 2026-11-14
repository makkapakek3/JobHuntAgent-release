(() => {
  const ui = (name) => document.getElementById('hr-' + name);
  const errors = {
    HR_REPLY_AGENT_UNAVAILABLE: '无法连接本机 Agent，请检查 PowerShell 和端口。',
    HR_REPLY_PAIRING_REQUIRED: '请先连接本机 Agent，重启后需要重新配对。',
    HR_REPLY_BINDING_MISMATCH: '当前会话已关联其他岗位。',
    HR_REPLY_NOT_FOUND: '草稿还没有保存，可以稍后刷新。',
    HR_REPLY_INTERRUPTED: '上次生成被中断，可以手动重试。',
    DAILY_TOKEN_BUDGET: 'Agent 的今日额度不足。'
  };
  let info;
  let shown;
  let shownResult;
  let viewVersion = 0;
  let originalDraft = '';
  const tell = (message) => { ui('status').textContent = errors[message] || message; };

  async function agent(action, data) {
    const answer = await chrome.runtime.sendMessage({ type: 'hr', action, data });
    if (answer.error) throw new Error(answer.error);
    return answer.result;
  }

  function fillJobs(result) {
    info = result;
    ui('port').value = result.port;
    const selected = ui('job').value;
    ui('job').replaceChildren(new Option('请选择岗位', ''), ...result.jobs.map(job =>
      new Option(`${job.company} · ${job.title}`, job.job_id)));
    if (result.jobs.some(job => job.job_id === selected)) ui('job').value = selected;
    ui('demo-controls').hidden = result.mode !== 'DEMO';
    ui('submit').textContent = result.mode === 'LIVE' ? '发送给 DeepSeek 生成草稿' : '生成模拟草稿';
    tell(result.mode === 'LIVE' ? '已连接。可粘贴消息生成回复草稿。' : '已连接模拟 Agent。');
  }

  async function chat(action, args = []) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (info?.mode !== 'DEMO' || !tab?.id || tab.url !== `http://127.0.0.1:${info.port}/demo/chat`) return {};
    const target = { tabId: tab.id };
    await chrome.scripting.executeScript({ target, files: ['hr-chat.js'] });
    const [{ result }] = await chrome.scripting.executeScript({
      target, func: (method, args) => {
        const watcher = globalThis.jobHuntDemoChat;
        watcher[method](...args);
        return watcher.current();
      }, args: [action, args]
    });
    return { ...result, tabId: tab.id };
  }

  function show(data, result) {
    shown = data;
    shownResult = result;
    ui('original').textContent = data?.message_text || '';
    originalDraft = result?.draft || '';
    ui('draft').value = originalDraft;
    ui('retry').hidden = result?.status !== 'failed';
    if (result?.status === 'ready') tell('草稿已生成，可以编辑和复制。');
    if (result?.status === 'needs_review') tell('需要核对。' + (result.review_reasons || []).join(' '));
    if (result?.status === 'processing') tell('正在生成，可稍后刷新。');
    if (result?.status === 'failed') tell(result.error_code || '生成失败。');
  }

  async function refresh(automatic = false) {
    const version = ++viewVersion;
    const state = await chat('current');
    const job = ui('job').value;
    const key = state.conversation ? `hrLatest:${state.tabId}:${state.conversation}` : `hrManualDraft:${job}`;
    const { [key]: saved } = await chrome.storage.session.get(key);
    if (version !== viewVersion) return;
    if (!saved || (job && saved.job_id !== job)) {
      show(null, null);
      if (state.conversation) tell(state.reason || (state.active ? '正在监听模拟会话。' : '请选择岗位并启用监听。'));
      return;
    }
    let result;
    try {
      result = await agent('get', saved);
    } catch (error) {
      if (!['HR_REPLY_NOT_FOUND', 'HR_REPLY_AGENT_UNAVAILABLE'].includes(error.message)) throw error;
      result = { status: 'failed', error_code: error.message };
    }
    const current = await chat('current');
    if (version !== viewVersion || current.conversation !== state.conversation) return;
    if (automatic && shown?.conversation_id === saved.conversation_id && ui('draft').value !== originalDraft) {
      tell('保留了你正在编辑的草稿。点「刷新结果」可查看最新消息。');
      return;
    }
    show(saved, result);
  }

  const click = (button, action) => ui(button).addEventListener('click', async () => {
    ui(button).disabled = true;
    try { await action(); } catch (error) { tell(error.message); }
    finally { ui(button).disabled = false; }
  });

  click('pair', async () => {
    const port = Number(ui('port').value);
    fillJobs({ ...(await agent('pair', { port, token: ui('token').value.trim() })), port });
    ui('token').value = '';
  });
  click('start', async () => {
    const { conversation } = await chat('current');
    if (!conversation || !ui('job').value) return tell('请打开模拟会话并选择岗位。');
    await agent('bind', { job_id: ui('job').value, conversation_id: conversation });
    const state = await chat('start', [ui('job').value, conversation]);
    tell(state.active ? '正在监听模拟会话。' : (state.reason || '会话已切换。'));
  });
  click('stop', async () => {
    await chat('stop');
    tell('已停止监听。');
  });
  click('submit', async () => {
    const version = ++viewVersion;
    const text = ui('manual').value.trim();
    const job = ui('job').value;
    if (!text || !job) return tell('请选择岗位并粘贴一条消息。');
    const key = `hrManualDraft:${job}`;
    const { [key]: previous } = await chrome.storage.session.get(key);
    const data = previous?.job_id === job && previous.message_text === text ? previous : {
      job_id: job, conversation_id: 'manual-' + job, message_id: crypto.randomUUID(), message_text: text
    };
    await agent('bind', { job_id: job, conversation_id: data.conversation_id });
    await chrome.storage.session.set({ [key]: data });
    tell('正在生成草稿…');
    const result = await agent('submit', data);
    if (version === viewVersion) show(data, result);
  });
  click('refresh', () => refresh());
  click('retry', async () => {
    const data = shown;
    if (!data) return;
    const action = ['HR_REPLY_NOT_FOUND', 'HR_REPLY_AGENT_UNAVAILABLE'].includes(shownResult?.error_code)
      ? 'submit' : 'retry';
    const version = ++viewVersion;
    const state = await chat('current');
    const result = await agent(action, data);
    const current = await chat('current');
    if (version === viewVersion && state.conversation === current.conversation) show(data, result);
  });
  click('copy', async () => {
    if (!ui('draft').value.trim()) return tell('当前没有草稿。');
    await navigator.clipboard.writeText(ui('draft').value);
    tell('已复制草稿。');
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'session' && Object.keys(changes).some(key => key.startsWith('hrLatest:'))) {
      refresh(true).catch(error => tell(error.message));
    }
  });
  ui('job').addEventListener('change', () => {
    viewVersion++;
    show(null, null);
    refresh().catch(error => tell(error.message));
  });
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === 'hr-update') refresh(true).catch(error => tell(error.message));
  });
  agent('jobs').then(result => { fillJobs(result); return refresh(); }).catch(error => tell(error.message));
})();
