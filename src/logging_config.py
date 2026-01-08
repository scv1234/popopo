# src/logging_config.py
from __future__ import annotations
import logging
import sys
from typing import Literal
import structlog

def configure_logging(level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO") -> None:
    # 1. 파일 핸들러 및 콘솔 핸들러 설정
    # force=True를 사용하여 기존 설정을 덮어씁니다.
    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", encoding="utf-8") # 파일 저장 추가
        ],
        force=True 
    )

    structlog.configure(
        processors=[
            # 가독성 좋은 타임스탬프 설정
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"), 
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("message"),
            # [수정] 대시보드 가독성을 위해 JSON 대신 ConsoleRenderer 사용
            structlog.dev.ConsoleRenderer(colors=False), 
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )