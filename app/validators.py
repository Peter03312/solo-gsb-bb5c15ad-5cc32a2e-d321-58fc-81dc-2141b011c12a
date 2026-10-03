"""邮箱地址与域名的语法校验、规范化。

规范化只做大小写折叠与去空白（域名天然大小写不敏感，
本地部分按 RFC 5321 仅保留大小写的，实践中别名系统通常
也按不敏感处理），不做重写。
"""
import re

from .errors import validation_error

# 常见的 atext + 点原子风格，足够覆盖企业内部别名系统的输入校验
_LOCAL_RE = re.compile(r"^(?=.{1,64}$)[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)$")


def normalize_address(value: str, *, field: str = "address") -> str:
    if not isinstance(value, str):
        raise validation_error(f"{field} 必须是字符串", field=field)
    addr = value.strip().lower()
    if not addr:
        raise validation_error(f"{field} 不能为空", field=field)
    if addr.count("@") != 1:
        raise validation_error(f"无效的邮箱地址（应恰好包含一个 @）: {value}", field=field)
    local, _, domain = addr.partition("@")
    if not _LOCAL_RE.match(local):
        raise validation_error(f"无效的邮箱地址（本地部分非法）: {value}", field=field)
    validate_domain(domain, field=field)
    return f"{local}@{domain}"


def normalize_domain(value: str, *, field: str = "domain") -> str:
    if not isinstance(value, str):
        raise validation_error(f"{field} 必须是字符串", field=field)
    domain = value.strip().lower().rstrip(".")
    validate_domain(domain, field=field)
    return domain


def validate_domain(domain: str, *, field: str = "domain") -> None:
    if not domain or "@" in domain or domain.startswith("."):
        raise validation_error(f"无效的域名: {domain}", field=field)
    if len(domain) > 253:
        raise validation_error(f"无效的域名（过长）: {domain}", field=field)
    labels = domain.split(".")
    # 不接受单标签域与 IP 字面量
    if len(labels) < 2 or any(not _LABEL_RE.match(label) for label in labels):
        raise validation_error(f"无效的域名: {domain}", field=field)
    tld = labels[-1]
    if tld.isdigit():
        raise validation_error(f"无效的域名（顶级域不能纯数字）: {domain}", field=field)


def split_domain(address: str) -> str:
    return address.rpartition("@")[2]
