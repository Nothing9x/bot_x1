# -*- coding: utf-8 -*-
"""
StrategyManager - PERFORMANCE OPTIMIZED VERSION
- Symbol-indexed lookup để check exits nhanh O(m) thay vì O(n)
- KHÔNG có Telegram notifications (chỉ BotManager mới notify)
- Memory management: limit trade_history, cleanup empty indexes
"""

import json
import traceback
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Set
import itertools

import numpy as np

from x1.bot.ai.trading_strategy import TradingStrategy
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils import constants
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class StrategyManager:
    """
    Manager để tạo và test nhiều strategies - OPTIMIZED VERSION

    Performance: Symbol-indexed lookup O(m) thay vì O(n)
    Memory: Cleanup empty indexes, limit history
    """

    def __init__(self, log: LoggerWrapper, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "StrategyManager"
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        self.strategies: List[TradingStrategy] = []
        self.candle_buffer = defaultdict(lambda: defaultdict(list))

        # Memory config
        self.max_candle_history = 50  # Giảm từ 100 xuống 50
        self.max_trade_history_per_strategy = 100  # Limit trade history

        # ===== PERFORMANCE: Symbol-indexed lookup =====
        # {symbol: set(strategy_ids có position)} - O(1) lookup
        self.symbol_to_strategies: Dict[str, Set[int]] = defaultdict(set)

        # Strategy ID -> Strategy object - O(1) lookup
        self.strategy_map: Dict[int, TradingStrategy] = {}

        # Top strategies
        self.top_strategies = []
        self.best_strategy = None
        self.best_long = None
        self.best_short = None

        # Stats for monitoring
        self._candle_count = 0
        self._cleanup_interval = 1000  # Cleanup mỗi 1000 candles

    def generate_strategies(self, max_strategies: int = 100):
        """Tạo strategies - 50% LONG, 50% SHORT"""
        self.log.i(self.tag, f"🔧 Generating {max_strategies} strategies (50% LONG, 50% SHORT)...")

        param_grid = {
            'take_profit': [2, 3, 5, 7, 10, 15, 20],
            'stop_loss': [1, 2, 3, 4, 5, 7, 10],
            'rsi_threshold': [20, 30, 40, 50, 60, 70],
            'volume_multiplier': [1.0, 1.5, 2, 3, 4],
            'price_increase_threshold': [0.3, 0.5, 1, 1.5, 2],
            'min_confidence': [30, 40, 50, 60, 70],
            'timeframe': ['1m'],
            'trailing_stop': [True, False],
            'min_trend_strength': [0.0, 0.3],
            'require_breakout': [False],
            'min_volume_consistency': [0.0, 0.3],
        }

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))

        target_per_direction = max_strategies // 2
        if len(combinations) > target_per_direction:
            import random
            combinations = random.sample(combinations, target_per_direction)

        strategy_id = 1

        # LONG strategies
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'LONG'
            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            self.strategy_map[strategy_id] = strategy
            strategy_id += 1

        # SHORT strategies
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'SHORT'
            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            self.strategy_map[strategy_id] = strategy
            strategy_id += 1

        long_count = sum(1 for s in self.strategies if s.config['direction'] == 'LONG')
        short_count = sum(1 for s in self.strategies if s.config['direction'] == 'SHORT')

        self.log.i(self.tag, f"✅ Generated {len(self.strategies)} strategies")
        self.log.i(self.tag, f"   📈 {long_count} LONG | 📉 {short_count} SHORT")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """
        Nhận candle update - OPTIMIZED với memory management
        """
        try:
            timestamp = candle_data.get('t', 0)
            candle = {
                'timestamp': timestamp,
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
                'volume': float(candle_data.get('a', 0)),
            }

            # Update buffer với limit
            history = self.candle_buffer[symbol][interval]

            if len(history) == 0 or timestamp > history[-1]['timestamp']:
                history.append(candle)
                # Giảm history size
                while len(history) > self.max_candle_history:
                    history.pop(0)
                await self.check_all_exits(symbol, interval, candle)

            elif timestamp == history[-1]['timestamp']:
                history[-1] = candle
                await self.check_all_exits(symbol, interval, candle)

            # Periodic cleanup
            self._candle_count += 1
            if self._candle_count >= self._cleanup_interval:
                self._cleanup_memory()
                self._candle_count = 0

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}\n{traceback.format_exc()}")

    def _cleanup_memory(self):
        """Cleanup empty indexes và old data để tránh memory leak"""
        try:
            # Cleanup empty symbol_to_strategies entries
            empty_symbols = [s for s, ids in self.symbol_to_strategies.items() if len(ids) == 0]
            for symbol in empty_symbols:
                del self.symbol_to_strategies[symbol]

            # Limit trade_history trong mỗi strategy
            for strategy in self.strategies:
                if len(strategy.trade_history) > self.max_trade_history_per_strategy:
                    # Giữ lại trades gần nhất
                    strategy.trade_history = strategy.trade_history[-self.max_trade_history_per_strategy:]

            # Cleanup candle_buffer cho symbols không còn track
            active_symbols = set(self.symbol_to_strategies.keys())
            # Giữ buffer cho symbols đang có positions

            if len(empty_symbols) > 0:
                self.log.d(self.tag, f"🧹 Cleaned up {len(empty_symbols)} empty symbol indexes")

        except Exception as e:
            self.log.e(self.tag, f"Error in cleanup: {e}")

    async def on_pump_signal(self, signal: Dict):
        """Nhận pump signal và test strategies"""
        try:
            symbol = signal['symbol']
            price = signal['price']
            timeframe = signal.get('timeframe', '1m')

            entered_count = 0
            matched_strategies = []

            for strategy in self.strategies:
                if strategy.should_enter(signal):
                    if symbol not in strategy.active_positions:
                        strategy.enter_position(symbol, price, signal)
                        entered_count += 1
                        matched_strategies.append(strategy.strategy_id)

                        # Add to symbol index for O(1) lookup later
                        self.symbol_to_strategies[symbol].add(strategy.strategy_id)

            if entered_count > 0:
                self.log.i(self.tag, f"✅ {entered_count}/{len(self.strategies)} strategies entered {symbol}")
            else:
                self.log.d(self.tag, f"⚠️ NO strategies matched for {symbol}")

        except Exception as e:
            self.log.e(self.tag, f"Error handling signal: {e}\n{traceback.format_exc()}")

    async def check_all_exits(self, symbol: str, interval: str, candle: Dict):
        """
        OPTIMIZED: Chỉ check strategies có position với symbol này
        O(m) thay vì O(n) - m << n
        """
        try:
            # Lấy strategy_ids có position với symbol này
            strategy_ids = self.symbol_to_strategies.get(symbol)

            if not strategy_ids:
                return  # Không có strategy nào - SKIP

            # Chỉ check strategies có position
            for strategy_id in list(strategy_ids):
                strategy = self.strategy_map.get(strategy_id)
                if not strategy:
                    continue

                if symbol in strategy.active_positions:
                    exit_info = strategy.check_exit(symbol, candle)

                    if exit_info:
                        strategy.close_position(
                            symbol,
                            exit_info['exit_price'],
                            exit_info['reason']
                        )

                        # Remove từ index
                        self.symbol_to_strategies[symbol].discard(strategy_id)

                        # Log only
                        pnl = strategy.trade_history[-1]['pnl_usdt']
                        emoji = "✅" if pnl > 0 else "❌"
                        if constants.DEBUG_LOG:
                            self.log.d(self.tag,
                                   f"{emoji} S#{strategy.strategy_id} closed {symbol} - "
                                   f"{exit_info['reason']} - PnL: ${pnl:.2f}"
                                   )

        except Exception as e:
            self.log.e(self.tag, f"Error checking exits: {e}\n{traceback.format_exc()}")

    def calculate_rankings(self):
        """Tính rankings"""
        self.log.i(self.tag, "📊 Calculating strategy rankings...")

        for strategy in self.strategies:
            strategy.calculate_final_stats()

        strategies_with_trades = [s for s in self.strategies if s.stats['total_trades'] > 0]

        if not strategies_with_trades:
            self.log.w(self.tag, "⚠️ No strategies have any trades yet")
            return

        long_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'LONG']
        short_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'SHORT']

        sorted_long = sorted(long_strategies, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)
        sorted_short = sorted(short_strategies, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)

        all_sorted = sorted(strategies_with_trades, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)
        self.top_strategies = all_sorted[:10]
        self.best_strategy = all_sorted[0] if all_sorted else None
        self.best_long = sorted_long[0] if sorted_long else None
        self.best_short = sorted_short[0] if sorted_short else None

        self.log.i(self.tag, f"✅ {len(long_strategies)} LONG + {len(short_strategies)} SHORT have trades")

    async def report_results(self):
        """Report kết quả với chi tiết config"""
        try:
            self.calculate_rankings()

            if not self.best_strategy:
                strategies_with_positions = sum(1 for s in self.strategies if len(s.active_positions) > 0)
                total_positions = sum(len(s.active_positions) for s in self.strategies)
                tracked = sum(len(s) for s in self.symbol_to_strategies.values())

                message = (
                    f"📊 <b>BACKTEST STATUS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ Waiting for trades...\n\n"
                    f"📈 Strategies with positions: {strategies_with_positions}\n"
                    f"💼 Total positions: {total_positions}\n"
                    f"🔍 Tracked (optimized): {tracked}"
                )
                await self.tele_message.send_message(message, self.chat_id)
                return

            message = "📊 <b>BACKTEST REPORT</b>\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            # Top 5 với config chi tiết
            message += "🏆 <b>TOP 5:</b>\n"
            for i, strategy in enumerate(self.top_strategies[:5], 1):
                config = strategy.config
                stats = strategy.stats
                direction = config.get('direction', 'LONG')
                emoji = "📈" if direction == 'LONG' else "📉"

                message += (
                    f"\n{i}. {emoji} <b>#{strategy.strategy_id}</b>\n"
                    f"   💰 ${stats['total_pnl']:.2f} | {stats['total_trades']}T | "
                    f"WR:{stats['win_rate']:.0f}%\n"
                    f"   ⚙️ {direction} TP{config['take_profit']}% SL{config['stop_loss']}% "
                    f"Vol{config['volume_multiplier']}x Conf{config['min_confidence']}%\n"
                )

            # Best LONG/SHORT
            if self.best_long:
                c = self.best_long.config
                s = self.best_long.stats
                message += (
                    f"\n📈 <b>BEST LONG #{self.best_long.strategy_id}:</b>\n"
                    f"   ${s['total_pnl']:.2f} | {s['total_trades']}T | WR:{s['win_rate']:.0f}%\n"
                    f"   TP{c['take_profit']}% SL{c['stop_loss']}% Vol{c['volume_multiplier']}x "
                    f"Conf{c['min_confidence']}% RSI{c['rsi_threshold']}\n"
                )

            if self.best_short:
                c = self.best_short.config
                s = self.best_short.stats
                message += (
                    f"\n📉 <b>BEST SHORT #{self.best_short.strategy_id}:</b>\n"
                    f"   ${s['total_pnl']:.2f} | {s['total_trades']}T | WR:{s['win_rate']:.0f}%\n"
                    f"   TP{c['take_profit']}% SL{c['stop_loss']}% Vol{c['volume_multiplier']}x "
                    f"Conf{c['min_confidence']}% RSI{c['rsi_threshold']}\n"
                )

            # Overall
            strategies_with_trades = [s for s in self.strategies if s.stats['total_trades'] > 0]
            total_trades = sum(s.stats['total_trades'] for s in strategies_with_trades)
            avg_wr = np.mean([s.stats['win_rate'] for s in strategies_with_trades]) if strategies_with_trades else 0

            message += (
                f"\n📊 <b>OVERALL:</b>\n"
                f"Strategies: {len(self.strategies)} | Active: {len(strategies_with_trades)}\n"
                f"Total trades: {total_trades} | Avg WR: {avg_wr:.1f}%"
            )

            self.log.i(self.tag, message)
            await self.tele_message.send_message(message, self.chat_id)
            self.save_results_to_file()

        except Exception as e:
            self.log.e(self.tag, f"Error reporting: {e}\n{traceback.format_exc()}")

    def save_results_to_file(self):
        """Lưu kết quả vào file"""
        try:
            results = []
            for strategy in self.strategies:
                summary = strategy.get_summary()
                summary['config_details'] = strategy.config
                summary['trade_history'] = strategy.trade_history
                results.append(summary)

            filename = f"strategy_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump(results, f, indent=2, default=str)

            self.log.i(self.tag, f"💾 Saved to {filename}")

        except Exception as e:
            self.log.e(self.tag, f"Error saving: {e}")

    def get_best_strategy_config(self) -> Optional[Dict]:
        if self.best_strategy:
            return self.best_strategy.config
        return None