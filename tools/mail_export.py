from __future__ import annotations

import csv
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .db import AliasDB
from .mail_alias import MailAliasExtractor


VERIFICATION_CODE_FIELDS = (
    "account",
    "parent_mail",
    "mailbox",
    "uid",
    "date_utc",
    "internaldate",
    "from_name",
    "from_addr",
    "sender_addr",
    "return_path",
    "envelope_from",
    "received_spf",
    "to_addr",
    "delivered_to",
    "alias_hme",
    "recipient_alias",
    "subject",
    "code",
    "summary",
    "message_id",
)


def _attrib(path: Path, value: str) -> None:
    if os.name != "nt":
        return
    subprocess.run(
        ["attrib", value, str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def unlock_file(path: str | Path) -> Path:
    """移除目标文件只读属性；文件不存在时只创建父目录。"""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _attrib(target, "-R")
        os.chmod(target, target.stat().st_mode | stat.S_IWRITE)
    return target


def set_file_readonly(path: str | Path) -> Path:
    target = Path(path).resolve()
    if target.exists():
        os.chmod(target, stat.S_IREAD)
        _attrib(target, "+R")
    return target


def _verification_records(db: AliasDB) -> Iterable[Any]:
    offset = 0
    page_size = 500
    while True:
        records = db.list_mails(mail_type="code", limit=page_size, offset=offset)
        if not records:
            return
        yield from records
        offset += len(records)
        if len(records) < page_size:
            return


def export_verification_codes_csv(
    db: AliasDB,
    output: str | Path,
    *,
    alias_extractor: MailAliasExtractor | None = None,
    replace_retries: int = 6,
    retry_delay: float = 0.5,
) -> tuple[Path, int]:
    """导出验证码邮件；写前解除只读，原子替换后重新设置为只读。"""
    target = unlock_file(output)
    temp_path: Path | None = None
    count = 0
    extractor = alias_extractor or MailAliasExtractor()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{target.stem}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=list(VERIFICATION_CODE_FIELDS))
            writer.writeheader()
            for record in _verification_records(db):
                row = record.to_dict()
                source_mail = extractor.inbox_mail(row)
                if source_mail:
                    # CSV 的 parent_mail 面向收件筛选，展示实际 provider 邮箱；
                    # 数据库中的 parent_mail 仍保留 HME 母号语义。
                    row["parent_mail"] = source_mail
                row["recipient_alias"] = extractor.extract_address(row)
                writer.writerow({name: row.get(name, "") for name in VERIFICATION_CODE_FIELDS})
                count += 1
            handle.flush()
            os.fsync(handle.fileno())

        last_error: PermissionError | None = None
        for attempt in range(max(1, int(replace_retries))):
            try:
                os.replace(temp_path, target)
                temp_path = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt + 1 < max(1, int(replace_retries)):
                    time.sleep(max(0.0, float(retry_delay)))
        else:
            raise PermissionError(
                f"CSV 正被 Excel 或其它程序占用，重试后仍未释放: {target}"
            ) from last_error

        set_file_readonly(target)
        return target, count
    except Exception:
        if target.exists():
            set_file_readonly(target)
        raise
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


__all__ = [
    "VERIFICATION_CODE_FIELDS",
    "export_verification_codes_csv",
    "set_file_readonly",
    "unlock_file",
]
