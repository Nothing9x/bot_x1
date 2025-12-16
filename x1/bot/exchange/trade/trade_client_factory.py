from x1.bot.config.exchange_config import ExchangeConfig
from x1.bot.database.database_models import BotConfig
from x1.bot.exchange.trade.gate_trade_client import GateTradeClient
from x1.bot.exchange.trade.i_trade_client import ITradeClient
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class TradeClientFactory:
    @staticmethod
    def create(exchange_name: str, bot: BotConfig, telegramMessage: TelegramMessageQueue, log: LoggerWrapper,
               trade_callback) -> ITradeClient:
        exchange_name = exchange_name.upper()
        log.i("TradeClientFactory", f"Creating TradeClient for exchange: {exchange_name}")

        if exchange_name == ExchangeConfig.EXCHANGE.GATE:
            return GateTradeClient(bot, telegramMessage, log, trade_callback)
        else:
            return GateTradeClient(bot, telegramMessage, log, trade_callback)
