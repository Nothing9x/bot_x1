"""
StrategyManager - COMPLETE VERSION
- Queue system (không block WebSocket)
- PnL Tracking integration
- Detailed reporting với full params
"""

import json
import traceback
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional
import itertools
import numpy as np

from x1.bot.ai.trading_strategy import TradingStrategy
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils import constants
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class StrategyManager:
    """Manager để tạo và test nhiều strategies"""

    def __init__(self, log: LoggerWrapper, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "StrategyManager"
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        self.strategies: List[TradingStrategy] = []
        self.candle_buffer = defaultdict(lambda: defaultdict(list))

        # Optimization indices
        self.active_strategies_by_symbol = defaultdict(set)
        self.strategies_with_positions = set()

        # Queue system
        self.candle_queue = asyncio.Queue(maxsize=10000)
        self.signal_queue = asyncio.Queue(maxsize=1000)
        self.batch_size = 1000

        # Top strategies
        self.top_strategies = []
        self.best_strategy = None
        self.best_long = None
        self.best_short = None

        # Performance
        self.last_candle_process_time = 0
        self.total_candles_processed = 0
        self.queue_processing_started = False

        # Enhanced manager (will be set by mexc_pump_bot)
        self.enhanced_manager = None

    def generate_strategies(self, max_strategies: int = 100):
        """Tạo strategies"""
        self.log.i(self.tag, f"🔧 Generating {max_strategies} strategies...")

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
            strategy_id += 1

        # SHORT strategies
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'SHORT'
            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            strategy_id += 1

        self.log.i(self.tag, f"✅ Generated {len(self.strategies)} strategies")

        # Start background processing
        if not self.queue_processing_started:
            asyncio.create_task(self._process_candle_queue())
            asyncio.create_task(self._process_signal_queue())
            self.queue_processing_started = True

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """Put candle vào queue"""
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

            history = self.candle_buffer[symbol][interval]

            if len(history) == 0 or timestamp > history[-1]['timestamp']:
                history.append(candle)
                if len(history) > 100:
                    history.pop(0)
                is_new = True
            elif timestamp == history[-1]['timestamp']:
                history[-1] = candle
                is_new = False
            else:
                return

            try:
                self.candle_queue.put_nowait({
                    'symbol': symbol,
                    'interval': interval,
                    'candle': candle,
                    'is_new': is_new
                })
            except asyncio.QueueFull:
                self.log.w(self.tag, "⚠️ Candle queue full")

        except Exception as e:
            self.log.e(self.tag, f"Error queueing candle: {e}")

    async def _process_candle_queue(self):
        """Background task xử lý candles"""
        while True:
            try:
                candle_item = await self.candle_queue.get()
                start_time = datetime.now()

                await self._check_exits_optimized(
                    candle_item['symbol'],
                    candle_item['interval'],
                    candle_item['candle']
                )

                process_time = (datetime.now() - start_time).total_seconds()
                self.last_candle_process_time = process_time
                self.total_candles_processed += 1

                if process_time > 5:
                    self.log.w(self.tag, f"⚠️ Slow: {process_time:.2f}s")

                self.candle_queue.task_done()
                await asyncio.sleep(0.001)

            except Exception as e:
                self.log.e(self.tag, f"Error processing candle queue: {e}")
                await asyncio.sleep(0.1)

    async def _check_exits_optimized(self, symbol: str, interval: str, candle: dict):
        """Chỉ check strategies có positions"""
        try:
            if symbol not in self.active_strategies_by_symbol:
                return

            strategy_ids = list(self.active_strategies_by_symbol[symbol])
            if not strategy_ids:
                return

            tasks = []
            for strategy_id in strategy_ids:
                strategy = self.strategies[strategy_id - 1]

                if symbol in strategy.active_positions:
                    task = self._check_strategy_exit_async(strategy, symbol, candle)
                    tasks.append(task)

                    if len(tasks) >= self.batch_size:
                        await asyncio.gather(*tasks, return_exceptions=True)
                        tasks = []
                        await asyncio.sleep(0)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self.log.e(self.tag, f"Error checking exits: {e}")

    async def _check_strategy_exit_async(self, strategy: TradingStrategy, symbol: str, candle: dict):
        """Check exit - không gửi telegram"""
        try:
            exit_info = strategy.check_exit(symbol, candle)

            if exit_info:
                strategy.close_position(
                    symbol,
                    exit_info['exit_price'],
                    exit_info['reason']
                )

                if not strategy.active_positions:
                    self.strategies_with_positions.discard(strategy.strategy_id)

                self.active_strategies_by_symbol[symbol].discard(strategy.strategy_id)

                # Chỉ log
                if strategy.trade_history:
                    pnl = strategy.trade_history[-1]['pnl_usdt']
                    emoji = "✅" if pnl > 0 else "❌"
                    if constants.DEBUG_LOG:
                        self.log.d(self.tag,
                              f"{emoji} Strategy {strategy.strategy_id} closed {symbol} - "
                              f"{exit_info['reason']} - PnL: ${pnl:.2f}")

        except Exception as e:
            self.log.e(self.tag, f"Error in strategy {strategy.strategy_id}: {e}")

    async def on_pump_signal(self, signal: Dict):
        """Put signal vào queue"""
        try:
            try:
                self.signal_queue.put_nowait(signal.copy())
            except asyncio.QueueFull:
                self.log.w(self.tag, "⚠️ Signal queue full")
        except Exception as e:
            self.log.e(self.tag, f"Error queueing signal: {e}")

    async def _process_signal_queue(self):
        """Background task xử lý signals"""
        while True:
            try:
                signal = await self.signal_queue.get()
                await self._process_pump_signal(signal)
                self.signal_queue.task_done()
                await asyncio.sleep(0.001)
            except Exception as e:
                self.log.e(self.tag, f"Error processing signal queue: {e}")
                await asyncio.sleep(0.1)

    async def _process_pump_signal(self, signal: Dict):
        """Xử lý pump signal"""
        try:
            symbol = signal['symbol']
            price = signal['price']

            entered_count = 0
            tasks = []

            for strategy in self.strategies:
                task = self._check_strategy_entry_async(strategy, signal, symbol, price)
                tasks.append(task)

                if len(tasks) >= self.batch_size:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    entered_count += sum(1 for r in results if r is True)
                    tasks = []
                    await asyncio.sleep(0)

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                entered_count += sum(1 for r in results if r is True)

            if entered_count > 0:
                self.log.i(self.tag, f"✅ {entered_count} strategies entered {symbol}")

        except Exception as e:
            self.log.e(self.tag, f"Error processing signal: {e}")

    async def _check_strategy_entry_async(self, strategy: TradingStrategy,
                                          signal: Dict, symbol: str, price: float) -> bool:
        """Check entry"""
        try:
            if symbol in strategy.active_positions:
                return False

            if strategy.should_enter(signal):
                strategy.enter_position(symbol, price, signal)
                self.strategies_with_positions.add(strategy.strategy_id)
                self.active_strategies_by_symbol[symbol].add(strategy.strategy_id)
                return True
            return False
        except Exception as e:
            return False

    def calculate_rankings(self):
        """Calculate rankings"""
        try:
            self.log.i(self.tag, "📊 Calculating rankings...")

            strategies_with_trades = []
            for strategy in self.strategies:
                if strategy.stats['total_trades'] > 0:
                    strategy.calculate_final_stats()
                    strategies_with_trades.append(strategy)

            if not strategies_with_trades:
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

        except Exception as e:
            self.log.e(self.tag, f"Error calculating rankings: {e}")

    async def report_results(self):
        """REPORT với PnL tracking nếu có"""
        try:
            # ✨ Nếu có enhanced manager, dùng nó
            if hasattr(self, 'enhanced_manager') and self.enhanced_manager:
                self.enhanced_manager.calculate_rankings_with_unrealized()
                message = self.enhanced_manager.build_detailed_report_with_unrealized()
            else:
                # Fallback to normal
                self.calculate_rankings()
                message = self._build_normal_report()

            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error reporting: {e}")

    def _build_normal_report(self) -> str:
        """Build report bình thường (không có unrealized PnL)"""
        if not self.best_strategy:
            message = (
                f"📊 BACKTEST STATUS\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ Waiting for trades...\n\n"
                f"📈 Strategies with positions: {len(self.strategies_with_positions)}/{len(self.strategies)}\n"
                f"⚡ Avg candle time: {self.last_candle_process_time:.3f}s\n"
                f"📦 Queue: C={self.candle_queue.qsize()} S={self.signal_queue.qsize()}"
            )
            return message

        # Build detailed report
        message = "📊 BACKTEST RESULTS - TOP 10\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        # Best overall
        s = self.best_strategy
        stats = s.stats
        config = s.config

        message += f"🏆 BEST OVERALL - Strategy #{s.strategy_id} ({config['direction']})\n"
        message += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        message += f"📊 Performance:\n"
        message += f"  • Trades: {stats['total_trades']} ({stats['winning_trades']}W/{stats['losing_trades']}L)\n"
        message += f"  • Win Rate: {stats['win_rate']:.1f}%\n"
        message += f"  • Total PnL: ${stats['total_pnl']:.2f}\n"
        message += f"  • Profit Factor: {stats.get('profit_factor', 0):.2f}\n"
        message += f"\n⚙️ Config:\n"
        message += f"  • TP: {config['take_profit']}% | SL: {config['stop_loss']}%\n"
        message += f"  • Vol: >{config['volume_multiplier']}x | RSI: >{config['rsi_threshold']}\n"
        message += f"  • Confidence: >{config['min_confidence']}%\n\n"

        # Top 10
        message += f"📊 TOP 10 STRATEGIES:\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for rank, strategy in enumerate(self.top_strategies, 1):
            stats = strategy.stats
            config = strategy.config

            message += (
                f"#{rank}. S{strategy.strategy_id} {config['direction']}: "
                f"WR={stats['win_rate']:.0f}% PnL=${stats['total_pnl']:.0f} "
                f"PF={stats.get('profit_factor', 0):.1f} "
                f"[TP{config['take_profit']}% SL{config['stop_loss']}%]\n"
            )

        # Performance
        message += f"\n⚡ PERFORMANCE:\n"
        message += f"  Candle time: {self.last_candle_process_time:.3f}s\n"
        message += f"  Active positions: {len(self.strategies_with_positions)}\n"

        return message

    def save_results_to_file(self):
        """Lưu kết quả"""
        try:
            results = []
            for strategy in self.strategies:
                if strategy.stats['total_trades'] > 0:
                    summary = strategy.get_summary()
                    summary['trade_history'] = strategy.trade_history
                    results.append(summary)

            filename = f"strategy_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump(results, f, indent=2, default=str)

            self.log.i(self.tag, f"💾 Results saved to {filename}")

        except Exception as e:
            self.log.e(self.tag, f"Error saving results: {e}")

    def get_best_strategy_config(self) -> Optional[Dict]:
        """Lấy config tốt nhất"""
        if self.best_strategy:
            return self.best_strategy.config
        return None

    def get_performance_stats(self) -> Dict:
        """Get stats"""
        return {
            'total_strategies': len(self.strategies),
            'strategies_with_trades': sum(1 for s in self.strategies if s.stats['total_trades'] > 0),
            'strategies_with_positions': len(self.strategies_with_positions),
            'avg_candle_process_time': self.last_candle_process_time,
            'total_candles_processed': self.total_candles_processed,
            'candle_queue_size': self.candle_queue.qsize(),
            'signal_queue_size': self.signal_queue.qsize(),
        }