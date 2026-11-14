# 岗位读取扩展

1. 在 `chrome://extensions` 开启开发者模式，点击「加载已解压的扩展程序」，选择本目录。
2. 打开扩展的「设置导出文件夹」，授权 Chrome 将 JSON 写入你选择的目录。可选项目里的 `job-exports/`，需要先创建该目录。
3. 在 BOSS 直聘、前程无忧或猎聘打开职位列表或详情页。点击「导出当前页」读取当前页面，或点击「批量补读并导出」补齐当前列表中已加载岗位的描述。首次批量读取时按浏览器提示授权访问对应平台。

批量读取按岗位编号核对详情。遇到登录、验证提示或手动停止时，保留已读取结果。导出 JSON 中的 `jobs` 含完整描述，`needs_jd` 记录待补描述的岗位。可从项目根目录用 `python -m jobhunt v2 import <文件路径> --match rules` 导入，或通过 `python -m jobhunt v2 desktop` 打开的 Windows 窗口导入。

## HR 回复草稿演示

从 Chrome 扩展卡片复制 ID，在项目根目录启动服务，把命令中的 `扩展ID` 换成复制的值。

```text
python -m jobhunt v2 hr-reply-serve --demo --extension-id 扩展ID
```

打开 `http://127.0.0.1:56876/demo/chat`，在扩展的「连接本机 Agent」输入终端显示的配对码。选择模拟岗位并点「监听模拟会话」，然后在网页触发一条模拟消息，在扩展中查看草稿。正式模式由用户手动提供消息，通过已配置的 DeepSeek API 生成可编辑草稿。

## 本地验证

安装 `requirements-browser.lock` 并准备 Chrome，在项目根目录执行。测试使用虚构岗位和模拟网页。

```text
python -X utf8 scripts/check_boss_extension.py
python -X utf8 scripts/check_51job_extension.py
python -X utf8 scripts/check_51job_batch.py
python -X utf8 scripts/check_liepin_extension.py
python -X utf8 scripts/check_liepin_batch.py
python -X utf8 scripts/check_boss_hr_reply.py
```
