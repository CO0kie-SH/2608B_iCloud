# 2608B_iCloud - AI 协作上下文

> 操作此仓库前先读本文件。完成有意义的变更后，在底部追加进度记录。

## 如何使用本文件

本文件是项目级指令：优先级低于用户本轮直接指令，高于通用默认习惯。

## 默认规则

### 工作原则

- 非微小改动先说明方案、范围、风险和验收标准。
- 需求模糊、风险高或影响大时，先澄清再编码。
- 优先小步迭代；实现与验证分开做。
- 优先遵循现有模块与命名，不引入用不上的抽象。

### 用户协作公约

- 先用业务语言复述要解决的问题，再谈实现。
- 不要求用户提供字段名、接口路径或模块名，除非仓库里确实推不出来。
- 结束时说明改了什么、如何验证、还有什么风险。

### 编码约束

- 新代码与注释跟当前文件语言走：`web/`、`tools/` 现有中文注释可延续，不要混进开发过程词。
- 用稳定模块名和路径定位代码，不依赖易漂移的行号。
- 不为尚未需要的需求提前加配置或扩展点。

### 质量与验证

- 影响运行时行为的改动必须跑相关测试。
- 任何“已完成”都要附命令或手工检查步骤。
- 无法验证时写明原因、风险和未覆盖范围。

### 禁止事项

- 不要把 `.env`、账户 YAML、Cookie、SQLite、浏览器 profile、验证码导出写进 git。
- 不要把真实工作台密码写进 README、示例或会被提交的文件。
- 不要给注册机取码接口 `/api/v1/code` 加登录墙。
- 不要绕过 HME 总量 740、每小时 5 个、13–15 分钟间隔。
- 不要使用 `/init`。
- commit message 和 PR 正文不要写 AI 工具名，也不要写 FIXED / Step / Phase 这类过程词。

## 项目概览

iCloud Hide My Email（HME）隐私邮箱池 + 邮件收取。CLI 在 `main.py`，Web 工作台是 FastAPI + Jinja2，默认端口 `8770`。

| 项 | 值 |
|----|-----|
| 版本 | 26.9.9A |
| Python | 3.11+ |
| Web | FastAPI / Jinja2 / SQLite |
| 仓库 | https://github.com/CO0kie-SH/2608B_iCloud |

## 仓库结构

- `main.py`：CLI 与 `web` 子命令
- `web/app.py`：FastAPI 入口、页面、登录墙挂载
- `web/auth.py`：工作台账号密码会话
- `web/routers/`：业务 API
- `tools/`：账户、HME、邮件、配额、Cookie
- `accounts/`：一文件一账户，仅 `_example.yaml.example` 可提交
- `tests/`：unittest
- `pack.py` / `push.py` / `upgrade.py`：打包、推送、离线升级

## 运行和构建命令

```powershell
python -m unittest tests.test_web_auth tests.test_claims -v
python main.py web --host 127.0.0.1 --port 8770
start_web.bat
```

## 鉴权约定

- 用户名固定 `lws`、`mhw`；密码只在服务器 `.env`：`AUTH_PASSWORD_LWS` / `AUTH_PASSWORD_MHW`
- `AUTH_SESSION_SECRET` 必填；cookie 名 `icloud_web_session`
- 公开：`/login`、`/logout`、`/static/`、`GET /api/health`、`GET /api/v1/code`
- HTML 未登录 302 到 `/login`；`/api/` 未登录 401 `unauthorized`
- HTTPS 反代设 `AUTH_COOKIE_SECURE=true`；只有可信反代才开 `AUTH_TRUST_PROXY=true`
- 生产不要开 `AUTH_DISABLED`

## 当前产品状态

工作台默认强制登录，可部署到服务器。领取取码接口仍走 token。生产配额与免费套餐拦截保持原规则。

## 进度记录

| 日期 | 范围 | 完成内容与验证摘要 |
|------|------|--------------------|
| 2026-09-09 | Web 登录 / 发版 | 工作台会话登录（lws/mhw）、文档与 GitHub Release 26.9.9A；`python -m unittest tests.test_web_auth tests.test_claims -v` 11 项通过 |
