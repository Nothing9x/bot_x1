#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RealBotLive - Bot trade thật follow best strategy từ database
- Tự động load best strategy từ DB
- Trade thật qua TradeClientFactory
- Auto-update strategy mỗi giờ
"""

import asyncio
import traceback
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from x1.bot.trading.config_loader import RealAccountConfig


@dataclass
class PositionInfo:
    """Thông tin position đang mở"""
    symbol: str
    direction: str  # 'LONG' or 'SHORT'
    entry_price: float
    quantity: float
    take_profit: float
    stop_loss: float
    entry_time: datetime
    reduce_levels: List[Dict] = field(default_factory=list)


class RealBotLive:
    """
    Bot trade thật - follow best strategy từ database

    Features:
    - Load best strategy từ DB (theo total_pnl, min trades)
    - Trade thật qua ITradeClient (Gate.io, MEXC)
    - Auto-update strategy mỗi giờ nếu có strategy tốt hơn
    - Reduce TP: Chốt lời từng phần
    - Notifications qua Telegram
    """

    def __init__(self, account_config: RealAccountConfig, db_manager, log,
                 tele_message=None, symbols: List = None):
        """
        Args:
            account_config: RealAccountConfig từ config_loader
            db_manager: DatabaseManager instance
            log: Logger instance
            tele_message: TelegramMessageQueue (optional)
            symbols: List[Symbol] từ init_symbols - chứa thông tin ps, leverage của mỗi symbol
        """
        self.account_config = account_config
        self.db_manager = db_manager
        self.log = log
        self.tag = f"RealBot-{account_config.account_id}"

        # Telegram
        self.tele_message = tele_message
        self.chat_id = account_config.chat_id

        # Symbols info - để lookup ps, leverage
        self.symbols = symbols or []
        self.symbol_map: Dict[str, any] = {s.symbol: s for s in self.symbols}

        # Trade client (sẽ tạo trong start())
        self.trade_client = None

        # Current strategy config (load từ DB)
        self.current_strategy = None
        self.strategy_id = None

        # Active positions
        self.active_positions: Dict[str, PositionInfo] = {}

        # Stats
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0

        # Config
        self.config = {
            'strategy_update_interval': 3600,  # Update strategy mỗi 1 giờ
            'min_strategy_trades': 5,  # Strategy phải có ít nhất 5 trades
            'min_win_rate': 50.0,  # Win rate tối thiểu
            'strategy_improvement_threshold': 0.10,  # Đổi strategy nếu tốt hơn 10%
        }

        # Running flag
        self._running = False

    async def start(self):
        """Start bot"""
        try:
            self.log.i(self.tag, f"🚀 Starting Real Bot: {self.account_config.account_id}")

            # Create trade client via factory
            await self._create_trade_client()

            # Load best strategy from DB
            await self._load_best_strategy()

            if not self.current_strategy:
                self.log.w(self.tag, "⚠️ No suitable strategy found in DB. Bot will wait...")

            self._running = True

            # Start background tasks
            asyncio.create_task(self._strategy_update_loop())
            asyncio.create_task(self._status_report_loop())

            self.log.i(self.tag, f"✅ Real Bot started: {self.account_config.account_id}")

            # Send notification
            await self._send_notification(
                f"🟢 Real Bot Started\n"
                f"Account: {self.account_config.account_id}\n"
                f"Exchange: {self.account_config.exchange}\n"
                f"Position Size: ${self.account_config.position_size_usdt}\n"
                f"Leverage: {self.account_config.leverage}x"
            )

        except Exception as e:
            self.log.e(self.tag, f"Error starting bot: {e}\n{traceback.format_exc()}")
            raise

    async def stop(self):
        """Stop bot"""
        try:
            self.log.i(self.tag, f"🛑 Stopping Real Bot: {self.account_config.account_id}")
            self._running = False

            await self._send_notification(
                f"🔴 Real Bot Stopped\n"
                f"Account: {self.account_config.account_id}\n"
                f"Total Trades: {self.total_trades}\n"
                f"Total PnL: ${self.total_pnl:.2f}"
            )

        except Exception as e:
            self.log.e(self.tag, f"Error stopping bot: {e}")

    async def _create_trade_client(self):
        """Tạo trade client từ TradeClientFactory"""
        try:
            from x1.bot.trading.trade_client_factory import TradeClientFactory

            self.trade_client = TradeClientFactory.create(
                exchange_name=self.account_config.exchange,
                bot=self.account_config,
                telegramMessage=self.tele_message,
                log=self.log,
                trade_callback=self._on_trade_callback
            )

            self.log.i(self.tag, f"✅ Trade client created for {self.account_config.exchange}")

        except Exception as e:
            self.log.e(self.tag, f"Error creating trade client: {e}")
            raise

    def _on_trade_callback(self, trade_info: Dict):
        """Callback khi có trade event từ exchange"""
        try:
            self.log.i(self.tag, f"📥 Trade callback: {trade_info}")
        except Exception as e:
            self.log.e(self.tag, f"Error in trade callback: {e}")

    async def _load_best_strategy(self):
        """Load best strategy từ database"""
        try:
            from x1.bot.database.database_models import BotConfig

            session = self.db_manager.get_session()

            # Query best strategy by total_pnl với min trades
            best_config = session.query(BotConfig).filter(
                BotConfig.total_trades >= self.config['min_strategy_trades'],
                BotConfig.is_active == True
            ).order_by(BotConfig.total_pnl.desc()).first()

            if best_config:
                self.current_strategy = {
                    'id': best_config.id,
                    'name': best_config.name,
                    'direction': best_config.direction.value,
                    'take_profit': best_config.take_profit,
                    'stop_loss': best_config.stop_loss,
                    'price_increase_threshold': best_config.price_increase_threshold,
                    'volume_multiplier': best_config.volume_multiplier,
                    'rsi_threshold': best_config.rsi_threshold,
                    'min_confidence': best_config.min_confidence,
                    'timeframe': best_config.timeframe,
                    'total_pnl': best_config.total_pnl,
                    'win_rate': best_config.win_rate,
                    'total_trades': best_config.total_trades,
                }
                self.strategy_id = best_config.id

                self.log.i(self.tag,
                           f"📊 Loaded strategy: {best_config.name} | "
                           f"PnL: ${best_config.total_pnl:.2f} | "
                           f"WR: {best_config.win_rate:.1f}% | "
                           f"Trades: {best_config.total_trades}"
                           )
            else:
                self.log.w(self.tag, "No suitable strategy found in database")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error loading strategy: {e}")

    async def on_pump_signal(self, signal: Dict):
        """Nhận pump signal và quyết định vào lệnh"""
        try:
            if not self._running:
                return

            if not self.current_strategy:
                return

            symbol = signal.get('symbol')
            if not symbol:
                return

            # Check if already have position
            if symbol in self.active_positions:
                return

            # Check max positions
            if len(self.active_positions) >= self.account_config.max_positions:
                return

            # Check if should enter based on strategy
            if self._should_enter(signal):
                await self._enter_position(signal)

        except Exception as e:
            self.log.e(self.tag, f"Error handling pump signal: {e}")

    def _should_enter(self, signal: Dict) -> bool:
        """Check xem có nên vào lệnh không dựa trên current strategy"""
        if not self.current_strategy:
            return False

        strategy = self.current_strategy

        # Check timeframe
        if signal.get('timeframe', '1m') != strategy.get('timeframe', '1m'):
            return False

        # Check price change
        timeframe = strategy.get('timeframe', '1m')
        if timeframe == '1m':
            price_change = signal.get('price_change_1m', 0)
        else:
            price_change = signal.get('price_change_5m', 0)

        if price_change < strategy.get('price_increase_threshold', 0):
            return False

        # Check volume
        if signal.get('volume_ratio', 0) < strategy.get('volume_multiplier', 0):
            return False

        # Check confidence
        if signal.get('confidence', 0) < strategy.get('min_confidence', 0):
            return False

        return True

    async def _enter_position(self, signal: Dict):
        """Vào lệnh thật"""
        try:
            symbol = signal['symbol']
            price = signal['price']
            strategy = self.current_strategy
            direction = strategy['direction']

            # Calculate TP/SL
            if direction == 'LONG':
                take_profit = price * (1 + strategy['take_profit'] / 100)
                stop_loss = price * (1 - strategy['stop_loss'] / 100)
            else:  # SHORT
                take_profit = price * (1 - strategy['take_profit'] / 100)
                stop_loss = price * (1 + strategy['stop_loss'] / 100)

            # Calculate quantity
            position_size = self.account_config.position_size_usdt
            leverage = self.account_config.leverage
            quantity = (position_size * leverage) / price

            self.log.i(self.tag,
                       f"📈 Entering {direction} {symbol} | "
                       f"Price: ${price:.6f} | "
                       f"TP: ${take_profit:.6f} | "
                       f"SL: ${stop_loss:.6f}"
                       )

            # Place order via trade client
            if self.trade_client:
                try:
                    side = 'buy' if direction == 'LONG' else 'sell'

                    result = await self._place_order(
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        price=price,
                        take_profit=take_profit,
                        stop_loss=stop_loss
                    )

                    if result:
                        # Track position
                        self.active_positions[symbol] = PositionInfo(
                            symbol=symbol,
                            direction=direction,
                            entry_price=price,
                            quantity=quantity,
                            take_profit=take_profit,
                            stop_loss=stop_loss,
                            entry_time=datetime.now(),
                            reduce_levels=self._generate_reduce_levels(price, take_profit, direction)
                        )

                        self.total_trades += 1

                        await self._send_notification(
                            f"📈 OPENED {direction}\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: ${price:.6f}\n"
                            f"TP: ${take_profit:.6f}\n"
                            f"SL: ${stop_loss:.6f}\n"
                            f"Size: ${position_size} x{leverage}"
                        )

                except Exception as e:
                    self.log.e(self.tag, f"Error placing order: {e}")

        except Exception as e:
            self.log.e(self.tag, f"Error entering position: {e}\n{traceback.format_exc()}")

    async def _place_order(self, symbol: str, side: str, quantity: float, price: float = 0,
                           take_profit: float = None, stop_loss: float = None) -> bool:
        """Place order qua trade client sử dụng send_order"""
        try:
            if not self.trade_client:
                self.log.e(self.tag, "Trade client not initialized")
                return False

            # Lấy thông tin symbol (ps, leverage)
            symbol_info = self.symbol_map.get(symbol)
            ps = symbol_info.price_scale if symbol_info else 1
            leverage = symbol_info.max_leverage if symbol_info else self.account_config.leverage

            # Convert side: 'buy' -> 1, 'sell' -> 2 (hoặc theo convention của bạn)
            side_value = 1 if side == 'buy' else 2

            # Gọi send_order
            await self.trade_client.send_order(
                orderId=-1,
                symbol=symbol,
                price=price,
                quantity=quantity,
                side=side_value,
                leverage=leverage,
                ps=ps,
                take_profit=take_profit,
                stop_loss=stop_loss,
                tag=self.tag
            )

            return True

        except Exception as e:
            self.log.e(self.tag, f"Error placing order: {e}")
            return False

    def _generate_reduce_levels(self, entry_price: float, take_profit: float,
                                 direction: str) -> List[Dict]:
        """Generate reduce TP levels"""
        levels = []
        reduce_config = [
            {'percent_of_tp': 0.5, 'close_percent': 30},
            {'percent_of_tp': 0.75, 'close_percent': 30},
        ]

        for config in reduce_config:
            if direction == 'LONG':
                price = entry_price + (take_profit - entry_price) * config['percent_of_tp']
            else:
                price = entry_price - (entry_price - take_profit) * config['percent_of_tp']

            levels.append({
                'price': price,
                'close_percent': config['close_percent'],
                'executed': False
            })

        return levels

    async def on_price_update(self, symbol: str, price: float, candle: Dict = None):
        """Nhận price update để check TP/SL/Reduce"""
        try:
            if symbol not in self.active_positions:
                return

            position = self.active_positions[symbol]

            # Check reduce levels first
            await self._check_reduce(symbol, price, position)

            # Check TP/SL
            await self._check_tp_sl(symbol, price, position)

        except Exception as e:
            self.log.e(self.tag, f"Error on price update: {e}")

    async def _check_reduce(self, symbol: str, price: float, position: PositionInfo):
        """Check và execute reduce TP levels"""
        try:
            if not position.reduce_levels:
                return

            for level in position.reduce_levels:
                if level['executed']:
                    continue

                should_reduce = False
                if position.direction == 'LONG' and price >= level['price']:
                    should_reduce = True
                elif position.direction == 'SHORT' and price <= level['price']:
                    should_reduce = True

                if should_reduce:
                    close_qty = position.quantity * (level['close_percent'] / 100)

                    self.log.i(self.tag, f"📉 Reduce {level['close_percent']}% {symbol} at ${price:.6f}")

                    if self.trade_client:
                        try:
                            side = 'sell' if position.direction == 'LONG' else 'buy'
                            await self._place_order(symbol=symbol, side=side, quantity=close_qty)

                            level['executed'] = True
                            position.quantity -= close_qty

                            if position.direction == 'LONG':
                                pnl = (price - position.entry_price) * close_qty
                            else:
                                pnl = (position.entry_price - price) * close_qty

                            self.total_pnl += pnl

                            await self._send_notification(
                                f"📉 REDUCED {level['close_percent']}%\n"
                                f"Symbol: {symbol}\n"
                                f"Price: ${price:.6f}\n"
                                f"PnL: ${pnl:.2f}"
                            )

                        except Exception as e:
                            self.log.e(self.tag, f"Error reducing position: {e}")

        except Exception as e:
            self.log.e(self.tag, f"Error in check reduce: {e}")

    async def _check_tp_sl(self, symbol: str, price: float, position: PositionInfo):
        """Check TP/SL và close position nếu cần"""
        try:
            should_close = False
            reason = ""

            if position.direction == 'LONG':
                if price >= position.take_profit:
                    should_close = True
                    reason = "TP"
                elif price <= position.stop_loss:
                    should_close = True
                    reason = "SL"
            else:
                if price <= position.take_profit:
                    should_close = True
                    reason = "TP"
                elif price >= position.stop_loss:
                    should_close = True
                    reason = "SL"

            if should_close:
                await self._close_position(symbol, price, reason)

        except Exception as e:
            self.log.e(self.tag, f"Error checking TP/SL: {e}")

    async def _close_position(self, symbol: str, price: float, reason: str):
        """Close position"""
        try:
            if symbol not in self.active_positions:
                return

            position = self.active_positions[symbol]

            if position.direction == 'LONG':
                pnl = (price - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - price) * position.quantity

            self.log.i(self.tag, f"{'✅' if pnl > 0 else '❌'} Closing {symbol} | Reason: {reason} | PnL: ${pnl:.2f}")

            if self.trade_client and position.quantity > 0:
                try:
                    side = 'sell' if position.direction == 'LONG' else 'buy'
                    await self._place_order(symbol=symbol, side=side, quantity=position.quantity)
                except Exception as e:
                    self.log.e(self.tag, f"Error closing position: {e}")

            self.total_pnl += pnl
            if pnl > 0:
                self.winning_trades += 1

            del self.active_positions[symbol]

            emoji = "✅" if pnl > 0 else "❌"
            await self._send_notification(
                f"{emoji} CLOSED ({reason})\n"
                f"Symbol: {symbol}\n"
                f"Entry: ${position.entry_price:.6f}\n"
                f"Exit: ${price:.6f}\n"
                f"PnL: ${pnl:.2f}\n"
                f"Total PnL: ${self.total_pnl:.2f}"
            )

        except Exception as e:
            self.log.e(self.tag, f"Error closing position: {e}\n{traceback.format_exc()}")

    async def _strategy_update_loop(self):
        """Loop check và update strategy"""
        while self._running:
            try:
                await asyncio.sleep(self.config['strategy_update_interval'])

                if not self._running:
                    break

                old_pnl = self.current_strategy.get('total_pnl', 0) if self.current_strategy else 0
                old_id = self.strategy_id

                await self._load_best_strategy()

                if self.current_strategy and self.strategy_id != old_id:
                    new_pnl = self.current_strategy.get('total_pnl', 0)
                    improvement = (new_pnl - old_pnl) / abs(old_pnl) if old_pnl != 0 else 1

                    if improvement >= self.config['strategy_improvement_threshold']:
                        self.log.i(self.tag,
                                   f"🔄 Strategy updated! Old PnL: ${old_pnl:.2f} → New PnL: ${new_pnl:.2f}"
                                   )

                        await self._send_notification(
                            f"🔄 Strategy Updated\n"
                            f"New: {self.current_strategy['name']}\n"
                            f"PnL: ${new_pnl:.2f}\n"
                            f"WR: {self.current_strategy['win_rate']:.1f}%"
                        )

            except Exception as e:
                self.log.e(self.tag, f"Error in strategy update loop: {e}")

    async def _status_report_loop(self):
        """Loop report status định kỳ"""
        while self._running:
            try:
                await asyncio.sleep(3600)

                if not self._running:
                    break

                win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0

                report = (
                    f"📊 HOURLY REPORT\n"
                    f"Account: {self.account_config.account_id}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Trades: {self.total_trades}\n"
                    f"Win Rate: {win_rate:.1f}%\n"
                    f"PnL: ${self.total_pnl:.2f}\n"
                    f"Open: {len(self.active_positions)}"
                )

                self.log.i(self.tag, report)

            except Exception as e:
                self.log.e(self.tag, f"Error in status report: {e}")

    async def _send_notification(self, message: str):
        """Send notification to Telegram"""
        try:
            if self.tele_message and self.chat_id:
                await self.tele_message.send_message(message, self.chat_id)
        except Exception as e:
            self.log.e(self.tag, f"Error sending notification: {e}")

    def get_stats(self) -> Dict:
        """Get bot statistics"""
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0

        return {
            'account_id': self.account_config.account_id,
            'exchange': self.account_config.exchange,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': win_rate,
            'total_pnl': self.total_pnl,
            'open_positions': len(self.active_positions),
            'current_strategy': self.current_strategy.get('name') if self.current_strategy else None,
            'is_running': self._running,
        }