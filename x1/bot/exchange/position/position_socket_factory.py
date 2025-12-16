from x1.bot.config.exchange_config import ExchangeConfig
from x1.bot.database.database_models import BotConfig
from x1.bot.exchange.position.gate_position_socket import GatePositionSocket
from x1.bot.exchange.position.i_position_socket import IPositionSocket
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class MexcPositionSocket:
    pass


class PositionSocketFactory:
    @staticmethod
    def create(exchange: str, bot: BotConfig, log: LoggerWrapper,
               position_callback, trade_callback) -> IPositionSocket:
        exchange = exchange.upper()
        if exchange == ExchangeConfig.EXCHANGE.MEXC:
            return GatePositionSocket(bot, log, position_callback, trade_callback)
        # elif exchange == "bitget":
        #     return BitgetPositionSocket(bot, log, position_callback, trade_callback)
        elif exchange == ExchangeConfig.EXCHANGE.GATE:
            return GatePositionSocket(bot, log, position_callback, trade_callback)
        else:
            return GatePositionSocket(bot, log, position_callback, trade_callback)

