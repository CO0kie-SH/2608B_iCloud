# 2608B_iCloud

基于 Python 的 iCloud **Hide My Email（HME）** 管理 + **邮件收发** 工具。

参考项目：[CO0kie-SH/2608A_iCloud](https://github.com/CO0kie-SH/2608A_iCloud)（浏览器油猴脚本版），本仓库为多账户、可脚本化的本地 CLI 实现。

---

## 今日完成内容（2026-08-04）

### 1. 项目骨架
- 入口 `main.py` + `.env` / `.env.example`
- 依赖：`python-dotenv`、`requests`
- 运行环境：`D:\0Code2\py312\python.exe`

### 2. 多账户认证（三段式）
- 账户目录：`accounts/`
- **文件名 = 主邮箱**，例如：`user001@icloud.com.txt`
- **文件内容一行三段**：`MAIL|APPPWD|COOKIE`
  - `MAIL`：iCloud 主邮箱
  - `APPPWD`：Apple ID App 专用密码（用于 IMAP/SMTP）
  - `COOKIE`：浏览器登录 icloud.com 后的完整 Cookie（用于 HME API）

### 3. Hide My Email（HME）
- 通过 Cookie 调用 iCloud Web API（对齐油猴脚本逻辑）
- 功能：列表 / 生成别名 / 停用 / 恢复
- 本地落库：`db/aliases.db`（SQLite）

### 4. 邮件收发（IMAP/SMTP）
- IMAP：`imap.mail.me.com:993`（收信）
- SMTP：`smtp.mail.me.com:587` STARTTLS（发信）
- 支持读取：UID、FLAGS、发送时间 `Date`、到达时间 `INTERNALDATE`、正文、附件元数据等
- 类化实现 + 便捷入口：`get_mail_by_uid(主邮箱, uid) -> dict`

### 5. 实测结果（节选）
- HME 生成成功：`sheaves.sud-0c@icloud.com`、`honker-toed-2b@icloud.com`
- IMAP/SMTP 探测：均 OK
- 按 UID 拉取 ChatGPT 验证码邮件、iCloud 欢迎邮件成功

---

## 目录结构

```text
2608B_iCloud/
├── main.py                 # CLI 入口
├── requirements.txt
├── .env / .env.example
├── accounts/               # 多账户（每文件一个主邮箱）
│   ├── _example.txt.example
│   └── user001@icloud.com.txt
├── db/
│   └── aliases.db          # HME 别名本地库
└── tools/
    ├── config.py           # 配置
    ├── cookies.py          # Cookie 解析/校验
    ├── accounts.py         # 账户加载（三段式）
    ├── client.py           # iCloud HME HTTP 客户端
    ├── hme.py              # HME 业务（list/generate/on/off）
    ├── db.py               # SQLite 持久化
    └── mail.py             # 邮件类：解析器 / 客户端 / 服务
```

---

## 环境准备

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

### `.env` 关键项

```env
APP_NAME=2608B_iCloud
DEBUG=true
ICLOUD_DOMAIN=icloud.com
ACCOUNTS_FILES=accounts/
CLIENT_BUILD=2610Hotfix23
CLIENT_ID=37bd9669-50c3-4d52-af42-1d240d3ac4f3
```

### 账户文件示例

`accounts/user001@icloud.com.txt`：

```text
user001@icloud.com|xxxx-xxxx-xxxx-xxxx|X-APPLE-WEBAUTH-TOKEN=...; X-APPLE-WEBAUTH-USER=...; ...
```

**注意：**
1. HME 需要 **iCloud+**，且建议使用 `@icloud.com` 后缀别名
2. Cookie 会过期，失效后需重新登录网页并更新第三段
3. App 专用密码在 [appleid.apple.com](https://appleid.apple.com) 生成
4. 不要把真实 Cookie / App 密码提交到公开仓库

---

## CLI 用法

```bash
# 查看本地账户
python main.py accounts

# HME
python main.py list -a user001@icloud.com
python main.py generate -a user001@icloud.com
python main.py db
python main.py off <anonymousId> -a user001@icloud.com
python main.py on  <anonymousId> -a user001@icloud.com

# 邮件
python main.py mail-probe -a user001@icloud.com
python main.py mail-inbox -n 10 --box INBOX
python main.py mail-get -a user001@icloud.com --uid 2
python main.py mail-send --to someone@example.com --subject 测试 --body hello
```

---

## 代码调用示例

### 按主邮箱 + UID 取邮件字典

```python
from tools.mail import get_mail_by_uid

d = get_mail_by_uid("user001@icloud.com", 2)
# d["date"]          发送时间（邮件头 Date）
# d["internaldate"]  到达时间（服务器 INTERNALDATE）
# d["subject"] / d["from"] / d["to"]
# d["body_text"] / d["body_html"]
```

### 返回字典字段（归纳格式）

| 字段 | 含义 |
|------|------|
| `account` | 主邮箱 |
| `mailbox` | 文件夹（INBOX / Junk / ...） |
| `uid` / `seq` | UID / 序号 |
| `flags` | 如 `\Seen` |
| `size` | 字节大小 |
| `date` / `date_parsed` | 发送时间 |
| `internaldate` | 服务器到达时间 |
| `from` / `to` / `cc` / `bcc` / `reply_to` | 地址头 |
| `subject` / `message_id` | 主题 / Message-ID |
| `content_type` | MIME 类型 |
| `attachments` | 附件元数据列表 |
| `body_text` / `body_html` | 正文 |
| `body_text_len` / `body_html_len` | 正文长度 |

### HME 生成别名

```python
from tools.accounts import load_all_accounts, find_account
from tools.client import ICloudHMEClient
from tools.config import load_settings
from tools.hme import HMEService

settings = load_settings()
_, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
acc = find_account(accounts, "user001@icloud.com")

with ICloudHMEClient(settings, acc.cookies) as client:
    alias = HMEService(client).create_alias()
    print(alias.hme, alias.anonymous_id)
```

---

## tools 模块说明

| 模块 | 说明 |
|------|------|
| `tools/config.py` | 读取 `.env`，提供 `Settings` |
| `tools/accounts.py` | 解析 `accounts/*.txt` 三段式，`Account` 数据类 |
| `tools/cookies.py` | Cookie 键校验（TOKEN / USER / SESSION 等） |
| `tools/client.py` | HME：`validate` 取 API base + `call_api` |
| `tools/hme.py` | list / generate+reserve / deactivate / reactivate |
| `tools/db.py` | `AliasDB`：别名 upsert / 查询 |
| `tools/mail.py` | `MailMessageParser`、`ICloudMailClient`、`MailService`、`get_mail_by_uid` |

### 邮件类职责

```text
MailMessageParser   解析 FETCH 元数据 + MIME → 标准 dict
ICloudMailClient    单账户 IMAP/SMTP 操作
MailService         按主邮箱从 accounts/ 取凭证
get_mail_by_uid()   对外便捷函数：mail + uid → dict
```

---

## iCloud 文件夹对照

| 名称 | 含义 |
|------|------|
| `INBOX` | 收件箱 |
| `Junk` | 垃圾邮件 |
| `Archive` | 归档 |
| `Deleted Messages` | 已删除 |
| `Sent Messages` | 已发送 |
| `Drafts` | 草稿 |

`mail-inbox` 默认只读 `INBOX`；`mail-get --all-boxes` 可在全部文件夹中按 UID 查找。

---

## HME API（与油猴脚本一致）

| 操作 | 路径 |
|------|------|
| 校验/取 webservice | `POST setup.icloud.com/setup/ws/1/validate` |
| 列表 | `GET /v2/hme/list` |
| 生成 | `POST /v1/hme/generate` |
| 保留 | `POST /v1/hme/reserve` |
| 停用/恢复 | `POST /v1/hme/deactivate` / `reactivate` |

---

## 安全提示

- `accounts/*.txt`、`.env` 含敏感凭证，请加入 `.gitignore`
- Cookie 与 App 专用密码仅保存在本地，勿外传
- HME 请求仅发往 `icloud.com` / `icloud.com.cn`

---

## 后续可扩展

- [ ] Cookie 过期自动检测与提示刷新
- [ ] 邮件按 HME 别名过滤 / 验证码自动提取
- [ ] 多账户批量生成别名与轮询收信
- [ ] 删除别名、改标签等更多 HME 接口
- [ ] 附件下载到本地
