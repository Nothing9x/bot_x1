# -*- coding: utf-8 -*-
"""
GatePositionSocket - Gate.io Futures Position WebSocket
Sử dụng lowercase attributes từ BotConfig database
"""
from aiohttp_socks import ProxyConnector

import asyncio
import json
import time
import hmac
import hashlib
import aiohttp
import traceback

from x1.bot.database.database_models import BotConfig
from x1.bot.exchange.position.i_position_socket import IPositionSocket
from x1.bot.model.reposonse.gate_order_response import GateOrderResponse
from x1.bot.model.reposonse.gate_position_response import GatePositionResponse
from x1.bot.utils import Utils
from x1.bot.utils.LoggerWrapper import LoggerWrapper

GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
GATE_WS_URL_TEST_NET = "wss://fx-ws-testnet.gateio.ws/v4/ws/usdt"


class GatePositionSocket(IPositionSocket):
    def __init__(self, bot: BotConfig, log: LoggerWrapper, position_callback, trade_callback):
        self.tag = "GatePositionSocket"
        self.log = log
        self.bot = bot
        self.ws = None
        self.position_callback = position_callback
        self.trade_callback = trade_callback
        self.last_message_time = time.time()
        self.ping_task = None
        self.event_task = None

    # ===== Helper để lấy config values =====
    def _get_api_key(self):
        return getattr(self.bot, 'api_key', '') or ''

    def _get_api_secret(self):
        return getattr(self.bot, 'api_secret', '') or ''

    def _get_proxy(self):
        return getattr(self.bot, 'proxy', '') or ''

    async def start_position_socket(self):
        self.event_task = asyncio.create_task(self.connect())
        self.log.d(self.tag, "start position socket done")

    async def stop_position_socket(self):
        await Utils.stop_task(self.event_task)
        self.event_task = None
        await Utils.stop_task(self.ping_task)
        self.ping_task = None
        self.log.d(self.tag, "stop done")

    async def connect(self):
        self.log.d(self.tag, "start GatePositionSocket")
        try:
            enable = True
            while True:
                try:
                    proxy = self._get_proxy()
                    if proxy == "":
                        connector = None
                    else:
                        connector = ProxyConnector.from_url(f"http://{proxy}")
                    self.log.d(self.tag, "✅ Connecting to Gate.io Position WebSocket")
                    async with aiohttp.ClientSession(connector=connector) as session:
                        async with session.ws_connect(GATE_WS_URL) as ws:
                            if not enable:
                                await self.trade_callback(True)
                                enable = True
                            self.ws = ws
                            self.log.d(self.tag, "✅ Connected to Gate.io Position WebSocket")
                            await self._auth()
                            self.ping_task = asyncio.create_task(self.send_ping())
                            await self.listen()
                except Exception as e:
                    self.log.d(self.tag, f"🔴 disconnected: {traceback.format_exc()}")
                    await self.trade_callback(False)
                    enable = False
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                self.log.d(self.tag, "🔄 Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            self.log.d(self.tag, "stop GatePositionSocket")

    async def send_ping(self):
        try:
            while self.ws:
                await self.ws.send_str(json.dumps({
                    "channel": "futures.ping",
                    "time": int(time.time())
                }))
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            self.log.d(self.tag, "⛔ send_ping task cancelled")

    async def listen(self):
        async for message in self.ws:
            try:
                data = json.loads(message.data)
                self.last_message_time = time.time()
                channel = data.get("channel")
                self.log.d(self.tag, data)
                if channel == "futures.orders" and data.get("event") == "update":
                    order_info = data.get("result", [])[0]
                    symbol = order_info["contract"]
                    order_response = GateOrderResponse(order_info)
                    await self.position_callback(symbol, order_response, None)
                if channel == "futures.positions" and data.get("event") == "update":
                    position_info = data.get("result", [])[0]
                    symbol = position_info["contract"]
                    position_response = GatePositionResponse(position_info)
                    await self.position_callback(symbol, None, position_response)
            except Exception as e:
                self.log.e(self.tag, f"⚠️ Listen Exception: {e}")
                traceback.print_exc()

    async def _auth(self):
        current_time = int(time.time())
        api_secret = self._get_api_secret()
        api_key = self._get_api_key()

        sign_msg = f"api\nfutures.login\n\n{current_time}"
        sign = hmac.new(
            api_secret.encode(),
            sign_msg.encode(),
            hashlib.sha512
        ).hexdigest()

        login_req = {
            "time": current_time,
            "channel": "futures.login",
            "event": "api",
            "payload": {
                "api_key": api_key,
                "signature": sign,
                "timestamp": str(current_time),
                "req_id": f"{current_time}‑login"
            }
        }
        await self.ws.send_str(json.dumps(login_req))
        await self.subscribe()

    async def subscribe(self):
        api_key = self._get_api_key()
        api_secret = self._get_api_secret()

        for ch in ("futures.orders", "futures.positions"):
            ts = int(time.time())
            sign_msg = f"channel={ch}&event=subscribe&time={ts}"
            sign = hmac.new(api_secret.encode(), sign_msg.encode(), hashlib.sha512).hexdigest()
            await self.ws.send_str(json.dumps({
                "time": ts,
                "channel": ch,
                "event": "subscribe",
                "payload": ["!all"],
                "auth": {
                    "method": "api_key",
                    "KEY": api_key,
                    "SIGN": sign
                }
            }))
        self.log.d(self.tag, "📡 Subscribed to: futures.orders, futures.positions")