"""Local evidence reports, separate from ordinary execution logs."""
import json
import os

from ..models import display


def write_report(home, result):
    directory = home / 'reports' / 'v2' / result['mode'].lower()
    directory.mkdir(parents=True, exist_ok=True)
    stem = 'matches-' + result['run_id']
    lines = ['# V2 P2 岗位匹配报告', '',
             f"模式：{result['mode']}；模型：{display(result['model'])}；时间：{result['created_at']}", '',
             result['score_meaning'], '',
             f"共检查 {result['selected_count']} 条；评分 {result['scored_count']}；规则排除 {result['filtered_count']}；"
             f"失败 {result['failed_count']}；延期 {result['deferred_count']}；缓存命中 {result['cache_hits']}。", '',
             f"本报告覆盖当前批次；数据库可选 {result['available_count']} 条，本批未选择 {result['unselected_count']} 条。"
             + (f" 建议后续从 --offset {result['next_offset']} 继续；遇到校验失败先核查错误。"
                if result.get('next_offset') is not None else ''), '',
             f"资料版本：`{result['profile_version']}`；简历版本：`{result['resume_version']}`", '',
             '个人证据来自用户提供的本地资料，未经第三方核验。项目中的团队、用户和 Codex 分工以资料原文为准。', '',
             '## 本次求职偏好', '', '```json',
             json.dumps(result['preferences'], ensure_ascii=False, indent=2), '```', '', '## 岗位排序', '',
             '| 分数 | 岗位 | 企业 | 待核对事项 |', '| --- | --- | --- | --- |']
    for row in result['rankings']:
        lines.append(f"| {row['score']} | {display(row['title'])} | {display(row['company'])} | "
                     + display(', '.join(row['rules']['unknown'])) + ' |')
    for row in result['jobs']:
        lines += ['', f"## {display(row['title'])} · {display(row['company'])}", '',
                  f"ID：`{row['job_id']}`；状态：{row['state']}；分数：{row['score'] if row['score'] is not None else '未评分'}", '',
                  f"来源：{display(row['source'])}；采集时间：{row['fetched_at']}", '',
                  '申请入口（未访问）：' + display(row['url']), '',
                  '规则排除：' + display('；'.join(row['rules']['reasons']) or '无'), '',
                  '待确认：' + display(', '.join(row['rules']['unknown']) or '无'), '']
        for evidence in row['rules']['evidence']:
            lines.append('- ' + display(evidence['reason']) + '；来源字段：' + display(json.dumps(evidence['job_fields'], ensure_ascii=False)))
        if row['error_code']:
            lines += ['', '错误/延期原因：' + row['error_code'], '']
        if 'analysis' in row:
            lines += ['| 职位要求原文（字段） | 用户资料原文（编号） | 判断 / 重要性 | 模型推断 |',
                      '| --- | --- | --- | --- |']
            for item in row['analysis']['assessments']:
                citations = '; '.join(c['fact_id'] + ': ' + c['quote'] for c in item['profile_evidence']) or '无已确认的对应证据'
                lines.append('| ' + display(item['job_field'] + ': ' + item['requirement_quote']) + ' | '
                             + display(citations) + ' | ' + item['status'] + ' / ' + item['importance']
                             + ' | ' + display(item['reason']) + ' |')
            lines += ['', '风险（模型推断）：' + display('；'.join(row['analysis']['risks']) or '模型未另列风险；仍需检查上述未知项'), '']
    # Sanitize arbitrary text while preserving generated IDs for CLI recovery.
    from .cli import display_result
    clean = display_result(result)
    outputs = {'json': json.dumps(clean, ensure_ascii=False, indent=2), 'md': '\n'.join(lines) + '\n'}
    for extension, content in outputs.items():
        for name in (stem, 'latest-matches'):
            target = directory / (name + '.' + extension)
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_text(content, encoding='utf-8')
            os.replace(temporary, target)
    return {'json': str(directory / (stem + '.json')), 'markdown': str(directory / (stem + '.md'))}
