# DuckDuckGo browser

这是项目内 DuckDuckGo Chrome 扩展的 Patchright 启动入口。它使用项目内 Chromium、独立
持久化资料目录和 `.env` 中的 `PATCHRIGHT_PROXY`，默认加载
`browsers/extensions/duckduckgo/active`。

## Quick start

在项目根目录执行：

```powershell
# 有头打开 DuckDuckGo 首页，关闭窗口结束进程
D:\0Code2\py312\python.exe -m duck.duck_browser

# 打开任意页面
D:\0Code2\py312\python.exe -m duck.duck_browser https://mayips.com/

# 打开当前网页对应的隐私面板，面板会带上原网页 tabId
D:\0Code2\py312\python.exe -m duck.duck_browser https://mayips.com/ --popup

# 打开扩展设置页
D:\0Code2\py312\python.exe -m duck.duck_browser --options
```

`--popup` 和 `--options` 只能选择一个。浏览器会复用
`db/browser_profiles/duckduckgo`；需要另一套会话时使用 `--profile-dir PATH`。

## 对照和检查

```powershell
# 完全不加载扩展，使用独立 profile 做页面/网络对照
D:\0Code2\py312\python.exe -m duck.duck_browser https://mayips.com/ --no-extension --profile-dir db/browser_profiles/duckduckgo-no-extension

# 无头加载后退出，适合连通性检查或 CI
D:\0Code2\py312\python.exe -m duck.duck_browser https://mayips.com/ --headless --check

# 临时覆盖 .env 代理；支持 http、https、socks5、socks5h
D:\0Code2\py312\python.exe -m duck.duck_browser --proxy socks5://HOST:PORT --headless --check
```

`--no-extension` 会同时使用 `--disable-extensions` 和专用 profile，避免已有持久化会话
重新加载扩展。没有 `--check` 时，有头会话会一直运行到窗口关闭；`--headless` 必须配合
`--check`。

扩展运行期间持有更新锁。出现 `Extension is in use` 时，先关闭之前由项目脚本启动的
DuckDuckGo 窗口，再运行本入口或 `bat/update_duckduckgo_extension.bat`。脚本在启动时
动态获取扩展 ID；更换安装路径后 ID 可能变化，不应把测试日志中的 ID 固定在业务代码里。
启动日志只显示代理是否启用。页面网络错误会返回错误类型或 `net::ERR_*`，可用
`--proxy direct` 临时对照，原 `.env` 配置保持不变。

## 扩展功能

工具栏 DuckDuckGo 图标打开站点隐私面板，可查看跟踪器拦截、HTTPS 升级和站点保护状态；
面板中的 `Report Broken Site` 可提交匿名故障反馈。设置页用于调整保护和反馈选项。
扩展还包含 Cookie 弹窗处理、Autofill 和 DuckDuckGo 搜索提供商设置。代理、浏览器资料和
页面生命周期由 Patchright 启动器负责，扩展不改变项目的 iCloud API 代理逻辑。

## 使用邮件保护

```powershell
D:\0Code2\py312\python.exe -m duck.duck_browser https://duckduckgo.com/email/
```

1. 在已加载扩展的窗口中，按页面提示开通或登录 Email Protection，并确认转发邮箱。
2. 个人 Duck Address 是固定的 `名称@duck.com` 地址；Private Duck Address 是为网站生成的随机别名。
3. 使用 `Generate Private Duck Address` 生成地址，或在支持的网页邮箱输入框中使用扩展的 Autofill。
4. 邮件经 DuckDuckGo 处理邮件跟踪器后转发到你配置的邮箱，在原邮箱中收件。

本入口只负责浏览器与扩展启动；邮件账号登录、地址生成仍由你在页面上操作，没有接入
本项目的 iCloud 生产池。持久化资料会保留登录状态，并可能包含敏感会话信息。

## 参考实现

- 官方扩展：[duckduckgo/duckduckgo-privacy-extension](https://github.com/duckduckgo/duckduckgo-privacy-extension)
- [官方 Playwright 启动夹具](https://github.com/duckduckgo/duckduckgo-privacy-extension/blob/2026.8.24/integration-test/helpers/playwrightHarness.js)：使用持久化上下文加载扩展。
- [官方 Service Worker 辅助代码](https://github.com/duckduckgo/duckduckgo-privacy-extension/blob/2026.8.24/integration-test/helpers/playwrightHelpers.js)：通过后台 Worker 操作扩展。
- 社区邮件保护客户端：[Lanshuns/Qwacky](https://github.com/Lanshuns/Qwacky)
- 社区 Raycast 别名客户端：[Hugo-Persson/raycast-duckduckgo-email](https://github.com/Hugo-Persson/raycast-duckduckgo-email)

Qwacky 提供独立的邮件保护扩展、别名管理和 Autofill；Raycast 示例要求已有 DuckDuckGo
邮件账号及本地凭证，再生成别名。这些是第三方实现参考，并非 DuckDuckGo 官方 Python SDK。
本次仅阅读了这些项目的说明，没有安装它们或验证其 API。会话凭证应保存在本机忽略路径，
避免出现在提交、日志或截图中。
