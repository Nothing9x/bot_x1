#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Interface cho Market WebSocket
Cả MexcSocket và GateSocket đều implement interface này
"""

from abc import ABC, abstractmethod
from typing import List, Callable


class IMarketSocket(ABC):
    """
    Interface chuẩn cho Market WebSocket
    - MexcSocket và GateSocket phải implement tất cả methods này
    - Đảm bảo có thể swap giữa 2 exchange mà không cần thay đổi code khác
    """

    @abstractmethod
    def register_callback(self, callback: Callable):
        """
        Đăng ký callback để nhận dữ liệu candle
        Callback signature: async def callback(symbol: str, interval: str, candle_data: dict)

        candle_data format chuẩn hóa:
        {
            't': timestamp (int),
            'o': open price (float/str),
            'h': high price (float/str),
            'l': low price (float/str),
            'c': close price (float/str),
            'a': volume (float/str)  # amount/volume
        }
        """
        pass

    @abstractmethod
    async def start(self, symbols: list):
        """
        Khởi động WebSocket và subscribe các symbols

        Args:
            symbols: List[Symbol] - danh sách symbols cần theo dõi
        """
        pass

    @abstractmethod
    async def add_symbols(self, new_symbols: list):
        """
        Thêm symbols mới vào subscription

        Args:
            new_symbols: List[Symbol] - danh sách symbols mới cần thêm
        """
        pass