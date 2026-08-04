# 2608B_iCloud

基于 Python 的 iCloud **Hide My Email（HME）隐私邮箱** 管理 + **邮件收发** 本地工具。

参考：[CO0kie-SH/2608A_iCloud](https://github.com/CO0kie-SH/2608A_iCloud)（油猴脚本版）。本仓库为多账户 CLI / 类 API 实现。

**Python：** `D:\0Code2\py312\python.exe`  
**最后更新：** 2026-08-04

---

## 硬性规矩（必须遵守）

> ### 每个账户，滚动 1 小时内，最多创建 5 个隐私邮箱（HME）

| 项 | 规定 |
|----|------|
| 范围 | **按账户分别计数**（001 / 002 互不影响） |
| 窗口 | **滚动 1 小时**（非自然整点） |
| 上限 | **5 个 / 小时 / 账户** |
| 实现 | 代码强制：`create_alias` 创建前检查，超限直接拒绝，不请求 Apple |
| 记录 | `db/aliases.db` → 表 `create_events` 记录每次成功创建的本地 UTC 时间 |
| 常量 | `tools/rate_limit.py` → `HME_CREATE_LIMIT_PER_HOUR = 5` |

**原因：** 短时间大量创建会触发 Apple 限流（如 `-41015`），严重时导致账户暂时不可用（如 002 曾因连创被风控）。**禁止绕过此限流。**

```bash
python main.py quota
python main.py quota -a user001@icloud.com
```

---

## 功能一览

| 模块 | 能力 |
|------|------|
| 多账户 | `accounts/*.txt` 三段式：`MAIL\|APPPWD\|COOKIE` |
| HME | 列表 / 生成 / 停用 / 恢复；本地 SQLite 落库 |
| 限流 | 1 小时 5 个创建硬限制 + 配额查询 |
| 邮件 | IMAP 收信、SMTP 发信、按 UID 取完整字典 |
| CLI | `main.py` 子命令；`generate_alias.bat` 快捷生成 |

---

## 目录结构

```text
2608B_iCloud/
├── main.py                     # CLI 入口
├── generate_alias.bat          # 生成别名（调 scripts/）
├── requirements.txt
├── .env / .env.example
├── README.md
├── accounts/                   # 每文件 = 一个主邮箱
│   ├── _example.txt.example
│   ├── user001@icloud.com.txt
│   └── user002@icloud.com.txt
├── db/
│   └── aliases.db              # aliases + create_events
├── scripts/
│   └── generate_alias.py       # 类 API 生成（含限流）
└── tools/
    ├── __init__.py
    ├── config.py               # .env → Settings
    ├── cookies.py              # Cookie 解析/必填键校验
    ├── accounts.py             # 三段式账户加载
    ├── client.py               # HME HTTP 客户端（Cookie）
    ├── hme.py                  # list / create_alias / on/off
    ├── db.py                   # SQLite + 配额
    ├── rate_limit.py           # 1h/5 常量与异常
    └── mail.py                 # IMAP/SMTP + get_mail_by_uid
```

---

## 环境准备

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

`requirements.txt`：

```text
python-dotenv>=1.0.0
requests>=2.31.0
```

### `.env`

```env
APP_NAME=2608B_iCloud
DEBUG=true
ICLOUD_DOMAIN=icloud.com
ACCOUNTS_FILES=accounts/
CLIENT_BUILD=2610Hotfix23
CLIENT_ID=37bd9669-50c3-4d52-af42-1d240d3ac4f3
```

### 账户文件（三段式）

路径：`accounts/<主邮箱>.txt`  
内容（**一行**）：

```text
MAIL|APPPWD|COOKIE
```

| 段 | 含义 | 用途 |
|----|------|------|
| MAIL | 主邮箱，如 `user001@icloud.com` | 标识 / IMAP 用户名 |
| APPPWD | Apple ID **App 专用密码** | IMAP / SMTP |
| COOKIE | 浏览器登录 icloud.com 后的完整 Cookie | HME Web API |

Cookie 常用键（缺则 HME 易失败）：

- `X-APPLE-WEBAUTH-TOKEN`
- `X-APPLE-WEBAUTH-USER`
- `X-APPLE-DS-WEB-SESSION-TOKEN`
- `X-APPLE-WEBAUTH-LOGIN`

**注意：** Cookie 会过期（HTTP 421）；失效后重新登录网页并更新第三段。  
**安全：** 勿把真实 Cookie / App 密码提交公开仓库。

### 前提条件

1. 账户需 **iCloud+**（或家庭共享 iCloud+）才能用 HME  
2. 家庭组成员需已接受共享且 HME 可用  
3. 建议别名后缀为 `@icloud.com`

---

## CLI 命令

统一：

```bash
D:\0Code2\py312\python.exe main.py <command> [options]
```

### 账户

```bash
python main.py accounts
```

### HME 隐私邮箱

```bash
# 列表（并同步 db）
python main.py list -a user001@icloud.com

# 生成（受 1小时5个 限制）
python main.py generate -a user001@icloud.com
python main.py generate -a user001@icloud.com -l MyLabel

# 配额
python main.py quota
python main.py quota -a user002@icloud.com

# 本地 db
python main.py db
python main.py db -a user001@icloud.com

# 停用 / 恢复转发
python main.py off <anonymousId> -a user001@icloud.com
python main.py on  <anonymousId> -a user001@icloud.com
```

快捷 bat：

```bat
generate_alias.bat
generate_alias.bat user001@icloud.com
generate_alias.bat user001@icloud.com MyLabel
```

### 邮件

```bash
# IMAP/SMTP 连通性
python main.py mail-probe -a user001@icloud.com

# 最近邮件（默认 INBOX）
python main.py mail-inbox -a user001@icloud.com -n 5
python main.py mail-inbox -a user001@icloud.com --box Junk --no-body

# 按 UID 取完整字典（JSON）
python main.py mail-get -a user001@icloud.com --uid 2
python main.py mail-get -a user001@icloud.com --uid 2 --all-boxes

# 发信
python main.py mail-send -a user001@icloud.com --to someone@example.com --subject 测试 --body hello
```

---

## 代码 API

### 生成 HME（必须带 account + db）

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
    alias = HMEService(client, db=db).create_alias(account=acc.name, label="ChatGPT")
    print(alias.hme, alias.anonymous_id, alias.label)
```

超限抛出：`tools.rate_limit.HMECreateRateLimitError`。

### 按主邮箱 + UID 取邮件字典

```python
from tools.mail import get_mail_by_uid

d = get_mail_by_uid("user001@icloud.com", 2)
# d["date"]          发送时间（邮件头 Date）
# d["internaldate"]  到达时间（服务器 INTERNALDATE）
# d["subject"] / d["from"] / d["to"] / d["body_text"] / d["body_html"]
```

### 邮件字典字段

| 字段 | 含义 |
|------|------|
| `account` | 主邮箱 |
| `mailbox` | 文件夹 INBOX / Junk / … |
| `uid` / `seq` | UID / 序号 |
| `flags` | 如 `\Seen` |
| `size` | 字节 |
| `date` / `date_parsed` | 发送时间 |
| `internaldate` | 服务器到达时间 |
| `from` / `to` / `cc` / `bcc` / `reply_to` | 地址 |
| `subject` / `message_id` | 主题 / Message-ID |
| `content_type` | MIME |
| `attachments` | 附件元数据 |
| `body_text` / `body_html` | 正文 |
| `body_text_len` / `body_html_len` | 长度 |

### Label 规则

- 默认：`Alias_` + 4 位随机 `[A-Z0-9]`（本地标签，非邮箱地址）
- 邮箱地址由 Apple `generate` 分配，本地不能指定
- 实测 label **至少支持 256 个 ASCII 字符**

---

## 数据库 `db/aliases.db`

### 表 `aliases`（别名清单）

| 字段 | 含义 |
|------|------|
| `account` | 账户名（主邮箱） |
| `hme` | 隐私邮箱地址 |
| `label` | 标签 |
| `anonymous_id` | Apple 侧 ID（on/off 用） |
| `is_active` | 是否转发 1/0 |
| `create_timestamp` | Apple 返回时间戳（毫秒，可空） |
| **`created_at`** | **本地首次写入 UTC** |
| `updated_at` | 本地最后更新 UTC |
| `note` / `source` | 备注 / 来源 generate\|list\|… |
| `raw_json` | API 快照 |

唯一约束：`(account, hme)`。  
`list` 同步只 upsert 别名，**不计入**创建限流。

### 表 `create_events`（创建事件 / 限流）

| 字段 | 含义 |
|------|------|
| `account` | 账户 |
| `hme` / `label` | 创建结果 |
| **`created_at`** | **成功创建本地 UTC 时间** |

限流：统计 `account` 在 `[now-1h, now]` 内事件数，≥5 拒绝。

---

## HME API（与油猴一致）

| 操作 | 路径 |
|------|------|
| 校验 / 取 webservice | `POST setup.icloud.com/setup/ws/1/validate` |
| 列表 | `GET /v2/hme/list` |
| 生成候选 | `POST /v1/hme/generate` |
| 保留 | `POST /v1/hme/reserve` |
| 停用 / 恢复 | `POST /v1/hme/deactivate` · `reactivate` |

客户端参数（`.env` 可改）：`CLIENT_BUILD`、`CLIENT_ID`。

---

## 邮件协议

| 协议 | 主机 | 端口 | 认证 |
|------|------|------|------|
| IMAP | `imap.mail.me.com` | 993 SSL | 主邮箱 + App 专用密码 |
| SMTP | `smtp.mail.me.com` | 587 STARTTLS | 同上 |

常见文件夹：`INBOX`、`Junk`、`Archive`、`Deleted Messages`、`Sent Messages`、`Drafts`。

---

## 当前账户状态（2026-08-04 实测）

| 账户 | Cookie | 邮件 | HME 列表 |
|------|--------|------|----------|
| `user001@icloud.com` | OK | IMAP/SMTP OK，INBOX 有信 | **8** 个，均 ON |
| `user002@icloud.com` | OK（已更新） | IMAP/SMTP OK，INBOX 有信 | **5** 个，均 **OFF**（待恢复） |

待办（约 1 小时后再做，注意限流）：

- [ ] 将 002 的 5 个别名 `reactivate` 全部打开并同步 db  
- [ ] 清理测试用超长 `A…` label（可选）  
- [ ] 按需扩展：验证码提取、按别名过滤邮件、附件下载等  

---

## tools 模块速查

| 文件 | 职责 |
|------|------|
| `config.py` | `load_settings()` |
| `accounts.py` | `Account`、`load_all_accounts`、`find_account` |
| `cookies.py` | Cookie 解析、必填键 |
| `client.py` | `ICloudHMEClient` |
| `hme.py` | `HMEService.create_alias(account=…)` 强制限流 |
| `db.py` | `AliasDB`：upsert、create_events、`assert_can_create` |
| `rate_limit.py` | `HME_CREATE_LIMIT_PER_HOUR`、`HMECreateRateLimitError` |
| `mail.py` | `MailMessageParser`、`ICloudMailClient`、`MailService`、`get_mail_by_uid` |

---

## 安全

- `accounts/*.txt`、`.env`、`db/*.db` 含敏感信息，请加入 `.gitignore`
- Cookie / App 专用密码仅本地使用，勿外传
- HME 请求仅发往 `icloud.com` / `icloud.com.cn`

---

## 一小时后再开工检查清单

```bash
python main.py accounts
python main.py quota
python main.py list -a user001@icloud.com
python main.py list -a user002@icloud.com
python main.py mail-probe -a user001@icloud.com
python main.py mail-probe -a user002@icloud.com
```

若 Cookie 421：重新登录 icloud.com，更新对应 `accounts/*.txt` 第三段后再测。
