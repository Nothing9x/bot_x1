import asyncio
import json
import traceback
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional
import itertools
import copy


class TradingStrategy:
    """
    Một strategy cụ thể với các config riêng
    Hỗ trợ cả LONG và SHORT
    """

    def __init__(self, strategy_id: int, config: Dict):
        self.strategy_id = strategy_id
        self.config = config

        # Stats
        self.stats = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'total_pnl_percent': 0.0,
            'win_rate': 0.0,
            'avg_win': 0.0,
            'avg_loss': 0.0,
            'max_drawdown': 0.0,
            'profit_factor': 0.0,
            'sharpe_ratio': 0.0,
        }

        # Tracking positions
        self.active_positions = {}  # {symbol: position_info}
        self.trade_history = []

        # PnL tracking
        self.pnl_history = []
        self.peak_balance = 1000  # Initial balance
        self.current_balance = 1000

    def get_name(self):
        """Tên strategy - Thêm direction"""
        direction = self.config.get('direction', 'LONG')
        breakout_str = "BRK" if self.config.get('require_breakout', False) else ""
        return (f"S{self.strategy_id:03d}_{direction}_"
                f"TP{self.config['take_profit']}%_"
                f"SL{self.config['stop_loss']}%_"
                f"RSI{self.config['rsi_threshold']}_"
                f"Vol{self.config['volume_multiplier']}x_"
                f"Trend{self.config.get('min_trend_strength', 0.5)}_"
                f"{breakout_str}_"
                f"{self.config['timeframe']}")

    def should_enter(self, signal: Dict) -> bool:
        """
        Kiểm tra xem có nên vào lệnh không - DỄ DÀNG HƠN
        Để strategies tự test, không filter quá nhiều
        """

        # Kiểm tra timeframe
        signal_timeframe = signal.get('timeframe', '1m')
        if signal_timeframe != self.config['timeframe']:
            return False

        # Kiểm tra price change
        if self.config['timeframe'] == '1m':
            price_change = signal.get('price_change_1m', 0)
        else:
            price_change = signal.get('price_change_5m', 0)

        if price_change < self.config['price_increase_threshold']:
            return False

        # Kiểm tra volume
        if signal.get('volume_ratio', 0) < self.config['volume_multiplier']:
            return False

        # Kiểm tra RSI - CHO PHÉP BỎ QUA nếu không có RSI
        rsi = signal.get('rsi')
        if rsi is not None:
            if rsi < self.config['rsi_threshold']:
                return False
            # Bỏ check RSI > 85, cho phép trade khi RSI cao

        # Kiểm tra confidence
        if signal.get('confidence', 0) < self.config['min_confidence']:
            return False

        # Các điều kiện dưới đây là OPTIONAL - cho phép bỏ qua

        # Trend strength - cho phép strategy không yêu cầu
        min_trend = self.config.get('min_trend_strength', 0)
        if min_trend > 0:
            trend_strength = signal.get('trend_strength', 0)
            if trend_strength < min_trend:
                return False

        # Breakout - chỉ check nếu required
        if self.config.get('require_breakout', False):
            if not signal.get('is_breakout', False):
                return False

        # Volume consistency - cho phép strategy không yêu cầu
        min_vol_cons = self.config.get('min_volume_consistency', 0)
        if min_vol_cons > 0:
            volume_consistency = signal.get('volume_consistency', 0)
            if volume_consistency < min_vol_cons:
                return False

        return True

    def enter_position(self, symbol: str, entry_price: float, signal: Dict):
        """Vào lệnh - Hỗ trợ cả LONG và SHORT"""
        position_size = self.config['position_size_usdt']
        quantity = position_size / entry_price

        direction = self.config.get('direction', 'LONG')

        if direction == 'LONG':
            # LONG: Mua thấp, bán cao
            take_profit_price = entry_price * (1 + self.config['take_profit'] / 100)
            stop_loss_price = entry_price * (1 - self.config['stop_loss'] / 100)
        else:  # SHORT
            # SHORT: Bán cao, mua thấp
            take_profit_price = entry_price * (1 - self.config['take_profit'] / 100)
            stop_loss_price = entry_price * (1 + self.config['stop_loss'] / 100)

        self.active_positions[symbol] = {
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'entry_time': datetime.now(),
            'quantity': quantity,
            'take_profit': take_profit_price,
            'stop_loss': stop_loss_price,
            'highest_price': entry_price,  # For LONG trailing
            'lowest_price': entry_price,  # For SHORT trailing
            'signal': signal,
        }

    def check_exit(self, symbol: str, current_candle: Dict) -> Optional[Dict]:
        """
        Kiểm tra xem có nên thoát lệnh không
        Hỗ trợ cả LONG và SHORT
        Returns: {'reason': 'TP'/'SL', 'exit_price': float} or None
        """
        if symbol not in self.active_positions:
            return None

        position = self.active_positions[symbol]
        direction = position['direction']
        high = current_candle['high']
        low = current_candle['low']
        close = current_candle['close']

        if direction == 'LONG':
            # Update highest price for trailing stop
            if high > position['highest_price']:
                position['highest_price'] = high

                # Update trailing stop if enabled
                if self.config.get('trailing_stop', False):
                    new_stop = high * (1 - self.config['stop_loss'] / 100)
                    if new_stop > position['stop_loss']:
                        position['stop_loss'] = new_stop

            # Check TP (giá tăng lên)
            if high >= position['take_profit']:
                return {
                    'reason': 'TP',
                    'exit_price': position['take_profit']
                }

            # Check SL (giá giảm xuống)
            if low <= position['stop_loss']:
                return {
                    'reason': 'SL',
                    'exit_price': position['stop_loss']
                }

        else:  # SHORT
            # Update lowest price for trailing stop
            if low < position['lowest_price']:
                position['lowest_price'] = low

                # Update trailing stop if enabled
                if self.config.get('trailing_stop', False):
                    new_stop = low * (1 + self.config['stop_loss'] / 100)
                    if new_stop < position['stop_loss']:
                        position['stop_loss'] = new_stop

            # Check TP (giá giảm xuống)
            if low <= position['take_profit']:
                return {
                    'reason': 'TP',
                    'exit_price': position['take_profit']
                }

            # Check SL (giá tăng lên)
            if high >= position['stop_loss']:
                return {
                    'reason': 'SL',
                    'exit_price': position['stop_loss']
                }

        return None

    def close_position(self, symbol: str, exit_price: float, reason: str):
        """Đóng position và tính PnL - Hỗ trợ cả LONG và SHORT"""
        if symbol not in self.active_positions:
            return

        position = self.active_positions[symbol]
        direction = position['direction']
        entry_price = position['entry_price']

        # Calculate PnL dựa trên direction
        if direction == 'LONG':
            # LONG: Profit khi giá tăng
            pnl_percent = ((exit_price - entry_price) / entry_price) * 100
            pnl_usdt = (exit_price - entry_price) * position['quantity']
        else:  # SHORT
            # SHORT: Profit khi giá giảm
            pnl_percent = ((entry_price - exit_price) / entry_price) * 100
            pnl_usdt = (entry_price - exit_price) * position['quantity']

        # Update balance
        self.current_balance += pnl_usdt

        # Track peak for drawdown
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance

        # Update stats
        self.stats['total_trades'] += 1
        self.stats['total_pnl'] += pnl_usdt
        self.stats['total_pnl_percent'] += pnl_percent

        if pnl_usdt > 0:
            self.stats['winning_trades'] += 1
        else:
            self.stats['losing_trades'] += 1

        # Save trade
        trade_record = {
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'entry_time': position['entry_time'],
            'exit_time': datetime.now(),
            'pnl_usdt': pnl_usdt,
            'pnl_percent': pnl_percent,
            'reason': reason,
        }

        self.trade_history.append(trade_record)
        self.pnl_history.append(pnl_usdt)

        # Remove position
        del self.active_positions[symbol]

    def calculate_final_stats(self):
        """Tính toán các metrics cuối cùng"""
        if self.stats['total_trades'] == 0:
            return

        # Win rate
        self.stats['win_rate'] = (self.stats['winning_trades'] / self.stats['total_trades']) * 100

        # Average win/loss
        wins = [t['pnl_usdt'] for t in self.trade_history if t['pnl_usdt'] > 0]
        losses = [t['pnl_usdt'] for t in self.trade_history if t['pnl_usdt'] < 0]

        self.stats['avg_win'] = sum(wins) / len(wins) if wins else 0
        self.stats['avg_loss'] = sum(losses) / len(losses) if losses else 0

        # Profit factor
        total_wins = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0
        self.stats['profit_factor'] = total_wins / total_losses if total_losses > 0 else 0

        # Max drawdown
        drawdown = ((self.peak_balance - self.current_balance) / self.peak_balance) * 100
        self.stats['max_drawdown'] = drawdown

        # Sharpe ratio (simplified)
        if self.pnl_history:
            import numpy as np
            returns = np.array(self.pnl_history)
            if returns.std() != 0:
                self.stats['sharpe_ratio'] = returns.mean() / returns.std()

    def get_summary(self) -> Dict:
        """Lấy summary của strategy"""
        return {
            'strategy_id': self.strategy_id,
            'name': self.get_name(),
            'config': self.config,
            'stats': self.stats,
            'final_balance': self.current_balance,
            'roi': ((self.current_balance - 1000) / 1000) * 100,
        }
