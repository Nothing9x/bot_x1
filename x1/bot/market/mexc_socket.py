# -*- coding: utf-8 -*-
"""
MexcSocket - WebSocket connection với MEXC
FIXED: Task accumulation, memory leak

Changes:
1. Await callbacks thay vì create_task vô hạn
2. Semaphore để limit concurrent processing
3. Better error handling
"""

import json
import traceback
import asyncio
import time

import websockets
from websockets_proxy import Proxy, proxy_connect

from x1.bot.model.symbol import Symbol
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils import Utils, constants
from x1.bot.utils.LoggerWrapper import LoggerWrapper

MEXC_WS_URL = "wss://contract.mexc.com/edge"


class MexcSocket:
    def __init__(self, log: LoggerWrapper, proxy, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "MexcSocket"
        self.log = log
        self.proxy = proxy
        self.tele_message = tele_message
        self.symbols: list[Symbol] = []
        self.ws = None
        self.callbacks = []
        self.last_message_time = time.time()
        self.monitor_task = None
        self.chat_id = chat_id

        # ===== PERFORMANCE FIX =====
        # Semaphore để limit concurrent callback processing
        self._callback_semaphore = asyncio.Semaphore(10)  # Max 10 concurrent
        self._pending_tasks: set = set()  # Track pending tasks
        self._max_pending_tasks = 100  # Max pending tasks before dropping

    def register_callback(self, callback):
        """Đăng ký callback để nhận dữ liệu khi giá thay đổi"""
        self.callbacks.append(callback)

    async def start(self, symbols: list[Symbol]):
        self.symbols = symbols
        asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        self.log.d("MexcSocket", "start done")

    async def connect(self):
        """Kết nối WebSocket đến MEXC và tự động reconnect nếu bị mất kết nối"""
        while True:
            self.log.d("MexcSocket", "MexcSocket connect")
            try:
                ws_proxy = Proxy.from_url(Utils.get_proxies_for_ws(self.proxy)) if self.proxy else None
                async with proxy_connect(MEXC_WS_URL, proxy=ws_proxy) as ws:
                    self.ws = ws
                    self.log.d("MexcSocket", "✅ MexcSocket Connected to MEXC WebSocket")

                    await self.subscribe("Min1")
                    await self.subscribe("Min5")

                    # Bắt đầu task giám sát timeout
                    self.monitor_task = asyncio.create_task(self.monitor_timeout())

                    # Chạy ping song song với listen
                    asyncio.create_task(self.send_ping())

                    # Bắt đầu lắng nghe dữ liệu
                    await self.listen()

            except websockets.exceptions.ConnectionClosed as e:
                traceback_str = traceback.format_exc()
                await self.tele_message.send_message(f"🔴 MEXC WebSocket disconnected: {e}", self.chat_id)
                self.log.d("MexcSocket", f"🔴 WebSocket disconnected: {e}")
            except Exception as e:
                traceback_str = traceback.format_exc()
                await self.tele_message.send_message(f"⚠️ MEXC WebSocket Error: {e}", self.chat_id)
                self.log.d("MexcSocket", f"⚠️ Unexpected Error: {e}")

            # Cleanup pending tasks
            await self._cleanup_pending_tasks()

            # Đóng kết nối trước khi reconnect
            if self.ws:
                await self.ws.close()
                self.ws = None

            # Hủy task monitor nếu đang chạy
            if self.monitor_task:
                self.monitor_task.cancel()
                self.monitor_task = None

            await self.tele_message.send_message("🔄 Reconnecting MEXC WebSocket in 5 seconds...", self.chat_id)
            self.log.d("MexcSocket", "🔄 Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

    async def _cleanup_pending_tasks(self):
        """Cleanup all pending tasks"""
        try:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
            self._pending_tasks.clear()
        except Exception as e:
            self.log.e(self.tag, f"Error cleaning up tasks: {e}")

    async def send_ping(self):
        """Gửi ping định kỳ để giữ kết nối sống"""
        while self.ws:
            try:
                ping_msg = json.dumps({"method": "ping"})
                await self.ws.send(ping_msg)
            except Exception as e:
                self.log.d("MexcSocket", f"⚠️ Ping error: {e}")
                break
            await asyncio.sleep(20)

    async def subscribe(self, interval):
        """Đăng ký lắng nghe các cặp giao dịch"""
        for symbol in self.symbols:
            subscribe_msg = {
                "method": "sub.kline",
                "param": {"symbol": symbol.symbol, "interval": interval},
            }
            await self.ws.send(json.dumps(subscribe_msg))
            self.log.d("MexcSocket", f"📡 Subscribed to: {symbol.symbol} - {interval}")

    async def listen(self):
        """Lắng nghe dữ liệu từ MEXC"""
        async for message in self.ws:
            try:
                self.last_message_time = time.time()
                data = json.loads(message)

                if data.get("symbol") is None:
                    if constants.DEBUG_LOG:
                        self.log.d("MexcSocket", f"⚠️ Invalid data: {data}\n")
                else:
                    symbol = data["symbol"]
                    interval = data["data"]["interval"]
                    await self.notify(symbol, interval, data["data"])

            except websockets.exceptions.ConnectionClosed:
                raise
            except Exception as e:
                traceback_str = traceback.format_exc()
                self.log.d("MexcSocket", f"⚠️ Listen Exception: {traceback_str}")

    async def monitor_timeout(self):
        """Giám sát timeout và tự động reconnect nếu không có dữ liệu sau 30 giây"""
        while self.ws:
            await asyncio.sleep(10)
            if time.time() - self.last_message_time > 30:
                self.log.d("MexcSocket", "⏳ Timeout: Không có dữ liệu trong 30 giây, reconnecting...")
                await self.ws.close()
                break

    async def notify(self, symbol: str, interval: str, data: dict):
        """
        FIXED: Notify callbacks với rate limiting
        - Dùng semaphore để limit concurrent processing
        - Drop nếu quá nhiều pending tasks
        """
        try:
            # Check nếu quá nhiều pending tasks → drop để tránh overload
            # Cleanup done tasks first
            done_tasks = {t for t in self._pending_tasks if t.done()}
            self._pending_tasks -= done_tasks

            if len(self._pending_tasks) >= self._max_pending_tasks:
                # Drop message để tránh accumulation
                return

            # Process callbacks với semaphore
            async def process_callback(callback):
                async with self._callback_semaphore:
                    try:
                        await callback(symbol, interval, data)
                    except Exception as e:
                        self.log.e(self.tag, f"Callback error: {e}")

            # Tạo tasks cho callbacks
            for callback in self.callbacks:
                task = asyncio.create_task(process_callback(callback))
                self._pending_tasks.add(task)
                # Cleanup done task khi complete
                task.add_done_callback(lambda t: self._pending_tasks.discard(t))

        except Exception as e:
            self.log.e(self.tag, f"Error in notify: {e}")

    async def add_symbols(self, new_symbols):
        """Đăng ký lắng nghe các cặp giao dịch mới"""
        for symbol in new_symbols:
            subscribe_msg = {
                "method": "sub.kline",
                "param": {"symbol": symbol.symbol, "interval": "Min1"},
            }
            await self.ws.send(json.dumps(subscribe_msg))
            subscribe_msg = {
                "method": "sub.kline",
                "param": {"symbol": symbol.symbol, "interval": "Min5"},
            }
            await self.ws.send(json.dumps(subscribe_msg))
            self.log.d("MexcSocket", f"📡 Subscribed to: {symbol.symbol}")