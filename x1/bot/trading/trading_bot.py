# -*- coding: utf-8 -*-
"""
TradingBot - Bot trading với Reduce TP Strategy
- Reduce TP: TP giảm dần về entry rồi về SL theo thời gian
- Telegram notifications cho cả REAL và SIM
- Cache config values để tránh SQLAlchemy detached session error
"""

import asyncio
import time
import traceback
from datetime import datetime
from typing import Dict, Optional
import ccxt

from x1.bot.database.database_models import BotConfig, DatabaseManager, Order, OrderStatusEnum, DirectionEnum, \
    TradeStatusEnum, Trade, TradeModeEnum


class TradingBot:
    """
    Bot trading với Reduce TP Strategy

    Reduce TP: Mỗi phút, TP giảm reduce% khoảng cách về phía SL
    - Phút 0: TP = Entry ± TP%
    - Mỗi phút: TP giảm reduce% × (TP_distance + SL_distance)
    - Cuối cùng: TP = SL → force close
    """

    def __init__(self, bot_config: BotConfig, db_manager: DatabaseManager,
                 log, tele_message, exchange: ccxt.mexc = None, chat_id=""):
        self.db_manager = db_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange
        self.chat_id = chat_id

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

        # ===== REDUCE TP CONFIG =====
        # reduce = % giảm mỗi phút (0 = disabled)
        self.reduce = getattr(bot_config, 'reduce', 0)

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

        # ===== REDUCE TP TRACKING =====
        # Lưu thông tin reduce cho mỗi trade
        self.trade_reduce_info = {}  # {symbol: {initial_tp, last_reduce_minute}}

    def _get_config_string(self) -> str:
        """Tạo string config ngắn gọn"""
        reduce_str = f"_R{self.reduce}" if self.reduce > 0 else ""
        return (
            f"TP{self.take_profit}%_SL{self.stop_loss}%{reduce_str}_"
            f"Vol{self.volume_multiplier}x_Conf{self.min_confidence}%"
        )

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
                await self.place_real_order(trade_id, symbol, entry_price, quantity)
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

    async def place_real_order(self, trade_id: int, symbol: str, price: float, quantity: float):
        """Place lệnh THẬT lên MEXC"""
        try:
            session = self.db_manager.get_session()
            side = 'buy' if self.direction == DirectionEnum.LONG else 'sell'

            order = Order(
                trade_id=trade_id,
                symbol=symbol,
                side=side.upper(),
                order_type='MARKET',
                quantity=quantity,
                status=OrderStatusEnum.PENDING
            )

            session.add(order)
            session.commit()

            if self.exchange:
                try:
                    result = self.exchange.create_order(
                        symbol=symbol,
                        type='market',
                        side=side,
                        amount=quantity
                    )

                    order.exchange_order_id = result['id']
                    order.status = OrderStatusEnum.FILLED
                    order.filled_quantity = result.get('filled', quantity)
                    order.avg_fill_price = result.get('average', price)
                    order.filled_at = datetime.now()
                    session.commit()

                    self.log.i(self.tag, f"✅ REAL order filled: {result['id']}")

                except Exception as e:
                    order.status = OrderStatusEnum.REJECTED
                    order.error_message = str(e)
                    session.commit()
                    await self.cancel_trade(trade_id, "Order rejected")
                    self.log.e(self.tag, f"❌ REAL order failed: {e}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error placing real order: {e}")

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
                await self.check_exit(symbol, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}")

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
        """Check exit conditions với Reduce TP"""
        try:
            trade_id = self.active_trades.get(symbol)
            if not trade_id:
                return

            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if not trade or trade.status != TradeStatusEnum.OPEN:
                session.close()
                return

            # Lưu TP ban đầu để so sánh
            initial_tp = trade.take_profit

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

        Logic:
        - Mỗi phút, TP giảm reduce% của tổng khoảng cách (TP → Entry → SL)
        - LONG: TP giảm từ trên xuống
        - SHORT: TP tăng từ dưới lên (về phía SL)
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
            # LONG: TP > Entry > SL
            tp_distance = initial_tp - entry_price  # Dương
            sl_distance = entry_price - stop_loss  # Dương
            total_distance = tp_distance + sl_distance

            # Mỗi phút giảm reduce% của total_distance
            reduction_per_minute = total_distance * (self.reduce / 100)
            total_reduction = reduction_per_minute * minutes_held

            # TP mới = Initial TP - total_reduction
            new_tp = initial_tp - total_reduction

            # Không cho TP thấp hơn SL
            new_tp = max(new_tp, stop_loss)

        else:  # SHORT
            # SHORT: SL > Entry > TP
            tp_distance = entry_price - initial_tp  # Dương (TP < Entry)
            sl_distance = stop_loss - entry_price  # Dương (SL > Entry)
            total_distance = tp_distance + sl_distance

            # Mỗi phút giảm reduce%
            reduction_per_minute = total_distance * (self.reduce / 100)
            total_reduction = reduction_per_minute * minutes_held

            # TP mới = Initial TP + total_reduction (tăng về phía SL)
            new_tp = initial_tp + total_reduction

            # Không cho TP cao hơn SL
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

            # WIN/LOSS dựa trên PnL thực tế
            result_emoji = "✅" if pnl_usdt > 0 else "❌"
            result_text = "WIN" if pnl_usdt > 0 else "LOSS"

            # Reason emoji và text
            if 'TP' in reason:
                reason_emoji = "🎯"
                if '_REDUCED' in reason:
                    reason_text = f"Take Profit (Reduced after {hold_minutes:.0f}min)"
                else:
                    reason_text = "Take Profit"
            else:  # SL
                reason_emoji = "🛑"
                reason_text = "Stop Loss"

            # Reduce info
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

                self.log.i(self.tag, f"Cancelled trade {trade_id}: {reason}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error cancelling trade: {e}")