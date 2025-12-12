#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Bot Configuration
- Chọn exchange (MEXC hoặc GATE)
- API keys
- Telegram settings
- Other configs
"""

from enum import Enum


class ExchangeType(Enum):
    """Exchange type enum"""
    MEXC = "mexc"
    GATE = "gate"


class ExchangeConfig:
    """
    Configuration cho bot
    Sửa các giá trị ở đây để thay đổi behavior
    """

    # ==================== EXCHANGE SELECTION ====================
    # Chọn exchange: ExchangeType.MEXC hoặc ExchangeType.GATE
    EXCHANGE = ExchangeType.GATE

    # Gate.io specific
    GATE_TESTNET = False  # True để dùng testnet

    # ==================== API CREDENTIALS ====================
    # MEXC API (nếu dùng MEXC)
    MEXC_API_KEY = None
    MEXC_API_SECRET = None

    # Gate.io API (nếu dùng Gate)
    GATE_API_KEY = None
    GATE_API_SECRET = None

    # ==================== PROXY ====================
    # Format: "user:pass@host:port" hoặc None
    PROXY = "GPVNx6479:mWBK1h1J@103.145.254.137:27657"

    # ==================== TELEGRAM ====================
    TELEGRAM_BOT_TOKEN = "7519046021:AAER7iFwU2akFBZp111qCyZwBak_2NrT2lw"
    TELEGRAM_CHAT_ID = "@xbot_x1"

    # ==================== STRATEGY ====================
    NUM_STRATEGIES = 100000  # Số strategies để backtest

    # ==================== PUMP DETECTOR ====================
    PUMP_CONFIG = {
        'price_increase_1m': 0.5,  # % tăng trong 1 phút
        'price_increase_5m': 1.0,  # % tăng trong 5 phút
        'volume_spike_multiplier': 1.5,  # Volume tăng bao nhiêu lần
        'min_volume_usdt': 100,  # Volume tối thiểu (USDT)
        'min_confidence': 40,  # Confidence tối thiểu để notify
    }

    # ==================== LOGGING ====================
    LOG_LEVEL = 'DEBUG'  # DEBUG, INFO, WARNING, ERROR

    # ==================== DEBUG ====================
    DEBUG_LOG = False  # True để log chi tiết raw data

    @classmethod
    def get_exchange_name(cls) -> str:
        """Get exchange name string"""
        return cls.EXCHANGE.value.upper()

    @classmethod
    def is_mexc(cls) -> bool:
        """Check if using MEXC"""
        return cls.EXCHANGE == ExchangeType.MEXC

    @classmethod
    def is_gate(cls) -> bool:
        """Check if using Gate.io"""
        return cls.EXCHANGE == ExchangeType.GATE