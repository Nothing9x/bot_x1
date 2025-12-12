#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Socket Factory
- Tạo exchange socket dựa trên config
- Hỗ trợ MEXC và Gate.io
"""

from typing import List

from x1.bot.config.exchange_config import ExchangeConfig, ExchangeType
from x1.bot.market.i_market_socket import IMarketSocket
from x1.bot.model.symbol import Symbol
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class SocketFactory:
    """
    Factory class để tạo exchange socket
    """

    @staticmethod
    def create_socket(
            log: LoggerWrapper,
            proxy: str = None,
            tele_message: TelegramMessageQueue = None,
            chat_id: str = None,
            exchange: ExchangeType = None
    ) -> IMarketSocket:
        """
        Tạo exchange socket dựa trên exchange type

        Args:
            log: Logger instance
            proxy: Proxy string (nếu cần)
            tele_message: Telegram message queue
            chat_id: Telegram chat ID
            exchange: Exchange type (nếu None sẽ lấy từ BotConfig)

        Returns:
            IMarketSocket: Market socket instance
        """
        if exchange is None:
            exchange = ExchangeConfig.EXCHANGE

        if exchange == ExchangeType.MEXC:
            from x1.bot.market.mexc_socket import MexcSocket
            return MexcSocket(
                log=log,
                proxy=proxy,
                tele_message=tele_message,
                chat_id=chat_id
            )
        elif exchange == ExchangeType.GATE:
            from x1.bot.market.gate_socket import GateSocket
            return GateSocket(
                log=log,
                proxy=proxy,
                tele_message=tele_message,
                chat_id=chat_id,
                testnet=ExchangeConfig.GATE_TESTNET
            )
        else:
            raise ValueError(f"Unknown exchange type: {exchange}")

    @staticmethod
    def init_symbols(log: LoggerWrapper, exchange: ExchangeType = None) -> List[Symbol]:
        """
        Lấy danh sách symbols từ exchange

        Args:
            log: Logger instance
            exchange: Exchange type (nếu None sẽ lấy từ BotConfig)

        Returns:
            List[Symbol]: Danh sách symbols
        """
        if exchange is None:
            exchange = ExchangeConfig.EXCHANGE

        if exchange == ExchangeType.MEXC:
            from x1.bot.market.mexc_symbols import init_mexc_symbols
            return init_mexc_symbols(log=log, proxy=ExchangeConfig.PROXY)

        elif exchange == ExchangeType.GATE:
            from x1.bot.market.gate_socket import GateSocket
            return GateSocket.init_gate_symbols(
                log=log,
                testnet=ExchangeConfig.GATE_TESTNET
            )
        else:
            raise ValueError(f"Unknown exchange type: {exchange}")

    @staticmethod
    def get_exchange_name() -> str:
        """Lấy tên exchange hiện tại"""
        return ExchangeConfig.get_exchange_name()