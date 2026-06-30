import logging
import sys
from datetime import datetime
from pathlib import Path


class ColoredFormatter(logging.Formatter):
    """Цветной логгер для терминала"""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[41m",  # red background
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"


def get_logger(name: str = "transcriber", log_file: str = None, level=logging.DEBUG):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # Предотвращаем дублирование логов в root logger

    if logger.handlers:
        return logger

    # --- Formatter ---
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s"
    formatter = logging.Formatter(fmt)
    color_formatter = ColoredFormatter(fmt)

    # --- Console Handler ---
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)  # В консоль выводим только INFO и выше
    ch.setFormatter(color_formatter)
    logger.addHandler(ch)

    # file handler (optional)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def log_start_session(logger, file_path: str):
    logger.info("=" * 60)
    logger.info(f"NEW SESSION: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"INPUT: {Path(file_path).resolve()}")
    logger.info("=" * 60)
