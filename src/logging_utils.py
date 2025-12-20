import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("data/logs")
LOG_FILE = LOG_DIR / "scheduler.log"
BASE_LOGGER_NAME = "scheduler"


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _base_logger(level: int) -> logging.Logger:
    """
    Create/return a single base logger with one rotating file handler.
    Child loggers (scheduler.ui, scheduler.worker, etc.) inherit it without adding handlers.
    """
    _ensure_log_dir()
    logger = logging.getLogger(BASE_LOGGER_NAME)
    logger.setLevel(level)

    # Hard reset handlers to avoid duplicates from Streamlit reruns or module reloads.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

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

    logger.propagate = False  # keep records out of root handlers (e.g., Streamlit defaults)
    logging.captureWarnings(True)

    # Keep third-party verbosity reasonable without hiding errors
    for noisy in ("instagrapi", "urllib3", "selenium"):
        logging.getLogger(noisy).setLevel(logging.INFO)
    return logger


def init_logging(component_name: str = "app", level: int = logging.INFO) -> logging.Logger:
    _base_logger(level)
    component_logger = logging.getLogger(f"{BASE_LOGGER_NAME}.{component_name}")
    component_logger.setLevel(level)
    component_logger.propagate = True  # bubble to base logger only
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
