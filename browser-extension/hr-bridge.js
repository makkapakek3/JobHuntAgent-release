const configKey = 'hrConnection';

async function callAgent(config, path, data) {
  const headers = {
    Authorization: 'Bearer ' + config.token,
    'X-JobHunt-Extension': chrome.runtime.id
  };
  if (data) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${config.port}/v1/hr/${path}`, {
      method: data ? 'POST' : 'GET',
      headers,
      body: data ? JSON.stringify(data) : undefined,
      redirect: 'error',
      signal: AbortSignal.timeout(120000)
    });
  } catch {
    throw new Error('HR_REPLY_AGENT_UNAVAILABLE');
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error_code || 'HR_REPLY_AGENT_UNAVAILABLE');
  return result;
}

async function handle(message, sender) {
  if (sender.id !== chrome.runtime.id) throw new Error('HR_REPLY_SOURCE_DENIED');
  const { action, data } = message;
  const fromPopup = sender.url === chrome.runtime.getURL('popup.html');
  if (!fromPopup && action !== 'incoming') throw new Error('HR_REPLY_SOURCE_DENIED');
  if (action === 'pair') {
    const { port, token } = data;
    if (!Number.isInteger(port) || port < 1024 || port > 65535 ||
        typeof token !== 'string' || !/^[A-Za-z0-9_-]{16,100}$/.test(token)) {
      throw new Error('HR_REPLY_INVALID_SERVER_SETTINGS');
    }
    const info = await callAgent({ port, token }, 'jobs');
    await chrome.storage.session.set({ [configKey]: { port, token, mode: info.mode } });
    return info;
  }
  const { [configKey]: config } = await chrome.storage.session.get(configKey);
  if (!config) throw new Error('HR_REPLY_PAIRING_REQUIRED');
  if (action === 'jobs') return { ...(await callAgent(config, 'jobs')), port: config.port };
  if (action === 'incoming') {
    if (config.mode !== 'DEMO' || sender.frameId !== 0 ||
        sender.url !== `http://127.0.0.1:${config.port}/demo/chat`) {
      throw new Error('HR_REPLY_SOURCE_DENIED');
    }
    const key = `hrLatest:${sender.tab.id}:${data.conversation_id}`;
    await chrome.storage.session.set({ [key]: data });
    try {
      return await callAgent(config, 'reply', data);
    } finally {
      chrome.runtime.sendMessage({ type: 'hr-update', key }).catch(() => {});
    }
  }
  if (action === 'bind') return callAgent(config, 'bind', data);
  if (action === 'submit') return callAgent(config, 'reply', data);
  if (action === 'retry') return callAgent(config, 'reply/retry', data);
  if (action === 'get') {
    const { conversation_id, message_id } = data;
    return callAgent(config, 'reply?' + new URLSearchParams({ conversation_id, message_id }));
  }
  throw new Error('HR_REPLY_ROUTE_UNKNOWN');
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== 'hr') return false;
  handle(message, sender).then(result => respond({ result }), error => respond({ error: error.message }));
  return true;
});
