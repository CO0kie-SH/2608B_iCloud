# 2608B_iCloud

基于 Python 的 iCloud **Hide My Email（HME）隐私邮箱** 管理 + **邮件收发** 本地工具。

参考：[CO0kie-SH/2608A_iCloud](https://github.com/CO0kie-SH/2608A_iCloud)（油猴脚本版）。本仓库为多账户 CLI / 类 API 实现。

| 项 | 值 |
|----|-----|
| **版本** | **26.8.11** |
| **Python** | `D:\0Code2\py312\python.exe`（或本机 Python 3.11+） |
| **最后更新** | 2026-08-11 |

---

## 版本 26.8.11 变更摘要

| 模块 | 变更 |
|------|------|
| 账户格式 | 从 `accounts/*.txt` 全面改为 **YAML**（`.yml` / `.yaml`），一文件一账户 |
| 多 provider | 支持 `apple` + `163mail` + `inbox`；可缩减；扁平旧写法仍兼容 |
| CDK 标签 | 创建 HME 默认 `CDK_<sha256>`；安全随机按 OS 切换；DB 映射 CDK→隐私邮箱+母号 |
| 限流 | 仍强制 **1 小时 / 账户 / 最多 5 个**；`create_events` + `quota` |
| Cookie 采集 | **Camoufox 有头登录** → 写回 `apple.cookie`（2FA 在浏览器完成） |
| Camoufox | 二进制仅装到项目 `browsers/camoufox`；`camoufox-fetch` / `camoufox-path` |
| 邮件 | 多 provider IMAP/SMTP；163 登录后发 IMAP `ID`；`type` / `summary` / `code` |
| CLI | 新增 `cookie-login`、`camoufox-*`、`cdk`、`quota` 等 |
| 安全 | `.gitignore`：`accounts/*`、`db/`、`browsers/`、`logs/`、`.env` |

**升级注意：** 旧 `.txt` 账户请按 `accounts/_example.yaml.example` 迁到 YAML；Cookie 421 用 `cookie-login` 重采。

---

## 硬性规矩（必须遵守）

> ### 每个账户，滚动 1 小时内，最多创建 5 个隐私邮箱（HME）

| 项 | 规定 |
|----|------|
| 范围 | **按账户分别计数**（互不影响） |
| 窗口 | **滚动 1 小时**（非自然整点） |
| 上限 | **5 个 / 小时 / 账户** |
| 实现 | 代码强制：`create_alias` 创建前检查，超限直接拒绝 |
| 记录 | 表 `create_events` 记录每次成功创建的本地 UTC 时间 |
| 常量 | `tools/rate_limit.py` → `HME_CREATE_LIMIT_PER_HOUR = 5` |

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
| Cookie 采集 | Camoufox **有头**登录 iCloud → 写回 `apple.cookie`（2FA 在浏览器完成） |
| CLI | `main.py` 子命令；`generate_alias.bat` |

---

## 目录结构

```text
2608B_iCloud/
├── main.py
├── generate_alias.bat
├── requirements.txt
├── .env / .env.example          # 本地密钥，勿提交
├── README.md
├── accounts/                    # 每文件 = 一个账户（勿提交真实 cookie）
│   └── _example.yaml.example
├── browsers/
│   └── camoufox/                # Camoufox 二进制（gitignore，本机 fetch）
├── logs/                        # 采集过程日志（gitignore）
├── db/
│   └── aliases.db               # 本地库（勿提交）
├── scripts/
│   └── generate_alias.py
└── tools/
    ├── config.py                # .env → Settings
    ├── cookies.py
    ├── accounts.py              # YAML 账户
    ├── logging_setup.py         # 控制台 + 文件日志
    ├── camoufox_runtime.py      # 项目内 browsers/camoufox
    ├── cookie_capture.py        # 有头采 cookie + 写回
    ├── client.py                # HME HTTP（Cookie）
    ├── hme.py                   # list / create_alias / on/off + CDK 标签
    ├── secure_random.py         # 跨平台安全随机
    ├── db.py                    # SQLite：CDK 映射 / 配额
    ├── rate_limit.py            # 1h/5
    └── mail.py                  # IMAP/SMTP + get_mail_by_uid
```

---

## 环境准备

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

依赖：`python-dotenv`、`requests`、`PyYAML`、`camoufox[geoip]`。

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
```

1. 弹出 **有头** Camoufox 窗口，打开 iCloud。  
2. **你在浏览器里**完成 Apple 登录。  
3. 若出现手机验证码 / 双重认证：**在浏览器页面输入**，不要在终端输验证码。  
4. 脚本轮询 Cookie；必填键齐全后写回账户 YAML（`apple.cookie` 或根级 `cookie`），并生成 `.bak`。  
5. 过程写入 `logs/cookie-login-...log`（脱敏，不含完整 cookie / 验证码）。

```bash
python main.py accounts          # 确认 hme_ok=True
```

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
| `body_text` / `body_html` | 正文 |
| `flags` / `size` / `attachments` | 标志 / 大小 / 附件元数据 |

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

文件夹：iCloud 常用 `INBOX` / `Junk` / …；163 另有中文箱名（`已发送` 等），`INBOX` 通用。

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
| `mail.py` | 多 provider 邮件客户端（apple/163）+ `type`/`summary`/`code` |

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
```

Cookie 421 → `python main.py cookie-login -a <账户>`（或手工改 YAML 的 `apple.cookie` / `cookie`）。
