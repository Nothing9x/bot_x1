class LoggerWrapper:
    def __init__(self, base_logger, default_tag):
        self._logger = base_logger
        self._default_tag = default_tag

    def t(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).trace(message)

    def d(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).debug(message)

    def i(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).info(message)

    def w(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).warning(message)

    def e(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).error(message)

    def c(self, tag=None, message=""):
        self._logger.bind(tag=tag or self._default_tag).critical(message)