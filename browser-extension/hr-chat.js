// Executed in the selected synthetic chat's isolated world.
if (!globalThis.jobHuntDemoChat) {
  globalThis.jobHuntDemoChat = (() => {
    let observer;
    let watched = '';
    let reason = '';
    const root = () => document.querySelector('#chat[data-demo-conversation]');
    const read = (chat) => [...chat.querySelectorAll('li')].map(row => ({
      id: row.dataset.messageId,
      sequence: Number(row.dataset.sequence),
      direction: row.dataset.direction,
      text: row.textContent.trim()
    }));
    const valid = (row) => /^[A-Za-z0-9_-]{1,100}$/.test(row.id || '') &&
      Number.isSafeInteger(row.sequence) && row.sequence > 0 &&
      ['hr', 'self'].includes(row.direction) && row.text.length > 0;
    function stop(message = '') {
      observer?.disconnect();
      observer = undefined;
      watched = '';
      reason = message;
    }
    function current() {
      const conversation = root()?.dataset.demoConversation;
      return { conversation, active: Boolean(watched && watched === conversation), reason };
    }
    function start(job, conversation) {
      stop();
      const chat = root();
      if (!chat || chat.dataset.demoConversation !== conversation) return current();
      const baseline = read(chat);
      if (!baseline.every(valid)) {
        stop('消息格式无法确认，已暂停。');
        return current();
      }
      let last = Math.max(0, ...baseline.map(row => row.sequence));
      const seen = new Set(baseline.map(row => row.id));
      watched = conversation;
      observer = new MutationObserver(() => {
        if (root() !== chat || chat.dataset.demoConversation !== watched) {
          stop('会话已切换，请重新选择岗位并启用。');
          return;
        }
        const rows = read(chat);
        if (!rows.every(valid)) {
          stop('消息格式无法确认，已暂停。');
          return;
        }
        for (const row of rows.sort((left, right) => left.sequence - right.sequence)) {
          if (row.sequence <= last || seen.has(row.id)) continue;
          seen.add(row.id);
          last = row.sequence;
          if (row.direction !== 'hr') continue;
          const data = { job_id: job, conversation_id: conversation, message_id: row.id, message_text: row.text };
          chrome.runtime.sendMessage({ type: 'hr', action: 'incoming', data }).then(reply => {
            if (reply.error && watched === conversation) reason = reply.error;
          }).catch(() => {
            if (watched === conversation) reason = '本机 Agent 连接中断。';
          });
        }
      });
      observer.observe(document.body, { subtree: true, childList: true, attributes: true });
      return current();
    }
    return { start, stop, current };
  })();
}
