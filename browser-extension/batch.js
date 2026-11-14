const status = document.querySelector('#status');
const progress = document.querySelector('#progress');
const stop = document.querySelector('#stop');
const grant = document.querySelector('#grant');
const [tabNumber, platform = 'boss'] = location.hash.slice(1).split(':');
const tabId = Number(tabNumber);
const site = Object.hasOwn(batchPlatforms, platform) ? batchPlatforms[platform] : null;
const permission = site && { origins: site.origins };
let stopRequested = false;
let detailTabId;

stop.addEventListener('click', () => {
  stopRequested = true;
  stop.disabled = true;
  status.textContent = '正在保存已读结果…';
});

async function closeDetailTab() {
  if (!detailTabId) return;
  const id = detailTabId;
  detailTabId = undefined;
  await chrome.tabs.remove(id).catch(() => {});
}

window.addEventListener('pagehide', closeDetailTab);

async function readPage(tabId, options) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId }, func: site.collect, args: [options]
  });
  return result;
}

function siteUrl(href) {
  if (!href || !URL.canParse(href)) return null;
  const url = new URL(href);
  return site.origins.includes(url.origin + '/*') && !url.username && !url.password ? url : null;
}

async function readDetail(link, target) {
  if (stopRequested) return { stopped: '已按请求停止。' };
  if (!detailTabId) detailTabId = (await chrome.tabs.create({ url: 'about:blank', active: false })).id;
  await chrome.tabs.update(detailTabId, { url: link.href });
  const deadline = performance.now() + 7000;
  do {
    if (stopRequested) return { stopped: '已按请求停止。' };
    const tab = await chrome.tabs.get(detailTabId);
    const page = siteUrl(tab.url);
    if (tab.status === 'complete' && !tab.pendingUrl && !page && tab.url !== 'about:blank') {
      return { stopped: `${site.label} 详情跳转到验证页或无法访问的页面，已停止补读。` };
    }
    if (page && /\/(?:_security_check|web\/user|captcha|verify)(?:[/?#]|$)/.test(page.pathname)) {
      return { stopped: `${site.label} 要求登录或安全验证，已停止补读。` };
    }
    if (tab.status === 'complete' && page?.pathname === link.pathname) {
      const result = await readPage(detailTabId, { expectedJobId: target.external_id }).catch(() => null);
      if (result?.stopped) return result;
      const detail = result?.jobs?.find((job) => job.external_id === target.external_id);
      if (detail?.jd) return { detail };
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  } while (performance.now() < deadline);
  return { reason: 'DETAIL_NOT_CONFIRMED' };
}

async function exportBatch(tabId) {
  const folder = await exportDirectory();
  if (!folder || await folder.queryPermission({ mode: 'readwrite' }) !== 'granted') {
    status.textContent = `请先设置导出文件夹，再回到 ${site.label} 列表页重新开始。`;
    return;
  }
  const sourceTab = await chrome.tabs.get(tabId);
  if (!site.listUrl.test(sourceTab.url || '')) {
    status.textContent = `请从 ${site.label} 岗位列表页开始。`;
    return;
  }
  const initial = await readPage(tabId, { withDetailLinks: true });
  if (initial.stopped || initial.error || !initial.list_page) {
    status.textContent = initial.stopped || initial.error || `请从 ${site.label} 岗位列表页开始。`;
    return;
  }

  const { jobs, needs_jd: queue, skipped, detail_links: links } = initial;
  const total = jobs.length + queue.length;
  if (!total) {
    status.textContent = '当前列表没有可识别的岗位。请等页面加载后重试。';
    return;
  }
  const needs_jd = [];
  let attempted = 0;
  let stopReason = '';
  progress.max = total;
  progress.value = jobs.length;
  stop.disabled = !queue.length;

  try {
    for (const target of queue) {
      if (stopRequested || stopReason) {
        stopReason ||= '已按请求停止。';
        needs_jd.push({ ...target, detail_status: 'DETAIL_INTERRUPTED' });
        continue;
      }
      status.textContent = `正在打开第 ${progress.value + 1} / ${total} 条详情。已取得 ${jobs.length} 条描述。`;
      let link = siteUrl(Object.hasOwn(links, target.external_id) ? links[target.external_id] : '');
      let outcome = { reason: 'DETAIL_LINK_UNAVAILABLE' };
      if (!link && site.resolve && target.external_id) {
        const resolved = await chrome.scripting.executeScript({
          target: { tabId }, world: 'MAIN', func: site.resolve, args: [target],
        }).then(([entry]) => entry.result).catch(() => ({ stopped: '搜索页已关闭或无法访问，已停止补读。' }));
        outcome = resolved || outcome;
        link = siteUrl(resolved?.url);
      }
      if (link && site.detailPath.exec(link.pathname)?.[1] === target.external_id) {
        attempted++;
        outcome = await readDetail(link, target).catch(() => ({ stopped: '详情标签页已关闭或无法访问。' }));
      }
      if (outcome.detail) {
        const { detail_status, ...summary } = target;
        if (platform === 'liepin' || platform === '51job') {
          const detail = outcome.detail;
          jobs.push({
            ...summary, ...detail,
            city: detail.city || summary.city, salary_text: detail.salary_text || summary.salary_text,
          });
        } else {
          jobs.push({ ...summary, jd: outcome.detail.jd });
        }
      } else {
        needs_jd.push({ ...target, detail_status: outcome.reason || 'DETAIL_INTERRUPTED' });
      }
      stopReason = outcome.stopped || '';
      progress.value++;
    }
  } finally {
    await closeDetailTab();
  }

  stop.disabled = true;
  status.textContent = '正在保存 JSON…';
  const filename = await saveJobExport(folder, {
    jobs, needs_jd, skipped,
    ...(initial.search ? { search: { ...initial.search, scope: 'batch' } } : {}),
    detail_read: { attempted, stop_reason: stopReason },
  }, platform);
  status.textContent = `已保存 ${filename}。共 ${total} 条，带描述 ${jobs.length} 条，待补 ${needs_jd.length} 条。` +
    (skipped ? `另有 ${skipped} 张卡片无法核对。` : '') + (stopReason ? ` ${stopReason}` : '');
}

async function startBatch() {
  grant.hidden = true;
  status.textContent = '正在读取岗位列表…';
  try {
    await navigator.locks.request(`jobhunt-batch-${tabId}`, { ifAvailable: true }, async (lock) => {
      if (!lock) {
        status.textContent = `此 ${site.label} 标签页已有一轮补读，请查看先打开的进度页。`;
        return;
      }
      await exportBatch(tabId);
    });
  } catch {
    stop.disabled = true;
    status.textContent = `批量导出未完成。请检查 ${site.label} 页面和导出文件夹后重试。`;
  }
}

grant.addEventListener('click', async () => {
  grant.disabled = true;
  try {
    if (await chrome.permissions.request(permission)) {
      await startBatch();
    } else {
      status.textContent = `读取 ${site.label} 详情需要站点访问授权。`;
    }
  } catch {
    status.textContent = `未能申请 ${site.label} 站点授权，请重试。`;
  } finally {
    grant.disabled = false;
  }
});

if (site && Number.isSafeInteger(tabId) && tabId > 0) {
  grant.textContent = `授权读取${site.label}详情页`;
  chrome.permissions.contains(permission).then((granted) => {
    if (granted) return startBatch();
    status.textContent = `首次批量补读，请授权扩展读取临时打开的 ${site.label} 详情页。`;
    grant.hidden = false;
  }).catch(() => { status.textContent = `无法检查 ${site.label} 站点授权，请重新加载扩展。`; });
} else {
  status.textContent = '请回岗位列表页，从扩展菜单开始。';
}
