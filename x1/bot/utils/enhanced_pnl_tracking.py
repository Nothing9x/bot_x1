"""
Enhanced PnL Tracking System
- Realized PnL (đã close)
- Unrealized PnL (chưa close - tính theo giá hiện tại)
- Total PnL = Realized + Unrealized
- Hourly report cho Production Bots (REAL + SIM)
"""

import asyncio
from datetime import datetime
from typing import Dict, List
from collections import defaultdict

from x1.bot.database.database_models import BotConfig, Trade, TradeStatusEnum, TradeModeEnum
from x1.bot.trading.trading_bot import TradingBot


class PnLTracker:
    """
    Track PnL cho strategies và bots
    - Realized: Lệnh đã close
    - Unrealized: Lệnh đang mở (tính theo giá hiện tại)
    """
    
    def __init__(self):
        # Cache giá hiện tại của các symbols
        self.current_prices = {}  # {symbol: price}
        
    def update_price(self, symbol: str, price: float):
        """Update giá hiện tại"""
        self.current_prices[symbol] = price
    
    def calculate_unrealized_pnl_for_trade(self, trade: Trade) -> float:
        """
        Tính unrealized PnL cho 1 trade đang mở
        """
        if trade.status != TradeStatusEnum.OPEN:
            return 0.0
        
        symbol = trade.symbol
        if symbol not in self.current_prices:
            return 0.0
        
        current_price = self.current_prices[symbol]
        entry_price = trade.entry_price
        quantity = trade.quantity
        
        # Tính PnL dựa trên direction
        if trade.direction.value == 'LONG':
            unrealized_pnl = (current_price - entry_price) * quantity
        else:  # SHORT
            unrealized_pnl = (entry_price - current_price) * quantity
        
        return unrealized_pnl
    
    def calculate_strategy_pnl(self, strategy) -> Dict:
        """
        Tính PnL cho strategy (backtest)
        Returns: {
            'realized_pnl': float,
            'unrealized_pnl': float,
            'total_pnl': float,
            'unrealized_positions': int
        }
        """
        # Realized PnL từ trade history
        realized_pnl = strategy.stats.get('total_pnl', 0.0)
        
        # Unrealized PnL từ active positions
        unrealized_pnl = 0.0
        unrealized_count = 0
        
        for symbol, position in strategy.active_positions.items():
            if symbol in self.current_prices:
                current_price = self.current_prices[symbol]
                entry_price = position['entry_price']
                quantity = position['quantity']
                direction = position['direction']
                
                if direction == 'LONG':
                    pnl = (current_price - entry_price) * quantity
                else:  # SHORT
                    pnl = (entry_price - current_price) * quantity
                
                unrealized_pnl += pnl
                unrealized_count += 1
        
        return {
            'realized_pnl': realized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'total_pnl': realized_pnl + unrealized_pnl,
            'unrealized_positions': unrealized_count
        }
    
    def calculate_bot_pnl(self, bot_config: BotConfig, session) -> Dict:
        """
        Tính PnL cho bot (production)
        Returns: {
            'realized_pnl': float,
            'realized_trades': int,
            'unrealized_pnl': float,
            'unrealized_trades': int,
            'total_pnl': float,
            'win_rate': float,
            'winning_trades': int,
            'losing_trades': int
        }
        """
        # Get all trades
        all_trades = session.query(Trade).filter_by(
            bot_config_id=bot_config.id
        ).all()
        
        # Realized PnL (closed trades)
        closed_trades = [t for t in all_trades if t.status == TradeStatusEnum.CLOSED]
        realized_pnl = sum(t.pnl_usdt for t in closed_trades if t.pnl_usdt)
        
        winning_trades = len([t for t in closed_trades if t.pnl_usdt and t.pnl_usdt > 0])
        losing_trades = len([t for t in closed_trades if t.pnl_usdt and t.pnl_usdt <= 0])
        
        win_rate = (winning_trades / len(closed_trades) * 100) if closed_trades else 0.0
        
        # Unrealized PnL (open trades)
        open_trades = [t for t in all_trades if t.status == TradeStatusEnum.OPEN]
        unrealized_pnl = 0.0
        
        for trade in open_trades:
            unrealized_pnl += self.calculate_unrealized_pnl_for_trade(trade)
        
        return {
            'realized_pnl': realized_pnl,
            'realized_trades': len(closed_trades),
            'unrealized_pnl': unrealized_pnl,
            'unrealized_trades': len(open_trades),
            'total_pnl': realized_pnl + unrealized_pnl,
            'win_rate': win_rate,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades
        }


