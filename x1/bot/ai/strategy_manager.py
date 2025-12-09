# -*- coding: utf-8 -*-
"""
StrategyManager - Quản lý và test nhiều strategies với Reduce TP

UPDATED: Thay thế trailing_stop bằng reduce
- reduce = % TP giảm mỗi phút (0 = disabled)
- Ví dụ: reduce=5 với TP=40% → mỗi phút TP giảm 5% tổng khoảng cách
"""

import json
import traceback
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Set
import itertools
import random

import numpy as np

from x1.bot.ai.trading_strategy import TradingStrategy
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils import constants
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class StrategyManager:
    """
    Manager để tạo và test nhiều strategies
    Với Reduce TP Strategy
    """

    def __init__(self, log: LoggerWrapper, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "StrategyManager"
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        self.strategies: List[TradingStrategy] = []
        self.candle_buffer = defaultdict(lambda: defaultdict(list))  # {symbol: {interval: [candles]}}

        # ===== SYMBOL-INDEXED LOOKUP (O(1) performance) =====
        self.symbol_to_strategies: Dict[str, Set[int]] = defaultdict(set)

        # Top strategies
        self.top_strategies = []
        self.best_strategy = None
        self.best_long = None
        self.best_short = None

        # ===== MEMORY MANAGEMENT =====
        self.max_candle_history = 50  # Giảm từ 100
        self.max_trade_history_per_strategy = 100
        self._cleanup_counter = 0
        self._cleanup_interval = 1000

    def generate_strategies(self, max_strategies: int = 100):
        """
        Tạo ra nhiều strategies - Cả LONG và SHORT
        50% LONG, 50% SHORT để test

        UPDATED: Thay trailing_stop bằng reduce
        """
        self.log.i(self.tag, f"🔧 Generating {max_strategies} strategies (50% LONG, 50% SHORT)...")

        # Define parameter ranges - THAY trailing_stop BẰNG reduce
        param_grid = {
            'take_profit': [3, 5, 7, 10, 15, 20],  # Thêm TP cao hơn cho reduce
            'stop_loss': [3, 5, 7, 10],
            'rsi_threshold': [20, 30, 40, 50, 60],
            'volume_multiplier': [1.0, 1.5, 2, 3],
            'price_increase_threshold': [1, 1.5, 2],
            'min_confidence': [30, 40, 50, 60],
            'timeframe': ['1m'],
            # ===== REDUCE TP (thay thế trailing_stop) =====
            'reduce': [2, 5, 8, 10],  # 0 = disabled, 2-10% per minute
            'min_trend_strength': [0.0],
            'require_breakout': [False],
            'min_volume_consistency': [0.0],
        }

        # Generate all combinations
        keys = list(param_grid.keys())
        values = list(param_grid.values())

        combinations = list(itertools.product(*values))

        # Limit to max_strategies / 2 (vì sẽ nhân đôi cho LONG và SHORT)
        target_per_direction = max_strategies // 2

        if len(combinations) > target_per_direction:
            combinations = random.sample(combinations, target_per_direction)

        strategy_id = 1

        # Create LONG strategies
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'LONG'

            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            strategy_id += 1

        # Create SHORT strategies (cùng config nhưng SHORT)
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'SHORT'

            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            strategy_id += 1

        long_count = sum(1 for s in self.strategies if s.config['direction'] == 'LONG')
        short_count = sum(1 for s in self.strategies if s.config['direction'] == 'SHORT')

        # Count reduce strategies
        reduce_strategies = sum(1 for s in self.strategies if s.config.get('reduce', 0) > 0)

        self.log.i(self.tag, f"✅ Generated {len(self.strategies)} strategies")
        self.log.i(self.tag, f"   📈 {long_count} LONG strategies")
        self.log.i(self.tag, f"   📉 {short_count} SHORT strategies")
        self.log.i(self.tag, f"   ⏱️ {reduce_strategies} with Reduce TP enabled")

        # Log sample strategies
        if len(self.strategies) >= 4:
            self.log.i(self.tag, "📋 Sample strategies:")

            # Sample LONG with reduce
            long_reduce = next((s for s in self.strategies
                                if s.config['direction'] == 'LONG'
                                and s.config.get('reduce', 0) > 0), None)
            if long_reduce:
                self.log.i(self.tag,
                           f"  #{long_reduce.strategy_id} LONG: "
                           f"TP={long_reduce.config['take_profit']}% "
                           f"SL={long_reduce.config['stop_loss']}% "
                           f"Reduce={long_reduce.config['reduce']}%/min"
                           )

            # Sample SHORT with reduce
            short_reduce = next((s for s in self.strategies
                                 if s.config['direction'] == 'SHORT'
                                 and s.config.get('reduce', 0) > 0), None)
            if short_reduce:
                self.log.i(self.tag,
                           f"  #{short_reduce.strategy_id} SHORT: "
                           f"TP={short_reduce.config['take_profit']}% "
                           f"SL={short_reduce.config['stop_loss']}% "
                           f"Reduce={short_reduce.config['reduce']}%/min"
                           )

    async def on_pump_signal(self, signal: Dict):
        """Alias cho on_signal - được gọi từ mexc_pump_bot"""
        await self.on_signal(signal)

    async def on_signal(self, signal: Dict):
        """Nhận signal và test với tất cả strategies - O(1) lookup"""
        try:
            symbol = signal['symbol']
            timeframe = signal.get('timeframe', '1m')
            price = signal['price']

            # Test với tất cả strategies
            entered_count = 0
            for strategy in self.strategies:
                if strategy.should_enter(signal):
                    if symbol not in strategy.active_positions:
                        strategy.enter_position(symbol, price, signal)
                        # Track symbol → strategy mapping
                        self.symbol_to_strategies[symbol].add(strategy.strategy_id)
                        entered_count += 1

            if entered_count > 0:
                self.log.i(self.tag, f"📥 {symbol}: {entered_count} strategies entered")

        except Exception as e:
            self.log.e(self.tag, f"Error processing signal: {e}")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: Dict):
        """Nhận update candle và check exits - O(1) lookup"""
        try:
            # Memory management
            self._cleanup_counter += 1
            if self._cleanup_counter >= self._cleanup_interval:
                self._cleanup_memory()
                self._cleanup_counter = 0

            # ===== PARSE CANDLE DATA =====
            # WebSocket format: o, h, l, c, a (amount/volume)
            candle = {
                'timestamp': candle_data.get('t', 0),
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
                'volume': float(candle_data.get('a', 0)),
            }

            # Store candle
            buffer = self.candle_buffer[symbol][interval]

            if len(buffer) == 0 or candle['timestamp'] > buffer[-1].get('timestamp', 0):
                # New candle
                buffer.append(candle)
                if len(buffer) > self.max_candle_history:
                    self.candle_buffer[symbol][interval] = buffer[-self.max_candle_history:]
            elif len(buffer) > 0 and candle['timestamp'] == buffer[-1].get('timestamp', 0):
                # Update current candle
                buffer[-1] = candle

            # ===== O(1) LOOKUP: Chỉ check strategies có position với symbol này =====
            strategy_ids = self.symbol_to_strategies.get(symbol, set())
            if not strategy_ids:
                return

            strategies_to_check = [s for s in self.strategies if s.strategy_id in strategy_ids]

            for strategy in strategies_to_check:
                if symbol in strategy.active_positions:
                    exit_result = strategy.check_exit(symbol, candle)

                    if exit_result:
                        strategy.close_position(symbol, exit_result['exit_price'], exit_result['reason'])
                        # Remove from index
                        self.symbol_to_strategies[symbol].discard(strategy.strategy_id)

                        # Log với reduce info
                        reduce = strategy.config.get('reduce', 0)
                        reduce_str = f" (Reduce {reduce}%/min)" if reduce > 0 else ""
                        if constants.DEBUG_LOG:
                            self.log.d(self.tag,
                                   f"📤 {symbol}: Strategy {strategy.strategy_id} exited "
                                   f"({exit_result['reason']}){reduce_str}"
                                   )

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}")

    def _cleanup_memory(self):
        """Cleanup memory định kỳ"""
        # Cleanup empty symbol_to_strategies
        empty_symbols = [s for s, ids in self.symbol_to_strategies.items() if len(ids) == 0]
        for symbol in empty_symbols:
            del self.symbol_to_strategies[symbol]

        # Limit trade_history per strategy
        for strategy in self.strategies:
            if len(strategy.trade_history) > self.max_trade_history_per_strategy:
                strategy.trade_history = strategy.trade_history[-self.max_trade_history_per_strategy:]

        if empty_symbols:
            self.log.d(self.tag, f"🧹 Cleaned up {len(empty_symbols)} empty symbol indexes")

    def calculate_rankings(self):
        """Tính ranking các strategies"""
        self.log.i(self.tag, "📊 Calculating strategy rankings...")

        # Calculate final stats for all
        for strategy in self.strategies:
            strategy.calculate_final_stats()

        # Filter strategies that have trades
        strategies_with_trades = [s for s in self.strategies if s.stats['total_trades'] > 0]

        if not strategies_with_trades:
            self.log.w(self.tag, "⚠️ No strategies have any trades yet")
            return

        # Separate LONG and SHORT
        long_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'LONG']
        short_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'SHORT']

        # Sort by total PnL
        sorted_long = sorted(long_strategies, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)
        sorted_short = sorted(short_strategies, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)

        # Get top 10 overall
        all_sorted = sorted(strategies_with_trades, key=lambda s: s.stats.get('total_pnl', 0), reverse=True)
        self.top_strategies = all_sorted[:10]
        self.best_strategy = all_sorted[0] if all_sorted else None

        # Store best of each type
        self.best_long = sorted_long[0] if sorted_long else None
        self.best_short = sorted_short[0] if sorted_short else None

        self.log.i(self.tag,
                   f"✅ Rankings: {len(long_strategies)} LONG + {len(short_strategies)} SHORT have trades"
                   )

    async def report_results(self):
        """Report kết quả - So sánh LONG vs SHORT và Reduce vs No-Reduce"""
        try:
            self.calculate_rankings()

            if not self.best_strategy:
                strategies_with_positions = sum(1 for s in self.strategies if len(s.active_positions) > 0)
                total_active_positions = sum(len(s.active_positions) for s in self.strategies)

                message = (
                    f"📊 BACKTEST STATUS\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ Waiting for trades to complete...\n\n"
                    f"📈 Strategies with open positions: {strategies_with_positions}/{len(self.strategies)}\n"
                    f"💼 Total open positions: {total_active_positions}\n\n"
                    f"ℹ️ Strategies are testing entries.\n"
                    f"Results will be available when positions close (TP/SL hit)."
                )

                self.log.i(self.tag, message)
                await self.tele_message.send_message(message, self.chat_id)
                return

            # Build report message
            message = "📊 STRATEGY BACKTESTING RESULTS\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            # LONG vs SHORT comparison
            long_strategies = [s for s in self.strategies if
                               s.config['direction'] == 'LONG' and s.stats['total_trades'] > 0]
            short_strategies = [s for s in self.strategies if
                                s.config['direction'] == 'SHORT' and s.stats['total_trades'] > 0]

            if long_strategies and short_strategies:
                long_pnl = sum(s.stats['total_pnl'] for s in long_strategies)
                short_pnl = sum(s.stats['total_pnl'] for s in short_strategies)
                long_wr = np.mean([s.stats['win_rate'] for s in long_strategies])
                short_wr = np.mean([s.stats['win_rate'] for s in short_strategies])

                message += "🎯 LONG vs SHORT COMPARISON\n"
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                message += f"📈 LONG:  PnL=${long_pnl:.2f} | WR={long_wr:.1f}% | {len(long_strategies)} strats\n"
                message += f"📉 SHORT: PnL=${short_pnl:.2f} | WR={short_wr:.1f}% | {len(short_strategies)} strats\n"

                if long_pnl > short_pnl:
                    message += f"🏆 WINNER: LONG (+${long_pnl - short_pnl:.2f})\n\n"
                else:
                    message += f"🏆 WINNER: SHORT (+${short_pnl - long_pnl:.2f})\n\n"

            # ===== REDUCE vs NO-REDUCE COMPARISON =====
            reduce_strategies = [s for s in self.strategies if
                                 s.config.get('reduce', 0) > 0 and s.stats['total_trades'] > 0]
            no_reduce_strategies = [s for s in self.strategies if
                                    s.config.get('reduce', 0) == 0 and s.stats['total_trades'] > 0]

            if reduce_strategies and no_reduce_strategies:
                reduce_pnl = sum(s.stats['total_pnl'] for s in reduce_strategies)
                no_reduce_pnl = sum(s.stats['total_pnl'] for s in no_reduce_strategies)
                reduce_wr = np.mean([s.stats['win_rate'] for s in reduce_strategies])
                no_reduce_wr = np.mean([s.stats['win_rate'] for s in no_reduce_strategies])

                message += "⏱️ REDUCE TP vs NO-REDUCE\n"
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                message += f"⏱️ REDUCE:    PnL=${reduce_pnl:.2f} | WR={reduce_wr:.1f}% | {len(reduce_strategies)} strats\n"
                message += f"🔒 NO-REDUCE: PnL=${no_reduce_pnl:.2f} | WR={no_reduce_wr:.1f}% | {len(no_reduce_strategies)} strats\n"

                if reduce_pnl > no_reduce_pnl:
                    message += f"🏆 WINNER: REDUCE TP (+${reduce_pnl - no_reduce_pnl:.2f})\n\n"
                else:
                    message += f"🏆 WINNER: NO-REDUCE (+${no_reduce_pnl - reduce_pnl:.2f})\n\n"

            # Best overall strategy
            best = self.best_strategy.get_summary()
            reduce_info = f" | Reduce={best['config'].get('reduce', 0)}%/min" if best['config'].get('reduce',
                                                                                                    0) > 0 else ""

            message += f"🏆 BEST OVERALL: {best['name']}\n"
            message += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"Direction: {best['config']['direction']}{reduce_info}\n"
            message += f"📈 Total Trades: {best['stats']['total_trades']}\n"
            message += f"✅ Win Rate: {best['stats']['win_rate']:.1f}%\n"
            message += f"💰 Total PnL: ${best['stats']['total_pnl']:.2f}\n"
            message += f"📊 ROI: {best['roi']:.2f}%\n"
            message += f"🎯 Profit Factor: {best['stats']['profit_factor']:.2f}\n"
            message += f"📉 Max DD: {best['stats']['max_drawdown']:.2f}%\n\n"

            # Best LONG
            if self.best_long:
                best_long = self.best_long.get_summary()
                reduce_str = f" R{best_long['config'].get('reduce', 0)}%" if best_long['config'].get('reduce',
                                                                                                     0) > 0 else ""
                message += f"📈 BEST LONG: {best_long['name']}\n"
                message += f"TP{best_long['config']['take_profit']}% SL{best_long['config']['stop_loss']}%{reduce_str}\n"
                message += f"Trades: {best_long['stats']['total_trades']} | "
                message += f"WR: {best_long['stats']['win_rate']:.1f}% | "
                message += f"PnL: ${best_long['stats']['total_pnl']:.2f}\n\n"

            # Best SHORT
            if self.best_short:
                best_short = self.best_short.get_summary()
                reduce_str = f" R{best_short['config'].get('reduce', 0)}%" if best_short['config'].get('reduce',
                                                                                                       0) > 0 else ""
                message += f"📉 BEST SHORT: {best_short['name']}\n"
                message += f"TP{best_short['config']['take_profit']}% SL{best_short['config']['stop_loss']}%{reduce_str}\n"
                message += f"Trades: {best_short['stats']['total_trades']} | "
                message += f"WR: {best_short['stats']['win_rate']:.1f}% | "
                message += f"PnL: ${best_short['stats']['total_pnl']:.2f}\n\n"

            # Top 5
            message += "🔝 TOP 5 STRATEGIES:\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for i, strategy in enumerate(self.top_strategies[:5], 1):
                summary = strategy.get_summary()
                direction_emoji = "📈" if summary['config']['direction'] == 'LONG' else "📉"
                reduce_str = f" R{summary['config'].get('reduce', 0)}%" if summary['config'].get('reduce',
                                                                                                 0) > 0 else ""

                message += (f"{i}. {direction_emoji} "
                            f"TP{summary['config']['take_profit']}% "
                            f"SL{summary['config']['stop_loss']}%{reduce_str} | "
                            f"WR={summary['stats']['win_rate']:.0f}% | "
                            f"PnL=${summary['stats']['total_pnl']:.2f}\n")

            self.log.i(self.tag, message)
            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error reporting results: {e}\n{traceback.format_exc()}")