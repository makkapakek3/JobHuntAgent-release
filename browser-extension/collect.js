// Chrome serializes this function before running it in the active tab.
function collectBossJobs(options = {}) {
  const bossHost = /^(?:www\.)?zhipin\.com$/;
  const detailPath = /^\/job_detail\/([A-Za-z0-9_-]+)\.html$/;
  if (location.protocol !== 'https:' || !bossHost.test(location.hostname)) {
    return { error: '请切换到 BOSS 直聘的岗位列表或详情页。' };
  }
  const listPage = location.pathname.startsWith('/web/geek/job');
  const visible = (element) => element.getClientRects().length > 0;
  if ((options.expectedJobId || options.withDetailLinks) &&
      (/\/(?:_security_check|web\/user|captcha|verify)(?:[/?#]|$)/.test(location.pathname) ||
      [...document.querySelectorAll('.login-dialog, .boss-login-dialog, .captcha-dialog, .verify-dialog, iframe[src*="captcha"]')].some(visible))) {
    return { stopped: '页面要求登录或安全验证，已停止补读。' };
  }

  function jobId(href) {
    if (!href || !URL.canParse(href, location.origin)) return '';
    const url = new URL(href, location.origin);
    if (url.protocol !== 'https:' || !bossHost.test(url.hostname) || url.port || url.username || url.password) return '';
    return detailPath.exec(url.pathname)?.[1] || '';
  }

  function readText(root, selector) {
    return root?.querySelector(selector)?.innerText.trim() || '';
  }

  function record(id, title, company, city, salary, jd) {
    return {
      external_id: id,
      title,
      company,
      city,
      salary_text: salary,
      jd,
      url: `https://www.zhipin.com/job_detail/${id}.html`
    };
  }

  const description = '.job-sec-text, .job-detail-desc .desc, .job-detail-section .desc, .job-desc';
  const byId = new Map();
  const detailLinks = new Map();
  let skipped = 0;
  const pageJobId = jobId(location.href);
  if (options.expectedJobId && pageJobId !== options.expectedJobId) return { error: '页面岗位 ID 不符。' };
  if (pageJobId) {
    const headings = document.querySelectorAll('h1');
    const heading = document.querySelector('.job-banner h1, h1.job-name') ||
      (headings.length === 1 ? headings[0] : null);
    const title = heading?.innerText.trim();
    const sidebar = document.querySelector('.sider-company');
    const companyAfterLabel = sidebar?.innerText.match(/(?:^|\n)[ \t]*公司基本信息[ \t]*\n\s*([^\n]+)/)?.[1].trim();
    const company = readText(sidebar, '.company-name, .company-info a[href*="/gongsi/"]:not([href*="/gongsi/job/"])') ||
      companyAfterLabel || readText(document, '.job-detail-box .company-name');
    if (!title || !company) {
      const missing = !title && !company ? '职位名称、公司名称' : (!title ? '职位名称' : '公司名称');
      return { error: `已识别为岗位详情页，但未读到${missing}。` };
    }
    const salary = readText(document, '.job-banner .salary') || readText(heading.parentElement, '.salary');
    byId.set(pageJobId, record(pageJobId, title, company,
      readText(document, '.job-banner .job-area, .text-city'), salary, readText(document, description)));
  } else if (listPage) {
    const detailSelector = '.job-detail-box, .job-detail-container';
    const panels = () => [...document.querySelectorAll(detailSelector)].filter(visible);
    function panelDetails(panel) {
      const links = [...panel.querySelectorAll('a.more-job-btn[href], a.more[href], a[href*="/job_detail/"][href*="securityId="]')];
      const identities = new Set(links.map((link) => jobId(link.href)).filter(Boolean));
      return {
        id: identities.size === 1 ? [...identities][0] : '',
        title: readText(panel, '.job-name'),
        jd: readText(panel, description)
      };
    }
    let cards = document.querySelectorAll('.job-card-box');
    if (!cards.length) cards = document.querySelectorAll('.job-card-wrap, .job-card-wrapper');
    for (const card of cards) {
      if (!visible(card) || card.closest(detailSelector)) continue;
      const titleElement = card.querySelector('.job-name, .job-title a');
      const title = titleElement?.innerText.trim() || '';
      const company = readText(card, '.company-name, .company-info .name, .company-text') ||
        readText(card, 'a[ka^="company_logo_click"] .boss-name');
      const titleLink = titleElement?.closest('a[href]');
      const link = jobId(titleLink?.href) ? titleLink : card.querySelector('a[href*="/job_detail/"]');
      const id = jobId(link?.href);
      if (!id || !title || !company) {
        skipped++;
        continue;
      }
      if (!byId.has(id)) {
        byId.set(id, record(id, title, company, readText(card, '.job-area, .company-location'),
          readText(card, '.job-salary, .salary'), ''));
      }
      if (options.withDetailLinks && !detailLinks.has(id)) detailLinks.set(id, link.href);
    }

    const before = panels().map(panelDetails);
    for (const { id, title, jd } of before) {
      const job = byId.get(id);
      if (job && jd && (!title || title === job.title)) job.jd = jd;
    }
  } else {
    return { error: '请打开 BOSS 直聘的岗位列表或详情页。' };
  }

  const jobs = [];
  const needs_jd = [];
  for (const job of byId.values()) {
    if (job.jd) jobs.push(job);
    else needs_jd.push(job);
  }
  return { jobs, needs_jd, skipped, ...(options.withDetailLinks ? { detail_links: Object.fromEntries(detailLinks), list_page: listPage } : {}) };
}
