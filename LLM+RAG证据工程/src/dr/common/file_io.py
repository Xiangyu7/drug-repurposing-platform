"""文件I/O工具函数

统一的文件读写逻辑，确保原子操作和一致性。
"""
import json
from pathlib import Path
from typing import Any
import logging

logger = logging.getLogger(__name__)


def read_json(path: Path) -> Any:
    """读取JSON文件

    Args:
        path: JSON文件路径

    Returns:
        解析后的Python对象（dict/list）

    Raises:
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON格式错误

    Example:
        >>> data = read_json(Path("cache/drug_123.json"))
        >>> print(data["drug_id"])
    """
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    """写入JSON文件（原子操作）

    使用.tmp临时文件 + rename原子操作，确保：
    1. 写入过程中不会损坏原文件
    2. 写入完成前不会被其他进程读取到不完整数据
    3. 系统崩溃时不会留下损坏的文件

    Args:
        path: 目标JSON文件路径
        obj: 要写入的Python对象

    Example:
        >>> write_json(Path("output/dossier.json"), {"drug_id": "123"})

    Notes:
        - 自动创建父目录
        - 使用ensure_ascii=False支持中文等非ASCII字符
        - indent=2提高可读性
        - 原子操作：先写.tmp，成功后rename
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        tmp.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        tmp.replace(path)  # 原子操作
    except Exception:
        # 清理临时文件
        if tmp.exists():
            tmp.unlink()
        raise


def write_text(path: Path, txt: str) -> None:
    """写入文本文件（原子操作）

    Args:
        path: 目标文本文件路径
        txt: 要写入的文本内容

    Example:
        >>> write_text(Path("output/dossier.md"), "# Drug Dossier\\n...")
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        tmp.write_text(txt, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def is_empty(path: Path) -> bool:
    """检查文件是否为空或不存在

    Args:
        path: 文件路径

    Returns:
        True如果文件不存在或大小为0

    Example:
        >>> if is_empty(Path("cache/pmids.json")):
        ...     # 重新拉取数据
        ...     fetch_pmids()
    """
    return (not path.exists()) or path.stat().st_size == 0