class EnhancedBotManager:
    """
    BotManager với PnL tracking và hourly reports
    """
    
    def __init__(self, bot_manager, db_manager, log, tele_message, chat_id):
        self.bot_manager = bot_manager
        self.db_manager = db_manager
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id
        self.tag = "EnhancedBotManager"
        
        # PnL Tracker
        self.pnl_tracker = PnLTracker()
        
        # Start hourly report
        asyncio.create_task(self.hourly_bot_report())
    
    def update_price(self, symbol: str, price: float):
        """Update giá hiện tại"""
        self.pnl_tracker.update_price(symbol, price)
    
    async def hourly_bot_report(self):
        """
        Report hàng tiếng cho PRODUCTION BOTS (REAL + SIM)
        """
        self.log.i(self.tag, "⏰ Started hourly bot report task")
        
        # Đợi 1 tiếng trước khi report lần đầu
        await asyncio.sleep(3600)
        
        while True:
            try:
                await self._generate_bot_report()
                await asyncio.sleep(3600)  # Mỗi 1 tiếng
                
            except Exception as e:
                self.log.e(self.tag, f"Error in hourly report: {e}")
                await asyncio.sleep(3600)
    
    async def _generate_bot_report(self):
        """
        Tạo report chi tiết cho production bots
        """
        try:
            session = self.db_manager.get_session()
            
            # Get all active bots
            bot_configs = session.query(BotConfig).filter_by(is_active=True).all()
            
            if not bot_configs:
                session.close()
                return
            
            # Separate REAL and SIM
            real_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.REAL]
            sim_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.SIMULATED]
            
            # Build report
            message = "📊 HOURLY BOT PERFORMANCE REPORT\n"
            message += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            
            # REAL BOTS
            if real_bots:
                message += "🔴 REAL TRADING BOTS:\n"
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                
                total_real_realized = 0
                total_real_unrealized = 0
                
                for bot in real_bots:
                    pnl_data = self.pnl_tracker.calculate_bot_pnl(bot, session)
                    
                    message += f"🤖 {bot.name}\n"
                    message += f"  📊 Realized:\n"
                    message += f"    • PnL: ${pnl_data['realized_pnl']:+.2f}\n"
                    message += f"    • Trades: {pnl_data['realized_trades']} "
                    message += f"({pnl_data['winning_trades']}W/{pnl_data['losing_trades']}L)\n"
                    message += f"    • Win Rate: {pnl_data['win_rate']:.1f}%\n"
                    
                    message += f"  💼 Unrealized:\n"
                    message += f"    • PnL: ${pnl_data['unrealized_pnl']:+.2f}\n"
                    message += f"    • Open Trades: {pnl_data['unrealized_trades']}\n"
                    
                    message += f"  💰 Total PnL: ${pnl_data['total_pnl']:+.2f}\n\n"
                    
                    total_real_realized += pnl_data['realized_pnl']
                    total_real_unrealized += pnl_data['unrealized_pnl']
                
                message += f"📈 REAL BOTS SUMMARY:\n"
                message += f"  • Realized: ${total_real_realized:+.2f}\n"
                message += f"  • Unrealized: ${total_real_unrealized:+.2f}\n"
                message += f"  • Total: ${(total_real_realized + total_real_unrealized):+.2f}\n\n"
            
            # SIMULATED BOTS
            if sim_bots:
                message += "🔵 SIMULATED BOTS:\n"
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                
                total_sim_realized = 0
                total_sim_unrealized = 0
                
                for bot in sim_bots:
                    pnl_data = self.pnl_tracker.calculate_bot_pnl(bot, session)
                    
                    message += f"🤖 {bot.name}\n"
                    message += f"  📊 Realized: ${pnl_data['realized_pnl']:+.2f} "
                    message += f"({pnl_data['realized_trades']} trades, {pnl_data['win_rate']:.0f}% WR)\n"
                    message += f"  💼 Unrealized: ${pnl_data['unrealized_pnl']:+.2f} "
                    message += f"({pnl_data['unrealized_trades']} open)\n"
                    message += f"  💰 Total: ${pnl_data['total_pnl']:+.2f}\n\n"
                    
                    total_sim_realized += pnl_data['realized_pnl']
                    total_sim_unrealized += pnl_data['unrealized_pnl']
                
                message += f"📈 SIM BOTS SUMMARY:\n"
                message += f"  • Realized: ${total_sim_realized:+.2f}\n"
                message += f"  • Unrealized: ${total_sim_unrealized:+.2f}\n"
                message += f"  • Total: ${(total_sim_realized + total_sim_unrealized):+.2f}\n\n"
            
            # Overall summary
            if real_bots or sim_bots:
                grand_total_realized = (total_real_realized if real_bots else 0) + (total_sim_realized if sim_bots else 0)
                grand_total_unrealized = (total_real_unrealized if real_bots else 0) + (total_sim_unrealized if sim_bots else 0)
                
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                message += "💎 GRAND TOTAL:\n"
                message += f"  • Realized PnL: ${grand_total_realized:+.2f}\n"
                message += f"  • Unrealized PnL: ${grand_total_unrealized:+.2f}\n"
                message += f"  • Total PnL: ${(grand_total_realized + grand_total_unrealized):+.2f}\n"
            
            # Send report
            self.log.i(self.tag, message)
            await self.tele_message.send_message(message, self.chat_id)
            
            session.close()
            
        except Exception as e:
            self.log.e(self.tag, f"Error generating bot report: {e}")


