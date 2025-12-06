"""
TradingBot class - Một bot với config cụ thể
Hỗ trợ cả REAL và SIMULATED mode
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
    Bot trading với một config cụ thể
    - REAL mode: Trade thật với MEXC API
    - SIMULATED mode: Trade giả lập, check giá nến tiếp theo
    """

    def __init__(self, bot_config: BotConfig, db_manager: DatabaseManager,
                 log, tele_message, exchange: ccxt.mexc = None, chat_id = ""):
        self.bot_config = bot_config
        self.db_manager = db_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange
        self.chat_id = chat_id
        self.tag = f"Bot-{bot_config.name}"

        # Active positions tracking
        self.active_trades = {}  # {symbol: trade_id}

        # Pending orders (for SIMULATED mode)
        self.pending_orders = {}  # {symbol: order_info}

    def should_enter(self, signal: Dict) -> bool:
        """Kiểm tra xem có nên vào lệnh không dựa trên signal"""

        # Check timeframe
        if signal.get('timeframe', '1m') != self.bot_config.timeframe:
            return False

        # Check price change
        if self.bot_config.timeframe == '1m':
            price_change = signal.get('price_change_1m', 0)
        else:
            price_change = signal.get('price_change_5m', 0)

        if price_change < self.bot_config.price_increase_threshold:
            return False

        # Check volume
        if signal.get('volume_ratio', 0) < self.bot_config.volume_multiplier:
            return False

        # Check RSI
        rsi = signal.get('rsi')
        if rsi is not None and rsi < self.bot_config.rsi_threshold:
            return False

        # Check confidence
        if signal.get('confidence', 0) < self.bot_config.min_confidence:
            return False

        # Optional checks
        if self.bot_config.min_trend_strength > 0:
            if signal.get('trend_strength', 0) < self.bot_config.min_trend_strength:
                return False

        if self.bot_config.require_breakout:
            if not signal.get('is_breakout', False):
                return False

        if self.bot_config.min_volume_consistency > 0:
            if signal.get('volume_consistency', 0) < self.bot_config.min_volume_consistency:
                return False

        return True

    async def on_signal(self, signal: Dict):
        """Nhận signal và quyết định vào lệnh"""
        try:
            symbol = signal['symbol']

            # Check if should enter
            if not self.should_enter(signal):
                return

            # Check if already have position
            if symbol in self.active_trades:
                return

            # Check if pending order exists
            if symbol in self.pending_orders:
                return

            # Enter position
            await self.enter_position(signal)

        except Exception as e:
            self.log.e(self.tag, f"Error handling signal: {e}\n{traceback.format_exc()}")

    async def enter_position(self, signal: Dict):
        """Vào lệnh - REAL hoặc SIMULATED"""
        try:
            symbol = signal['symbol']
            entry_price = signal['price']

            # Calculate targets
            if self.bot_config.direction == DirectionEnum.LONG:
                take_profit = entry_price * (1 + self.bot_config.take_profit / 100)
                stop_loss = entry_price * (1 - self.bot_config.stop_loss / 100)
            else:  # SHORT
                take_profit = entry_price * (1 - self.bot_config.take_profit / 100)
                stop_loss = entry_price * (1 + self.bot_config.stop_loss / 100)

            quantity = self.bot_config.position_size_usdt / entry_price

            # Create trade record in database
            session = self.db_manager.get_session()

            trade = Trade(
                bot_config_id=self.bot_config.id,
                symbol=symbol,
                direction=self.bot_config.direction,
                trade_mode=self.bot_config.trade_mode,
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

            if self.bot_config.trade_mode == TradeModeEnum.REAL:
                # REAL mode: Place actual order
                await self.place_real_order(trade_id, symbol, entry_price, quantity)
            else:
                # SIMULATED mode: Create pending order, check next candle
                await self.place_simulated_order(trade_id, symbol, entry_price, quantity)

            # Log
            mode_str = "REAL" if self.bot_config.trade_mode == TradeModeEnum.REAL else "SIM"
            self.log.i(self.tag,
                       f"[{mode_str}] Entered {self.bot_config.direction.value} {symbol} at ${entry_price:.6f} | "
                       f"TP: ${take_profit:.6f} | SL: ${stop_loss:.6f}"
                       )

        except Exception as e:
            self.log.e(self.tag, f"Error entering position: {e}\n{traceback.format_exc()}")

    async def place_real_order(self, trade_id: int, symbol: str, price: float, quantity: float):
        """Place lệnh THẬT lên MEXC"""
        try:
            session = self.db_manager.get_session()

            # Determine side
            side = 'buy' if self.bot_config.direction == DirectionEnum.LONG else 'sell'

            # Create order record
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
            order_id = order.id

            # Place order via MEXC API
            if self.exchange:
                try:
                    result = self.exchange.create_order(
                        symbol=symbol,
                        type='market',
                        side=side,
                        amount=quantity
                    )

                    # Update order with exchange ID
                    order.exchange_order_id = result['id']
                    order.status = OrderStatusEnum.FILLED
                    order.filled_quantity = result.get('filled', quantity)
                    order.avg_fill_price = result.get('average', price)
                    order.filled_at = datetime.now()

                    session.commit()

                    self.log.i(self.tag, f"✅ REAL order filled: {result['id']}")

                except Exception as e:
                    # Order failed
                    order.status = OrderStatusEnum.REJECTED
                    order.error_message = str(e)
                    session.commit()

                    # Cancel trade
                    await self.cancel_trade(trade_id, "Order rejected")

                    self.log.e(self.tag, f"❌ REAL order failed: {e}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error placing real order: {e}")

    async def place_simulated_order(self, trade_id: int, symbol: str, price: float, quantity: float):
        """
        Place lệnh GIẢI LẬP
        - Đợi 1s
        - Check nến tiếp theo xem có khớp không
        - Nếu không khớp, cancel
        """
        try:
            # Store pending order
            self.pending_orders[symbol] = {
                'trade_id': trade_id,
                'entry_price': price,
                'quantity': quantity,
                'timestamp': time.time(),
                'candles_waited': 0
            }

            self.log.d(self.tag, f"📝 SIMULATED order pending for {symbol} at ${price:.6f}")

            # Đợi 1 giây
            await asyncio.sleep(1)

        except Exception as e:
            self.log.e(self.tag, f"Error placing simulated order: {e}")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """
        Nhận candle update để:
        1. Check pending orders (SIMULATED mode)
        2. Check TP/SL cho active trades
        """
        try:
            # Only process if we have trades/orders for this symbol
            if symbol not in self.active_trades and symbol not in self.pending_orders:
                return

            candle = {
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
            }

            # 1. Check pending orders (SIMULATED)
            if symbol in self.pending_orders:
                await self.check_pending_order(symbol, candle)

            # 2. Check TP/SL for active trades
            if symbol in self.active_trades:
                await self.check_exit(symbol, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}")

    async def check_pending_order(self, symbol: str, candle: Dict):
        """Check xem pending order có khớp không"""
        try:
            pending = self.pending_orders[symbol]
            trade_id = pending['trade_id']
            entry_price = pending['entry_price']

            # Increment candles waited
            pending['candles_waited'] += 1

            # Check if order would fill
            filled = False
            fill_price = entry_price

            if self.bot_config.direction == DirectionEnum.LONG:
                # LONG: Fill nếu price <= entry_price
                if candle['low'] <= entry_price:
                    filled = True
                    fill_price = min(entry_price, candle['open'])  # Best case fill
            else:  # SHORT
                # SHORT: Fill nếu price >= entry_price
                if candle['high'] >= entry_price:
                    filled = True
                    fill_price = max(entry_price, candle['open'])  # Best case fill

            if filled:
                # Order filled!
                session = self.db_manager.get_session()

                # Create order record
                side = 'BUY' if self.bot_config.direction == DirectionEnum.LONG else 'SELL'
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

                # Update trade entry price
                trade = session.query(Trade).filter_by(id=trade_id).first()
                if trade:
                    trade.entry_price = fill_price

                    # Recalculate TP/SL based on actual fill price
                    if self.bot_config.direction == DirectionEnum.LONG:
                        trade.take_profit = fill_price * (1 + self.bot_config.take_profit / 100)
                        trade.stop_loss = fill_price * (1 - self.bot_config.stop_loss / 100)
                    else:
                        trade.take_profit = fill_price * (1 - self.bot_config.take_profit / 100)
                        trade.stop_loss = fill_price * (1 + self.bot_config.stop_loss / 100)

                session.commit()
                session.close()

                # Remove from pending
                del self.pending_orders[symbol]

                self.log.i(self.tag, f"✅ SIMULATED order filled for {symbol} at ${fill_price:.6f}")

            elif pending['candles_waited'] >= 2:
                # Cancel sau 2 nến không khớp
                await self.cancel_trade(trade_id, "Order not filled after 2 candles")
                del self.pending_orders[symbol]

                self.log.i(self.tag, f"❌ SIMULATED order cancelled for {symbol} (not filled)")

        except Exception as e:
            self.log.e(self.tag, f"Error checking pending order: {e}")

    async def check_exit(self, symbol: str, candle: Dict):
        """Check xem có nên exit không"""
        try:
            trade_id = self.active_trades[symbol]

            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if not trade or trade.status != TradeStatusEnum.OPEN:
                session.close()
                return

            # Update highest/lowest
            if candle['high'] > trade.highest_price:
                trade.highest_price = candle['high']

                # Trailing stop for LONG
                if self.bot_config.direction == DirectionEnum.LONG and self.bot_config.trailing_stop:
                    new_stop = candle['high'] * (1 - self.bot_config.stop_loss / 100)
                    if new_stop > trade.stop_loss:
                        trade.stop_loss = new_stop

            if candle['low'] < trade.lowest_price:
                trade.lowest_price = candle['low']

                # Trailing stop for SHORT
                if self.bot_config.direction == DirectionEnum.SHORT and self.bot_config.trailing_stop:
                    new_stop = candle['low'] * (1 + self.bot_config.stop_loss / 100)
                    if new_stop < trade.stop_loss:
                        trade.stop_loss = new_stop

            # Check exit conditions
            exit_price = None
            exit_reason = None

            if self.bot_config.direction == DirectionEnum.LONG:
                if candle['high'] >= trade.take_profit:
                    exit_price = trade.take_profit
                    exit_reason = 'TP'
                elif candle['low'] <= trade.stop_loss:
                    exit_price = trade.stop_loss
                    exit_reason = 'SL'
            else:  # SHORT
                if candle['low'] <= trade.take_profit:
                    exit_price = trade.take_profit
                    exit_reason = 'TP'
                elif candle['high'] >= trade.stop_loss:
                    exit_price = trade.stop_loss
                    exit_reason = 'SL'

            if exit_price:
                await self.close_trade(trade, exit_price, exit_reason, session)
            else:
                session.commit()

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error checking exit: {e}")

    async def close_trade(self, trade: Trade, exit_price: float, reason: str, session):
        """Đóng trade"""
        try:
            # Calculate PnL
            if trade.direction == DirectionEnum.LONG:
                pnl_percent = ((exit_price - trade.entry_price) / trade.entry_price) * 100
                pnl_usdt = (exit_price - trade.entry_price) * trade.quantity
            else:  # SHORT
                pnl_percent = ((trade.entry_price - exit_price) / trade.entry_price) * 100
                pnl_usdt = (trade.entry_price - exit_price) * trade.quantity

            # Update trade
            trade.exit_price = exit_price
            trade.exit_time = datetime.now()
            trade.pnl_usdt = pnl_usdt
            trade.pnl_percent = pnl_percent
            trade.exit_reason = reason
            trade.status = TradeStatusEnum.CLOSED

            # Update bot config stats
            self.bot_config.total_trades += 1
            if pnl_usdt > 0:
                self.bot_config.winning_trades += 1
            else:
                self.bot_config.losing_trades += 1

            self.bot_config.total_pnl += pnl_usdt
            self.bot_config.win_rate = (self.bot_config.winning_trades / self.bot_config.total_trades) * 100

            session.commit()

            # Remove from active trades
            del self.active_trades[trade.symbol]

            # Log
            emoji = "✅" if pnl_usdt > 0 else "❌"
            mode_str = "REAL" if trade.trade_mode == TradeModeEnum.REAL else "SIM"

            self.log.i(self.tag,
                       f"{emoji} [{mode_str}] Closed {trade.direction.value} {trade.symbol} | "
                       f"Entry: ${trade.entry_price:.6f} | Exit: ${exit_price:.6f} | "
                       f"PnL: {pnl_percent:.2f}% (${pnl_usdt:.2f}) | Reason: {reason}"
                       )

            # Send notification
            await self.tele_message.send_message(
                f"{emoji} [{self.bot_config.name}] {reason}\n"
                f"{trade.direction.value} {trade.symbol}\n"
                f"Entry: ${trade.entry_price:.6f}\n"
                f"Exit: ${exit_price:.6f}\n"
                f"PnL: {pnl_percent:.2f}% (${pnl_usdt:.2f})", self.chat_id
            )

        except Exception as e:
            self.log.e(self.tag, f"Error closing trade: {e}")

    async def cancel_trade(self, trade_id: int, reason: str):
        """Cancel trade"""
        try:
            session = self.db_manager.get_session()
            trade = session.query(Trade).filter_by(id=trade_id).first()

            if trade:
                trade.status = TradeStatusEnum.CANCELLED
                trade.exit_reason = reason
                session.commit()

                # Remove from active trades
                if trade.symbol in self.active_trades:
                    del self.active_trades[trade.symbol]

                self.log.i(self.tag, f"Cancelled trade {trade_id}: {reason}")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error cancelling trade: {e}")