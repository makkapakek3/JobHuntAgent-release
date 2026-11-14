// Chrome serializes this function when it runs in the selected tab.
function collect51Job(options = {}) {
  const searchPage = location.origin === 'https://we.51job.com' && location.pathname === '/pc/search';
  const jobPath = /^\/(?!all\/)[a-z0-9-]+\/(\d+)\.html$/i;
  const visible = (element) => element.getClientRects().length > 0 && getComputedStyle(element).visibility === 'visible';

  function detailLink(href) {
    if (!href || !URL.canParse(href, location.href)) return null;
    const url = new URL(href, location.href);
    if (url.origin !== 'https://jobs.51job.com' || url.username || url.password) return null;
    const id = jobPath.exec(url.pathname)?.[1];
    return id ? { external_id: id, url: url.origin + url.pathname } : null;
  }

  function readText(root, selector) {
    return [...root.querySelectorAll(selector)].find(visible)?.innerText.trim() || '';
  }

  if (/滑动验证|安全验证|访问验证|请进行验证|登录前程无忧/.test(document.title + '\n' + readText(document, 'h1')) ||
      [...document.querySelectorAll('iframe[src*="captcha"], .captcha-dialog, .verify-dialog')].some(visible)) {
    const message = '请在前程无忧页面完成登录或验证，再重新导出。';
    return options.expectedJobId || options.withDetailLinks ? { stopped: message } : { error: message };
  }

  const jobs = [];
  const needs_jd = [];
  let skipped = 0;
  const search = { platform: '51job', scope: searchPage ? 'list' : 'detail' };
  const detail = detailLink(location.href);
  const detailLinks = new Map();
  if (options.expectedJobId && detail?.external_id !== options.expectedJobId) {
    return { error: '页面职位编号不符。' };
  }
  if (detail) {
    const header = [...document.querySelectorAll('.tHeader .cn')].find(visible);
    const title = header && readText(header, 'h1');
    const company = (header && readText(header, '.cname a, .cname')) ||
      readText(document, '.rgt-sidebar .corp-card .com_msg .com_name');
    if (!title || !company) return { error: '未识别到职位名称或公司。请确认前程无忧详情已加载。' };
    const city = readText(header, '.msg.ltype .type_2') ||
      readText(header, '.msg.ltype').split('|')[0].trim();
    const salary_text = readText(header, 'strong');
    const lines = readText(document, '.job_msg').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const end = lines.findIndex((line) => /^(?:职能类别|关键字)\s*[:：]/.test(line));
    const jd = lines.slice(0, end < 0 ? lines.length : end)
      .filter((line) => !/^(职位信息|职位描述)$/.test(line)).join('\n');
    const record = { ...detail, company, title, city, salary_text, jd };
    if (jd) jobs.push(record);
    else needs_jd.push({ ...record, detail_status: 'DESCRIPTION_UNAVAILABLE' });
  } else if (searchPage) {
    const seen = new Set();
    for (const card of document.querySelectorAll('.joblist-item')) {
      if (!visible(card)) continue;
      const title = readText(card, '.jname');
      const company = readText(card, '.cname');
      if (!title || !company) {
        skipped++;
        continue;
      }
      const links = [...card.querySelectorAll('a[href]')].filter(visible)
        .map((anchor) => detailLink(anchor.href)).filter(Boolean);
      const byId = new Map(links.map((link) => [link.external_id, link]));
      let identity = byId.size === 1 ? byId.values().next().value : null;
      // Read only the public job ID attached to this card, never export tracking data.
      if (options.withDetailLinks && byId.size === 0) {
        try {
          const node = [...card.querySelectorAll('.joblist-item-job[sensorsdata]')].find(visible);
          const id = JSON.parse(node?.getAttribute('sensorsdata') || '{}').jobId;
          if (typeof id === 'string' && /^\d+$/.test(id)) identity = { external_id: id };
        } catch { /* Keep the readable summary pending if the ID cannot be confirmed. */ }
      }
      if (identity && seen.has(identity.external_id)) continue;
      if (identity) seen.add(identity.external_id);
      if (identity?.url) detailLinks.set(identity.external_id, identity.url);
      needs_jd.push({
        ...identity, title, company,
        city: readText(card, '.area .shrink-0'),
        salary_text: readText(card, '.sal'), jd: '',
        detail_status: identity ? 'DESCRIPTION_UNAVAILABLE' : 'DETAIL_LINK_UNAVAILABLE',
      });
    }
  } else {
    return { error: '请打开前程无忧的搜索结果或职位详情。' };
  }
  return {
    jobs, needs_jd, skipped, search,
    ...(options.withDetailLinks ? { list_page: searchPage, detail_links: Object.fromEntries(detailLinks) } : {}),
  };
}

// Runs in the page's MAIN world so the site's synchronous title click uses this window.open.
// Capture navigation, then let the extension reuse its own single detail tab.
function resolve51JobLink(target) {
  if (location.origin !== 'https://we.51job.com' || location.pathname !== '/pc/search') {
    return { stopped: '前程无忧搜索页已离开，请回到搜索结果重新开始。' };
  }
  const visible = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility === 'visible';
  const read = (root, selector) => [...root.querySelectorAll(selector)].find(visible)?.innerText.trim() || '';
  const cards = [...document.querySelectorAll('.joblist-item')].filter((card) => {
    if (!visible(card) || read(card, '.jname') !== target.title || read(card, '.cname') !== target.company) return false;
    try {
      const node = [...card.querySelectorAll('.joblist-item-job[sensorsdata]')].find(visible);
      return JSON.parse(node?.getAttribute('sensorsdata') || '{}').jobId === target.external_id;
    } catch { return false; }
  });
  if (cards.length !== 1) return { stopped: '原岗位卡片已变化或无法确认，已停止补读，请从搜索页重新开始。' };
  const title = [...cards[0].querySelectorAll('.jname')].find(visible);
  const destinations = [];
  const capture = (value) => { if (typeof value === 'string') destinations.push(value); };
  const locationStub = { set href(value) { capture(value); } };
  const windowStub = { get location() { return locationStub; }, set location(value) { capture(value); } };
  const originalOpen = window.open;
  const captureOpen = (url) => { capture(url); return windowStub; };
  try {
    window.open = captureOpen;
    if (window.open !== captureOpen) return { reason: 'DETAIL_LINK_UNAVAILABLE' };
    title.click();
  } catch {
    return { reason: 'DETAIL_LINK_UNAVAILABLE' };
  } finally {
    window.open = originalOpen;
  }
  const urls = new Set();
  for (const href of destinations) {
    if (!URL.canParse(href)) continue;
    const url = new URL(href);
    if (url.origin === 'https://jobs.51job.com' && !url.username && !url.password &&
        /^\/(?!all\/)[a-z0-9-]+\/(\d+)\.html$/i.exec(url.pathname)?.[1] === target.external_id) {
      urls.add(url.origin + url.pathname);
    }
  }
  return urls.size === 1 ? { url: [...urls][0] } : { reason: 'DETAIL_LINK_UNAVAILABLE' };
}
