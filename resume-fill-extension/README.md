# 通用简历填写扩展

1. 在 Chrome 120 或更新版本打开 `chrome://extensions`，开启开发者模式，点击「加载已解压的扩展程序」，选择本目录。
2. 打开扩展的「资料卡设置」，导入 JSON 并确认保存。初次体验可用 [`fixtures/profile.demo.json`](fixtures/profile.demo.json) 中的虚构资料。录入真实资料时，在仓库外另存一份文件，参照样例的字段结构填写。
3. 打开要填写的页面，先按资料卡数量新增并展开对应的经历区块。若网站分步骤，处理当前页面可见的区块。
4. 点击「识别并预览」。核对条目和字段，可调整顺序、取消字段或手动纠正映射。确认后点击「填写选中的空白项」。
5. 检查网页结果，由本人保存或提交。扩展识别到自动保存提示时，仅提供预览和复制。

已有值会保留。数量不匹配、边界不清或条目无法唯一对应时，相关区块会停在预览。支持文本、多行文本、月份和可明确匹配的标准下拉。学校自动补全、联动城市、自制日期控件、附件与嵌入表单需要手动处理。

资料卡及保存的字段映射留在当前 Chrome 的本地存储中，可以在扩展设置里清除。

## 本地验证

从项目根目录运行。浏览器测试需要 `requirements-browser.lock` 中的依赖，以及本机 Chrome 或用 `python -m playwright install chromium` 安装的 Chromium。测试使用临时浏览器配置和虚构资料。

```text
node --test resume-fill-extension/tests/plan.test.cjs
python -X utf8 resume-fill-extension/tests/check_resume_fill_extension.py
```
