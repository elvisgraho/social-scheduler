import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("data/logs")
LOG_FILE = LOG_DIR / "scheduler.log"

def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _handler_exists(logger: logging.Logger) -> bool:
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(LOG_FILE):
            return True
    return False


def init_logging(component_name: str = "app", level: int = logging.INFO) -> logging.Logger:
    _ensure_log_dir()
    logger = logging.getLogger()
    logger.setLevel(level)

    if not _handler_exists(logger):
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)

    logging.captureWarnings(True)
    component_logger = logging.getLogger(component_name)
    component_logger.debug("Logging initialized for %s", component_name)
    return component_logger


def get_log_file_path() -> Path:
    _ensure_log_dir()
    return LOG_FILE


def tail_log(lines: int = 200) -> str:
    path = get_log_file_path()
    if not path.exists():
        return "Log file not created yet."
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        data = fh.readlines()[-lines:]
    return "".join(data) if data else "Log file is empty."
