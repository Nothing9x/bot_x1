from loguru import logger
from datetime import datetime
import os

from x1.bot.utils.LoggerWrapper import LoggerWrapper


class Log:
    def __init__(self):
        pass

    _task_loggers = {}

    @classmethod
    def init(cls, task_name: str, log_level="DEBUG"):
        if task_name in cls._task_loggers:
            return cls._task_loggers[task_name]["logger"]

        os.makedirs("log", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        log_filename = f"log/{task_name}_{timestamp}.log"

        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[tag]: <40}</cyan> | "
            "{message}"
        )

        # Filter function để chỉ log của task này mới vào sink này
        def filter_by_task(record):
            return record["extra"].get("task") == task_name

        # Bind task và tag vào logger
        bound_logger = logger.bind(tag=task_name, task=task_name)

        file_sink_id = logger.add(
            log_filename,
            rotation="100 MB",
            compression="zip",
            format=log_format,
            level=log_level,
            filter=filter_by_task,
            enqueue=True,
        )

        console_sink_id = logger.add(
            lambda msg: print(msg, end=""),
            format=log_format,
            level=log_level,
            colorize=True,
            filter=filter_by_task,
        )

        wrapped_logger = LoggerWrapper(bound_logger, task_name)

        cls._task_loggers[task_name] = {
            "logger": wrapped_logger,
            "sink_ids": [file_sink_id, console_sink_id],
        }

        return wrapped_logger

    @classmethod
    def remove_logger(cls, task_name: str):
        if task_name in cls._task_loggers:
            for sink_id in cls._task_loggers[task_name]["sink_ids"]:
                logger.remove(sink_id)
            del cls._task_loggers[task_name]
