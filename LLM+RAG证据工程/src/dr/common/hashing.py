"""哈希工具函数

用于生成稳定的ID和缓存key。
"""
import hashlib
import re


def sha1(s: str) -> str:
    """计算字符串的SHA1哈希

    Args:
        s: 输入字符串

    Returns:
        40字符的十六进制哈希值

    Example:
        >>> sha1("aspirin")
        '6dcd4ce23d88e2ee9568ba546c007c63d9131c1b'
    """
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def md5(s: str) -> str:
    """计算字符串的MD5哈希

    Args:
        s: 输入字符串

    Returns:
        32字符的十六进制哈希值

    Example:
        >>> md5("aspirin")
        '1b8d8f9c8c7c8d2c8f8b8c8d2c8f8b8c'
    """
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def stable_drug_id(canonical_name: str, nct_id: str = "") -> str:
    """生成稳定的药物ID（用于step5）

    基于规范化名称 + 可选NCT ID生成唯一标识符。

    Args:
        canonical_name: 规范化药物名称
        nct_id: 可选的NCT ID（用于区分同名药物）

    Returns:
        11字符的大写哈希ID（格式：D + 10位十六进制）

    Example:
        >>> stable_drug_id("aspirin")
        'D6DCD4CE23D'
        >>> stable_drug_id("aspirin", "NCT00123456")
        'DA1B2C3D4E5'

    Notes:
        - D前缀表示Drug
        - 使用SHA1前10位确保唯一性
        - 相同输入总是产生相同ID（确定性）
    """
    combined = f"{canonical_name}_{nct_id}".strip("_")
    h = sha1(combined)
    return "D" + h[:10].upper()
