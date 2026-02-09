"""统一日志配置

替换所有脚本中的简陋print()函数，提供工业级日志系统。
"""
import logging
import logging.handlers
from pathlib import Path
import sys


def setup_logger(
    name: str = "dr",
    log_file: str = "dr_pipeline.log",
    level: str = "INFO",
    log_dir: str = "."
) -> logging.Logger:
    """配置标准logger（文件+控制台）

    Args:
        name: Logger名称（通常使用__name__）
        log_file: 日志文件名（默认dr_pipeline.log）
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        log_dir: 日志目录（默认当前目录）

    Returns:
        配置好的Logger对象

    Example:
        >>> from dr.logger import setup_logger
        >>> logger = setup_logger(__name__)
        >>> logger.info("Processing drug: %s", "aspirin")
        >>> logger.error("Failed to fetch PMID: %s", pmid, exc_info=True)

    Features:
        - 文件轮换（100MB，保留5份）
        - 彩色控制台输出（可选）
        - 详细格式（时间戳、模块、函数、行号）
        - 异常自动记录栈（exc_info=True）
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # 避免重复添加handler
    if logger.handlers:
        return logger

    # 创建日志目录
    log_path = Path(log_dir) / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 文件handler（带轮换）
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=100 * 1024 * 1024,  # 100MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别

    # 控制台handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)  # 控制台只显示INFO及以上

    # 格式
    detailed_formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    simple_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )

    file_handler.setFormatter(detailed_formatter)
    console_handler.setFormatter(simple_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def get_logger(name: str = "dr") -> logging.Logger:
    """获取已配置的logger（懒加载）

    如果logger尚未配置，则使用默认配置。

    Args:
        name: Logger名称

    Returns:
        Logger对象

    Example:
        >>> from dr.logger import get_logger
        >>> logger = get_logger(__name__)
        >>> logger.info("Task completed")
    """
    logger = logging.getLogger(name)

    # 如果没有handler，使用默认配置
    if not logger.handlers:
        return setup_logger(name)

    return logger
