const exportButton = document.querySelector('#export');
const batchButton = document.querySelector('#batch');
const status = document.querySelector('#status');
const platformLabel = document.querySelector('#platform');
const liepinVerification = /^https:\/\/safe\.liepin\.com\//;
const collectors = { boss: collectBossJobs, '51job': collect51Job, liepin: collectLiepinJobs };

function platformFor(url = '') {
  if (/^https:\/\/(?:www\.)?zhipin\.com\//.test(url)) return 'boss';
  if (/^https:\/\/(?:we|jobs)\.51job\.com\//.test(url)) return '51job';
  if (/^https:\/\/www\.liepin\.com\//.test(url)) return 'liepin';
  return '';
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

activeTab().then((tab) => {
  const platform = platformFor(tab?.url);
  batchButton.hidden = !batchPlatformFor(tab?.url);
  if (platform === 'boss') {
    platformLabel.textContent = 'BOSS 岗位导出';
  } else if (platform === '51job') {
    platformLabel.textContent = '前程无忧岗位导出';
    if (tab.url.startsWith('https://we.51job.com/')) {
      status.textContent = '批量补读会导出当前搜索页已加载的岗位，请保留搜索页并保持搜索条件不变。';
    }
  } else if (platform === 'liepin' || liepinVerification.test(tab?.url || '')) {
    platformLabel.textContent = '猎聘岗位导出';
    if (liepinVerification.test(tab.url)) {
      status.textContent = '请在猎聘页面完成验证，再重新导出。';
    } else if (batchPlatformFor(tab.url)) {
      status.textContent = '批量补读会导出当前搜索页已加载的岗位。';
    }
  }
}).catch(() => {});

batchButton.addEventListener('click', async () => {
  batchButton.disabled = true;
  try {
    const tab = await activeTab();
    const platform = batchPlatformFor(tab?.url);
    if (!tab?.id || !platform) {
      status.textContent = '请从支持批量补读的岗位列表页开始。';
      return;
    }
    await chrome.tabs.create({ url: chrome.runtime.getURL(`batch.html#${tab.id}:${platform}`) });
  } catch {
    status.textContent = '未能打开补读进度页，请重试。';
  } finally {
    batchButton.disabled = false;
  }
});

exportButton.addEventListener('click', async () => {
  exportButton.disabled = true;
  status.textContent = '正在读取当前页…';
  try {
    const tab = await activeTab();
    const platform = platformFor(tab?.url);
    if (liepinVerification.test(tab?.url || '')) {
      status.textContent = '请在猎聘页面完成验证，再重新导出。';
      return;
    }
    if (!tab?.id || !platform) {
      status.textContent = '请打开 BOSS、前程无忧或猎聘的搜索结果或职位详情。';
      return;
    }
    const folder = await exportDirectory();
    if (!folder || await folder.queryPermission({ mode: 'readwrite' }) !== 'granted') {
      status.textContent = '请先点「设置导出文件夹」，选择并授权「岗位信息」目录。';
      return;
    }
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: collectors[platform]
    });
    if (result.error) {
      status.textContent = result.error;
      return;
    }
    const total = result.jobs.length + result.needs_jd.length;
    const skipped = result.skipped ? `跳过 ${result.skipped} 张无法核对的卡片。` : '';
    if (!total) {
      status.textContent = `未读到可识别的岗位。请确认网页已加载。${skipped}`;
      return;
    }
    status.textContent = '正在保存 JSON…';
    const filename = await saveJobExport(folder, result, platform);
    status.textContent = `已保存 ${filename}。共 ${total} 条，可导入 ${result.jobs.length} 条，待补描述 ${result.needs_jd.length} 条。${skipped}`;
  } catch (error) {
    status.textContent = ['NotAllowedError', 'NotFoundError', 'SecurityError'].includes(error.name)
      ? '无法访问导出目录。请在「设置导出文件夹」重新选择并授权。'
      : '导出中断。请确认当前页面和导出目录可用后重试。';
  } finally {
    exportButton.disabled = false;
  }
});
