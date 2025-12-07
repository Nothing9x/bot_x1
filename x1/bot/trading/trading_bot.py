"""
TradingBot class - FIXED với Telegram Notifications
- Gửi thông báo khi OPEN/CLOSE cho REAL và SIMULATED bots
- Backtest strategies KHÔNG gửi telegram
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
    - SIMULATED mode: Trade giả lập
    - GỬI TELEGRAM cho cả REAL và SIM
    """

    def __init__(self, bot_config: BotConfig, db_manager: DatabaseManager,
                 log, tele_message, exchange: ccxt.mexc = None, chat_id=""):
        self.bot_config = bot_config
        self.db_manager = db_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange
        self.chat_id = chat_id
        self.tag = f"Bot-{bot_config.name}"

        # Active positions tracking
        self.active_trades = {}  # {symbol: trade_id}
        self.pending_orders = {}  # {symbol: order_info}

    def should_enter(self, signal: Dict) -> bool:
        """Kiểm tra xem có nên vào lệnh không"""
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
        """Vào lệnh - REAL hoặc SIMULATED - GỬI TELEGRAM"""
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

            # Create trade record
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

            # Track active trade
            self.active_trades[symbol] = trade.id

            # Log
            mode_str = "🔴 REAL" if trade.trade_mode == TradeModeEnum.REAL else "🔵 SIM"
            self.log.i(self.tag,
                       f"📈 [{mode_str}] OPENED {trade.direction.value} {symbol} | "
                       f"Entry: ${entry_price:.6f} | TP: ${take_profit:.6f} | SL: ${stop_loss:.6f}")

            # 🔔 GỬI TELEGRAM NOTIFICATION
            await self.tele_message.send_message(
                f"📈 [{mode_str}] OPENED\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot: {self.bot_config.name}\n"
                f"📊 {trade.direction.value} {symbol}\n"
                f"💰 Entry: ${entry_price:.6f}\n"
                f"🎯 TP: ${take_profit:.6f} (+{self.bot_config.take_profit}%)\n"
                f"🛡️ SL: ${stop_loss:.6f} (-{self.bot_config.stop_loss}%)\n"
                f"📦 Size: ${self.bot_config.position_size_usdt:.2f}",
                self.chat_id
            )

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error entering position: {e}\n{traceback.format_exc()}")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """Nhận candle update và check exit"""
        try:
            if symbol not in self.active_trades:
                return

            candle = {
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
            }

            await self.check_exit(symbol, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error on candle update: {e}")

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
        """Đóng trade - GỬI TELEGRAM"""
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
            mode_str = "🔴 REAL" if trade.trade_mode == TradeModeEnum.REAL else "🔵 SIM"

            self.log.i(self.tag,
                       f"{emoji} [{mode_str}] CLOSED {trade.direction.value} {trade.symbol} | "
                       f"Entry: ${trade.entry_price:.6f} | Exit: ${exit_price:.6f} | "
                       f"PnL: {pnl_percent:.2f}% (${pnl_usdt:.2f}) | Reason: {reason}")

            # 🔔 GỬI TELEGRAM NOTIFICATION
            profit_emoji = "💰" if pnl_usdt > 0 else "💸"

            await self.tele_message.send_message(
                f"{emoji} [{mode_str}] CLOSED - {reason}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot: {self.bot_config.name}\n"
                f"📊 {trade.direction.value} {trade.symbol}\n"
                f"💰 Entry: ${trade.entry_price:.6f}\n"
                f"🚪 Exit: ${exit_price:.6f}\n"
                f"{profit_emoji} PnL: {pnl_percent:+.2f}% (${pnl_usdt:+.2f})\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📈 Bot Stats:\n"
                f"  Trades: {self.bot_config.total_trades}\n"
                f"  Win Rate: {self.bot_config.win_rate:.1f}%\n"
                f"  Total PnL: ${self.bot_config.total_pnl:+.2f}",
                self.chat_id
            )

        except Exception as e:
            self.log.e(self.tag, f"Error closing trade: {e}")

    def get_stats(self) -> Dict:
        """Get bot statistics"""
        return {
            'name': self.bot_config.name,
            'mode': self.bot_config.trade_mode.value,
            'direction': self.bot_config.direction.value,
            'total_trades': self.bot_config.total_trades,
            'win_rate': self.bot_config.win_rate,
            'total_pnl': self.bot_config.total_pnl,
            'active_trades': len(self.active_trades),
        }