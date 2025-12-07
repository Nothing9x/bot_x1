"""
BotManager - Quản lý nhiều trading bots
- Tạo bots từ best backtest results
- Update config từ backtest mới
- Monitor performance
- AUTO CREATE simulation bots sau 1 tiếng
- FILTER strategies theo custom rules
"""

import asyncio
import traceback
from typing import List, Dict
import json
from datetime import datetime, timedelta

from x1.bot.database.database_models import Trade, TradeStatusEnum, DatabaseManager, BotConfig, DirectionEnum, \
    BacktestResult, TradeModeEnum
from x1.bot.ai.strategy_manager import StrategyManager
from x1.bot.trading.trading_bot import TradingBot


class SimulationBotConfig:
    """Config cho việc tự động tạo simulation bots"""

    def __init__(self):
        # Timing
        self.auto_create_after_minutes = 60  # Tạo bot sau 60 phút
        self.update_interval_minutes = 60    # Update config mỗi 60 phút

        # Strategy Filters
        self.max_stop_loss = 2.0            # SL không quá 2%
        self.max_trades_per_hour = 20       # Không quá 10 lệnh/giờ
        self.min_win_rate = 50.0            # Win rate tối thiểu 50%
        self.min_profit_factor = 1.2        # Profit factor tối thiểu 1.2
        self.min_total_trades = 10          # Số lệnh tối thiểu trong backtest

        # Bot Creation
        self.num_long_bots = 5              # Số bot LONG
        self.num_short_bots = 10             # Số bot SHORT
        self.starting_capital = 100.0       # Vốn ban đầu cho mỗi bot (USDT)

        # Promotion Rules
        self.min_trades_for_promotion = 50  # Số lệnh tối thiểu để promote
        self.min_win_rate_for_promotion = 60.0
        self.min_profit_for_promotion = 20.0  # Profit % tối thiểu


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

        # Simulation bot config
        self.sim_config = SimulationBotConfig()

        # Tracking
        self.backtest_start_time = datetime.now()
        self.last_update_time = None
        self.auto_created = False  # Flag để track đã tạo bot chưa

    async def initialize(self):
        """Khởi tạo BotManager"""
        try:
            self.log.i(self.tag, "🤖 Initializing BotManager...")

            # Load existing bots from database
            await self.load_bots_from_db()

            # Start monitoring tasks
            asyncio.create_task(self.auto_create_simulation_bots())
            asyncio.create_task(self.auto_update_configs())
            asyncio.create_task(self.monitor_performance())
            asyncio.create_task(self.check_promotions())

            self.log.i(self.tag,
                      f"✅ BotManager initialized with {len(self.bots)} bots\n"
                      f"   Auto-create simulation after: {self.sim_config.auto_create_after_minutes}min")

        except Exception as e:
            self.log.e(self.tag, f"Error initializing: {e}\n{traceback.format_exc()}")

    async def auto_create_simulation_bots(self):
        """
        Task tự động tạo simulation bots sau X phút
        """
        try:
            # Đợi X phút
            wait_seconds = self.sim_config.auto_create_after_minutes * 60
            self.log.i(self.tag,
                      f"⏳ Will auto-create simulation bots in {self.sim_config.auto_create_after_minutes} minutes...")

            await asyncio.sleep(wait_seconds)

            # Tạo bots
            if not self.auto_created:
                self.log.i(self.tag, "🎯 Auto-creating simulation bots from filtered backtest results...")

                await self.create_filtered_simulation_bots()
                self.auto_created = True

                # Gửi thông báo
                await self.tele_message.send_message(
                    f"🤖 AUTO-CREATED SIMULATION BOTS!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Filters applied:\n"
                    f"  • Max SL: {self.sim_config.max_stop_loss}%\n"
                    f"  • Max trades/hour: {self.sim_config.max_trades_per_hour}\n"
                    f"  • Min win rate: {self.sim_config.min_win_rate}%\n"
                    f"  • Min profit factor: {self.sim_config.min_profit_factor}\n"
                    f"  • Bots created: {self.sim_config.num_long_bots} LONG + {self.sim_config.num_short_bots} SHORT",
                    self.chat_id
                )

        except Exception as e:
            self.log.e(self.tag, f"Error in auto_create_simulation_bots: {e}")

    async def create_filtered_simulation_bots(self):
        """
        Tạo simulation bots với filters
        """
        try:
            # Calculate rankings
            self.strategy_manager.calculate_rankings()

            # Filter strategies
            filtered_strategies = self._filter_strategies(
                self.strategy_manager.top_strategies
            )

            if not filtered_strategies:
                self.log.w(self.tag, "⚠️ No strategies passed the filters!")
                return

            self.log.i(self.tag,
                      f"✅ {len(filtered_strategies)} strategies passed filters out of "
                      f"{len(self.strategy_manager.top_strategies)} total")

            # Tách LONG và SHORT
            long_strategies = [s for s in filtered_strategies
                             if s.config['direction'] == 'LONG']
            short_strategies = [s for s in filtered_strategies
                              if s.config['direction'] == 'SHORT']

            session = self.db_manager.get_session()
            created_count = 0

            # Tạo LONG bots
            for i in range(min(self.sim_config.num_long_bots, len(long_strategies))):
                strategy = long_strategies[i]
                bot_name = f"SimBot-LONG-{i+1}"

                if await self._create_simulation_bot(session, strategy, bot_name, DirectionEnum.LONG):
                    created_count += 1

            # Tạo SHORT bots
            for i in range(min(self.sim_config.num_short_bots, len(short_strategies))):
                strategy = short_strategies[i]
                bot_name = f"SimBot-SHORT-{i+1}"

                if await self._create_simulation_bot(session, strategy, bot_name, DirectionEnum.SHORT):
                    created_count += 1

            session.commit()
            session.close()

            self.log.i(self.tag, f"✅ Created {created_count} simulation bots")

            # Reload bots
            await self.load_bots_from_db()

        except Exception as e:
            self.log.e(self.tag, f"Error creating filtered simulation bots: {e}")

    def _filter_strategies(self, strategies: list) -> list:
        """
        Filter strategies theo rules
        """
        filtered = []

        for strategy in strategies:
            config = strategy.config
            stats = strategy.stats

            # Check SL
            if config['stop_loss'] > self.sim_config.max_stop_loss:
                continue

            # Check trades per hour
            trades_per_hour = self._calculate_trades_per_hour(stats)
            if trades_per_hour > self.sim_config.max_trades_per_hour:
                continue

            # Check win rate
            if stats['win_rate'] < self.sim_config.min_win_rate:
                continue

            # Check profit factor
            if stats.get('profit_factor', 0) < self.sim_config.min_profit_factor:
                continue

            # Check minimum trades
            if stats['total_trades'] < self.sim_config.min_total_trades:
                continue

            filtered.append(strategy)

        return filtered

    def _calculate_trades_per_hour(self, stats: dict) -> float:
        """
        Tính số lệnh trung bình mỗi giờ từ stats
        """
        # Giả sử backtest chạy trong 24h
        # Bạn có thể adjust logic này dựa trên thực tế
        total_trades = stats.get('total_trades', 0)
        hours = 24  # hoặc lấy từ strategy.backtest_duration nếu có

        return total_trades / hours if hours > 0 else 0

    async def _create_simulation_bot(self, session, strategy, bot_name: str,
                                     direction: DirectionEnum) -> bool:
        """
        Tạo một simulation bot
        """
        try:
            # Check if exists
            existing = session.query(BotConfig).filter_by(name=bot_name).first()
            if existing:
                self.log.d(self.tag, f"Bot {bot_name} already exists")
                return False

            config = strategy.config
            stats = strategy.stats

            # Create bot config
            bot_config = BotConfig(
                name=bot_name,
                direction=direction,
                take_profit=config['take_profit'],
                stop_loss=config['stop_loss'],
                position_size_usdt=self.sim_config.starting_capital,
                price_increase_threshold=config['price_increase_threshold'],
                volume_multiplier=config['volume_multiplier'],
                rsi_threshold=config['rsi_threshold'],
                min_confidence=config['min_confidence'],
                trailing_stop=config.get('trailing_stop', False),
                min_trend_strength=config.get('min_trend_strength', 0.0),
                require_breakout=config.get('require_breakout', False),
                min_volume_consistency=config.get('min_volume_consistency', 0.0),
                timeframe=config.get('timeframe', '1m'),
                trade_mode=TradeModeEnum.SIMULATED,
                is_active=True,
                source_strategy_id=strategy.strategy_id
            )

            session.add(bot_config)
            session.flush()

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
                roi=(stats['total_pnl'] / self.sim_config.starting_capital) * 100,
                profit_factor=stats.get('profit_factor', 0),
                sharpe_ratio=stats.get('sharpe_ratio', 0),
                max_drawdown=stats.get('max_drawdown', 0),
                avg_win=stats.get('avg_win', 0),
                avg_loss=stats.get('avg_loss', 0),
            )

            session.add(backtest_result)

            self.log.i(self.tag,
                      f"✅ Created {bot_name}: WR={stats['win_rate']:.1f}% "
                      f"PF={stats.get('profit_factor', 0):.2f} "
                      f"SL={config['stop_loss']}%")

            return True

        except Exception as e:
            self.log.e(self.tag, f"Error creating bot {bot_name}: {e}")
            return False

    async def load_bots_from_db(self):
        """Load bots từ database"""
        try:
            session = self.db_manager.get_session()

            # Clear existing bots
            self.bots.clear()

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
                # Đợi interval
                wait_seconds = self.sim_config.update_interval_minutes * 60
                await asyncio.sleep(wait_seconds)

                # Chỉ update nếu đã tạo bot
                if not self.auto_created:
                    continue

                self.log.i(self.tag, "🔄 Auto-updating simulation bot configs...")

                # Get latest filtered strategies
                self.strategy_manager.calculate_rankings()
                filtered_strategies = self._filter_strategies(
                    self.strategy_manager.top_strategies
                )

                if not filtered_strategies:
                    self.log.w(self.tag, "No strategies passed filters for update")
                    continue

                # Update configs
                await self._update_simulation_bot_configs(filtered_strategies)

                self.last_update_time = datetime.now()

                # Send notification
                await self.tele_message.send_message(
                    f"🔄 Updated simulation bot configs\n"
                    f"Time: {self.last_update_time.strftime('%H:%M:%S')}\n"
                    f"Strategies available: {len(filtered_strategies)}",
                    self.chat_id
                )

            except Exception as e:
                self.log.e(self.tag, f"Error in auto update: {e}")

    async def _update_simulation_bot_configs(self, filtered_strategies: list):
        """
        Update config cho simulation bots
        """
        try:
            session = self.db_manager.get_session()

            # Tách LONG và SHORT
            long_strategies = [s for s in filtered_strategies
                             if s.config['direction'] == 'LONG']
            short_strategies = [s for s in filtered_strategies
                              if s.config['direction'] == 'SHORT']

            update_count = 0

            # Update LONG bots
            for i in range(self.sim_config.num_long_bots):
                bot_name = f"SimBot-LONG-{i+1}"

                if i < len(long_strategies):
                    strategy = long_strategies[i]
                    if self._update_bot_config(session, bot_name, strategy):
                        update_count += 1

            # Update SHORT bots
            for i in range(self.sim_config.num_short_bots):
                bot_name = f"SimBot-SHORT-{i+1}"

                if i < len(short_strategies):
                    strategy = short_strategies[i]
                    if self._update_bot_config(session, bot_name, strategy):
                        update_count += 1

            session.commit()
            session.close()

            self.log.i(self.tag, f"✅ Updated {update_count} bot configs")

        except Exception as e:
            self.log.e(self.tag, f"Error updating configs: {e}")

    def _update_bot_config(self, session, bot_name: str, strategy) -> bool:
        """
        Update config cho một bot
        """
        try:
            bot_config = session.query(BotConfig).filter_by(name=bot_name).first()

            if not bot_config:
                return False

            config = strategy.config

            # Update các parameters
            bot_config.take_profit = config['take_profit']
            bot_config.stop_loss = config['stop_loss']
            bot_config.volume_multiplier = config['volume_multiplier']
            bot_config.min_confidence = config['min_confidence']
            bot_config.price_increase_threshold = config['price_increase_threshold']
            bot_config.rsi_threshold = config['rsi_threshold']
            bot_config.trailing_stop = config.get('trailing_stop', False)
            bot_config.min_trend_strength = config.get('min_trend_strength', 0.0)
            bot_config.require_breakout = config.get('require_breakout', False)
            bot_config.min_volume_consistency = config.get('min_volume_consistency', 0.0)
            bot_config.source_strategy_id = strategy.strategy_id

            self.log.d(self.tag,
                      f"Updated {bot_name}: TP={config['take_profit']}% "
                      f"SL={config['stop_loss']}%")

            return True

        except Exception as e:
            self.log.e(self.tag, f"Error updating {bot_name}: {e}")
            return False

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
                    # Update config
                    config = strategy.config
                    bot_config.take_profit = config['take_profit']
                    bot_config.stop_loss = config['stop_loss']
                    bot_config.volume_multiplier = config['volume_multiplier']
                    bot_config.min_confidence = config['min_confidence']

                    update_count += 1

            if update_count > 0:
                session.commit()
                self.log.i(self.tag, f"✅ Updated {update_count} bot configs")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error updating bot configs: {e}")

    async def check_promotions(self):
        """Check và promote bots từ SIMULATED sang REAL"""
        while True:
            try:
                await asyncio.sleep(3600)  # Mỗi 1 giờ

                session = self.db_manager.get_session()

                # Get all simulated bots
                sim_bots = session.query(BotConfig).filter_by(
                    trade_mode=TradeModeEnum.SIMULATED,
                    is_active=True
                ).all()

                for bot_config in sim_bots:
                    # Check if meets promotion criteria
                    if (bot_config.total_trades >= self.config['min_trades_for_promotion'] and
                        bot_config.win_rate >= self.config['min_win_rate_for_promotion'] and
                        bot_config.total_pnl > 0):

                        # Check profit factor từ trades
                        trades = session.query(Trade).filter_by(bot_config_id=bot_config.id).all()

                        winning_pnl = sum(t.pnl_usdt for t in trades if t.pnl_usdt and t.pnl_usdt > 0)
                        losing_pnl = abs(sum(t.pnl_usdt for t in trades if t.pnl_usdt and t.pnl_usdt < 0))

                        profit_factor = winning_pnl / losing_pnl if losing_pnl > 0 else 0

                        if profit_factor >= self.config['min_profit_factor']:
                            # Promote to REAL
                            await self.promote_bot_to_real(bot_config)

                session.close()

            except Exception as e:
                self.log.e(self.tag, f"Error checking promotions: {e}")

    async def promote_bot_to_real(self, bot_config: BotConfig):
        """Promote một bot từ SIMULATED sang REAL"""
        try:
            session = self.db_manager.get_session()

            bot_config = session.query(BotConfig).filter_by(id=bot_config.id).first()
            bot_config.trade_mode = TradeModeEnum.REAL

            session.commit()
            session.close()

            self.log.i(self.tag,
                      f"🎉 PROMOTED {bot_config.name} to REAL trading! "
                      f"Stats: {bot_config.total_trades} trades, "
                      f"{bot_config.win_rate:.1f}% WR, "
                      f"${bot_config.total_pnl:.2f} PnL"
                      )

            # Send notification
            await self.tele_message.send_message(
                f"🎉 BOT PROMOTED TO REAL TRADING!\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Bot: {bot_config.name}\n"
                f"Trades: {bot_config.total_trades}\n"
                f"Win Rate: {bot_config.win_rate:.1f}%\n"
                f"Total PnL: ${bot_config.total_pnl:.2f}\n"
                f"Direction: {bot_config.direction.value}",
                self.chat_id
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
            'auto_created': self.auto_created,
            'last_update': self.last_update_time
        }

    def update_sim_config(self, **kwargs):
        """
        Update simulation bot config

        Example:
            bot_manager.update_sim_config(
                max_stop_loss=1.5,
                max_trades_per_hour=5,
                min_win_rate=55.0
            )
        """
        for key, value in kwargs.items():
            if hasattr(self.sim_config, key):
                setattr(self.sim_config, key, value)
                self.log.i(self.tag, f"Updated sim_config.{key} = {value}")