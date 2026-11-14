# JobHuntAgent

本地求职工作台。用 Python、LangGraph 和 SQLite 导入岗位、按条件筛选、查看匹配证据并准备申请材料。两个独立的 Chrome 扩展分别负责岗位导出和简历表单辅助填写。

| 组件 | 用途 |
| --- | --- |
| Python CLI / Windows 岗位评分窗口 | 导入 CSV、JSON，按本地规则或可选的 DeepSeek 模型匹配，查看排名和生成待人工处理的材料。 |
| [岗位读取扩展](browser-extension/README.md) | 从当前 Chrome 页面导出 BOSS 直聘、前程无忧、猎聘岗位，供工作台导入。附本机 HR 回复草稿入口。 |
| [通用简历填写扩展](resume-fill-extension/README.md) | 导入本地资料卡，预览字段映射，经确认后填写当前表单的空白项。 |

## 本地演示

需要 Python 3.12 或更高版本。以下命令在项目根目录运行。DEMO 使用虚构资料和独立的 SQLite 文件，无需账号或 API Key。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m jobhunt doctor --demo
.\.venv\Scripts\python.exe -m jobhunt v2 import examples/v2-jobs.demo.csv --match rules --demo
.\.venv\Scripts\python.exe -m jobhunt v2 match --demo
.\.venv\Scripts\python.exe -m jobhunt v2 rankings --demo
```

`v2 match --demo` 使用本地模拟分数演示排名。Windows 可用 `python -m jobhunt v2 desktop --demo` 打开岗位评分窗口，选择同一份 CSV 导入。命令示例中的 `python` 指虚拟环境的解释器，在 macOS 或 Linux 上路径为 `.venv/bin/python`。

```text
python -m jobhunt nightly --demo --dry-run
python -m jobhunt report --latest --demo
python -m jobhunt v2 create --job examples/v2-job.demo.json --demo
python -m jobhunt v2 list --demo
```

## 使用本地资料

1. 将 `profile/basic_profile.example.yaml` 和 `profile/career_preferences.example.yaml` 复制到原目录，去掉文件名里的 `.example`。填写已核实的经历和求职偏好。
2. 将 `profile/resumes/summary.example.txt` 复制为 `profile/resumes/summary.txt`，填写经过核对的文字摘要。联系方式、证件和简历原件请单独保存。
3. 执行 `python -m jobhunt doctor`。规则匹配无需 API Key。需要语义匹配时，将 `.env.example` 复制为 `.env` 并在本地填写自己的 `DEEPSEEK_API_KEY`。语义匹配会把岗位文本和必要的脱敏资料发给模型服务。
4. 把扩展导出的 JSON 保存到本地 `job-exports/`，然后执行 `python -m jobhunt v2 import job-exports/岗位文件.json --match rules` 查看规则筛选结果。语义匹配用 `--match semantic`，评分后可用 `v2 rankings` 查看排名。`v2 prepare-manual --job-id <岗位ID>` 生成供本人核对的材料。

`approve` 记录本地审批。真实岗位的申请和 HR 消息由使用者在招聘页面提交。申请和消息的自动执行演示使用 `--demo` 与本机模拟网页。

`.gitignore` 已排除本地档案、密钥、数据库、报告、浏览器会话和常见岗位导出文件。简历填写扩展把导入的资料保存在当前 Chrome 的本地存储中，可在设置页清除。公开仓库中的 `examples/` 和扩展测试样例均使用虚构数据。

## 测试

```text
python -m unittest discover -s tests
node --test resume-fill-extension/tests/plan.test.cjs
```

浏览器测试需要 Chrome 和 `requirements-browser.lock` 中的可选依赖。运行方法见两个扩展的说明。Windows UIA 实验另需安装 `requirements-uia.lock`。
