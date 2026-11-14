const batchPlatforms = {
  '51job': {
    label: '前程无忧',
    listUrl: /^https:\/\/we\.51job\.com\/pc\/search(?:[?#]|$)/,
    origins: ['https://we.51job.com/*', 'https://jobs.51job.com/*'],
    detailPath: /^\/(?!all\/)[a-z0-9-]+\/(\d+)\.html$/i,
    collect: collect51Job,
    resolve: resolve51JobLink,
  },
  boss: {
    label: 'BOSS',
    listUrl: /^https:\/\/(?:www\.)?zhipin\.com\/web\/geek\/job/,
    origins: ['https://www.zhipin.com/*', 'https://zhipin.com/*'],
    detailPath: /^\/job_detail\/([A-Za-z0-9_-]+)\.html$/,
    collect: collectBossJobs,
  },
  liepin: {
    label: '猎聘',
    listUrl: /^https:\/\/www\.liepin\.com\/zhaopin\/?(?:[?#]|$)/,
    origins: ['https://www.liepin.com/*'],
    detailPath: /^\/job\/(\d+)\.shtml$/,
    collect: collectLiepinJobs,
  },
};

function batchPlatformFor(url = '') {
  return Object.keys(batchPlatforms).find((key) => batchPlatforms[key].listUrl.test(url));
}
