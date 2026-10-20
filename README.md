# HTML 发布服务

把本机静态 HTML 发布到公开的 GitHub Pages 网址。公网仓库是 [jamiepak/html-publish-hub](https://github.com/jamiepak/html-publish-hub)，站点首页是 <https://jamiepak.github.io/html-publish-hub/>。

## 首次使用

1. 在 GitHub 的 [细粒度 Token 页面](https://github.com/settings/personal-access-tokens/new) 创建 Token。Repository access 只选 `html-publish-hub`；Repository permissions 中把 **Contents** 设为 **Read and write**。无需把 Token 发给 Codex 或保存在本地文件。
2. 双击 `启动发布服务.command`，或在此目录运行 `/usr/bin/python3 -B publisher.py`。浏览器会打开 <http://127.0.0.1:8788/>。
3. 在本机页面粘贴 Token 并连接。Token 仅放在进程内存里；关闭运行窗口即清除，下次启动需重新输入。

## 每次发布

1. 选一个 HTML 文件，或选一个包含 `index.html` 及 CSS、JS、图片等资源的目录。
2. 填标题和英文网址名，生成预览。检查展示的每个文件、目标公网网址和路径警告。
3. 勾选“可以公开”，点击发布。再次用相同网址名发布会更新同一网址。
4. 点击“检查公网部署”，直到显示线上入口文件与本次提交一致。发布后关闭本机服务，公网网页仍可访问。

页面地址形如 `https://jamiepak.github.io/html-publish-hub/sites/product-demo/`。站点首页列出已发布页面。文件进入公开仓库的 `gh-pages` 分支，任何人都能查看或下载；请勿放入客户资料、Token、密钥或其他不该公开的内容。预览中的简单检查不代替人工审阅。

此工具只发布静态文件，不能运行 Python、Node 服务或后端 API。单次最多 100 个文件、合计 25 MB；目录根部需有 `index.html`。包含本机绝对地址或 `/` 开头资源路径的页面可能无法在项目站点正常显示。

## 本机安全与实现

- 服务只监听 `127.0.0.1:8788`；写操作校验 `Origin` 和随机会话令牌。
- 每次发布先在本机暂存，再经用户确认提交；不会扫描未选中的目录。
- 使用 GitHub Git Data API 在 `gh-pages` 分支生成单个提交，分支更新不强制覆盖并发变更。
- 发布状态会读取公网入口文件并核对 SHA-256。这个检查确认入口文件已经上线，不代替逐一测试页面的所有交互和资源。

运行本地检查：`/usr/bin/python3 -B -m unittest -v test_publisher.py`。
