"""
日志工具
"""
import sys
from pathlib import Path

from loguru import logger

import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import LOG_CONFIG


def setup_logger():
    """配置日志系统"""
    # 移除默认处理器
    logger.remove()

    # 添加控制台输出
    logger.add(
        sys.stdout,
        format=LOG_CONFIG["format"],
        level=LOG_CONFIG["level"],
        colorize=True
    )

    # 添加文件输出
    log_dir = Path(LOG_CONFIG["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.add(
        str(log_dir / "app_{time:YYYY-MM-DD}.log"),
        format=LOG_CONFIG["format"],
        level=LOG_CONFIG["level"],
        rotation=LOG_CONFIG["rotation"],
        retention=LOG_CONFIG["retention"],
        encoding="utf-8"
    )

    # 添加错误日志
    logger.add(
        str(log_dir / "error_{time:YYYY-MM-DD}.log"),
        format=LOG_CONFIG["format"],
        level="ERROR",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8"
    )

    return logger
