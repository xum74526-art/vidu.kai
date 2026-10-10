# 0.4.0 本机安装验收

验收日期：2026-10-10。环境：Windows、Codex 本机插件宿主、Python 3.12、uv、系统安装的 Edge。服务包版本为 0.9.0。

## 已实际验证

| 项目 | 结果与证据 |
| --- | --- |
| 安装到 Codex | `codex plugin list --json` 返回 `yingban-vidu@yingban-local`，版本 0.4.0，installed/enabled 均为 true |
| 宿主解析的启动命令 | `scripts/install_local.py` 使用 `codex mcp list` 的真实配置启动，发现 9 个工具；热启动约 4.36 秒 |
| 解压包首次启动 | 使用 ZIP 解压目录中的 `mcp.json`、全新虚拟环境和全新账号目录启动成功 |
| 已安装包首次连接 | 使用 Codex 缓存中的 0.4.0 包、全新账号目录启动成功；整个检查约 45.25 秒 |
| 自动登录环境 | MCP 调用自动创建独立浏览器及本机助手，无需先运行开发服务器、分配 CDP 端口或复制 Token |
| 调用结束后的登录窗口 | stdio 客户端退出后，本机助手仍能交换面板票据并返回官网画面 |
| 真实官网 | `https://www.vidu.com` 的当前登录页可见，包含谷歌、苹果、邮箱登录入口；已人工检查返回的截图 |
| 插件图标与运行时 | manifest 引用的 PNG 图标、技能、锁定依赖及面板资源均在包内；内部延迟导入检查通过 |
| 包内容 | 本机包 40 个文件，不含本机账号、浏览器配置、数据库、测试夹具或 demo provider |

官网已将部分旧版登录弹窗替换为 `/zh/login` 页面，本版同时识别其可见邮箱登录入口。检测到页面 DOM 后还会等待画面绘制；加载动画不能作为可用登录界面的验收证据。

## 回归与复现

Python 回归、15 项 JavaScript 测试、Ruff 和安装包检查已通过。浏览器池的隔离、恢复、并发上限、进程互斥，以及认证失败不创建浏览器的场景由隔离测试覆盖。这些测试不证明真实支付或视频生成完成。

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests scripts
node --test sdk/yingban-client.test.mjs sdk/yingban-mobile.test.mjs sdk/panel-input.test.mjs
uv run python -X utf8 scripts/build_plugin.py
uv run python -X utf8 scripts/check_plugin.py
uv run python -X utf8 scripts/install_local.py
uv run python -X utf8 scripts/verify_plugin_install.py --plugin <解压包或已安装插件的绝对路径>
```

最后一个脚本为每次检查创建全新账号目录，连接真实官网，只读取未登录的画面。它不输入登录信息、不授权、不购买、不生成视频；结束时关闭自己的临时助手。报告和截图保存在 `.local`，不会进入 Git 或发布包。

如果 `yingban-local` 已指向另一份旧源码，先检查 `codex plugin marketplace list --json`，再用 Codex 的 marketplace remove/add 命令更换来源。无需删除账号目录。已有聊天的插件进程需要重新加载，才能使用新版本。

## 尚未验收与适用范围

- 本次没有完成一个新账号的真实登录与授权，也没有执行真实支付、到账核验或视频生成。
- Codex 桌面 GUI 中的完整连续对话未做自动化验收；已完成的是宿主安装记录、宿主解析的 MCP 启动命令及真实登录面板协议检查。
- Codex 本机包提供账号与视频工具；套餐购买由自建智能体使用服务包的 HTTP/MCP 和官方交互面板。浏览器方式的支付到账自动核验仍未完成。
- 没有域名或服务器，本次未部署公网服务，也未发布到 Codex 公共插件目录。其他用户可安装源码包，但不能把本机地址当作远程服务地址。
- 测试没有支付或生成消费。付款完成、余额变化、登录成功均不会直接触发视频提交，继续生成仍需用户确认。

## 构建校验值

| 文件 | SHA-256 |
| --- | --- |
| `yingban-vidu-0.4.0.zip` | `2fa53e74728615e0df0ad715d3617926561468875e7f5fa65117160350b0fddf` |
| `yingban-selfhost-0.9.0.zip` | `2dc22f8480d670dc892f1326115dd684ba480261d3d613ac471662bae0dc2b17` |

校验值对应本次本机构建产物。重新构建 ZIP 时文件时间戳可能变化，应使用该次构建生成的 `.sha256` 文件核对。