class EnhancedStrategyManager:
    """
    StrategyManager với unrealized PnL tracking
    """
    
    def __init__(self, strategy_manager):
        self.strategy_manager = strategy_manager
        self.pnl_tracker = PnLTracker()
        self.tag = "EnhancedStrategyManager"
    
    def update_price(self, symbol: str, price: float):
        """Update giá hiện tại"""
        self.pnl_tracker.update_price(symbol, price)
    
    def calculate_rankings_with_unrealized(self):
        """
        Calculate rankings với UNREALIZED PnL
        """
        strategies_with_trades = []
        
        for strategy in self.strategy_manager.strategies:
            if strategy.stats['total_trades'] > 0 or len(strategy.active_positions) > 0:
                strategy.calculate_final_stats()
                
                # Calculate unrealized PnL
                pnl_data = self.pnl_tracker.calculate_strategy_pnl(strategy)
                
                # Store unrealized data in strategy
                strategy.unrealized_pnl = pnl_data['unrealized_pnl']
                strategy.total_pnl_with_unrealized = pnl_data['total_pnl']
                strategy.unrealized_positions = pnl_data['unrealized_positions']
                
                strategies_with_trades.append(strategy)
        
        if not strategies_with_trades:
            return
        
        # Sort by TOTAL PnL (realized + unrealized)
        all_sorted = sorted(
            strategies_with_trades, 
            key=lambda s: s.total_pnl_with_unrealized, 
            reverse=True
        )
        
        self.strategy_manager.top_strategies = all_sorted[:10]
        self.strategy_manager.best_strategy = all_sorted[0] if all_sorted else None
        
        # Separate LONG and SHORT
        long_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'LONG']
        short_strategies = [s for s in strategies_with_trades if s.config['direction'] == 'SHORT']
        
        sorted_long = sorted(long_strategies, key=lambda s: s.total_pnl_with_unrealized, reverse=True)
        sorted_short = sorted(short_strategies, key=lambda s: s.total_pnl_with_unrealized, reverse=True)
        
        self.strategy_manager.best_long = sorted_long[0] if sorted_long else None
        self.strategy_manager.best_short = sorted_short[0] if sorted_short else None
    
    def build_detailed_report_with_unrealized(self) -> str:
        """
        Build report với unrealized PnL
        """
        message = "📊 BACKTEST RESULTS (with Unrealized PnL)\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # Best overall
        if self.strategy_manager.best_strategy:
            s = self.strategy_manager.best_strategy
            stats = s.stats
            config = s.config
            
            message += f"🏆 BEST OVERALL - Strategy #{s.strategy_id} ({config['direction']})\n"
            message += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"📊 Realized (Closed):\n"
            message += f"  • PnL: ${stats['total_pnl']:.2f}\n"
            message += f"  • Trades: {stats['total_trades']} ({stats['winning_trades']}W/{stats['losing_trades']}L)\n"
            message += f"  • Win Rate: {stats['win_rate']:.1f}%\n"
            message += f"  • Profit Factor: {stats.get('profit_factor', 0):.2f}\n"
            
            message += f"\n💼 Unrealized (Open):\n"
            message += f"  • PnL: ${s.unrealized_pnl:+.2f}\n"
            message += f"  • Open Positions: {s.unrealized_positions}\n"
            
            message += f"\n💰 Total PnL: ${s.total_pnl_with_unrealized:+.2f}\n"
            
            message += f"\n⚙️ Config:\n"
            message += f"  • TP: {config['take_profit']}% | SL: {config['stop_loss']}%\n"
            message += f"  • Vol: >{config['volume_multiplier']}x | RSI: >{config['rsi_threshold']}\n"
            message += f"  • Confidence: >{config['min_confidence']}%\n\n"
        
        # Top 10
        message += f"📊 TOP 10 STRATEGIES:\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for rank, strategy in enumerate(self.strategy_manager.top_strategies, 1):
            stats = strategy.stats
            config = strategy.config
            
            message += (
                f"#{rank}. S{strategy.strategy_id} {config['direction']}: "
                f"Realized=${stats['total_pnl']:.0f} Unrealized=${strategy.unrealized_pnl:+.0f} "
                f"Total=${strategy.total_pnl_with_unrealized:+.0f} "
                f"WR={stats['win_rate']:.0f}% "
                f"[TP{config['take_profit']}% SL{config['stop_loss']}%]\n"
            )
        
        return message


# Integration helper
def integrate_pnl_tracking(bot_manager, strategy_manager, db_manager, log, tele_message, chat_id):
    """
    Helper function để integrate PnL tracking vào existing managers
    
    Usage:
        enhanced_bot_mgr, enhanced_strat_mgr = integrate_pnl_tracking(
            bot_manager, strategy_manager, db_manager, log, tele_message, chat_id
        )
        
        # Update prices khi có candle mới
        enhanced_bot_mgr.update_price(symbol, price)
        enhanced_strat_mgr.update_price(symbol, price)
    """
    
    enhanced_bot_mgr = EnhancedBotManager(
        bot_manager, db_manager, log, tele_message, chat_id
    )
    
    enhanced_strat_mgr = EnhancedStrategyManager(strategy_manager)
    
    return enhanced_bot_mgr, enhanced_strat_mgr