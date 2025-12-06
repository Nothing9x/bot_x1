import json
import traceback

import websockets
import asyncio
import time

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
        self.callbacks = []  # Danh sách callback
        self.last_message_time = time.time()  # Lưu thời gian nhận dữ liệu cuối cùng
        self.monitor_task = None  # Task giám sát timeout
        self.chat_id = chat_id

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
                await self.tele_message.send_message(f"🔴 MEXC WebSocket disconnected: {traceback_str}", self.chat_id)
                self.log.d("MexcSocket", f"🔴 WebSocket disconnected: {traceback_str}")
            except Exception as e:
                traceback_str = traceback.format_exc()
                await self.tele_message.send_message(f"⚠️ MEXC WebSocket got an unexpected Error: {traceback_str}", self.chat_id)
                self.log.d("MexcSocket", f"⚠️ Unexpected Error: {traceback_str}")

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
                self.last_message_time = time.time()  # Cập nhật thời gian khi có dữ liệu mới
                data = json.loads(message)

                # if Constants.DEBUG_LOG:
                #     self.log.d("MexcSocket", f"📩 Raw market data: {data}\n")

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
        """Giám sát timeout và tự động reconnect nếu không có dữ liệu sau 60 giây"""
        while self.ws:
            await asyncio.sleep(10)  # Kiểm tra mỗi 10 giây
            if time.time() - self.last_message_time > 30:  # Nếu quá 60 giây không có dữ liệu
                self.log.d("MexcSocket", "⏳ Timeout: Không có dữ liệu trong 60 giây, reconnecting...")
                #await send_chat_to_channel("⏳ Timeout: Không có dữ liệu trong 60 giây, reconnecting...")
                await self.ws.close()  # Đóng WebSocket để `connect()` xử lý reconnect
                break  # Thoát vòng lặp

    async def notify(self, symbol, interval, data):
        for callback in self.callbacks:
            asyncio.create_task(callback(symbol, interval, data))
        await asyncio.sleep(0)

    async def add_symbols(self, new_symbols):
        """Đăng ký lắng nghe các cặp giao dịch"""
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

# Tạo một instance của MexcSocket

