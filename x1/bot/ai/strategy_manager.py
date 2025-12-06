import json
import traceback
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional
import itertools

import numpy as np

from x1.bot.ai.trading_strategy import TradingStrategy
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class StrategyManager:
    """
    Manager để tạo và test nhiều strategies
    """

    def __init__(self, log: LoggerWrapper, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "StrategyManager"
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        self.strategies: List[TradingStrategy] = []
        self.candle_buffer = defaultdict(lambda: defaultdict(list))  # {symbol: {interval: [candles]}}

        # Top strategies
        self.top_strategies = []
        self.best_strategy = None

    def generate_strategies(self, max_strategies: int = 100):
        """
        Tạo ra nhiều strategies - Cả LONG và SHORT
        50% LONG, 50% SHORT để test
        """
        self.log.i(self.tag, f"🔧 Generating {max_strategies} strategies (50% LONG, 50% SHORT)...")

        # Define parameter ranges
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

        # Generate all combinations
        keys = list(param_grid.keys())
        values = list(param_grid.values())

        combinations = list(itertools.product(*values))

        # Limit to max_strategies / 2 (vì sẽ nhân đôi cho LONG và SHORT)
        target_per_direction = max_strategies // 2

        if len(combinations) > target_per_direction:
            import random
            combinations = random.sample(combinations, target_per_direction)

        strategy_id = 1

        # Create LONG strategies
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'LONG'  # THÊM direction

            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            strategy_id += 1

        # Create SHORT strategies (cùng config nhưng SHORT)
        for combo in combinations:
            config = dict(zip(keys, combo))
            config['position_size_usdt'] = 50
            config['direction'] = 'SHORT'  # THÊM direction

            strategy = TradingStrategy(strategy_id, config)
            self.strategies.append(strategy)
            strategy_id += 1

        long_count = sum(1 for s in self.strategies if s.config['direction'] == 'LONG')
        short_count = sum(1 for s in self.strategies if s.config['direction'] == 'SHORT')

        self.log.i(self.tag, f"✅ Generated {len(self.strategies)} strategies")
        self.log.i(self.tag, f"   📈 {long_count} LONG strategies")
        self.log.i(self.tag, f"   📉 {short_count} SHORT strategies")

        # Log easiest strategies
        easiest_long = sum(1 for s in self.strategies
                           if s.config['direction'] == 'LONG'
                           and s.config['min_confidence'] <= 40
                           and s.config['volume_multiplier'] <= 2)

        easiest_short = sum(1 for s in self.strategies
                            if s.config['direction'] == 'SHORT'
                            and s.config['min_confidence'] <= 40
                            and s.config['volume_multiplier'] <= 2)

        self.log.i(self.tag, f"   💡 {easiest_long} LONG + {easiest_short} SHORT with EASY conditions")

        # Log sample strategies
        if len(self.strategies) >= 4:
            self.log.i(self.tag, "📋 Sample strategies:")
            # First LONG
            long_sample = next((s for s in self.strategies if s.config['direction'] == 'LONG'), None)
            if long_sample:
                self.log.i(self.tag,
                           f"  #{long_sample.strategy_id} LONG: TP={long_sample.config['take_profit']}% "
                           f"SL={long_sample.config['stop_loss']}% "
                           f"Vol>={long_sample.config['volume_multiplier']}x "
                           f"Conf>={long_sample.config['min_confidence']}%"
                           )

            # First SHORT
            short_sample = next((s for s in self.strategies if s.config['direction'] == 'SHORT'), None)
            if short_sample:
                self.log.i(self.tag,
                           f"  #{short_sample.strategy_id} SHORT: TP={short_sample.config['take_profit']}% "
                           f"SL={short_sample.config['stop_loss']}% "
                           f"Vol>={short_sample.config['volume_multiplier']}x "
                           f"Conf>={short_sample.config['min_confidence']}%"
                           )

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """
        Nhận candle update và test tất cả strategies
        """
        try:
            # Parse candle
            timestamp = candle_data.get('t', 0)
            candle = {
                'timestamp': timestamp,
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
                'volume': float(candle_data.get('a', 0)),
            }

            # Update buffer
            history = self.candle_buffer[symbol][interval]

            if len(history) == 0 or timestamp > history[-1]['timestamp']:
                # New candle
                history.append(candle)
                if len(history) > 100:
                    history.pop(0)

                # Process new candle - check exits first
                await self.check_all_exits(symbol, interval, candle)

            elif timestamp == history[-1]['timestamp']:
                # Update current candle
                history[-1] = candle

                # Still check exits on update
                await self.check_all_exits(symbol, interval, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle: {e}\n{traceback.format_exc()}")

    async def on_pump_signal(self, signal: Dict):
        """
        Nhận pump signal và cho tất cả strategies vào lệnh nếu match điều kiện
        """
        try:
            symbol = signal['symbol']
            price = signal['price']
            timeframe = signal.get('timeframe', '1m')

            self.log.d(self.tag, f"📊 Testing signal for {symbol} at ${price} (timeframe: {timeframe})")

            # Debug: Log signal details CHI TIẾT
            price_change = signal.get('price_change_1m' if timeframe == '1m' else 'price_change_5m', 0)
            volume_ratio = signal.get('volume_ratio', 0)
            confidence = signal.get('confidence', 0)
            rsi = signal.get('rsi')

            self.log.d(self.tag,
                       f"   Signal: price_change={price_change:.2f}%, "
                       f"volume={volume_ratio:.1f}x, "
                       f"rsi={rsi}, "
                       f"confidence={confidence}%"
                       )

            # Test với tất cả strategies
            entered_count = 0
            matched_strategies = []
            failed_reasons = {}  # Track lý do fail

            # Test với 5 strategies đầu tiên để debug
            test_strategies = self.strategies[:5] if len(self.strategies) > 5 else self.strategies

            for strategy in test_strategies:
                # DEBUG: Check từng điều kiện
                reasons = []

                # Check timeframe
                if signal.get('timeframe', '1m') != strategy.config['timeframe']:
                    reasons.append(f"timeframe_{strategy.config['timeframe']}")

                # Check price
                if timeframe == '1m':
                    pc = signal.get('price_change_1m', 0)
                else:
                    pc = signal.get('price_change_5m', 0)

                if pc < strategy.config['price_increase_threshold']:
                    reasons.append(f"price_{pc:.2f}<{strategy.config['price_increase_threshold']}")

                # Check volume
                if signal.get('volume_ratio', 0) < strategy.config['volume_multiplier']:
                    reasons.append(f"vol_{volume_ratio:.1f}<{strategy.config['volume_multiplier']}")

                # Check confidence
                if signal.get('confidence', 0) < strategy.config['min_confidence']:
                    reasons.append(f"conf_{confidence}<{strategy.config['min_confidence']}")

                # Check RSI
                if rsi is not None and rsi < strategy.config['rsi_threshold']:
                    reasons.append(f"rsi_{rsi}<{strategy.config['rsi_threshold']}")

                if reasons:
                    reason_str = ",".join(reasons)
                    failed_reasons[reason_str] = failed_reasons.get(reason_str, 0) + 1
                    if len(test_strategies) <= 5:  # Debug chi tiết cho 5 strategies đầu
                        self.log.d(self.tag, f"      Strategy {strategy.strategy_id} FAILED: {reason_str}")

            # Test với TẤT CẢ strategies
            for strategy in self.strategies:
                if strategy.should_enter(signal):
                    # Check if already has position
                    if symbol not in strategy.active_positions:
                        strategy.enter_position(symbol, price, signal)
                        entered_count += 1
                        matched_strategies.append(strategy.strategy_id)

            if entered_count > 0:
                self.log.i(self.tag, f"✅ {entered_count}/{len(self.strategies)} strategies entered {symbol}")
                if len(matched_strategies) <= 20:
                    self.log.d(self.tag, f"   Strategies: {matched_strategies}")
            else:
                self.log.w(self.tag, f"⚠️ NO strategies matched for {symbol} (timeframe: {timeframe})")

                # Log top 3 lý do fail
                if failed_reasons:
                    sorted_reasons = sorted(failed_reasons.items(), key=lambda x: x[1], reverse=True)
                    self.log.w(self.tag, f"   Top failure reasons:")
                    for reason, count in sorted_reasons[:3]:
                        self.log.w(self.tag, f"      {count} strategies: {reason}")

        except Exception as e:
            self.log.e(self.tag, f"Error handling signal: {e}\n{traceback.format_exc()}")

    async def check_all_exits(self, symbol: str, interval: str, candle: Dict):
        """
        Kiểm tra tất cả strategies xem có cần thoát lệnh không
        """
        try:
            for strategy in self.strategies:
                if symbol in strategy.active_positions:
                    exit_info = strategy.check_exit(symbol, candle)

                    if exit_info:
                        strategy.close_position(
                            symbol,
                            exit_info['exit_price'],
                            exit_info['reason']
                        )

                        # Log
                        pnl = strategy.trade_history[-1]['pnl_usdt']
                        emoji = "✅" if pnl > 0 else "❌"
                        self.log.d(self.tag,
                                   f"{emoji} {strategy.get_name()} closed {symbol} - "
                                   f"{exit_info['reason']} - PnL: ${pnl:.2f}"
                                   )

        except Exception as e:
            self.log.e(self.tag, f"Error checking exits: {e}\n{traceback.format_exc()}")

    def calculate_rankings(self):
        """Tính toán và xếp hạng strategies - Riêng cho LONG và SHORT"""
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
        """Report kết quả - So sánh LONG vs SHORT"""
        try:
            # Calculate rankings first
            self.calculate_rankings()

            if not self.best_strategy:
                # ... existing waiting message ...
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
            if hasattr(self, 'best_long') and hasattr(self, 'best_short'):
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
                    message += f"📈 LONG:  PnL=${long_pnl:.2f} | WR={long_wr:.1f}% | {len(long_strategies)} strategies\n"
                    message += f"📉 SHORT: PnL=${short_pnl:.2f} | WR={short_wr:.1f}% | {len(short_strategies)} strategies\n"

                    if long_pnl > short_pnl:
                        message += f"🏆 WINNER: LONG (+${long_pnl - short_pnl:.2f})\n\n"
                    else:
                        message += f"🏆 WINNER: SHORT (+${short_pnl - long_pnl:.2f})\n\n"

            # Best overall strategy
            best = self.best_strategy.get_summary()
            message += f"🏆 BEST OVERALL: {best['name']}\n"
            message += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"Direction: {best['config']['direction']}\n"
            message += f"📈 Total Trades: {best['stats']['total_trades']}\n"
            message += f"✅ Win Rate: {best['stats']['win_rate']:.1f}%\n"
            message += f"💰 Total PnL: ${best['stats']['total_pnl']:.2f}\n"
            message += f"📊 ROI: {best['roi']:.2f}%\n"
            message += f"🎯 Profit Factor: {best['stats']['profit_factor']:.2f}\n"
            message += f"📉 Max DD: {best['stats']['max_drawdown']:.2f}%\n\n"

            # Best LONG
            if hasattr(self, 'best_long') and self.best_long:
                best_long = self.best_long.get_summary()
                message += f"📈 BEST LONG: {best_long['name']}\n"
                message += f"Trades: {best_long['stats']['total_trades']} | "
                message += f"WR: {best_long['stats']['win_rate']:.1f}% | "
                message += f"PnL: ${best_long['stats']['total_pnl']:.2f}\n\n"

            # Best SHORT
            if hasattr(self, 'best_short') and self.best_short:
                best_short = self.best_short.get_summary()
                message += f"📉 BEST SHORT: {best_short['name']}\n"
                message += f"Trades: {best_short['stats']['total_trades']} | "
                message += f"WR: {best_short['stats']['win_rate']:.1f}% | "
                message += f"PnL: ${best_short['stats']['total_pnl']:.2f}\n\n"

            # Top 5
            message += "🔝 TOP 5 STRATEGIES:\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for i, strategy in enumerate(self.top_strategies[:5], 1):
                summary = strategy.get_summary()
                direction_emoji = "📈" if summary['config']['direction'] == 'LONG' else "📉"
                message += f"{i}. {direction_emoji} ROI: {summary['roi']:.1f}% | "
                message += f"WR: {summary['stats']['win_rate']:.0f}% | "
                message += f"PF: {summary['stats']['profit_factor']:.2f} | "
                message += f"Trades: {summary['stats']['total_trades']}\n"

            # Overall stats
            strategies_with_trades = [s for s in self.strategies if s.stats['total_trades'] > 0]
            total_trades = sum(s.stats['total_trades'] for s in strategies_with_trades)
            avg_trades = total_trades / len(strategies_with_trades) if strategies_with_trades else 0
            avg_win_rate = np.mean(
                [s.stats['win_rate'] for s in strategies_with_trades]) if strategies_with_trades else 0

            message += f"\n📊 OVERALL STATS:\n"
            message += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"Total Strategies: {len(self.strategies)}\n"
            message += f"Active Strategies: {len(strategies_with_trades)}\n"
            message += f"Avg Trades/Strategy: {avg_trades:.1f}\n"
            message += f"Avg Win Rate: {avg_win_rate:.1f}%\n"
            message += f"Total Signals Processed: {total_trades}\n"

            # Send report
            self.log.i(self.tag, message)
            await self.tele_message.send_message(message, self.chat_id)

            # Save detailed results to file
            self.save_results_to_file()

        except Exception as e:
            self.log.e(self.tag, f"Error reporting results: {e}\n{traceback.format_exc()}")

    def save_results_to_file(self):
        """Lưu kết quả chi tiết vào file"""
        try:
            results = []
            for strategy in self.strategies:
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
        """Lấy config của strategy tốt nhất"""
        if self.best_strategy:
            return self.best_strategy.config
        return None
