"""
BotManager - Quản lý nhiều trading bots
- Tạo bots từ best backtest results
- Update config từ backtest mới
- Monitor performance
"""

import asyncio
import traceback
from typing import List, Dict
import json

from x1.bot.database.database_models import Trade, TradeStatusEnum, DatabaseManager, BotConfig, DirectionEnum, \
    BacktestResult, TradeModeEnum
from x1.bot.ai.strategy_manager import StrategyManager
from x1.bot.trading.trading_bot import TradingBot


class BotManager:
    """
    Quản lý nhiều trading bots
    - Tự động tạo bots từ best strategies
    - Update config từ backtest results
    - Monitor và báo cáo performance
    """

    def __init__(self, db_manager: DatabaseManager, strategy_manager: StrategyManager,
                 log, tele_message, exchange=None, chat_id = ""):
        self.tag = "BotManager"
        self.db_manager = db_manager
        self.strategy_manager = strategy_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange
        self.chat_id = chat_id

        # Active bots
        self.bots: List[TradingBot] = []

        # Config
        self.config = {
            'max_bots': 10,  # Số bots tối đa
            'min_trades_for_promotion': 20,  # Trades tối thiểu để promote từ SIM -> REAL
            'min_win_rate_for_promotion': 60,  # Win rate tối thiểu để promote
            'min_profit_factor': 1.5,  # Profit factor tối thiểu
            'auto_update_interval': 3600,  # Update config mỗi 1h
            'auto_create_from_backtest': True,  # Tự động tạo bot từ backtest
        }

    async def initialize(self):
        """Khởi tạo BotManager"""
        try:
            self.log.i(self.tag, "🤖 Initializing BotManager...")

            # Load existing bots from database
            await self.load_bots_from_db()

            # Start monitoring tasks
            asyncio.create_task(self.auto_update_configs())
            asyncio.create_task(self.monitor_performance())
            asyncio.create_task(self.check_promotions())

            self.log.i(self.tag, f"✅ BotManager initialized with {len(self.bots)} bots")

        except Exception as e:
            self.log.e(self.tag, f"Error initializing: {e}\n{traceback.format_exc()}")

    async def load_bots_from_db(self):
        """Load bots từ database"""
        try:
            session = self.db_manager.get_session()

            # Get all active bot configs
            bot_configs = session.query(BotConfig).filter_by(is_active=True).all()

            for bot_config in bot_configs:
                bot = TradingBot(
                    bot_config=bot_config,
                    db_manager=self.db_manager,
                    log=self.log,
                    tele_message=self.tele_message,
                    exchange=self.exchange
                )
                self.bots.append(bot)

            session.close()

            self.log.i(self.tag, f"Loaded {len(self.bots)} bots from database")

        except Exception as e:
            self.log.e(self.tag, f"Error loading bots: {e}")

    async def create_bots_from_backtest(self, top_n: int = 5, mode: TradeModeEnum = TradeModeEnum.SIMULATED):
        """
        Tạo bots từ top N strategies của backtest
        Mặc định là SIMULATED mode
        """
        try:
            self.log.i(self.tag, f"📊 Creating {top_n} bots from top backtest results...")

            # Get top strategies
            self.strategy_manager.calculate_rankings()
            top_strategies = self.strategy_manager.top_strategies[:top_n]

            if not top_strategies:
                self.log.w(self.tag, "No strategies available from backtest")
                return

            session = self.db_manager.get_session()
            created_count = 0

            for rank, strategy in enumerate(top_strategies, 1):
                config = strategy.config
                stats = strategy.stats

                # Create bot name
                bot_name = f"Bot-{config['direction']}-Top{rank}"

                # Check if bot already exists
                existing = session.query(BotConfig).filter_by(name=bot_name).first()
                if existing:
                    self.log.d(self.tag, f"Bot {bot_name} already exists, skipping")
                    continue

                # Create bot config
                bot_config = BotConfig(
                    name=bot_name,
                    direction=DirectionEnum.LONG if config['direction'] == 'LONG' else DirectionEnum.SHORT,
                    take_profit=config['take_profit'],
                    stop_loss=config['stop_loss'],
                    position_size_usdt=config['position_size_usdt'],
                    price_increase_threshold=config['price_increase_threshold'],
                    volume_multiplier=config['volume_multiplier'],
                    rsi_threshold=config['rsi_threshold'],
                    min_confidence=config['min_confidence'],
                    trailing_stop=config.get('trailing_stop', False),
                    min_trend_strength=config.get('min_trend_strength', 0.0),
                    require_breakout=config.get('require_breakout', False),
                    min_volume_consistency=config.get('min_volume_consistency', 0.0),
                    timeframe=config.get('timeframe', '1m'),
                    trade_mode=mode,
                    is_active=True,
                    source_strategy_id=strategy.strategy_id
                )

                session.add(bot_config)
                session.flush()  # Get ID

                # Save backtest result
                backtest_result = BacktestResult(
                    strategy_id=strategy.strategy_id,
                    strategy_name=strategy.get_name(),
                    config_json=json.dumps(config),
                    total_trades=stats['total_trades'],
                    winning_trades=stats['winning_trades'],
                    losing_trades=stats['losing_trades'],
                    win_rate=stats['win_rate'],
                    total_pnl=stats['total_pnl'],
                    roi=(stats['total_pnl'] / 1000) * 100,
                    profit_factor=stats.get('profit_factor', 0),
                    sharpe_ratio=stats.get('sharpe_ratio', 0),
                    max_drawdown=stats.get('max_drawdown', 0),
                    avg_win=stats.get('avg_win', 0),
                    avg_loss=stats.get('avg_loss', 0),
                    rank=rank
                )

                session.add(backtest_result)

                # Create bot instance
                bot = TradingBot(
                    bot_config=bot_config,
                    db_manager=self.db_manager,
                    log=self.log,
                    tele_message=self.tele_message,
                    exchange=self.exchange
                )

                self.bots.append(bot)
                created_count += 1

                self.log.i(self.tag,
                           f"✅ Created {bot_name}: {config['direction']} | "
                           f"TP={config['take_profit']}% SL={config['stop_loss']}% | "
                           f"Backtest: {stats['total_trades']} trades, {stats['win_rate']:.1f}% WR"
                           )

            session.commit()
            session.close()

            # Send notification
            await self.tele_message.send_message(
                f"🤖 Created {created_count} new bots from backtest results\n"
                f"Mode: {mode.value}\n"
                f"Total active bots: {len(self.bots)}", self.chat_id
            )

            self.log.i(self.tag, f"✅ Created {created_count} bots from backtest")

        except Exception as e:
            self.log.e(self.tag, f"Error creating bots from backtest: {e}\n{traceback.format_exc()}")

    async def on_signal(self, signal: Dict):
        """Broadcast signal đến tất cả bots"""
        try:
            tasks = []
            for bot in self.bots:
                if bot.bot_config.is_active:
                    tasks.append(bot.on_signal(signal))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self.log.e(self.tag, f"Error broadcasting signal: {e}")

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """Broadcast candle update đến tất cả bots"""
        try:
            tasks = []
            for bot in self.bots:
                if bot.bot_config.is_active:
                    tasks.append(bot.on_candle_update(symbol, interval, candle_data))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self.log.e(self.tag, f"Error broadcasting candle: {e}")

    async def auto_update_configs(self):
        """Tự động update config từ backtest results mới nhất"""
        while True:
            try:
                await asyncio.sleep(self.config['auto_update_interval'])

                if not self.config['auto_create_from_backtest']:
                    continue

                self.log.i(self.tag, "🔄 Checking for backtest updates...")

                # Get latest backtest results
                self.strategy_manager.calculate_rankings()

                # Update existing bots or create new ones
                await self.update_bot_configs()

            except Exception as e:
                self.log.e(self.tag, f"Error in auto update: {e}")

    async def update_bot_configs(self):
        """Update bot configs từ backtest results mới"""
        try:
            session = self.db_manager.get_session()

            # Get top strategies by direction
            long_strategies = [s for s in self.strategy_manager.top_strategies
                               if s.config['direction'] == 'LONG'][:3]
            short_strategies = [s for s in self.strategy_manager.top_strategies
                                if s.config['direction'] == 'SHORT'][:3]

            update_count = 0

            # Update LONG bots
            for rank, strategy in enumerate(long_strategies, 1):
                bot_name = f"Bot-LONG-Top{rank}"
                bot_config = session.query(BotConfig).filter_by(name=bot_name).first()

                if bot_config:
                    # Update config
                    config = strategy.config
                    bot_config.take_profit = config['take_profit']
                    bot_config.stop_loss = config['stop_loss']
                    bot_config.volume_multiplier = config['volume_multiplier']
                    bot_config.min_confidence = config['min_confidence']
                    # ... update other fields

                    update_count += 1

            # Update SHORT bots
            for rank, strategy in enumerate(short_strategies, 1):
                bot_name = f"Bot-SHORT-Top{rank}"
                bot_config = session.query(BotConfig).filter_by(name=bot_name).first()

                if bot_config:
                    config = strategy.config
                    bot_config.take_profit = config['take_profit']
                    bot_config.stop_loss = config['stop_loss']
                    bot_config.volume_multiplier = config['volume_multiplier']
                    bot_config.min_confidence = config['min_confidence']

                    update_count += 1

            if update_count > 0:
                session.commit()
                self.log.i(self.tag, f"✅ Updated {update_count} bot configs from backtest")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error updating configs: {e}")

    async def check_promotions(self):
        """
        Check xem bot nào đủ điều kiện promote từ SIMULATED -> REAL
        """
        while True:
            try:
                await asyncio.sleep(1800)  # Check mỗi 30 phút

                session = self.db_manager.get_session()

                # Get SIMULATED bots
                sim_configs = session.query(BotConfig).filter_by(
                    trade_mode=TradeModeEnum.SIMULATED,
                    is_active=True
                ).all()

                for bot_config in sim_configs:
                    # Check criteria
                    if (bot_config.total_trades >= self.config['min_trades_for_promotion'] and
                            bot_config.win_rate >= self.config['min_win_rate_for_promotion']):

                        # Calculate profit factor
                        from sqlalchemy import and_
                        trades = session.query(Trade).filter(
                            and_(
                                Trade.bot_config_id == bot_config.id,
                                Trade.status == TradeStatusEnum.CLOSED
                            )
                        ).all()

                        wins = sum(t.pnl_usdt for t in trades if t.pnl_usdt > 0)
                        losses = abs(sum(t.pnl_usdt for t in trades if t.pnl_usdt < 0))
                        profit_factor = wins / losses if losses > 0 else 0

                        if profit_factor >= self.config['min_profit_factor']:
                            # Promote to REAL!
                            await self.promote_bot_to_real(bot_config, session)

                session.close()

            except Exception as e:
                self.log.e(self.tag, f"Error checking promotions: {e}")

    async def promote_bot_to_real(self, bot_config: BotConfig, session):
        """Promote bot từ SIMULATED sang REAL mode"""
        try:
            old_mode = bot_config.trade_mode.value
            bot_config.trade_mode = TradeModeEnum.REAL
            session.commit()

            self.log.i(self.tag,
                       f"🎉 PROMOTED {bot_config.name} to REAL mode! | "
                       f"Stats: {bot_config.total_trades} trades, "
                       f"{bot_config.win_rate:.1f}% WR, "
                       f"${bot_config.total_pnl:.2f} PnL"
                       )

            await self.tele_message.send_message(
                f"🎉 BOT PROMOTED TO REAL MODE\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Name: {bot_config.name}\n"
                f"Direction: {bot_config.direction.value}\n"
                f"Stats: {bot_config.total_trades} trades\n"
                f"Win Rate: {bot_config.win_rate:.1f}%\n"
                f"Total PnL: ${bot_config.total_pnl:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ This bot will now trade with REAL money!", self.chat_id
            )

        except Exception as e:
            self.log.e(self.tag, f"Error promoting bot: {e}")

    async def monitor_performance(self):
        """Monitor performance của tất cả bots"""
        while True:
            try:
                await asyncio.sleep(3600)  # Mỗi 1 giờ

                session = self.db_manager.get_session()

                # Get all active bots
                bot_configs = session.query(BotConfig).filter_by(is_active=True).all()

                if not bot_configs:
                    session.close()
                    continue

                # Build report
                message = "📊 BOTS PERFORMANCE REPORT\n"
                message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

                real_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.REAL]
                sim_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.SIMULATED]

                if real_bots:
                    message += f"🔴 REAL BOTS ({len(real_bots)}):\n"
                    for bot in real_bots:
                        message += (
                            f"  {bot.name}: "
                            f"{bot.total_trades} trades | "
                            f"{bot.win_rate:.1f}% WR | "
                            f"${bot.total_pnl:.2f}\n"
                        )
                    message += "\n"

                if sim_bots:
                    message += f"🔵 SIMULATED BOTS ({len(sim_bots)}):\n"
                    for bot in sim_bots[:5]:  # Top 5
                        message += (
                            f"  {bot.name}: "
                            f"{bot.total_trades} trades | "
                            f"{bot.win_rate:.1f}% WR | "
                            f"${bot.total_pnl:.2f}\n"
                        )

                self.log.i(self.tag, message)

                session.close()

            except Exception as e:
                self.log.e(self.tag, f"Error monitoring performance: {e}")

    def get_stats(self) -> Dict:
        """Get overall stats của tất cả bots"""
        session = self.db_manager.get_session()

        bot_configs = session.query(BotConfig).filter_by(is_active=True).all()

        total_trades = sum(b.total_trades for b in bot_configs)
        total_pnl = sum(b.total_pnl for b in bot_configs)

        real_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.REAL]
        sim_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.SIMULATED]

        session.close()

        return {
            'total_bots': len(bot_configs),
            'real_bots': len(real_bots),
            'simulated_bots': len(sim_bots),
            'total_trades': total_trades,
            'total_pnl': total_pnl,
        }