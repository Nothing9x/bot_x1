# -*- coding: utf-8 -*-
"""
TradingBot - Bot trading với Reduce TP Strategy
- Reduce TP: TP giảm dần về entry rồi về SL theo thời gian
- Telegram notifications cho cả REAL và SIM
- Cache config values để tránh SQLAlchemy detached session error
- ✨ NEW: Support GateTradeClient cho real trading
"""

import asyncio
import time
import traceback
from datetime import datetime
from typing import Dict, Optional

from x1.bot.database.database_models import BotConfig, DatabaseManager, Order, OrderStatusEnum, DirectionEnum, \
    TradeStatusEnum, Trade, TradeModeEnum


class TradingBot:
    """
    Bot trading với Reduce TP Strategy

    Reduce TP: Mỗi phút, TP giảm reduce% khoảng cách về phía SL
    - Phút 0: TP = Entry ± TP%
    - Mỗi phút: TP giảm reduce% × (TP_distance + SL_distance)
    - Cuối cùng: TP = SL → force close

    ✨ Real Trading: Dùng GateTradeClient để vào/ra lệnh thật
    """

    def __init__(self, bot_config: BotConfig, db_manager: DatabaseManager,
                 log, tele_message, exchange=None, chat_id="",
                 trade_client=None, position_socket=None):
        """
        Args:
            bot_config: Config từ database
            db_manager: Database manager
            log: Logger
            tele_message: Telegram message queue
            exchange: (deprecated) CCXT exchange - không dùng nữa
            chat_id: Default chat ID
            trade_client: GateTradeClient instance cho real trading
            position_socket: GatePositionSocket instance để nhận order updates
        """
        self.db_manager = db_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange  # Deprecated, giữ lại cho backward compatibility
        self.chat_id = chat_id

        # ✨ NEW: Real trading clients
        self.trade_client = trade_client  # GateTradeClient
        self.position_socket = position_socket  # GatePositionSocket

        # ===== CACHE CONFIG VALUES =====
        self.bot_config_id = bot_config.id
        self.bot_name = bot_config.name
        self.direction = bot_config.direction
        self.take_profit = bot_config.take_profit
        self.stop_loss = bot_config.stop_loss
        self.position_size_usdt = bot_config.position_size_usdt
        self.price_increase_threshold = bot_config.price_increase_threshold
        self.volume_multiplier = bot_config.volume_multiplier
        self.rsi_threshold = bot_config.rsi_threshold
        self.min_confidence = bot_config.min_confidence
        self.min_trend_strength = getattr(bot_config, 'min_trend_strength', 0.0)
        self.require_breakout = getattr(bot_config, 'require_breakout', False)
        self.min_volume_consistency = getattr(bot_config, 'min_volume_consistency', 0.0)
        self.timeframe = getattr(bot_config, 'timeframe', '1m')
        self.trade_mode = bot_config.trade_mode
        self.is_active = bot_config.is_active

        # ✨ NEW: Real bot specific fields
        self.is_real_bot = getattr(bot_config, 'is_real_bot', False)
        self.leverage = getattr(bot_config, 'leverage', 20) or 20

        # ===== REDUCE TP CONFIG =====
        self.reduce = getattr(bot_config, 'reduce', 5) or 5

        # Stats
        self.total_trades = bot_config.total_trades
        self.winning_trades = bot_config.winning_trades
        self.losing_trades = bot_config.losing_trades
        self.total_pnl = bot_config.total_pnl
        self.win_rate = bot_config.win_rate

        self.tag = f"Bot-{self.bot_name}"

        # Active positions tracking
        self.active_trades = {}  # {symbol: trade_id}
        self.pending_orders = {}  # {symbol: order_info}

        # ✨ NEW: Track exchange order IDs for real trading
        self.exchange_orders = {}  # {symbol: {'entry_order_id': x, 'tp_order_id': y, 'sl_order_id': z}}

        # ===== REDUCE TP TRACKING =====
        self.trade_reduce_info = {}  # {symbol: {initial_tp, last_reduce_minute}}

    async def start(self):
        """Start bot - khởi động trade client và position socket nếu là real bot"""
        if self.trade_mode == TradeModeEnum.REAL and self.trade_client:
            await self.trade_client.start()
            self.log.i(self.tag, f"✅ Trade client started for {self.bot_name}")

        if self.trade_mode == TradeModeEnum.REAL and self.position_socket:
            await self.position_socket.start_position_socket()
            self.log.i(self.tag, f"✅ Position socket started for {self.bot_name}")

    async def stop(self):
        """Stop bot - dừng trade client và position socket"""
        if self.trade_client:
            await self.trade_client.stop()

        if self.position_socket:
            await self.position_socket.stop_position_socket()

        self.log.i(self.tag, f"🛑 Bot {self.bot_name} stopped")

    def _get_config_string(self) -> str:
        """Tạo string config ngắn gọn"""
        reduce_str = f"_R{self.reduce}" if self.reduce > 0 else ""
        return (
            f"TP{self.take_profit}%_SL{self.stop_loss}%{reduce_str}_"
            f"Vol{self.volume_multiplier}x_Conf{self.min_confidence}%"
        )

    def _convert_to_gate_symbol(self, symbol: str) -> str:
        """
        Convert symbol từ MEXC format sang Gate.io format
        BTCUSDT -> BTC_USDT
        ETHUSDT -> ETH_USDT
        """
        if '_' in symbol:
            return symbol  # Đã là Gate format

        # Tìm vị trí USDT
        if symbol.endswith('USDT'):
            base = symbol[:-4]
            return f"{base}_USDT"
        elif symbol.endswith('USD'):
            base = symbol[:-3]
            return f"{base}_USD"
        elif symbol.endswith('BTC'):
            base = symbol[:-3]
            return f"{base}_BTC"

        return symbol  # Không đổi nếu không match

    def should_enter(self, signal: Dict) -> bool:
        """Kiểm tra điều kiện vào lệnh"""
        if signal.get('timeframe', '1m') != self.timeframe:
            return False

        if self.timeframe == '1m':
            price_change = signal.get('price_change_1m', 0)
        else:
            price_change = signal.get('price_change_5m', 0)

        if price_change < self.price_increase_threshold:
            return False

        if signal.get('volume_ratio', 0) < self.volume_multiplier:
            return False

        rsi = signal.get('rsi')
        if rsi is not None and rsi < self.rsi_threshold:
            return False

        if signal.get('confidence', 0) < self.min_confidence:
            return False

        if self.min_trend_strength > 0:
            if signal.get('trend_strength', 0) < self.min_trend_strength:
                return False

        if self.require_breakout:
            if not signal.get('is_breakout', False):
                return False

        if self.min_volume_consistency > 0:
            if signal.get('volume_consistency', 0) < self.min_volume_consistency:
                return False

        return True

    async def on_signal(self, signal: Dict):
        """Nhận signal và quyết định vào lệnh"""
        try:
            symbol = signal['symbol']

            if not self.should_enter(signal):
                return

            if symbol in self.active_trades:
                return

            if symbol in self.pending_orders:
                return

            await self.enter_position(signal)

        except Exception as e:
            self.log.e(self.tag, f"Error handling signal: {e}\n{traceback.format_exc()}")

    async def enter_position(self, signal: Dict):
        """Vào lệnh - REAL hoặc SIMULATED"""
        try:
            symbol = signal['symbol']
            entry_price = signal['price']

            # Calculate targets
            if self.direction == DirectionEnum.LONG:
                take_profit = entry_price * (1 + self.take_profit / 100)
                stop_loss = entry_price * (1 - self.stop_loss / 100)
            else:
                take_profit = entry_price * (1 - self.take_profit / 100)
                stop_loss = entry_price * (1 + self.stop_loss / 100)

            quantity = self.position_size_usdt / entry_price

            # Create trade record in database
            session = self.db_manager.get_session()

            trade = Trade(
                bot_config_id=self.bot_config_id,
                symbol=symbol,
                direction=self.direction,
                trade_mode=self.trade_mode,
                entry_price=entry_price,
                entry_time=datetime.now(),
                quantity=quantity,
                take_profit=take_profit,
                stop_loss=stop_loss,
                highest_price=entry_price,
                lowest_price=entry_price,
                status=TradeStatusEnum.OPEN,
                signal_data=str(signal)
            )

            session.add(trade)
            session.commit()
            trade_id = trade.id
            session.close()

            # Track active trade
            self.active_trades[symbol] = trade_id

            # ===== REDUCE TP: Lưu initial TP =====
            self.trade_reduce_info[symbol] = {
                'initial_tp': take_profit,
                'last_reduce_minute': 0,
                'entry_time': datetime.now(),
            }

            if self.trade_mode == TradeModeEnum.REAL:
                await self.place_real_order(trade_id, symbol, entry_price, quantity, take_profit, stop_loss)
            else:
                await self.place_simulated_order(trade_id, symbol, entry_price, quantity)

            # Log
            mode_str = "🔴 REAL" if self.trade_mode == TradeModeEnum.REAL else "🔵 SIM"
            direction_str = "📈 LONG" if self.direction == DirectionEnum.LONG else "📉 SHORT"
            reduce_str = f" | Reduce: {self.reduce}%/min" if self.reduce > 0 else ""

            self.log.i(self.tag,
                       f"[{mode_str}] {direction_str} {symbol} @ ${entry_price:.6f} | "
                       f"TP: ${take_profit:.6f} | SL: ${stop_loss:.6f}{reduce_str}"
                       )

            # Telegram notification
            await self._send_entry_notification(
                symbol, entry_price, take_profit, stop_loss, quantity, signal
            )

        except Exception as e:
            self.log.e(self.tag, f"Error entering position: {e}\n{traceback.format_exc()}")

    async def _send_entry_notification(self, symbol: str, entry_price: float,
                                       take_profit: float, stop_loss: float,
                                       quantity: float, signal: Dict):
        """Gửi Telegram notification khi mở lệnh"""
        try:
            direction = self.direction.value
            direction_emoji = "📈" if direction == "LONG" else "📉"

            if self.trade_mode == TradeModeEnum.REAL:
                mode_emoji = "🔴"
                mode_text = "REAL"
            else:
                mode_emoji = "🔵"
                mode_text = "SIM"

            # Reduce info
            reduce_str = f"⏱️ Reduce: {self.reduce}%/min\n" if self.reduce > 0 else ""

            message = (
                f"{mode_emoji} <b>{mode_text} OPEN {direction}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot: <b>{self.bot_name}</b>\n"
                f"🪙 Symbol: <b>{symbol}</b>\n"
                f"\n"
                f"💰 Entry: ${entry_price:.6f}\n"
                f"🎯 TP: ${take_profit:.6f} (+{self.take_profit}%)\n"
                f"🛑 SL: ${stop_loss:.6f} (-{self.stop_loss}%)\n"
                f"📦 Size: ${self.position_size_usdt}\n"
                f"{reduce_str}"
                f"\n"
                f"⚙️ <b>Config:</b>\n"
                f"├ {direction_emoji} {direction}\n"
                f"├ TP: {self.take_profit}% | SL: {self.stop_loss}%\n"
                f"├ Vol: {self.volume_multiplier}x | Conf: {self.min_confidence}%\n"
                f"└ Reduce: {self.reduce}%/min\n"
                f"\n"
                f"📊 Signal: Conf={signal.get('confidence', 0)}% | "
                f"Vol={signal.get('volume_ratio', 0):.1f}x"
            )

            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error sending entry notification: {e}")

    async def place_real_order(self, trade_id: int, symbol: str, price: float,
                               quantity: float, take_profit: float, stop_loss: float):
        """
        ✨ Place lệnh THẬT qua GateTradeClient
        """
        try:
            if not self.trade_client:
                self.log.w(self.tag, f"⚠️ No trade client - skipping real order for {symbol}")
                await self.cancel_trade(trade_id, "No trade client configured")
                return

            # Import TradeSide
            from x1.bot.exchange.trade.trade_side import TradeSide

            # ✨ Convert symbol format: BTCUSDT -> BTC_USDT (Gate.io format)
            gate_symbol = self._convert_to_gate_symbol(symbol)

            # Determine side
            if self.direction == DirectionEnum.LONG:
                side = TradeSide.OPEN_LONG
            else:
                side = TradeSide.OPEN_SHORT

            # Calculate quantity in contracts (Gate.io uses contracts, not coins)
            qty_contracts = int(quantity)  # Hoặc tính toán dựa trên contract size

            self.log.i(self.tag, f"📤 Placing REAL order: {gate_symbol} qty={qty_contracts} side={side}")

            # Place entry order với TP/SL
            order_id = await self.trade_client.send_order(
                orderId=-1,  # -1 = new order
                symbol=gate_symbol,  # ✨ Dùng Gate.io format
                price=0,  # 0 = market order
                quantity=qty_contracts,
                side=side,
                leverage=self.leverage,
                take_profit=take_profit,
                stop_loss=stop_loss,
                tag=f"Entry-{self.bot_name}"
            )

            if order_id and order_id > 0:
                # Track order
                self.exchange_orders[symbol] = {
                    'entry_order_id': order_id,
                    'trade_id': trade_id,
                    'gate_symbol': gate_symbol,  # ✨ Lưu gate symbol
                }

                # Update database
                session = self.db_manager.get_session()
                order = Order(
                    trade_id=trade_id,
                    exchange_order_id=str(order_id),
                    symbol=symbol,
                    side='BUY' if self.direction == DirectionEnum.LONG else 'SELL',
                    order_type='MARKET',
                    quantity=qty_contracts,
                    status=OrderStatusEnum.FILLED,
                    filled_at=datetime.now()
                )
                session.add(order)
                session.commit()
                session.close()

                self.log.i(self.tag, f"✅ REAL order placed: {order_id}")
            else:
                self.log.e(self.tag, f"❌ REAL order failed: {order_id}")
                await self.cancel_trade(trade_id, f"Order failed: {order_id}")

        except Exception as e:
            self.log.e(self.tag, f"Error placing real order: {e}\n{traceback.format_exc()}")

    async def place_simulated_order(self, trade_id: int, symbol: str, price: float, quantity: float):
        """Place lệnh SIMULATED"""
        try:
            self.pending_orders[symbol] = {
                'trade_id': trade_id,
                'entry_price': price,
                'quantity': quantity,
                'timestamp': time.time(),
                'candles_waited': 0
            }
            self.log.d(self.tag, f"📝 SIM order pending for {symbol} at ${price:.6f}")
        except Exception as e:
            self.log.e(self.tag, f"Error placing simulated order: {e}")

    async def on_position_update(self, symbol: str, order_response, position_response):
        """
        ✨ NEW: Callback từ GatePositionSocket khi có order/position update
        """
        try:
            if symbol not in self.active_trades:
                return

            trade_id = self.active_trades[symbol]

            if order_response:
                # Order update
                status = order_response.status
                self.log.i(self.tag, f"📡 Order update for {symbol}: {status}")

                if status == 'finished':
                    # Order filled - có thể là TP hoặc SL
                    text = getattr(order_response, 'text', '')
                    if 'tp' in text.lower():
                        await self._handle_real_exit(symbol, trade_id, 'TP')
                    elif 'sl' in text.lower():
                        await self._handle_real_exit(symbol, trade_id, 'SL')

            if position_response:
                # Position update
                size = getattr(position_response, 'size', 0)
                if size == 0:
                    # Position closed
                    self.log.i(self.tag, f"📡 Position closed for {symbol}")

        except Exception as e:
            self.log.e(self.tag, f"Error handling position update: {e}")

    async def _handle_real_exit(self, symbol: str, trade_id: int, reason: str):
        """Handle exit cho real trade"""
        try:
            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if not trade or trade.status != TradeStatusEnum.OPEN:
                session.close()
                return

            # Get exit price from exchange orders hoặc dùng TP/SL price
            if reason == 'TP':
                exit_price = trade.take_profit
            else:
                exit_price = trade.stop_loss

            await self.close_trade(trade, exit_price, reason, session)

            # Cleanup
            if symbol in self.exchange_orders:
                del self.exchange_orders[symbol]
            if symbol in self.trade_reduce_info:
                del self.trade_reduce_info[symbol]

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error handling real exit: {e}")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """Nhận candle update để check exits"""
        try:
            if symbol not in self.active_trades and symbol not in self.pending_orders:
                return

            candle = {
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
            }

            if symbol in self.pending_orders:
                await self.check_pending_order(symbol, candle)

            if symbol in self.active_trades:
                # Real trades: chỉ update reduce TP, không check exit (exchange sẽ handle)
                if self.trade_mode == TradeModeEnum.REAL:
                    await self.update_real_trade_tp(symbol, candle)
                else:
                    await self.check_exit(symbol, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}")

    async def update_real_trade_tp(self, symbol: str, candle: Dict):
        """
        ✨ NEW: Update TP cho real trade (reduce TP strategy)
        """
        try:
            if self.reduce <= 0:
                return  # Không có reduce

            trade_id = self.active_trades.get(symbol)
            if not trade_id:
                return

            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if not trade or trade.status != TradeStatusEnum.OPEN:
                session.close()
                return

            # Update highest/lowest
            if candle['high'] > trade.highest_price:
                trade.highest_price = candle['high']
            if candle['low'] < trade.lowest_price:
                trade.lowest_price = candle['low']

            # Calculate new TP
            if symbol in self.trade_reduce_info:
                new_tp = self._calculate_reduced_tp(trade, symbol)

                if new_tp != trade.take_profit:
                    old_tp = trade.take_profit
                    trade.take_profit = new_tp
                    session.commit()

                    # Update TP on exchange
                    if self.trade_client:
                        await self._update_exchange_tp(symbol, trade, new_tp)

                    self.log.i(self.tag, f"📉 Reduced TP for {symbol}: ${old_tp:.6f} → ${new_tp:.6f}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error updating real trade TP: {e}")

    async def _update_exchange_tp(self, symbol: str, trade: Trade, new_tp: float):
        """Update TP order trên exchange"""
        try:
            if not self.trade_client:
                return

            # Cancel old TP order nếu có
            order_info = self.exchange_orders.get(symbol, {})
            old_tp_order_id = order_info.get('tp_order_id')

            if old_tp_order_id:
                await self.trade_client.send_order(orderId=old_tp_order_id)  # Cancel

            # Place new TP order
            from x1.bot.exchange.trade.trade_side import TradeSide

            if self.direction == DirectionEnum.LONG:
                side = TradeSide.CLOSE_LONG
            else:
                side = TradeSide.CLOSE_SHORT

            new_order_id = await self.trade_client.send_order(
                orderId=-1,
                symbol=symbol,
                price=new_tp,
                quantity=0,  # auto_size
                side=side,
                take_profit=new_tp,
                tag=f"TP-Update-{self.bot_name}"
            )

            if new_order_id and new_order_id > 0:
                self.exchange_orders[symbol]['tp_order_id'] = new_order_id

        except Exception as e:
            self.log.e(self.tag, f"Error updating exchange TP: {e}")

    async def check_pending_order(self, symbol: str, candle: Dict):
        """Check pending order (SIMULATED mode)"""
        try:
            pending = self.pending_orders[symbol]
            trade_id = pending['trade_id']
            entry_price = pending['entry_price']
            pending['candles_waited'] += 1

            filled = False
            fill_price = entry_price

            if self.direction == DirectionEnum.LONG:
                if candle['low'] <= entry_price:
                    filled = True
                    fill_price = min(entry_price, candle['open'])
            else:
                if candle['high'] >= entry_price:
                    filled = True
                    fill_price = max(entry_price, candle['open'])

            if filled:
                session = self.db_manager.get_session()

                side = 'BUY' if self.direction == DirectionEnum.LONG else 'SELL'
                order = Order(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    order_type='SIMULATED_MARKET',
                    quantity=pending['quantity'],
                    filled_quantity=pending['quantity'],
                    avg_fill_price=fill_price,
                    status=OrderStatusEnum.FILLED,
                    filled_at=datetime.now()
                )
                session.add(order)

                trade = session.query(Trade).filter_by(id=trade_id).first()
                if trade:
                    trade.entry_price = fill_price
                    if self.direction == DirectionEnum.LONG:
                        trade.take_profit = fill_price * (1 + self.take_profit / 100)
                        trade.stop_loss = fill_price * (1 - self.stop_loss / 100)
                    else:
                        trade.take_profit = fill_price * (1 - self.take_profit / 100)
                        trade.stop_loss = fill_price * (1 + self.stop_loss / 100)

                    # Update reduce info với TP mới
                    if symbol in self.trade_reduce_info:
                        self.trade_reduce_info[symbol]['initial_tp'] = trade.take_profit

                session.commit()
                session.close()

                del self.pending_orders[symbol]
                self.log.i(self.tag, f"✅ SIM order filled for {symbol} at ${fill_price:.6f}")

            elif pending['candles_waited'] >= 2:
                await self.cancel_trade(trade_id, "Order not filled after 2 candles")
                del self.pending_orders[symbol]
                if symbol in self.trade_reduce_info:
                    del self.trade_reduce_info[symbol]
                self.log.i(self.tag, f"❌ SIM order cancelled for {symbol}")

        except Exception as e:
            self.log.e(self.tag, f"Error checking pending order: {e}")

    async def check_exit(self, symbol: str, candle: Dict):
        """Check exit conditions với Reduce TP (SIMULATED mode)"""
        try:
            trade_id = self.active_trades.get(symbol)
            if not trade_id:
                return

            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if not trade or trade.status != TradeStatusEnum.OPEN:
                session.close()
                return

            # Update highest/lowest
            if candle['high'] > trade.highest_price:
                trade.highest_price = candle['high']

            if candle['low'] < trade.lowest_price:
                trade.lowest_price = candle['low']

            # ===== REDUCE TP: Apply reduction =====
            exit_reason_suffix = ""
            if self.reduce > 0 and symbol in self.trade_reduce_info:
                new_tp = self._calculate_reduced_tp(trade, symbol)
                if new_tp != trade.take_profit:
                    trade.take_profit = new_tp
                    exit_reason_suffix = "_REDUCED"

            # Check exit
            exit_price = None
            exit_reason = None

            if self.direction == DirectionEnum.LONG:
                if candle['high'] >= trade.take_profit:
                    exit_price = trade.take_profit
                    exit_reason = 'TP' + exit_reason_suffix
                elif candle['low'] <= trade.stop_loss:
                    exit_price = trade.stop_loss
                    exit_reason = 'SL'
            else:  # SHORT
                if candle['low'] <= trade.take_profit:
                    exit_price = trade.take_profit
                    exit_reason = 'TP' + exit_reason_suffix
                elif candle['high'] >= trade.stop_loss:
                    exit_price = trade.stop_loss
                    exit_reason = 'SL'

            if exit_price:
                await self.close_trade(trade, exit_price, exit_reason, session)
                # Cleanup reduce info
                if symbol in self.trade_reduce_info:
                    del self.trade_reduce_info[symbol]
            else:
                session.commit()

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error checking exit: {e}")

    def _calculate_reduced_tp(self, trade: Trade, symbol: str) -> float:
        """
        Tính TP mới sau khi apply reduce
        """
        reduce_info = self.trade_reduce_info.get(symbol)
        if not reduce_info:
            return trade.take_profit

        # Tính số phút đã hold
        entry_time = reduce_info.get('entry_time', trade.entry_time)
        hold_time = datetime.now() - entry_time
        minutes_held = int(hold_time.total_seconds() / 60)

        # Chỉ reduce mỗi phút một lần
        if minutes_held <= reduce_info['last_reduce_minute']:
            return trade.take_profit

        reduce_info['last_reduce_minute'] = minutes_held

        initial_tp = reduce_info['initial_tp']
        entry_price = trade.entry_price
        stop_loss = trade.stop_loss

        if self.direction == DirectionEnum.LONG:
            tp_distance = initial_tp - entry_price
            sl_distance = entry_price - stop_loss
            total_distance = tp_distance + sl_distance

            reduction_per_minute = total_distance * (self.reduce / 100)
            total_reduction = reduction_per_minute * minutes_held

            new_tp = initial_tp - total_reduction
            new_tp = max(new_tp, stop_loss)

        else:  # SHORT
            tp_distance = entry_price - initial_tp
            sl_distance = stop_loss - entry_price
            total_distance = tp_distance + sl_distance

            reduction_per_minute = total_distance * (self.reduce / 100)
            total_reduction = reduction_per_minute * minutes_held

            new_tp = initial_tp + total_reduction
            new_tp = min(new_tp, stop_loss)

        return new_tp

    async def close_trade(self, trade: Trade, exit_price: float, reason: str, session):
        """Đóng trade"""
        try:
            # Calculate PnL
            if trade.direction == DirectionEnum.LONG:
                pnl_percent = ((exit_price - trade.entry_price) / trade.entry_price) * 100
                pnl_usdt = (exit_price - trade.entry_price) * trade.quantity
            else:
                pnl_percent = ((trade.entry_price - exit_price) / trade.entry_price) * 100
                pnl_usdt = (trade.entry_price - exit_price) * trade.quantity

            hold_time = datetime.now() - trade.entry_time
            hold_minutes = hold_time.total_seconds() / 60

            # Update trade
            trade.exit_price = exit_price
            trade.exit_time = datetime.now()
            trade.pnl_usdt = pnl_usdt
            trade.pnl_percent = pnl_percent
            trade.exit_reason = reason
            trade.status = TradeStatusEnum.CLOSED

            # Update bot stats in DB
            bot_config = session.query(BotConfig).filter_by(id=self.bot_config_id).first()
            if bot_config:
                bot_config.total_trades += 1
                if pnl_usdt > 0:
                    bot_config.winning_trades += 1
                else:
                    bot_config.losing_trades += 1
                bot_config.total_pnl += pnl_usdt
                bot_config.win_rate = (bot_config.winning_trades / bot_config.total_trades) * 100

                # Update local cache
                self.total_trades = bot_config.total_trades
                self.winning_trades = bot_config.winning_trades
                self.losing_trades = bot_config.losing_trades
                self.total_pnl = bot_config.total_pnl
                self.win_rate = bot_config.win_rate

            session.commit()

            # Remove from active
            if trade.symbol in self.active_trades:
                del self.active_trades[trade.symbol]

            # Log
            emoji = "✅" if pnl_usdt > 0 else "❌"
            mode_str = "🔴 REAL" if trade.trade_mode == TradeModeEnum.REAL else "🔵 SIM"

            self.log.i(self.tag,
                       f"{emoji} [{mode_str}] Closed {trade.direction.value} {trade.symbol} | "
                       f"Entry: ${trade.entry_price:.6f} | Exit: ${exit_price:.6f} | "
                       f"PnL: {pnl_percent:.2f}% (${pnl_usdt:.2f}) | {reason} | Hold: {hold_minutes:.1f}min"
                       )

            # Telegram notification
            await self._send_exit_notification(
                trade, exit_price, reason, pnl_usdt, pnl_percent, hold_minutes
            )

        except Exception as e:
            self.log.e(self.tag, f"Error closing trade: {e}")

    async def _send_exit_notification(self, trade: Trade, exit_price: float,
                                      reason: str, pnl_usdt: float, pnl_percent: float,
                                      hold_minutes: float):
        """Gửi Telegram notification khi đóng lệnh"""
        try:
            direction = trade.direction.value
            direction_emoji = "📈" if direction == "LONG" else "📉"

            if trade.trade_mode == TradeModeEnum.REAL:
                mode_emoji = "🔴"
                mode_text = "REAL"
            else:
                mode_emoji = "🔵"
                mode_text = "SIM"

            result_emoji = "✅" if pnl_usdt > 0 else "❌"
            result_text = "WIN" if pnl_usdt > 0 else "LOSS"

            if 'TP' in reason:
                reason_emoji = "🎯"
                if '_REDUCED' in reason:
                    reason_text = f"Take Profit (Reduced after {hold_minutes:.0f}min)"
                else:
                    reason_text = "Take Profit"
            else:
                reason_emoji = "🛑"
                reason_text = "Stop Loss"

            reduce_str = f"⏱️ Reduce: {self.reduce}%/min\n" if self.reduce > 0 else ""

            message = (
                f"{mode_emoji} {result_emoji} <b>{mode_text} CLOSE - {result_text}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot: <b>{self.bot_name}</b>\n"
                f"🪙 Symbol: <b>{trade.symbol}</b>\n"
                f"📍 Reason: {reason_emoji} <b>{reason_text}</b>\n"
                f"\n"
                f"💰 Entry: ${trade.entry_price:.6f}\n"
                f"💵 Exit: ${exit_price:.6f}\n"
                f"📊 PnL: <b>{pnl_percent:+.2f}%</b> (${pnl_usdt:+.2f})\n"
                f"⏱️ Hold: {hold_minutes:.1f} min\n"
                f"{reduce_str}"
                f"\n"
                f"📈 Price Range:\n"
                f"├ High: ${trade.highest_price:.6f}\n"
                f"└ Low: ${trade.lowest_price:.6f}\n"
                f"\n"
                f"📊 <b>Bot Stats:</b>\n"
                f"├ Trades: {self.total_trades}\n"
                f"├ Win Rate: {self.win_rate:.1f}%\n"
                f"└ Total PnL: ${self.total_pnl:.2f}\n"
                f"\n"
                f"⚙️ {direction_emoji} {direction} | "
                f"TP{self.take_profit}% SL{self.stop_loss}% "
                f"R{self.reduce}%"
            )

            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error sending exit notification: {e}")

    async def cancel_trade(self, trade_id: int, reason: str):
        """Cancel trade"""
        try:
            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if trade:
                trade.status = TradeStatusEnum.CANCELLED
                trade.exit_reason = reason
                session.commit()

                if trade.symbol in self.active_trades:
                    del self.active_trades[trade.symbol]

                if trade.symbol in self.trade_reduce_info:
                    del self.trade_reduce_info[trade.symbol]

                if trade.symbol in self.exchange_orders:
                    del self.exchange_orders[trade.symbol]

                self.log.i(self.tag, f"Cancelled trade {trade_id}: {reason}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error cancelling trade: {e}")