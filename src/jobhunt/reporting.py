from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import quote

from .models import display, now
from .filters import job_kind, role_priority

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def link(url):
    # URLs are validated before storage; encode markdown delimiters.
    return "[原始链接](" + quote(url, safe=":/?=&%+@#~_-.") + ")"


def generate(ctx, store, run_id, stats, status):
    from .matching import cache_key
    from .models import Job
    time = datetime.now(SHANGHAI)
    from .timeutil import local_zone
    time = datetime.now(local_zone(ctx.settings.schedule_timezone))
    mode = "DEMO 虚构档案 / 测试运行" if ctx.demo else "本地个人资料 / 正式数据空间"
    rows = sorted(store.listing("PENDING_REVIEW"),
                  key=lambda row: role_priority(json.loads(row["payload"])["title"], ctx.preferences))
    lines = [f"# 求职日报 — {time:%Y-%m-%d}（{mode}）", "",
             f"运行：`{run_id}` · {ctx.settings.schedule_timezone} {time:%Y-%m-%d %H:%M:%S} · {status}", "",
             "由 JobHuntAgent 的 LangGraph 单 Agent 工作流生成。Codex 仅展示报告、调用命令。",
             "匹配方式：" + display(ctx.settings.matching_backend) + "；mock 是关键词模拟评分，不是 LLM 语义结论。",
             "",
             f"扫描：{stats['scanned']} · 新增：{stats['new']} · JD 更新：{stats['changed']} · 去重/已存在：{stats['duplicates']}",
             f"规则排除：{stats['filtered']} · 本轮分析：{stats['analyzed']} · 缓存复用：{stats['cached']} · 待分析：{stats['deferred']}",
             f"当前待审批：{len(rows)}（包括以前保留的待审批岗位；编号仅对应本报告）", "",
             "## 实际来源", ""]
    if not stats["sources"]:
        lines.append("本轮未扫描任何来源。")
    for item in stats["sources"]:
        lines.append(f"- {display(item['name'])}：{item.get('status', 'UNKNOWN')}；扫描 {item['scanned']}，有效 {item['valid']}；{display(item['attribution'])}")
    if not any(item['name'] == 'Remotive' and item['valid'] for item in stats['sources']):
        lines += ['', '本轮没有联网获取的真实来源；离线演示 / 用户导入资料待核实。']
    if ctx.preferences.review_notes:
        lines += ["", "## 本人确认的条件与人工复核", ""]
        lines += ["- " + display(note) for note in ctx.preferences.review_notes]
    lines += ["", "## 待审批清单", "",
              "| 编号 | 稳定岗位 ID | 公司 / 岗位 | 地点 | 分数 / 置信度 | 来源 / 链接 | 状态 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    details = []
    for i, row in enumerate(rows, 1):
        job = json.loads(row["payload"])
        facts = Job.model_validate(job)
        match = store.conn.execute("SELECT * FROM match_results WHERE cache_key=?",
                                   (cache_key(Job.model_validate(job), ctx),)).fetchone()
        if match is None:
            continue
        result = json.loads(match["result"])
        sources = store.conn.execute("SELECT DISTINCT source FROM job_sources WHERE job_id=? ORDER BY source", (row["job_id"],)).fetchall()
        source_names = ", ".join(x[0] for x in sources)
        tag = "DEMO 虚构岗位" if job["is_demo"] else ('用户导入，未联网验证' if job['source'].startswith('import:') else '来源记录，岗位当前有效性待核实')
        lines.append(f"| {i} | `{row['job_id']}` | {display(job['company'])} / {display(job['title'])} | {display(job['city'] or 'UNKNOWN')} | {result['score']} / {result['confidence']:.2f} | {display(source_names)} · {tag} · {link(job['url'])} | PENDING_REVIEW |")
        details += [f"### {i}. {display(job['title'])} — `{row['job_id']}`", "",
                    f"- 岗位优先级：{'次选' if role_priority(facts.title, ctx.preferences) == 2 else '同等优先'}；用工类型：{display(job_kind(facts) or '待核实')}",
                    f"- 薪资：{facts.salary_min if facts.salary_min is not None else '?'}–{facts.salary_max if facts.salary_max is not None else '?'} {display(facts.salary_currency)}/{display(facts.salary_period)}；不跨币种或日/月换算。",
                    f"- 到岗月份：{display(facts.start_month or '待核实')}；每周出勤：{facts.days_per_week if facts.days_per_week is not None else '待核实'}；要求至少实习月数：{facts.duration_months if facts.duration_months is not None else '待核实'}",
                    f"- 远程：{ {True: '是，地域与时区资格待核实', False: '否', None: '待核实'}[facts.remote] }；出差要求：{display(facts.travel or '待核实')}。具体安排由本人决定。",
                    f"- 理由：{display('；'.join(result['reasons']))}",
                    f"- 技能缺口：{display('、'.join(result['gaps']) or '未识别；不代表完全符合')}",
                    f"- 风险 / 待核实：{display('；'.join(result['risks']) or '仍需人工核实岗位有效性')}",
                    f"- 数据获取：{display(row['last_seen'])}；分析：{display(match['analyzed_at'])}；模型：{display(match['backend'])}", ""]
    if not rows:
        lines += ["", "当前没有可展示的高匹配待审批岗位。没有编造结果。"]
    lines += [""] + details + ["## 失败与受限项", ""]
    lines += ["- " + display(x) for x in stats["errors"]] or ["无。"]
    lines += ["", "## Token 与人工操作", ""]
    lines += [f"耗时：采集/解析 {stats.get('collection_seconds', 0):.4f}s；匹配 {stats.get('llm_seconds', 0):.4f}s；截至生成日报 {stats.get('total_seconds', 0):.4f}s。", '']
    if ctx.settings.matching_backend == 'deepseek':
        from .usage import UsageLedger
        ledger = UsageLedger(ctx.home, ctx.settings.schedule_timezone)
        for label, summary in [('本轮 API', ledger.summary(run_id=run_id)), ('今日 API', ledger.summary()), ('本月 API', ledger.summary('monthly'))]:
            lines += [f"{label}：请求 {summary['calls']}；输入 {summary['input_tokens']}；输出 {summary['output_tokens']}；用量未知 {summary['unknown_usage_calls']}；预算占用 {summary['budget_used']}；费用 {summary['cost'] if summary['cost'] is not None else '未知（未配置完整单价或 usage 不完整）'}。"]
        lines += ['失败或中断请求的未知 Token 以保守预留值占用预算；不会宣称实际费用为零。', '']
    usage = store.conn.execute("SELECT sum(input_tokens),sum(output_tokens),sum(cache_hit),sum(cost),sum(input_tokens IS NULL OR output_tokens IS NULL) FROM llm_usage WHERE run_id=?", (run_id,)).fetchone()
    lines += [f"本轮已知输入 Token：{usage[0] or 0}；已知输出 Token：{usage[1] or 0}；缓存记录：{usage[2] or 0}；usage 未知记录：{usage[4] or 0}。",
              "mock 未调用模型，Token 为 0；失败调用的未知 usage 保存为 NULL，不当作已测得 0。",
              "估算费用：" + (str(usage[3]) if usage[3] is not None else "未配置单价 / 无法估算") + "。", "",
              "按岗位 ID 批准、拒绝、收藏或暂缓。批准仅写入数据库；生成投递包仍需单独命令。",
              "所有渠道均为人工投递；程序不提交申请、不发送 HR 消息、不上传简历。",
              "人工包和确认记录：使用 `jobs list` 或 `jobs show JOB-ID` 查看当前状态。", "",
              "查看入口：在 Codex 中说“展示昨晚求职日报”，调用本项目 report latest。日报为运行时快照，审批后的实时状态以 SQLite 为准。", ""]
    content = "\n".join(lines)
    dated = ctx.reports / f"{time:%Y-%m-%d}_{run_id}.md"
    atomic_text(dated, content)
    atomic_text(ctx.reports / "latest.md", content)
    atomic_text(ctx.home / "reports" / "latest.md", content)
    return str(dated)
