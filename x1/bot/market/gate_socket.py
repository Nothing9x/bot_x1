#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Gate.io Futures WebSocket
- Implement IMarketSocket interface
- Chuẩn hóa candle data format giống MEXC để các callback có thể dùng chung
"""

import asyncio
import json
import time
import traceback
import requests
import websockets

from x1.bot.market.i_market_socket import IMarketSocket
from x1.bot.model.symbol import Symbol
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils.LoggerWrapper import LoggerWrapper
from x1.bot.utils.black_list_symbol import BLACK_LIST_SYMBOL

GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
GATE_WS_URL_TESTNET = "wss://fx-ws-testnet.gateio.ws/v4/ws/usdt"

GATE_FUTURES_CONTRACTS = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
GATE_FUTURES_CONTRACTS_TESTNET = "https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/contracts"


class GateSocket(IMarketSocket):
    """
    Gate.io Futures WebSocket Client
    - Chuẩn hóa candle data format giống MEXC
    - Có thể thay thế MexcSocket mà không cần thay đổi code khác
    """

    def __init__(self, log: LoggerWrapper, proxy=None, tele_message: TelegramMessageQueue = None,
                 chat_id: str = None, testnet: bool = False):
        self.tag = "GateSocket"
        self.log = log
        self.proxy = proxy  # Gate.io có thể không cần proxy
        self.tele_message = tele_message
        self.chat_id = chat_id
        self.testnet = testnet

        # WebSocket URL
        self.ws_url = GATE_WS_URL_TESTNET if testnet else GATE_WS_URL

        self.symbols: list[Symbol] = []
        self.ws = None
        self.callbacks = []  # Danh sách callback (giống MexcSocket)
        self.last_message_time = time.time()
        self.monitor_task = None

    def register_callback(self, callback):
        """Đăng ký callback để nhận dữ liệu khi giá thay đổi"""
        self.callbacks.append(callback)

    async def start(self, symbols: list[Symbol]):
        """Khởi động WebSocket"""
        self.symbols = symbols
        asyncio.create_task(self.connect())
        await asyncio.sleep(1)
        self.log.d(self.tag, "✅ GATE Candle WebSocket started")

    async def connect(self):
        """Kết nối WebSocket và tự động reconnect"""
        while True:
            self.log.d(self.tag, "🔌 Connecting to Gate.io WebSocket...")
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.ws = ws
                    self.log.d(self.tag, "✅ Connected to Gate.io WebSocket")

                    if self.tele_message:
                        await self.tele_message.send_message(
                            "✅ Connected to Gate.io WebSocket",
                            self.chat_id
                        )

                    await self.subscribe_all()
                    self.monitor_task = asyncio.create_task(self.monitor_timeout())
                    asyncio.create_task(self.send_ping())
                    await self.listen()

            except websockets.exceptions.ConnectionClosed as e:
                self.log.d(self.tag, f"🔴 WebSocket disconnected: {e}")
                if self.tele_message:
                    await self.tele_message.send_message(
                        f"🔴 Gate.io WebSocket disconnected: {e}",
                        self.chat_id
                    )
            except Exception:
                self.log.d(self.tag, f"⚠️ Unexpected Error:\n{traceback.format_exc()}")

            if self.ws:
                await self.ws.close()
                self.ws = None

            if self.monitor_task:
                self.monitor_task.cancel()
                self.monitor_task = None

            self.log.d(self.tag, "🔄 Reconnecting in 5 seconds...")
            if self.tele_message:
                await self.tele_message.send_message(
                    "🔄 Reconnecting Gate.io WebSocket in 5 seconds...",
                    self.chat_id
                )
            await asyncio.sleep(5)

    async def send_ping(self):
        """Gửi ping định kỳ để giữ kết nối"""
        while self.ws:
            try:
                await self.ws.send(json.dumps({
                    "channel": "futures.ping",
                    "time": int(time.time())
                }))
            except Exception as e:
                self.log.d(self.tag, f"⚠️ Ping error: {e}")
                break
            await asyncio.sleep(20)

    async def subscribe_all(self):
        """Subscribe tất cả symbols"""
        for symbol in self.symbols:
            await self.subscribe(symbol.symbol)

    async def subscribe(self, symbol: str):
        """Subscribe một symbol với interval 1m và 5m"""
        # Subscribe 1m
        subscribe_msg_1m = {
            "time": int(time.time()),
            "channel": "futures.candlesticks",
            "event": "subscribe",
            "payload": ["1m", symbol.upper()]
        }
        await self.ws.send(json.dumps(subscribe_msg_1m))
        self.log.d(self.tag, f"📡 Subscribed to: {symbol.upper()} - 1m")

        # Subscribe 5m
        subscribe_msg_5m = {
            "time": int(time.time()),
            "channel": "futures.candlesticks",
            "event": "subscribe",
            "payload": ["5m", symbol.upper()]
        }
        await self.ws.send(json.dumps(subscribe_msg_5m))
        self.log.d(self.tag, f"📡 Subscribed to: {symbol.upper()} - 5m")

    async def listen(self):
        """Lắng nghe dữ liệu từ WebSocket"""
        async for message in self.ws:
            try:
                self.last_message_time = time.time()
                data = json.loads(message)

                if data.get("channel") == "futures.candlesticks" and data.get("event") == "update":
                    result = data.get("result", {})
                    if isinstance(result, list) and len(result) > 0:
                        item = result[0]
                        if "n" in item:
                            name = item["n"]  # "1m_BTC_USDT"
                            parts = name.split("_", 1)
                            if len(parts) == 2:
                                gate_interval, symbol = parts

                                # Chuẩn hóa interval từ Gate format sang MEXC format
                                mexc_interval = self.normalize_interval_to_mexc(gate_interval)

                                # Chuẩn hóa candle data sang format MEXC
                                # MEXC format: {'t': timestamp, 'o': open, 'h': high, 'l': low, 'c': close, 'a': volume}
                                candle_data = {
                                    't': item["t"],  # timestamp
                                    'o': item["o"],  # open
                                    'h': item["h"],  # high
                                    'l': item["l"],  # low
                                    'c': item["c"],  # close
                                    'a': item["v"],  # volume (Gate: 'v', MEXC: 'a')
                                    'interval': mexc_interval  # Thêm interval vào data
                                }

                                # Notify với format giống MEXC
                                await self.notify(symbol, mexc_interval, candle_data)
                        else:
                            self.log.d(self.tag, f"⚠️ Missing 'n' in result[0]: {item}")
                    else:
                        self.log.d(self.tag, f"⚠️ Unexpected candlestick format: {result}")

                elif data.get("event") == "pong":
                    self.log.t(self.tag, "📶 Pong received")

                else:
                    pass  # Ignore other messages

            except Exception:
                self.log.d(self.tag, f"⚠️ Listen Exception:\n{traceback.format_exc()}")

    async def monitor_timeout(self):
        """Giám sát timeout và reconnect nếu không có dữ liệu"""
        while self.ws:
            await asyncio.sleep(10)
            if time.time() - self.last_message_time > 30:
                self.log.d(self.tag, "⏳ Timeout: No data in 30s, reconnecting...")
                await self.ws.close()
                break

    async def notify(self, symbol: str, interval: str, candle_data: dict):
        """
        Notify tất cả callbacks với format chuẩn hóa
        - symbol: "BTC_USDT" (Gate format, có thể khác MEXC "BTC_USDT" vs "BTCUSDT")
        - interval: "Min1", "Min5" (MEXC format)
        - candle_data: dict với keys 't', 'o', 'h', 'l', 'c', 'a'
        """
        # Chuẩn hóa symbol nếu cần (Gate: BTC_USDT, MEXC: BTCUSDT)
        normalized_symbol = symbol.replace("_", "")  # BTC_USDT -> BTCUSDT

        for callback in self.callbacks:
            asyncio.create_task(callback(normalized_symbol, interval, candle_data))
        await asyncio.sleep(0)

    async def add_symbols(self, new_symbols: list[Symbol]):
        """Thêm symbols mới vào subscription"""
        for symbol in new_symbols:
            await self.subscribe(symbol.symbol)
            self.symbols.append(symbol)

    def normalize_interval_to_mexc(self, gate_interval: str) -> str:
        """
        Chuyển đổi interval từ Gate format sang MEXC format
        Gate: "1m", "5m", "15m", "30m", "1h", etc.
        MEXC: "Min1", "Min5", "Min15", "Min30", "Hour1", etc.
        """
        mapping = {
            "1m": "Min1",
            "5m": "Min5",
            "10m": "Min15",  # Gate có 10m, map sang 15m
            "15m": "Min15",
            "30m": "Min30",
            "1h": "Hour1",
            "4h": "Hour4",
            "1d": "Day1",
        }
        return mapping.get(gate_interval, "Min1")

    @staticmethod
    def init_gate_symbols(log: LoggerWrapper, testnet: bool = False) -> list[Symbol]:
        """
        Lấy danh sách symbols từ Gate.io API

        Returns:
            List[Symbol]: Danh sách symbols có thể trade
        """
        tag = "GateSocket"
        symbols: list[Symbol] = []

        url = GATE_FUTURES_CONTRACTS_TESTNET if testnet else GATE_FUTURES_CONTRACTS

        try:
            log.i(tag, f"📥 Fetching symbols from {url}...")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            log.d(tag, f"Got {len(data)} contracts from API")

            for d in data:
                # Chỉ lấy hợp đồng đang giao dịch - Gate.io dùng "status": "trading"
                if d.get("status") != "trading":
                    continue

                name = d.get("name", "")  # "BTC_USDT", "SOL_USDT", ...

                if not name.endswith("_USDT"):
                    continue

                if name in BLACK_LIST_SYMBOL:
                    log.i(tag, f"Symbol {name} in black-list – ignore")
                    continue

                # Ánh xạ sang các tham số Symbol
                price_scale = GateSocket._price_scale_from_tick(d.get("order_price_round", "0.0001"))
                contract_size = float(d.get("quanto_multiplier", 1))
                max_vol = float(d.get("order_size_max", 100000))
                max_leverage = int(d.get("leverage_max", 100))

                # Tạo Symbol object - dùng positional args giống MEXC
                sym = Symbol(
                    name,
                    price_scale,
                    contract_size,
                    max_vol,
                    max_leverage
                )
                symbols.append(sym)

                log.d(tag, f"Symbol {name} ps={price_scale} cs={contract_size} vol={max_vol} lev={max_leverage}")

        except Exception as e:
            log.e(tag, f"Error fetching Gate symbols: {e}\n{traceback.format_exc()}")

        log.i(tag, f"✅ Loaded {len(symbols)} Gate.io symbols")
        return symbols

    @staticmethod
    def _price_scale_from_tick(tick_size: str) -> int:
        """
        Tính price_scale từ tick_size
        tick_size "0.01" -> price_scale = 2
        tick_size "0.0001" -> price_scale = 4
        """
        try:
            tick = float(tick_size)
            if tick >= 1:
                return 0
            # Đếm số chữ số sau dấu chấm
            tick_str = str(tick)
            if '.' in tick_str:
                return len(tick_str.split('.')[1].rstrip('0'))
            return 0
        except:
            return 4  # Default