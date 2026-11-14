"""Offline P4 channel assessment. A login or public GET API grants no POST authority."""
from copy import deepcopy
import re
from urllib.parse import parse_qsl, urlsplit

from ..config import UserError

REVIEW_DATE = '2026-11-05'
_CHANNELS = (
    {'id': 'boss', 'name': 'BOSS 直聘', 'domains': ('zhipin.com',),
     'permission_status': 'PLATFORM_PERMISSION_REQUIRED',
     'reason': '检索到的官方协议要求第三方接入取得平台许可；本项目尚无适用授权和真实执行适配器。',
     'evidence': [{'url': 'https://www.zhipin.com/web/common/protocol/protocol-2019-09-30.html',
                   'retrieval': 'SEARCH_INDEX_ONLY', 'note': '直接读取返回 404，不能确认当前全文版本。'}]},
    {'id': 'zhaopin', 'name': '智联招聘', 'domains': ('zhaopin.com', 'zhaopin.cn'),
     'permission_status': 'PLATFORM_PERMISSION_REQUIRED',
     'reason': '官方用户行为规范要求第三方工具接入取得许可；本项目尚无适用授权和真实执行适配器。',
     'evidence': [{'url': 'https://rd5-public.zhaopin.cn/docs/智联招聘用户行为规范_20230731.pdf',
                   'retrieval': 'FETCHED', 'note': '2023-07-31 版，第三部分第 4(1) 项；未证明这是最新版本。'},
                  {'url': 'https://special.zhaopin.com/sh/2009/aboutus/law.html',
                   'retrieval': 'FETCHED', 'note': '官方法律声明要求第三方使用内容取得书面同意。'}]},
    {'id': '51job', 'name': '前程无忧', 'domains': ('51job.com',),
     'permission_status': 'PLATFORM_PERMISSION_REQUIRED',
     'reason': '公开协议检索文本限制自动化程序接入；本项目未取得适用许可，也未实现真实执行适配器。',
     'evidence': [{'url': 'https://login.51job.com/xyservice.php?display=h5&page=1',
                   'retrieval': 'SEARCH_INDEX_ONLY', 'note': '直接读取超时；2024-05 PDF 未检索到同一自动化条款，版本待进一步核验。'}]},
    {'id': 'greenhouse', 'name': 'Greenhouse',
     'domains': ('greenhouse.io',), 'permission_status': 'EMPLOYER_API_AUTHORIZATION_REQUIRED',
     'reason': '公开岗位 GET 与申请 POST 权限不同；申请接口需要招聘企业提供受限 API 凭据，尚无对应授权和执行适配器。',
     'evidence': [{'url': 'https://docs.greenhouse.io/job-board', 'retrieval': 'FETCHED',
                   'note': 'Authentication / Submit an application：POST 使用 Job Board API key。'}]},
    {'id': 'lever', 'name': 'Lever', 'domains': ('lever.co',),
     'permission_status': 'EMPLOYER_API_AUTHORIZATION_REQUIRED',
     'reason': '提交接口需要招聘企业管理员生成的 API key；求职者登录状态不能代替该授权，真实执行适配器尚未实现。',
     'evidence': [{'url': 'https://github.com/lever/postings-api', 'retrieval': 'FETCHED',
                   'note': 'Apply to a job posting：POST API 需要企业 Super Admin 生成的 key。'}]},
    {'id': 'italent', 'name': '北森 iTalent', 'domains': ('italent.cn',),
     'permission_status': 'TARGET_PERMISSION_NOT_VERIFIED',
     'reason': '已收到私人简历完善邀请；尚未核验目标表单的适用条款、企业与职位，也没有真实填写适配器。邀请链接和登录状态不能代替自动操作许可。',
     'evidence': [{'url': 'https://www.beisen.com/privacy/', 'retrieval': 'FETCHED',
                   'scope': 'PROVIDER_WEBSITE_ONLY',
                   'note': '北森官网协议 7.1.4、7.1.6 涉及未授权第三方工具；该官网协议是否适用于目标招聘租户尚未确认，不能据此断言目标允许或禁止自动填写。'}]},
)


def public_url(url):
    """Validate without DNS or HTTP; never echo credentials or arbitrary input in errors."""
    try:
        if (not isinstance(url, str) or not 1 <= len(url) <= 4096
                or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url):
            raise ValueError()
        parts = urlsplit(url)
        host = parts.hostname or ''
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment or not host.isascii()
                or len(host) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))
                or '.' not in host or '..' in host or host.endswith(('.localhost', '.local', '.internal'))):
            raise ValueError()
        # DNS suffixes exclude browser legacy IPv4 spellings (127.1, octal, hex).
        if not re.fullmatch(r'(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})', host.rsplit('.', 1)[-1]):
            raise ValueError()
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if (re.search(r'(?i)token|password|cookie|secret|session|signature|authorization|api.?key', key)
                    or key.lower() in {'key', 'code', 'auth', 'sig', 'sign', 'invitekey', 'invite_key',
                                       'bc_sign', 'bc_nonce', 'quark_s', 'shadow_context'}):
                raise ValueError()
        return url
    except (TypeError, ValueError):
        raise UserError('P4_PUBLIC_JOB_URL_REQUIRED: 请提供无凭据的公开 HTTPS 岗位链接') from None


def channel_catalog():
    channels = deepcopy(list(_CHANNELS))
    for channel in channels:
        channel.update(reviewed_on=REVIEW_DATE, adapter_status='NOT_IMPLEMENTED',
                       automation_authorized=False)
    return {'scope': 'P4_CHANNEL_ASSESSMENT', 'channels': channels,
            'network_checked': False, 'external_submission': False}


def preflight(url):
    host = urlsplit(public_url(url)).hostname
    channel = next((row for row in channel_catalog()['channels']
                    if any(host == domain or host.endswith('.' + domain) for domain in row['domains'])), None)
    if channel is None:
        channel = {'id': 'unknown', 'name': '未核验的招聘入口', 'domains': (),
                   'permission_status': 'NOT_VERIFIED', 'adapter_status': 'NOT_IMPLEMENTED',
                   'automation_authorized': False, 'reviewed_on': None, 'evidence': [],
                   'reason': '尚未核验该入口的访问与操作许可，也没有对应执行适配器。'}
    return {'scope': 'P4_CHANNEL_ASSESSMENT', 'status': 'MANUAL_REQUIRED',
            'target_origin': 'https://' + host, 'channel': channel,
            'automation': {operation: False for operation in ('open_application', 'inspect_form', 'fill_form', 'upload_resume', 'submit')},
            'per_application_approval_required': True, 'login_required_now': False,
            'next_action': '可生成本地人工申请材料；自动操作需先核实具体渠道授权并完成执行器适配。',
            'network_checked': False, 'redirects_resolved': False, 'external_submission': False}


def require_automated_channel(url):
    """Fail closed before constructing any real browser or HTTP executor."""
    result = preflight(url)
    if not result['automation']['submit']:
        raise UserError('P4_MANUAL_REQUIRED: 当前渠道未具备自动执行条件')
    return result
