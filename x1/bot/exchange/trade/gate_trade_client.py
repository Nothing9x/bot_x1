# -*- coding: utf-8 -*-
"""
GateTradeClient - Gate.io Futures Trading Client
Sử dụng lowercase attributes từ BotConfig database
"""
from __future__ import annotations

import asyncio
import json
import traceback
from collections import deque
from decimal import Decimal
from typing import Dict, Optional

import gate_api
from gate_api import FuturesPositionCrossMode
from gate_api.exceptions import GateApiException, ApiException
from urllib3 import make_headers

from x1.bot.database.database_models import BotConfig
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.exchange.trade.i_trade_client import ITradeClient
from x1.bot.exchange.trade.trade_side import TradeSide
from x1.bot.utils import Utils
from x1.bot.utils.LoggerWrapper import LoggerWrapper

RULE_MAP = {">=": 1, "<=": 2}


class GateTradeClient(ITradeClient):
    HOST = "https://api.gate.io/api/v4"
    TEST_NET_HOST = "https://fx-api-testnet.gateio.ws/api/v4"
    IS_DEV = False

    DEFAULT_SETTLE = "usdt"

    def __init__(
            self,
            bot: BotConfig,
            telegramMessage: TelegramMessageQueue,
            log: LoggerWrapper,
            trade_callback,
    ):
        self.bot = bot
        self.tele = telegramMessage
        self.log = log
        self.tag = "GateTradeClient"
        self.trade_callback = trade_callback

        self._queue: asyncio.Queue = asyncio.Queue()
        self._trade_task: Optional[asyncio.Task] = None
        self._cancel_cache: deque[int] = deque(maxlen=100)

        # ✨ CHANGED: Dùng lowercase attributes
        api_key = getattr(bot, 'api_key', '') or ''
        api_secret = getattr(bot, 'api_secret', '') or ''
        proxy = getattr(bot, 'proxy', '') or ''

        if self.IS_DEV:
            cfg = gate_api.Configuration(host=self.TEST_NET_HOST,
                                         key=api_key,
                                         secret=api_secret)
        else:
            cfg = gate_api.Configuration(key=api_key,
                                         secret=api_secret)
        if proxy:
            auth_part, addr_part = proxy.split("@")
            user, pwd = auth_part.split(":", 1)
            host, port = addr_part.split(":", 1)
            cfg.proxy = f"http://{host}:{port}"
            cfg.proxy_headers = make_headers(proxy_basic_auth=f"{user}:{pwd}")
        else:
            cfg.proxy = None
        self._client = gate_api.ApiClient(cfg)
        self._fapi = gate_api.FuturesApi(self._client)

        self._leverage_cache: Dict[str, int] = {}
        self._dual_mode_checked = False

    # ===== Helper để lấy config values =====
    def _get_chat_id(self):
        return getattr(self.bot, 'chat_id', '') or ''

    def _get_leverage(self):
        return getattr(self.bot, 'leverage', 20) or 20

    def _get_auto_place_sl_market(self):
        return getattr(self.bot, 'auto_place_sl_market', True)

    # ──────────────────────────── WORKER CONTROL ──────────────────────
    async def start(self):
        if not self._trade_task or self._trade_task.done():
            self._trade_task = asyncio.create_task(self._worker())
        self.log.d(self.tag, "start done")

    async def stop(self):
        if self._trade_task:
            await Utils.stop_task(self._trade_task)
            self._trade_task = None
        self.log.d(self.tag, "stop done")

    # ──────────────────────────── PUBLIC API ─────────────────────────
    async def send_order(
            self,
            orderId: int = -1,
            symbol: str = "",
            price: float | Decimal = 0,
            quantity: float | Decimal = 0,
            side: int = 0,
            leverage: Optional[int] = None,
            ps: int = 1,
            take_profit: Optional[float] = None,
            stop_loss: Optional[float] = None,
            tag: str = "",
    ):
        if leverage is None:
            leverage = self._get_leverage()

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put(
            (
                fut,
                orderId,
                symbol,
                Decimal(str(price)) if price is not None else None,
                quantity,
                side,
                Decimal(str(take_profit)) if take_profit is not None else None,
                Decimal(str(stop_loss)) if stop_loss is not None else None,
                leverage,
                ps,
            )
        )
        try:
            res = await asyncio.wait_for(fut, timeout=60)
            return None if res == -1 else res
        except asyncio.TimeoutError:
            msg = (
                f"{tag} TIMEOUT symbol={symbol} price={price} qty={quantity} side={side}"
            )
            self.log.e(self.tag, msg)
            await self.tele.send_message(msg, self._get_chat_id())
            return None

    # ──────────────────────────── INTERNAL WORKER ────────────────────
    async def _worker(self):
        self.log.d(self.tag, "Start _worker")
        try:
            while True:
                (
                    fut,
                    orderId,
                    symbol,
                    price,
                    qty,
                    side,
                    tp,
                    sl,
                    lev,
                    ps,
                ) = await self._queue.get()
                try:
                    if orderId == -1:
                        res = await self.place_order(symbol, price, qty, side, lev, ps, tp, sl)
                    else:
                        res = await self.cancel_order(orderId)
                    if not fut.done():
                        fut.set_result(res)
                except Exception as e:
                    tb = traceback.format_exc()
                    self.log.e(self.tag, f"worker error: {e}\n{tb}")
                    if not fut.done():
                        fut.set_result(-1)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            self.log.d(self.tag, "Stop _worker")

    # ──────────────────────────── ORDER HELPERS ──────────────────────
    async def _ensure_dual_mode(self):
        if self._dual_mode_checked:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._fapi.set_dual_mode(
                    self.DEFAULT_SETTLE, True
                ),
            )
            self._dual_mode_checked = True
            self.log.d(self.tag, "Dual‑position mode enabled")
        except (GateApiException, ApiException) as e:
            body = e.body
            err = json.loads(body)
            label = err.get("label")
            if label != "NO_CHANGE":
                self.log.w(self.tag, f"Enable dual_mode failed (maybe already on): {e}")
            self._dual_mode_checked = True

    async def place_order(self, symbol, price, quantity, side, leverage, ps, take_profit=None, stop_loss=None,
                          retry_count=3):
        self.log.i(
            self.tag,
            f"PLACE {symbol} qty={quantity} side={side} lev={leverage} price={price} TP={take_profit} SL={stop_loss}"
        )
        await self._ensure_dual_mode()
        await self._ensure_leverage(symbol, leverage)

        size, reduce_only, position_is_long = self._translate_side(quantity, side)
        try:
            if stop_loss:
                return await self._create_tp_sl(contract=symbol, is_long=position_is_long, tp=take_profit, sl=stop_loss)
            elif take_profit:
                order = gate_api.FuturesOrder(
                    contract=symbol,
                    size=0,
                    price="0" if float(take_profit) == 0 else str(take_profit),
                    tif="ioc" if float(take_profit) == 0 else "gtc",
                    text="t-tp",
                    auto_size="close_long" if position_is_long else "close_short",
                    reduce_only=True,
                    close=False
                )
            else:
                order = gate_api.FuturesOrder(
                    contract=symbol,
                    size=int(size),
                    price="0" if float(price) == 0 else str(price),
                    tif="ioc" if float(price) == 0 else "gtc",
                    text="t-open",
                    reduce_only=reduce_only,
                )
            created = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._fapi.create_futures_order(self.DEFAULT_SETTLE, order)
            )
            oid = created.id
            return oid
        except (GateApiException, ApiException) as e:
            self.log.e(self.tag, f"Gate place error {symbol}: {e}")
            body = e.body
            err = json.loads(body)
            label = err.get("label")
            if label == "REDUCE_EXCEEDED" or "POSITION_EMPTY":
                return 2009
            if label == "CONTRACT_IN_DELISTING":
                return 8819
            if retry_count > 0:
                await asyncio.sleep(1)
                return await self.place_order(
                    symbol, price, quantity, side, leverage, ps, take_profit, stop_loss, retry_count - 1
                )
            await self.tele.send_message(f"Gate place order error: {e}", self._get_chat_id())
            return -1

    async def _create_tp_sl(
            self,
            contract: str,
            is_long: bool,
            tp: Optional[float],
            sl: Optional[float],
    ) -> int:
        def _build(price: float, rule_str: str, label: str):
            rule = RULE_MAP[rule_str]
            order_type = "plan-close-long-position" if is_long else "plan-close-short-position"

            initial = gate_api.FuturesInitialOrder(
                contract=contract,
                size=0,
                price="0",
                tif="ioc",
                reduce_only=True,
                auto_size="close_long" if is_long else "close_short",
                text=f"t-{label}",
            )
            trigger = gate_api.FuturesPriceTrigger(price=str(price), rule=rule)
            return gate_api.FuturesPriceTriggeredOrder(
                trigger=trigger,
                initial=initial,
                order_type=order_type,
            )

        async def _submit(obj):
            return await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._fapi.create_price_triggered_order(self.DEFAULT_SETTLE, obj)
            )

        try:
            if tp:
                res = await _submit(_build(tp, ">=" if is_long else "<=", "tp"))
                return res.id
            if sl:
                res = await _submit(_build(sl, "<=" if is_long else ">=", "sl"))
                return res.id
        except (GateApiException, ApiException) as e:
            self.log.e(self.tag, f"TP/SL creation failed: {e}")
            body = getattr(e, "body", None)
            await self.tele.send_message(
                f"⚠️ Failed to place initial SL. Please open the web/app to handle this case:\n{body or e}",
                self._get_chat_id()
            )

            if self._get_auto_place_sl_market():
                try:
                    order = gate_api.FuturesOrder(
                        contract=contract,
                        size=0,
                        price="0",
                        tif="ioc",
                        reduce_only=True,
                        auto_size="close_long" if is_long else "close_short",
                        text="t-close-exchange",
                    )
                    res = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self._fapi.create_futures_order(self.DEFAULT_SETTLE, order)
                    )
                    self.log.e(self.tag, f"✅ Retry place SL succeeded: closed position at exchange. Order ID: {res.id}")
                    await self.tele.send_message(f"✅ Retry place SL succeeded: closed position at exchange.",
                                                 self._get_chat_id())
                    return res.id
                except Exception as ex:
                    self.log.e(self.tag, f"❌ Retry close exchange failed: {ex}")
                    await self.tele.send_message(f"❌ Retry close exchange failed: {ex}", self._get_chat_id())
        return -1

    async def cancel_order(self, oid: int):
        if oid in self._cancel_cache:
            return oid
        try:
            self._cancel_cache.append(oid)
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._fapi.cancel_futures_order(self.DEFAULT_SETTLE, str(oid))
            )
            return oid
        except (GateApiException, ApiException) as e:
            body = e.body
            err = json.loads(body)
            label = err.get("label")
            if label == "ORDER_NOT_FOUND":
                return oid
            self.log.e(self.tag, f"Cancel error: {e}")
            await self.tele.send_message(f"Cancel_order {oid}, error: {e}", self._get_chat_id())
            return -1

    # ──────────────────────────── UTILS ─────────────────────────────
    async def _ensure_leverage(self, contract: str, lev: int):
        if self._leverage_cache.get(contract) == lev:
            return

        def _call():
            self._update_dual_mode_cross(contract, limit_lev=lev)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _call)
            self._leverage_cache[contract] = lev
            self.log.d(self.tag, f"Leverage {contract}=x{lev} set")
        except Exception as e:
            self.log.e(self.tag, f"Set leverage error: {e}")

    def _update_dual_mode_cross(self, contract: str, limit_lev: int | None = None):
        try:
            mode = FuturesPositionCrossMode(mode="CROSS", contract=contract)
            self._fapi.update_position_cross_mode(self.DEFAULT_SETTLE, mode)
            self.log.i(self.tag, f"[CROSS] Dual-mode enabled for {self.DEFAULT_SETTLE}")
        except (GateApiException, ApiException) as ex:
            self.log.e(self.tag, f"[CROSS] Enable cross failed {contract}: {ex}")

        if limit_lev is not None:
            try:
                self._fapi.update_dual_mode_position_leverage(self.DEFAULT_SETTLE,
                                                              contract,
                                                              leverage="0",
                                                              cross_leverage_limit=str(int(limit_lev)))
                self.log.i(self.tag, f"[DUAL CROSS] Set cross_leverage_limit={limit_lev} for {contract} OK")
            except (GateApiException, ApiException) as ex:
                self.log.e(self.tag, f"[DUAL CROSS] Set limit ApiException {contract}: {ex}")

    def _update_single_mode_cross(self, contract: str, limit_lev: int | None = None):
        try:
            self._fapi.update_position_leverage(self.DEFAULT_SETTLE,
                                                contract,
                                                leverage="0",
                                                cross_leverage_limit=str(int(limit_lev)))
            self.log.i(self.tag, f"[SINGLE CROSS] Set cross_leverage_limit={limit_lev} for {contract} OK")
        except (GateApiException, ApiException) as ex:
            self.log.e(self.tag, f"[SINGLE CROSS] Set limit ApiException {contract}: {ex}")

    @staticmethod
    def _translate_side(qty: int | Decimal, side_code: int):
        """Return (size, reduce_only, is_long_side)."""
        if side_code == TradeSide.OPEN_LONG:
            return qty, False, True
        if side_code == TradeSide.CLOSE_SHORT:
            return qty, True, False
        if side_code == TradeSide.OPEN_SHORT:
            return -qty, False, False
        if side_code == TradeSide.CLOSE_LONG:
            return -qty, True, True
        raise ValueError(f"Unsupported side {side_code}")

    async def close_all_positions_and_orders(self):
        fapi = self._fapi
        settle = self.DEFAULT_SETTLE
        chat_id = self._get_chat_id()

        msgs: list[str] = ["🧹 Close all positions and orders summary:"]
        errs: list[str] = []
        open_total = open_canceled = 0
        pt_total = pt_canceled = 0
        closed_count = 0

        def _fmt_err(prefix: str, e: Exception) -> str:
            body = getattr(e, "body", None)
            return f"{prefix}: {body or e}"

        # 1) Cancel ALL open normal orders
        try:
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, lambda: fapi.list_futures_orders(settle, status="open", limit=100)
            )
            open_total = len(open_orders or [])

            if open_orders:
                async def _cancel_one(o):
                    nonlocal open_canceled
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda: fapi.cancel_futures_order(settle, o.id)
                        )
                        open_canceled += 1
                    except Exception as ce:
                        err = _fmt_err("❌ Cancel order failed", ce)
                        self.log.e(self.tag, err)
                        errs.append(f"• {err}")

                await asyncio.gather(*[_cancel_one(o) for o in open_orders])

            self.log.i(self.tag, f"✅ Canceled {open_canceled}/{open_total} open orders")
            msgs.append(f"✅ Open orders canceled: {open_canceled}/{open_total}")
        except Exception as e:
            err = _fmt_err("❌ List/Cancel open orders failed", e)
            self.log.e(self.tag, err)
            errs.append(f"• {err}")
            msgs.append(f"✅ Open orders canceled: {open_canceled}/{open_total}")

        # 2) Cancel ALL open price-triggered orders (TP/SL)
        try:
            pt_orders = await asyncio.get_event_loop().run_in_executor(
                None, lambda: fapi.list_price_triggered_orders(settle, status="open", limit=100)
            )
            pt_total = len(pt_orders or [])

            if pt_orders:
                async def _cancel_pt(po):
                    nonlocal pt_canceled
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda: fapi.cancel_price_triggered_order(settle, po.id)
                        )
                        pt_canceled += 1
                    except Exception as ce:
                        err = _fmt_err("❌ Cancel price-triggered order failed", ce)
                        self.log.e(self.tag, err)
                        errs.append(f"• {err}")

                await asyncio.gather(*[_cancel_pt(po) for po in pt_orders])

            self.log.i(self.tag, f"✅ Canceled {pt_canceled}/{pt_total} TP/SL orders")
            msgs.append(f"✅ TP/SL orders canceled: {pt_canceled}/{pt_total}")
        except Exception as e:
            err = _fmt_err("❌ List/Cancel price-triggered orders failed", e)
            self.log.e(self.tag, err)
            errs.append(f"• {err}")
            msgs.append(f"✅ TP/SL orders canceled: {pt_canceled}/{pt_total}")

        # 3) Close ALL positions (exchange)
        try:
            positions = await asyncio.get_event_loop().run_in_executor(
                None, lambda: fapi.list_positions(settle)
            )

            async def _close_side(symbol: str, is_long: bool):
                nonlocal closed_count
                try:
                    order = gate_api.FuturesOrder(
                        contract=symbol,
                        size=0,
                        price="0",
                        tif="ioc",
                        reduce_only=True,
                        auto_size="close_long" if is_long else "close_short",
                        text="t-close-all",
                    )
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: fapi.create_futures_order(settle, order)
                    )
                    closed_count += 1
                except Exception as ce:
                    err = _fmt_err(f"❌ Close {'LONG' if is_long else 'SHORT'} failed", ce)
                    self.log.e(self.tag, err)
                    errs.append(f"• {err}")

            tasks = []
            for p in positions or []:
                contract = p.contract
                long_sz = float(getattr(p, "long_size", 0) or 0)
                short_sz = float(getattr(p, "short_size", 0) or 0)

                if long_sz == 0 and short_sz == 0:
                    size = float(getattr(p, "size", 0) or 0)
                    if size > 0:
                        tasks.append(_close_side(contract, True))
                    elif size < 0:
                        tasks.append(_close_side(contract, False))
                else:
                    if long_sz > 0:
                        tasks.append(_close_side(contract, True))
                    if short_sz > 0:
                        tasks.append(_close_side(contract, False))

            if tasks:
                await asyncio.gather(*tasks)

            self.log.i(self.tag, f"✅ Closed {closed_count} position side(s) by exchange")
            msgs.append(f"✅ Position sides closed by exchange: {closed_count}")
        except Exception as e:
            err = _fmt_err("❌ List/Close positions failed", e)
            self.log.e(self.tag, err)
            errs.append(f"• {err}")
            msgs.append(f"✅ Position sides closed by exchange: {closed_count}")

        # 4) Send ONE consolidated Telegram message
        if errs:
            msgs.append("—")
            msgs.append("⚠️ Details:")
            msgs.extend(errs)

        await self.tele.send_message("\n".join(msgs), chat_id)