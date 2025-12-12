#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MEXC Symbols Loader
Tách riêng logic load symbols từ MEXC để dùng với Factory
"""

import time
import traceback
import requests

from x1.bot.model.symbol import Symbol
from x1.bot.utils import Utils
from x1.bot.utils.LoggerWrapper import LoggerWrapper
from x1.bot.utils.black_list_symbol import BLACK_LIST_SYMBOL
from x1.bot.config.exchange_config import ExchangeConfig

MEXC_CONTRACT_DETAIL_URL = "https://contract.mexc.com/api/v1/contract/detail"


def init_mexc_symbols(log: LoggerWrapper, proxy: str = None) -> list:
    """
    Load danh sách symbols từ MEXC Futures

    Args:
        log: Logger instance
        proxy: Optional proxy string (format: "user:pass@host:port")

    Returns:
        List[Symbol]: Danh sách symbols có thể trade
    """
    tag = "MexcSymbols"
    symbols: list[Symbol] = []

    # Dùng proxy từ config nếu không được truyền vào
    if proxy is None:
        proxy = ExchangeConfig.PROXY

    try:
        log.i(tag, "📥 Loading MEXC symbols...")

        proxies = Utils.get_proxies(proxy) if proxy else None
        response = requests.get(MEXC_CONTRACT_DETAIL_URL, proxies=proxies, timeout=10)
        data = response.json()

        cur_time = int(time.time() * 1000)

        for d in data.get("data", []):
            # Skip symbols chưa mở trading
            if cur_time < d.get("openingTime", 0):
                continue

            # Chỉ lấy USDT pairs
            if not d.get("symbol", "").endswith("_USDT"):
                continue

            # Skip blacklisted symbols
            symbol_name = d['symbol']
            if symbol_name in BLACK_LIST_SYMBOL:
                log.d(tag, f"Symbol {symbol_name} in blacklist - skipped")
                continue

            # Tạo Symbol object
            symbols.append(Symbol(
                symbol_name,
                d.get("priceScale", 2),
                d.get("contractSize", 1),
                d.get("maxVol", 1000000),
                d.get("maxLeverage", 100)
            ))

        log.i(tag, f"✅ Loaded {len(symbols)} valid MEXC symbols")

    except Exception as e:
        log.e(tag, f"Error loading MEXC symbols: {e}\n{traceback.format_exc()}")

    return symbols