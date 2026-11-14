// Chrome runs this function in the selected tab.
function collectLiepinJobs(options = {}) {
  const origin = 'https://www.liepin.com';
  const detailPath = /^\/job\/(\d+)\.shtml$/;
  const cardSelector = '.jobCardPcContainer, .job-card-pc-container';
  const visible = (element) => element.getClientRects().length > 0 && getComputedStyle(element).visibility === 'visible';

  function detailIdentity(href) {
    if (!href || !URL.canParse(href, origin)) return null;
    const url = new URL(href, origin);
    const id = detailPath.exec(url.pathname)?.[1];
    if (!id || url.origin !== origin || url.username || url.password) return null;
    return { external_id: id, url: origin + url.pathname };
  }

  function readText(root, selector) {
    return [...root.querySelectorAll(selector)].find(visible)?.innerText.trim() || '';
  }

  if (location.origin === 'https://safe.liepin.com' ||
      /猎聘安全中心|滑动验证|请进行验证/.test(document.title + '\n' + readText(document, 'h1'))) {
    const message = '请在猎聘页面完成验证，再重新导出。';
    return options.expectedJobId || options.withDetailLinks ? { stopped: message } : { error: message };
  }
  if (location.origin !== origin) return { error: '请打开猎聘的搜索结果或职位详情。' };

  const jobs = [];
  const needs_jd = [];
  let skipped = 0;
  const detailLinks = new Map();
  const list = /^\/zhaopin\/?$/.test(location.pathname);
  const search = { platform: 'liepin', scope: list ? 'list' : 'detail' };
  const detail = detailIdentity(location.href);
  if (options.expectedJobId && detail?.external_id !== options.expectedJobId) {
    return { error: '页面职位编号不符。' };
  }
  if (detail) {
    const titleNode = [...document.querySelectorAll('.job-title')].find((node) =>
      visible(node) && !node.closest(cardSelector));
    const header = titleNode?.closest('.job-apply-content, .job-brief-container') ||
      titleNode?.parentElement?.parentElement;
    const title = titleNode?.innerText.trim();
    const companyCardNames = [...document.querySelectorAll('.company-info-container .company-card > .content > .name')]
      .filter(visible);
    const companyAreas = '.job-brief-container, .company-info-container, .job-company-info-container, .company-card-container, .company-info, .company-card';
    const companyNodes = companyCardNames.length ? companyCardNames : [...document.querySelectorAll('.company-name')]
      .filter((node) => visible(node) && node.closest(companyAreas) && !node.closest(cardSelector));
    const companies = new Set(companyNodes.map((node) => node.innerText.trim()).filter(Boolean));
    if (!title || !header || companies.size !== 1) {
      return { error: '未能确认猎聘详情里的职位或公司，请检查页面是否已加载。' };
    }
    const city = readText(header, '.job-dq-box .ellipsis-1, .job-properties > span:first-child, .job-properties > a:first-child');
    const salary_text = readText(header, '.name-box > .salary, .job-salary');
    const jd = readText(document, '.job-intro-container .paragraph dd').replace(/\n{2,}/g, '\n');
    const record = { ...detail, title, company: companies.values().next().value, city, salary_text, jd };
    if (jd) jobs.push(record);
    else needs_jd.push({ ...record, detail_status: 'DESCRIPTION_UNAVAILABLE' });
  } else if (list) {
    for (const card of document.querySelectorAll(cardSelector)) {
      if (!visible(card)) continue;
      const links = [...card.querySelectorAll('a[href]')].filter(visible)
        .map((anchor) => detailIdentity(anchor.href)).filter(Boolean);
      const ids = new Map(links.map((link) => [link.external_id, link]));
      const identity = ids.size === 1 ? ids.values().next().value : null;
      const title = readText(card, '.jobTitleBox > .ellipsis-1, .job-title-box > .ellipsis-1');
      const company = readText(card, '.companyName, .company-name');
      if (!identity && (!title || !company)) {
        skipped++;
        continue;
      }
      if (identity) {
        if (detailLinks.has(identity.external_id)) continue;
        detailLinks.set(identity.external_id, identity.url);
      }
      needs_jd.push({
        ...identity, title, company,
        city: readText(card, '.jobTitleBox > div:nth-child(2) .ellipsis-1, .job-dq-box .ellipsis-1'),
        salary_text: readText(card, '.jobTitleBox + span, .job-salary'),
        jd: '', detail_status: identity ? 'DESCRIPTION_UNAVAILABLE' : 'DETAIL_LINK_UNAVAILABLE',
      });
    }
  } else {
    return { error: '请打开猎聘的搜索结果或职位详情。' };
  }
  return {
    jobs, needs_jd, skipped, search,
    ...(options.withDetailLinks ? { list_page: list, detail_links: Object.fromEntries(detailLinks) } : {}),
  };
}
