# 2608B_iCloud

基于 Python 的 iCloud **Hide My Email（HME）隐私邮箱** 管理 + **邮件收发** 本地工具。

参考：[CO0kie-SH/2608A_iCloud](https://github.com/CO0kie-SH/2608A_iCloud)（油猴脚本版）。本仓库为多账户 CLI / 类 API 实现。

| 项 | 值 |
|----|-----|
| **版本** | **26.8.15** |
| **Python** | `D:\0Code2\py312\python.exe`（或本机 Python 3.11+） |
| **最后更新** | 2026-08-15 |

---

## 版本 26.8.15 变更摘要

| 模块 | 变更 |
|------|------|
| 生产间隔 | 老接口除每小时 5 个外，成功后再随机冷却 13–15 分钟；`produce_at` / `next_produce_at` 以 unix 时间入库存 |
| curl 客户端 | `produce.bat` / `produce.sh` 只打 HTTP；`--forever` 无限跑；撞配额按 `retry_after` 睡 |
| Cookie 失效标 | 421 / 缺 cookie 写入 `account_flags`；再生产立刻 `409 COOKIE_INVALID`；`--all` 跳过；`cookie-login` 成功摘标 |
| 区域 | `--region cn` / `--suffix cn` → `icloud.com.cn`；先登录 iCloud 再进 `/settings/` |
| cookie-login | `--debug` 按页面内容变化落盘；`--keep-open` 挂窗；`--appleid` 登录后再打开设置页 |
| 会话复用 | 实验性质：`db/cookie/<账户>.json` 保存并在下次注入浏览器会话 |
| Web 设置 | 顶栏设置框，仅前端时区偏移，默认 UTC+8 |
| 新增账户 | README 补充 003 接入步骤 |

**升级注意：** 首次启动会给 `create_events` 补 unix 时间列，并建 `account_flags`。旧 YAML 仍可用。中国区号生产时 Web/CLI 需 `--region cn`，或先把 cookie 收成 `X-APPLE-*` 再打国际站 setup。

---

## 版本 26.8.13 变更摘要

| 模块 | 变更 |
|------|------|
| WebUI | 新增邮箱池三栏界面、邮件分类、正文详情和后台增量收信 |
| 生产页 | `/production` 可按 iCloud 账户选择数量与线程，查看实时及历史任务 |
| 接口选择 | 当前提供“旧版接口（每小时5个）”；“新接口（每小时20个）”保留为禁用选项 |
| 并发限流 | SQLite 事务原子占位；多线程、多客户端共用每账户滚动一小时配额 |
| 多端同步 | 客户端打开时登记并获取中央状态，自动触发去重后的全账户收信任务 |
| 任务持久化 | 生产任务、进度、结果与客户端心跳写入 SQLite，服务重启后历史仍可查询 |
| 163 收件 | 解析 IMAP modified UTF-7，自动识别并选择 163 的“垃圾邮件”目录 |
| 邮件正文 | HTML-only 邮件自动生成可读文本，详情页仅显示有内容的文本/HTML视图 |
| 邮件元数据 | 增加真实发件地址、代发判断、Return-Path、收件别名和附件信息 |

**升级注意：** 版本仍兼容 26.8.11 的 YAML；首次启动会自动扩展 `db/aliases.db`。Cookie 421 用 `cookie-login` 重采。

---

## 硬性规矩（必须遵守）

> ### 每个账户，滚动 1 小时内最多 5 个；两次生产至少间隔 13–15 分钟

| 项 | 规定 |
|----|------|
| 范围 | **按账户分别计数**（互不影响） |
| 窗口 | **滚动 1 小时**（非自然整点） |
| 上限 | **5 个 / 小时 / 账户** |
| 间隔 | **`3600/5+1 = 13` 分钟起**，成功后随机落到 **[13, 15] 分钟** |
| 实现 | `create_alias` 创建前通过 SQLite 事务原子占位；失败释放，成功记账 |
| 记录 | 表 `create_events`：`created_at` + unix `produce_at` / `next_produce_at` |
| 并发 | Web 线程和多个浏览器客户端共用数据库配额，不按客户端分别计数 |
| 常量 | `tools/rate_limit.py` → `HME_CREATE_LIMIT_PER_HOUR = 5`，`HME_CREATE_MIN/MAX_INTERVAL_MINUTES = 13/15` |
| Cookie 失效 | HTTP 421 / 缺 cookie 会写入 `account_flags.cookie_invalid`；再生产立刻 `409 COOKIE_INVALID`，`--all` 跳过。`cookie-login` 成功后自动摘标 |

**原因：** 短时间大量创建会触发 Apple 限流（如 `-41015`），严重时导致账户暂时不可用。**禁止绕过。**

```bash
python main.py quota
python main.py quota -a user001@icloud.com
```

---

## 功能一览

| 模块 | 能力 |
|------|------|
| 多账户 | `accounts/*.yml`/`.yaml`：`mail` + `apple` / `163mail` / `inbox`（可缩减） |
| HME | 列表 / 生成 / 停用 / 恢复；本地 SQLite |
| **CDK** | 标签 `CDK_<sha256>`；**CDK → 隐私邮箱 + 母号** 查询 |
| 限流 | 1 小时 5 个创建硬限制 + 配额查询 |
| 安全随机 | 按 OS 切换：Linux `getrandom`/`urandom`，Windows/macOS `secrets` |
| 邮件 | IMAP/SMTP；`type`/`summary`/`code`；按 UID 取 JSON |
| WebUI | 邮箱池、邮件分类、文本/HTML详情、后台增量同步 |
| 生产 | 按账号控制数量和线程；任务进度与结果持久化 |
| 多客户端 | 打开即同步；生产历史、配额和邮件状态以服务器 SQLite 为准 |
| Cookie 采集 | Camoufox **有头**登录 iCloud → 写回 `apple.cookie`（2FA 在浏览器完成） |
| CLI | `main.py` 子命令；`generate_alias.bat`；`start_web.bat`；`cookie_login.bat`；`produce.bat` / `produce.sh` |

---

## 目录结构

```text
2608B_iCloud/
├── main.py
├── generate_alias.bat
├── start_web.bat
├── cookie_login.bat                # 有头登录 / debug / 会话复用
├── produce.bat / produce.sh        # 独立 curl 生产客户端（不参与风控）
├── requirements.txt
├── .env / .env.example          # 本地密钥，勿提交
├── README.md
├── accounts/                    # 每文件 = 一个账户（勿提交真实 cookie）
│   └── _example.yaml.example
├── browsers/
│   └── camoufox/                # Camoufox 二进制（gitignore，本机 fetch）
├── logs/                        # 采集过程日志（gitignore）
├── db/
│   ├── aliases.db               # 本地库（勿提交）
│   └── cookie/                  # 实验性质浏览器会话（勿提交）
├── scripts/
│   ├── generate_alias.py
│   └── produce_json.js          # produce.bat 用的 JSON 小助手
├── web/
│   ├── __init__.py
│   ├── app.py                  # FastAPI 应用
│   ├── jobs.py                 # 收信/生产后台任务
│   ├── routers/                # Web API
│   ├── static/                 # 邮箱池、生产页、设置
│   └── templates/              # HTML 页面
└── tools/
    ├── config.py                # .env → Settings；区域 cn/us
    ├── cookies.py
    ├── accounts.py              # YAML 账户
    ├── logging_setup.py         # 控制台 + 文件日志
    ├── camoufox_runtime.py      # 项目内 browsers/camoufox
    ├── cookie_capture.py        # 有头采 cookie + debug 落盘
    ├── session_store.py         # db/cookie 会话保存/注入
    ├── client.py                # HME HTTP（Cookie）
    ├── hme.py                   # list / create_alias / on/off + CDK 标签
    ├── secure_random.py         # 跨平台安全随机
    ├── db.py                    # SQLite：CDK / 配额 / 任务 / 失效标
    ├── rate_limit.py            # 1h/5 + 13–15 分钟间隔
    ├── mail.py                  # IMAP/SMTP + MIME/目录解析
    ├── mail_sync.py             # 增量收信入库
    └── production.py            # 多线程 HME 生产流水线
```

---

## 环境准备

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

依赖：`python-dotenv`、`requests`、`PyYAML`、`camoufox[geoip]`、`FastAPI`、`Uvicorn`、`Jinja2`、`Pydantic`。

### Camoufox（项目内二进制）

浏览器**只**安装到 `browsers/camoufox`（可用 `CAMOUFOX_DIR` 改），**不用**全局用户 cache。

下载走 **curl + 代理**（默认 `http://127.0.0.1:7897`，可用 `CAMOUFOX_PROXY` / `HTTPS_PROXY` 改；`none` 关闭）。  
解析 release 优先 GitHub 网页（绕过 API 限额）；大 zip 不走易 SSL 失败的纯 requests。

```bash
# .env 示例
# CAMOUFOX_DIR=browsers/camoufox
# CAMOUFOX_PROXY=http://127.0.0.1:7897

python main.py camoufox-fetch    # 下载/更新到项目目录（约 400MB+）
python main.py camoufox-path     # 查看路径与是否已安装
```

> 探测安装状态**只扫本地文件**，不会调用会 `cleanup` 删目录的官方 `pkgman.install`。
### Cookie 有头采集（含 2FA）

Cookie 过期（如 HTTP 421）时：

```bash
python main.py cookie-login -a user001@icloud.com
# 可选：--timeout 600  --url https://www.icloud.com/
# 采完先不关浏览器，再挂 120 秒：
python main.py cookie-login -a maohongwei003@icloud.com --keep-open 120
# 打开 Apple 账户页，复用 db/cookie 会话，并继续扒页面 / App 专用密码：
python main.py cookie-login -a maohongwei003@icloud.com --appleid --debug --keep-open 1200
# 中国区：打开 www.icloud.com.cn
python main.py cookie-login -a maohongwei003@icloud.com --region cn --debug --keep-open 1200
# 等价写法：
python main.py cookie-login -a maohongwei003@icloud.com --suffix cn
```

1. 弹出 **有头** Camoufox 窗口，打开 iCloud。  
2. **你在浏览器里**完成 Apple 登录。  
3. 若出现手机验证码 / 双重认证：**在浏览器页面输入**，不要在终端输验证码。  
4. 脚本轮询 Cookie；必填键齐全后写回账户 YAML（`apple.cookie` 或根级 `cookie`），并生成 `.bak`。  
5. `--keep-open SEC`：写回成功后浏览器再开 SEC 秒，到点再关；默认 `0` 立刻关。  
6. `--debug`：页面**内容一变**就落盘（同一 URL 的弹窗/iframe 也会再采），目录 `logs/page-debug-<账户>-<时间>/`。扫到 `xxxx-xxxx-xxxx-xxxx` 形态的 App 专用密码会写回 YAML 的 `apple.app_password`，给 IMAP 用。  
7. `--appleid`：先在 iCloud 登录（`--region cn` → `https://www.icloud.com.cn/`），必填 cookie 齐后再打开设置页 `https://www.icloud.com.cn/settings/`（debug 里第一次看到 Apple 账户邮箱的页面）。没有 `appleid.apple.com.cn`。  
8. 实验性质会话复用：浏览器 cookie 写入 `db/cookie/<账户>.json`，下次 `cookie-login` 自动注入；`--debug` 时每次变化再记一份 `db/cookie/<账户>/<时间>-<host>.json`。`--no-reuse-session` 可关掉。  
7. 过程写入 `logs/cookie-login-...log`（脱敏，不含完整 cookie / 验证码）。

```bash
python main.py accounts          # 确认 hme_ok=True
```

### iCloud 区域 / 网址后缀

默认 `.env` 的 `ICLOUD_DOMAIN=icloud.com`（国际站）。命令行可临时改后缀，**不改文件**：

| 参数 | 例 | 打开的域名 |
|------|----|------------|
| `--region cn` | 中国区预选 | `icloud.com.cn` |
| `--suffix cn` / `--suffix com.cn` | 改后缀 | `icloud.com.cn` |
| `--region us` / `--suffix com` | 国际站 | `icloud.com` |

```bat
python main.py cookie-login -a maohongwei003@icloud.com --region cn --debug --keep-open 1200
python main.py generate -a maohongwei003@icloud.com --region cn
python main.py web --region cn
```

`cn` 和 `com.cn` 都落成 `icloud.com.cn`。收信 IMAP 仍按邮箱地址域名走（icloud.com / 163.com），不受这个开关影响。

> 本期仅有头模式。无头 + 自动 2FA 后续再做；日志里的 `stage=2fa_challenge` 供后续自动化衔接。

### `.env`

```env
APP_NAME=2608B_iCloud
DEBUG=true
ICLOUD_DOMAIN=icloud.com
ACCOUNTS_FILES=accounts/
CLIENT_BUILD=2610Hotfix23
CLIENT_ID=37bd9669-50c3-4d52-af42-1d240d3ac4f3
```

### 账户文件（YAML）

`accounts/<主标识>.yml` 或 `.yaml`，一文件一账户。模板见 `accounts/_example.yaml.example`。

**完整（多 provider）**

```yaml
mail: user001@icloud.com

apple:
  appleid: user001@xxx.com
  app_password: xxxx-xxxx-xxxx-xxxx
  cookie: "X-APPLE-WEBAUTH-TOKEN=...; X-APPLE-WEBAUTH-USER=...; ..."

163mail:
  mail: user001@163.com
  imap: abcdefg          # 163 客户端授权码

inbox:
  mail: user001@163.com  # 默认收件用哪套邮箱
```

**缩减（只要 iCloud HME + 邮件）**

```yaml
mail: user001@icloud.com
apple:
  appleid: user001@icloud.com
  app_password: xxxx-xxxx-xxxx-xxxx
  cookie: "X-APPLE-...; ..."
```

**扁平缩减（兼容旧写法，等价于只有 apple）**

```yaml
mail: user001@icloud.com
app_password: xxxx-xxxx-xxxx-xxxx
cookie: "X-APPLE-...; ..."
```

| 字段 | 含义 | 用途 |
|------|------|------|
| `mail` | 账户主标识（母号） | 文件名建议一致；HME/DB 账户名 |
| `apple.appleid` | Apple ID 登录邮箱 | 可与 `mail` 不同 |
| `apple.app_password` | App 专用密码 | iCloud IMAP/SMTP |
| `apple.cookie` | icloud.com Cookie | HME Web API |
| `163mail.mail` / `imap` | 163 邮箱 + 授权码 | 后续多邮箱收信（已解析入库） |
| `inbox.mail` | 默认收件邮箱 | 指向某一 provider 的 `mail` |

- 用不到的块**整段删掉**即可（缩减格式）。
- 后续可同样增加 `qqmail` / `gmail` 等块（`*mail` 或登记 provider 名）。
- `cookie` 含 `:` `;` `=` 时**建议双引号**。  
  必填键：`X-APPLE-WEBAUTH-TOKEN`、`X-APPLE-WEBAUTH-USER`、`X-APPLE-DS-WEB-SESSION-TOKEN`、`X-APPLE-WEBAUTH-LOGIN`。

**前提（HME）：** iCloud+（或家庭共享）；Cookie 过期会 HTTP 421，更新 `apple.cookie`。

> 说明：`resolve_inbox()` 选中的 provider 会映射到对应主机（apple→iCloud，163mail→网易）。网易 IMAP 登录后会发 `ID` 命令，避免 Unsafe Login。

### 新增账户（示例：003）

现有号是 `maohongwei001` / `002` / `004`。加 **003** 不用改代码，丢一个 YAML 再采 cookie 即可。Web 会按文件 mtime 自动重载，**不必为加号重启服务**。

**1. 复制模板**

```bat
copy accounts\_example.yaml.example accounts\maohongwei003@icloud.com.yaml
```

**2. 先写成最小可用稿**（cookie 先留空，下一步有头登录会写回去）

```yaml
mail: maohongwei003@icloud.com
apple:
  appleid: maohongwei003@icloud.com
  app_password: xxxx-xxxx-xxxx-xxxx
  cookie: ""
```

| 必填 | 填什么 |
|------|--------|
| 文件名 | 与 `mail` 一致：`accounts/maohongwei003@icloud.com.yaml` |
| `mail` | 母号 / 账户名，生产、配额、打标都按这个认 |
| `apple.appleid` | 真正用来登 iCloud 的 Apple ID，可以和 `mail` 不同 |
| `apple.app_password` | [appleid.apple.com](https://appleid.apple.com) 生成的 App 专用密码；只要收信就填 |
| `apple.cookie` | 先空着，`cookie-login` 会写回 |

只要 HME、暂不收信：`app_password` 可以先不填。要 163 收信就再加 `163mail` / `inbox` 块，格式同上。

**3. 有头采集 Cookie（含 2FA）**

```bat
python main.py cookie-login -a maohongwei003@icloud.com
python main.py cookie-login -a maohongwei003@icloud.com --keep-open 180
```

弹出 Camoufox 后在浏览器里登录；验证码也在页面里输。必填 Cookie 齐了会写回 YAML，并清掉该号的 `COOKIE_INVALID` 标记。`--keep-open 180` 表示写回后再把窗口挂 180 秒，方便核对登录态。中国区加 `--region cn`（或 `--suffix cn` / `--suffix com.cn`），打开的是 `https://www.icloud.com.cn`。

**4. 确认进池子**

```bat
python main.py accounts
python main.py quota -a maohongwei003@icloud.com
produce.bat --list
```

`hme_ok=True`、`cookie_invalid=false` 才能生产。然后：

```bat
produce.bat -a maohongwei003@icloud.com
produce.bat --all --forever
```

`--all` 会带上 003。老规矩照旧：每号每小时 5 个，间隔 13–15 分钟。

**5. Cookie 又 421 了**

```bat
python main.py cookie-login -a maohongwei003@icloud.com
```

失效号会被打标，再生产立刻 `409 COOKIE_INVALID`，`--all` 自动跳过。

---

## 启动 WebUI

```powershell
& 'D:\0Code2\py312\python.exe' main.py web --host 127.0.0.1 --port 8770
```

也可以直接运行：

```bat
start_web.bat
start_web.bat 8771
```

| 页面 | 地址 | 用途 |
|------|------|------|
| 邮箱池 | `http://127.0.0.1:8770/` | 账户、隐私邮箱、分类邮件与正文详情 |
| 生产 | `http://127.0.0.1:8770/production` | 按 iCloud 账户生产 HME、查看配额和任务 |
| API 文档 | `http://127.0.0.1:8770/api/docs` | OpenAPI 交互文档 |

生产页参数：

| 参数 | 当前规则 |
|------|----------|
| iCloud 账号 | 精确到单个账户，配额互相独立 |
| 生产接口 | `legacy`：旧版接口（每小时5个，间隔13–15分钟） |
| 生产数量 | 旧版接口单次只能 `1` 个；间隔未到时剩余为 0 |
| 并发线程 | `1-5`；每个线程使用独立 Apple 会话 |

页面打开后会调用 `POST /api/client-sync/open`：登记当前客户端、读取生产任务与配额快照，并自动提交一次全账户增量收信。多个客户端同时打开时，相同范围的运行中收信任务会去重。

> 多端共享的前提是所有客户端访问同一个服务器实例和同一份 `db/aliases.db`。SQLite 适合单服务器多客户端；部署多个独立服务实例时应使用共享数据库或只运行一个写入实例。

### Web API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/accounts` | 账户、provider、邮件数和创建配额 |
| `GET` | `/api/aliases` | 本地隐私邮箱池 |
| `POST` | `/api/aliases/refresh` | 从 iCloud 同步某账户的别名 |
| `GET` | `/api/mails` | 邮件元数据列表 |
| `GET` | `/api/mails/{account}/{mailbox}/{uid}` | 实时拉取单封正文 |
| `POST` | `/api/sync` | 提交后台增量收信任务 |
| `GET` | `/api/sync/{job_id}` | 查询收信任务 |
| `GET` | `/api/production/options` | 生产接口、账户和配额 |
| `POST` | `/api/production` | 提交生产任务 |
| `GET` | `/api/production/jobs` | 查询持久化生产历史 |
| `GET` | `/api/production/jobs/{job_id}` | 查询一个生产任务 |
| `POST` | `/api/client-sync/open` | 客户端打开同步快照与自动收信 |

独立 curl 客户端（不参与风控，配额仍由 Python 服务执行）：

```bat
produce.bat --list
produce.bat -a user001@icloud.com
produce.bat --all --loop 30
produce.bat --all --forever
```

```bash
chmod +x produce.sh
./produce.sh --list
./produce.sh -a user001@icloud.com
./produce.sh --all --loop 30
./produce.sh --all --forever
```

提交一个旧版生产任务：

```powershell
$body = @{
  account = 'user001@icloud.com'
  interface = 'legacy'
  count = 1
  threads = 1
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8770/api/production' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $body
```

---

## CDK 标签与数据库映射（核心）

### 标签格式

```text
CDK_<sha256_hex>
```

| 项 | 说明 |
|----|------|
| 前缀 | 固定 `CDK_` |
| 随机源 | `secure_random_bytes(32)`（按系统切换） |
| 摘要 | SHA256 → 默认 **64** 位 hex |
| 总长 | 4 + 64 = **68**（Apple label 上限实测 ≥256） |
| 可截断 | `--cdk-hex-len 16/32/64` |

示例：

```text
CDK_d8c2311bd0f31a293161e8878bf9cfdebc5a539feabe8c4b011b4c07645a6402
```

### 安全随机（按系统）

| 系统 | 优先接口 |
|------|----------|
| Linux | `os.getrandom` → `/dev/urandom` → `secrets` |
| Windows | `secrets.token_bytes`（BCrypt/CryptGenRandom） |
| macOS | `secrets.token_bytes` |

```python
from tools.secure_random import random_backend_info, secure_random_bytes
from tools.hme import generate_cdk_label

random_backend_info()   # {'system','backend','platform'}
generate_cdk_label()    # CDK_ + 64hex
```

### 键值关系

```text
CDK_xxx  →  {
  hme:          隐私邮箱（如 xxx@icloud.com）
  parent_mail:  母号（主邮箱）
  anonymous_id: Apple 侧 ID
  is_active:    是否转发
  ...
}
```

### 数据库表 `aliases`

| 字段 | 含义 |
|------|------|
| **`cdk`** | 业务键（唯一索引；历史非 CDK 标签可为空） |
| **`parent_mail`** | 母号 |
| **`hme`** | 隐私邮箱 |
| `account` | 账户名（通常同母号） |
| `label` | Apple 侧标签（新号与 cdk 一致） |
| `anonymous_id` | 停用/恢复用 |
| `is_active` | 1/0 |
| `create_timestamp` | Apple 时间戳 |
| `created_at` / `updated_at` | 本地 UTC |
| `note` / `source` / `raw_json` | 备注 / 来源 / API 快照 |

唯一约束：`(account, hme)`；**CDK 部分唯一索引**（非空唯一）。

### 表 `create_events`（限流）

记录每次成功创建：`account`、`parent_mail`、`hme`、`cdk`、`label`、`created_at`。

并发与多端相关表：

| 表 | 用途 |
|----|------|
| `create_claims` | 创建前原子占位；防止多线程同时越过每小时上限 |
| `production_jobs` | 持久化生产任务、进度、结果和错误 |
| `client_sync_state` | 记录多客户端最近打开时间 |
| `mails` | 邮件元数据和分类结果；正文不落库 |
| `mail_sync_state` | 每账户、每目录的增量 UID 水位 |

### CDK 查询 API

```python
from tools.db import AliasDB
from tools.config import load_settings

db = AliasDB(load_settings().base_dir / "db" / "aliases.db")

db.resolve_cdk("CDK_xxx")
# {
#   "cdk": "CDK_xxx",
#   "hme": "alias@icloud.com",
#   "parent_mail": "user001@icloud.com",
#   "mapping": {"cdk","hme","parent_mail"},
#   "anonymous_id": "...",
#   "is_active": true,
#   ...
# }

db.get_by_cdk("CDK_xxx")           # AliasRecord | None
db.list_by_parent("user001@...")   # 母号下全部别名
db.get_by_hme("alias@icloud.com")
db.cdk_map()                       # {CDK: {hme, parent_mail, ...}}
db.cdk_map(parent_mail="user001@icloud.com")
```

CLI：

```bash
python main.py cdk --cdk CDK_xxx
python main.py cdk --list-map
python main.py cdk --list-map -a user001@icloud.com
python main.py db
```

---

## CLI 命令

```bash
D:\0Code2\py312\python.exe main.py <command> [options]
```

### 账户 / 配额 / DB

```bash
python main.py accounts
python main.py quota
python main.py db
python main.py cdk --cdk CDK_xxx
python main.py cdk --list-map
```

### HME

```bash
python main.py list -a user001@icloud.com
python main.py generate -a user001@icloud.com              # 默认 CDK_ 标签
python main.py generate -a user001@icloud.com --cdk-hex-len 32
python main.py generate -a user001@icloud.com -l MyLabel   # 自定义标签（无 CDK 映射）
python main.py off <anonymousId> -a user001@icloud.com
python main.py on  <anonymousId> -a user001@icloud.com
```

快捷：

```bat
generate_alias.bat
generate_alias.bat user001@icloud.com
```

### 邮件

```bash
python main.py mail-probe -a user001@icloud.com
python main.py mail-inbox -a user001@icloud.com -n 5
python main.py mail-inbox -a user001@icloud.com --box Junk --no-body
python main.py mail-get -a user001@icloud.com --uid 2
python main.py mail-send -a user001@icloud.com --to someone@example.com --subject t --body hi
```

---

## 邮件 JSON 字段

`get_mail_by_uid(mail, uid)` / `mail-get` 返回字典，核心字段：

| 字段 | 含义 |
|------|------|
| `account` / `mailbox` / `uid` | 账户 / 文件夹 / UID |
| `date` / `internaldate` | 发送时间 / **服务器到达时间** |
| `from` / `to` / `subject` | 地址与主题 |
| **`type`** | 类型：`code` / `welcome` / `invite` / `other`… |
| **`summary`** | 摘要（验证码邮件如 `验证码 253708（…）`） |
| **`code`** | 解析出的验证码（非 code 为空） |
| `body_text` / `body_html` | 可读文本 / 原始 HTML；HTML-only 邮件会自动生成 `body_text` |
| `flags` / `size` / `attachments` | 标志 / 大小 / 附件元数据 |
| `from_addr` / `sender_addr` | 展示发件地址 / 实际代发地址 |
| `return_path_addr` | 信封退信地址（邮件投递失败时使用） |

```python
from tools.mail import get_mail_by_uid
d = get_mail_by_uid("user001@icloud.com", 2)
print(d["type"], d["summary"], d.get("code"))
```

---

## 生成 HME（代码）

```python
from tools.accounts import find_account, load_all_accounts
from tools.client import ICloudHMEClient
from tools.config import load_settings
from tools.db import AliasDB
from tools.hme import HMEService

settings = load_settings()
_, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
acc = find_account(accounts, "user001@icloud.com")
db = AliasDB(settings.base_dir / "db" / "aliases.db")

with ICloudHMEClient(settings, acc.cookies) as client:
    # 必须 account + db（限流）；默认 label = CDK_...
    alias = HMEService(client, db=db).create_alias(account=acc.name)
    print(alias.hme, alias.label)
    print(db.resolve_cdk(alias.label))
```

超限：`HMECreateRateLimitError`。

---

## HME API（与油猴一致）

| 操作 | 路径 |
|------|------|
| 校验 / webservice | `POST setup.icloud.com/setup/ws/1/validate` |
| 列表 | `GET /v2/hme/list` |
| 生成 | `POST /v1/hme/generate` |
| 保留 | `POST /v1/hme/reserve` |
| 停用 / 恢复 | `POST /v1/hme/deactivate` · `reactivate` |

---

## 邮件协议（多 provider）

由 `Account.resolve_inbox()` 决定用哪套凭证，再映射到服务器：

| provider | IMAP | SMTP | 备注 |
|----------|------|------|------|
| `apple` / `icloud` | `imap.mail.me.com:993` SSL | `smtp.mail.me.com:587` STARTTLS | App 专用密码 |
| `163mail` / `163` | `imap.163.com:993` SSL | `smtp.163.com:465` SSL（失败回退 `587` STARTTLS） | 客户端授权码；LOGIN 后发 IMAP `ID`（防 Unsafe Login） |

YAML 示例（163 作默认收件）：

```yaml
mail: user@icloud.com
apple:
  appleid: user@163.com
  cookie: "..."
163mail:
  mail: user@163.com
  imap: 授权码
inbox:
  mail: user@163.com   # 可选；不写则优先 apple，否则第一个 ready 的 provider
```

文件夹：iCloud 常用 `INBOX` / `Junk` / …；163 的“垃圾邮件”等中文目录通过 IMAP modified UTF-7 返回。程序会解析 `LIST`、按 `\Junk` 或中文名识别目录，并在 `SELECT` 时使用真实编码箱名；数据库和 Web API 统一保存逻辑名 `Junk`。

---

## tools 速查

| 文件 | 职责 |
|------|------|
| `config.py` | `load_settings()` |
| `accounts.py` | `Account` / `MailProvider`、多 provider YAML |
| `logging_setup.py` | 采集日志 |
| `camoufox_runtime.py` | 项目内 Camoufox 路径 / fetch |
| `cookie_capture.py` | 有头登录采 cookie、写回 YAML |
| `client.py` | `ICloudHMEClient` |
| `hme.py` | `HMEService`、`generate_cdk_label` |
| `secure_random.py` | 跨平台 `secure_random_bytes` |
| `db.py` | `AliasDB`：CDK 映射、配额、`resolve_cdk` |
| `rate_limit.py` | `HME_CREATE_LIMIT_PER_HOUR=5` |
| `mail.py` | 多 provider 邮件客户端、UTF-7、MIME、`type`/`summary`/`code` |
| `mail_sync.py` | IMAP 增量同步、分类与元数据入库 |
| `production.py` | 按账户多线程生产 HME |
| `web/` | FastAPI WebUI、生产页、后台任务和多端同步 API |

---

## 安全

- **勿提交：** `.env`、`accounts/*`（除 example）、`db/*.db`
- 已配置 `.gitignore` 忽略上述路径
- Cookie / App 密码仅本地使用
- 文档示例使用 `user001@icloud.com` 等占位符，不含真实账号

---

## 常用检查清单

```bash
python main.py accounts
python main.py camoufox-path
python main.py cookie-login -a user001@icloud.com
python main.py quota
python main.py list -a user001@icloud.com
python main.py cdk --list-map
python main.py mail-probe -a user001@icloud.com
python main.py mail-inbox -a user001@icloud.com -n 3 --no-body
python main.py web --port 8770
```

Cookie 421 → `python main.py cookie-login -a <账户>`（或手工改 YAML 的 `apple.cookie` / `cookie`）。
