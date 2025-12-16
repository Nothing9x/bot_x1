from com.BotConfig import BotConfig
from com.xbot.mexc.bot.gate_trade_client import GateTradeClient
from com.xbot.mexc.bot.i_trade_client import ITradeClient
from com.xbot.mexc.bot.mexc_trade_client import MexcTradeClient
from com.xbot.mexc.market.model.supported_exchange_name import SupportedExchangeName
from com.xbot.mexc.notification.notification_manager import TelegramMessageQueue
from com.xbot.mexc.utils.LoggerWrapper import LoggerWrapper


class TradeClientFactory:
    @staticmethod
    def create(exchange_name: str, bot: BotConfig, telegramMessage: TelegramMessageQueue, log: LoggerWrapper,
               trade_callback) -> ITradeClient:
        exchange_name = exchange_name.upper()
        log.i("TradeClientFactory", f"Creating TradeClient for exchange: {exchange_name}")

        if exchange_name == SupportedExchangeName.GATE:
            return GateTradeClient(bot, telegramMessage, log, trade_callback)
        else:
            return MexcTradeClient(bot, telegramMessage, log, trade_callback)
