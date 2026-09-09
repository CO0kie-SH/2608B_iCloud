# 收件别名解析算法

从邮件元数据中识别实际投递的注册邮箱，保留 `+tag`。这不是邮箱地址生成算法。

本目录是截至 2026-09-08 的精简参考实现，仅依赖 Python 标准库，支持 Python 3.10+。
生产入口仍是 [tools/mail_alias.py](../tools/mail_alias.py) 的 `MailAliasExtractor`，
现有邮件接口、导出和数据库流程保持原样。规则依据当前邮件样本，不是通用 VERP 解码器。

## 文件与运行

```text
mail_alias/
  __init__.py
  simple.py     精简算法和可运行示例
  README.md     输入、优先级、解码步骤与限制
```

在项目根目录运行示例，不连接邮箱、不读取账号配置：

```powershell
D:\0Code2\py312\python.exe mail_alias\simple.py
```

预期输出：

```json
{
  "address": "demo+2@icloud.com",
  "source": "envelope_from_verp",
  "provider": "163mail",
  "matched": true
}
```

## 在代码中调用

```python
from mail_alias.simple import extract_alias

match = extract_alias(
    {
        "account": "owner@icloud.com",
        "return_path": "bounce+57ca0c.b462b7-demo-name+1=icloud.com@relay.example.com",
        "alias_hme": "demo-name@icloud.com",
    },
    provider="apple",
    inbox_mail="owner@icloud.com",
)
assert match["address"] == "demo-name+1@icloud.com"
assert match["source"] == "return_path_verp"
```

`simple.py` 可独立使用，无需导入本项目的 `tools`、账号对象或数据库。

## 输入与输出

`extract_alias(record, *, provider, account="", parent_mail="", inbox_mail="")`
接受一条字典形式的邮件元数据。缺失字段按空值处理。

| 输入 | 含义 |
|------|------|
| `provider` 参数 | 实际收件服务商：`163mail`、`apple`、`outlook`；忽略首尾空格与大小写 |
| `account` / `parent_mail` / `inbox_mail` 参数 | 所属账号、母邮箱、最终收件邮箱，用于排除基础地址 |
| `record.account` / `record.parent_mail` | 邮件记录中的所属账号和母邮箱，同样加入排除集合 |
| `record.envelope_from` | 已提取出的信封发件地址，不是完整 Received-SPF 头 |
| `record.return_path` | Return-Path 地址；原始邮件结果若名为 `return_path_addr`，先映射为此键 |
| `record.alias_hme` | 上游已识别的隐私邮箱地址，作为可信回退值 |
| `record.delivered_to` / `record.to_addr` | 投递地址头 / 收件人地址头，支持标准显示名及多个地址 |

`provider` 指最终收件端，不按别名域名猜测。例如 iCloud 别名转发到 163，使用 `163mail`。
Apple/iCloud 对应参数值 `apple`。精简版需手动传入这些上下文，生产版由账号的
`resolve_inbox()` 取得；精简版仅接受字典类输入，生产版还接受邮件记录对象。

返回 `address`、`source`、`provider`、`matched` 四个字段。未命中时，
`address` 和 `source` 为空字符串，`matched` 为 `false`，保留传入的 `provider`。

## 提取优先级

每一步取得合格地址即返回；VERP 和地址头推断结果需要排除基础账号地址。

| 收件服务商 | 尝试顺序 |
|------------|------------|
| `163mail` | `envelope_from` VERP → `return_path` VERP → `alias_hme` → `delivered_to` → `to_addr` |
| `apple` / `outlook` | `return_path` VERP → `alias_hme` |
| 其他值或空字符串 | 仅 `alias_hme` |

地址统一转小写并清理外围空格和标点，保留 `+tag`、连字符、下划线。
基础地址排除使用完整地址相等判断：`owner@outlook.com` 可被排除，
`owner+5@outlook.com` 仍可命中。

`alias_hme` 与生产版一致，直接信任上游结果，不再执行基础地址排除。
Apple/Outlook 没有 `To` 回退；QQ 也没有专用解析规则。

## VERP 解码步骤

示例：`bounces+12345-a1b2-demo+2=icloud.com@relay.example.com`

1. 使用 `email.utils.getaddresses()` 提取地址，正则作为 Graph 特殊显示格式的补充，取首个地址。
2. 取外层 `@` 前的部分，要求以 `bounce+` 或 `bounces+` 开头。
3. 去掉前缀和第一个 `-` 前的活动标识，得到 `a1b2-demo+2=icloud.com`。
4. 若接下来 `-` 前的一段由至少 4 个十六进制字符构成，视为 token 并跳过；否则保留。
5. 按最后一个 `=` 分隔本地部分与域名，组合成 `demo+2@icloud.com`。
6. 检查两部分非空且匹配项目地址正则；格式不匹配时返回空字符串，继续下一条规则。

不带 token 的 `bounce+campaign-demo-name+1=icloud.com@relay.example.com`
会保留 `demo-name+1@icloud.com` 中的连字符。

## 适用边界

- 这是项目邮件样本的启发式解析，不负责邮件真实性校验、域名查询或邮箱存在性验证。
- VERP 仅取字段中的第一个地址，不处理所有服务商的编码方案，也不解码 SRS。
- 十六进制 token 判定存在歧义：若实际别名以 `abcd-` 开头，该段也会被跳过。这是现有规则的限制。
- 地址正则不是完整 RFC 邮箱验证器；国际化地址、带引号的本地部分等格式未作专门适配。
- `+tag` 是否可实际收件由邮箱服务商决定；解析出地址本身不代表该服务商支持该地址。
- 上游必须先提供邮件头元数据及正确收件上下文；精简版不采集邮件头、不读取正文。

## 验证与生产调用

```powershell
D:\0Code2\py312\python.exe -m unittest tests.test_mail_alias_simple tests.test_mail_alias -v
```

[精简版测试](../tests/test_mail_alias_simple.py) 覆盖优先级、字段回退、`+tag`、基础地址
排除、空值及 VERP 边界，并将输出与生产版逐项比较。修改解析规则时，两份实现及测试应同步。

现有调用位置：

- [tools/mail_alias.py](../tools/mail_alias.py)：生产解析类及账号上下文。
- [web/routers/mails.py](../web/routers/mails.py)：邮件列表和详情的 `recipient_alias`。
- [tools/mail_export.py](../tools/mail_export.py)：验证码 CSV 的收件别名字段。
- [tests/test_mail_alias.py](../tests/test_mail_alias.py)：原始回归样例。
