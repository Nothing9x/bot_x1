#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BotManager với hỗ trợ Real Trading
- Load bots giả lập từ database (như cũ)
- Load bots thật từ config_loader
- Real bots follow best strategy
- Auto-update config cho real bots
"""

import asyncio
import traceback
from typing import List, Dict, Optional
import json

from x1.bot.database.database_models import (
    Trade, TradeStatusEnum, DatabaseManager, BotConfig,
    DirectionEnum, BacktestResult, TradeModeEnum
)
from x1.bot.ai.strategy_manager import StrategyManager
from x1.bot.trading.trading_bot import TradingBot
from x1.bot.trading.config_loader import ConfigLoader, RealAccountConfig
from x1.bot.trading.real_bot_live import RealBotLive


class BotManagerReal:
    """
    BotManager mở rộng với hỗ trợ Real Trading

    Features:
    - Quản lý simulated bots (như BotManager cũ)
    - Quản lý real bots từ config_loader
    - Real bots tự động follow best strategy
    - Dispatch signals đến cả sim và real bots
    """

    def __init__(self, db_manager: DatabaseManager, strategy_manager: StrategyManager,
                 log, tele_message, exchange=None, chat_id="",
                 config_loader: ConfigLoader = None):
        """
        Args:
            db_manager: Database manager
            strategy_manager: Strategy manager for backtest
            log: Logger
            tele_message: Telegram message queue
            exchange: Exchange instance (for sim bots)
            chat_id: Default chat ID
            config_loader: ConfigLoader với real account configs
        """
        self.tag = "BotManager"
        self.db_manager = db_manager
        self.strategy_manager = strategy_manager
        self.log = log
        self.tele_message = tele_message
        self.exchange = exchange
        self.chat_id = chat_id
        self.config_loader = config_loader

        # Simulated bots (từ database)
        self.sim_bots: List[TradingBot] = []

        # Real bots (từ config_loader)
        self.real_bots: List[RealBotLive] = []

        # Config
        self.config = {
            'max_sim_bots': 10,
            'min_trades_for_promotion': 20,
            'min_win_rate_for_promotion': 60,
            'min_profit_factor': 1.5,
            'auto_update_interval': 3600,
            'auto_create_from_backtest': True,
            'num_long_bots': 5,
            'num_short_bots': 5,

            # Real trading settings
            'enable_real_trading': True,
            'real_bot_min_strategy_trades': 10,  # Min trades của strategy trước khi real bot follow
            'real_bot_min_win_rate': 50,  # Min win rate để real bot follow
        }

        # Control
        self.is_running = False
        self.initial_bots_created = False

    async def initialize(self):
        """Khởi tạo BotManager"""
        try:
            self.log.i(self.tag, "🤖 Initializing BotManager...")

            # Load simulated bots từ database
            await self.load_sim_bots_from_db()

            # Load real bots từ config_loader
            if self.config_loader and self.config['enable_real_trading']:
                await self.load_real_bots()

            # Start monitoring tasks
            self.is_running = True
            asyncio.create_task(self.auto_update_configs())
            asyncio.create_task(self.monitor_performance())
            asyncio.create_task(self.check_promotions())
            asyncio.create_task(self.auto_create_initial_bots())

            self.log.i(self.tag,
                       f"✅ BotManager initialized | "
                       f"Sim: {len(self.sim_bots)} | Real: {len(self.real_bots)}"
                       )

        except Exception as e:
            self.log.e(self.tag, f"Error initializing: {e}\n{traceback.format_exc()}")

    async def load_sim_bots_from_db(self):
        """Load simulated bots từ database"""
        try:
            session = self.db_manager.get_session()

            bot_configs = session.query(BotConfig).filter_by(
                is_active=True,
                trade_mode=TradeModeEnum.SIMULATED
            ).all()

            self.sim_bots.clear()

            for bot_config in bot_configs:
                bot = TradingBot(
                    bot_config=bot_config,
                    db_manager=self.db_manager,
                    log=self.log,
                    tele_message=self.tele_message,
                    exchange=self.exchange,
                    chat_id=self.chat_id
                )
                self.sim_bots.append(bot)

            session.close()
            self.log.i(self.tag, f"Loaded {len(self.sim_bots)} simulated bots")

        except Exception as e:
            self.log.e(self.tag, f"Error loading sim bots: {e}")

    async def load_real_bots(self):
        """Load real bots từ config_loader"""
        try:
            if not self.config_loader:
                self.log.w(self.tag, "No config_loader provided, skipping real bots")
                return

            active_accounts = self.config_loader.get_active_accounts()

            if not active_accounts:
                self.log.w(self.tag, "No active accounts in config_loader")
                return

            self.real_bots.clear()

            for account in active_accounts:
                try:
                    real_bot = RealBotLive(
                        account_config=account,
                        db_manager=self.db_manager,
                        log=self.log,
                    )

                    # Start real bot
                    await real_bot.start()

                    self.real_bots.append(real_bot)

                    self.log.i(self.tag,
                               f"✅ Loaded real bot: {account.account_id} | "
                               f"Exchange: {account.exchange} | Chat: {account.chat_id}"
                               )

                except Exception as e:
                    self.log.e(self.tag, f"Error loading real bot {account.account_id}: {e}")

            self.log.i(self.tag, f"Loaded {len(self.real_bots)} real bots")

        except Exception as e:
            self.log.e(self.tag, f"Error loading real bots: {e}")

    async def on_candle_update(self, symbol: str, interval: str, candle: Dict):
        """
        Nhận candle update từ WebSocket
        Dispatch đến cả sim và real bots
        """
        try:
            # Dispatch to simulated bots
            for bot in self.sim_bots:
                await bot.on_candle_update(symbol, interval, candle)

            # Dispatch price update to real bots
            price = candle.get('close', candle.get('c', 0))
            for real_bot in self.real_bots:
                await real_bot.on_price_update(symbol, price, candle)

        except Exception as e:
            self.log.e(self.tag, f"Error on candle update: {e}")

    async def on_pump_signal(self, signal: Dict):
        """
        Nhận pump signal từ pump detector
        Dispatch đến cả sim và real bots
        """
        try:
            # Dispatch to simulated bots
            for bot in self.sim_bots:
                await bot.on_signal(signal)

            # Dispatch to real bots
            for real_bot in self.real_bots:
                await real_bot.on_pump_signal(signal)

        except Exception as e:
            self.log.e(self.tag, f"Error on pump signal: {e}")

    async def auto_create_initial_bots(self):
        """
        Tự động tạo bots từ backtest sau 1 giờ
        """
        try:
            await asyncio.sleep(3600)  # Wait 1 hour

            if self.initial_bots_created:
                return

            self.log.i(self.tag, "🤖 Auto-creating bots from backtest results...")

            # Create simulated bots from top strategies
            await self.create_bots_from_backtest(
                num_long=self.config['num_long_bots'],
                num_short=self.config['num_short_bots'],
                mode=TradeModeEnum.SIMULATED
            )

            self.initial_bots_created = True

        except Exception as e:
            self.log.e(self.tag, f"Error auto-creating bots: {e}")

    async def create_bots_from_backtest(self, num_long: int = 5, num_short: int = 5,
                                        mode: TradeModeEnum = TradeModeEnum.SIMULATED):
        """
        Tạo bots từ top strategies
        """
        try:
            self.strategy_manager.calculate_rankings()

            session = self.db_manager.get_session()
            created_count = 0

            # Get top LONG strategies
            long_strategies = [s for s in self.strategy_manager.strategies
                               if s.config['direction'] == 'LONG' and s.stats['total_trades'] > 0]
            long_strategies = sorted(long_strategies,
                                     key=lambda s: s.stats.get('total_pnl', 0), reverse=True)[:num_long]

            # Get top SHORT strategies
            short_strategies = [s for s in self.strategy_manager.strategies
                                if s.config['direction'] == 'SHORT' and s.stats['total_trades'] > 0]
            short_strategies = sorted(short_strategies,
                                      key=lambda s: s.stats.get('total_pnl', 0), reverse=True)[:num_short]

            all_strategies = long_strategies + short_strategies

            for rank, strategy in enumerate(all_strategies, 1):
                config = strategy.config
                stats = strategy.stats

                direction_prefix = 'L' if config['direction'] == 'LONG' else 'S'
                bot_name = (
                    f"Bot-{direction_prefix}-R{rank}_"
                    f"TP{config['take_profit']}_SL{config['stop_loss']}"
                )

                # Check existing
                existing = session.query(BotConfig).filter_by(name=bot_name).first()
                if existing:
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
                    timeframe=config.get('timeframe', '1m'),
                    trade_mode=mode,
                    is_active=True,
                    source_strategy_id=strategy.strategy_id
                )

                session.add(bot_config)
                session.flush()

                # Create TradingBot instance
                bot = TradingBot(
                    bot_config=bot_config,
                    db_manager=self.db_manager,
                    log=self.log,
                    tele_message=self.tele_message,
                    exchange=self.exchange,
                    chat_id=self.chat_id
                )
                self.sim_bots.append(bot)
                created_count += 1

                self.log.i(self.tag,
                           f"✅ Created {bot_name}: WR={stats['win_rate']:.1f}% | "
                           f"PnL=${stats['total_pnl']:.2f}"
                           )

            session.commit()
            session.close()

            self.log.i(self.tag, f"Created {created_count} bots from backtest")

            # Notify
            await self.tele_message.send_message(
                f"🤖 AUTO-CREATED BOTS\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Created: {created_count} bots\n"
                f"LONG: {len(long_strategies)}\n"
                f"SHORT: {len(short_strategies)}\n"
                f"Mode: {mode.value}",
                self.chat_id
            )

        except Exception as e:
            self.log.e(self.tag, f"Error creating bots: {e}\n{traceback.format_exc()}")

    async def auto_update_configs(self):
        """Auto update configs từ backtest results"""
        while self.is_running:
            try:
                await asyncio.sleep(self.config['auto_update_interval'])

                if not self.is_running:
                    break

                # Update sim bot configs from latest backtest
                # ... (giữ nguyên logic cũ)

            except Exception as e:
                self.log.e(self.tag, f"Error updating configs: {e}")

    async def monitor_performance(self):
        """Monitor và report performance"""
        while self.is_running:
            try:
                await asyncio.sleep(3600)  # Every hour

                if not self.is_running:
                    break

                await self._send_performance_report()

            except Exception as e:
                self.log.e(self.tag, f"Error monitoring performance: {e}")

    async def _send_performance_report(self):
        """Send performance report"""
        try:
            session = self.db_manager.get_session()

            # Get sim bot stats
            sim_configs = session.query(BotConfig).filter_by(
                is_active=True,
                trade_mode=TradeModeEnum.SIMULATED
            ).all()

            sim_total_trades = sum(b.total_trades for b in sim_configs)
            sim_total_pnl = sum(b.total_pnl for b in sim_configs)

            # Get real bot stats
            real_total_trades = sum(b.total_trades for b in self.real_bots)
            real_total_pnl = sum(b.total_pnl for b in self.real_bots)

            message = (
                f"📊 HOURLY PERFORMANCE REPORT\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔵 SIMULATED BOTS ({len(sim_configs)}):\n"
                f"   Trades: {sim_total_trades}\n"
                f"   PnL: ${sim_total_pnl:.2f}\n\n"
            )

            # Top 3 sim bots
            top_sim = sorted(sim_configs, key=lambda b: b.total_pnl, reverse=True)[:3]
            for i, bot in enumerate(top_sim, 1):
                message += f"   #{i} {bot.name}: {bot.win_rate:.1f}% | ${bot.total_pnl:.2f}\n"

            if self.real_bots:
                message += (
                    f"\n🔴 REAL BOTS ({len(self.real_bots)}):\n"
                    f"   Trades: {real_total_trades}\n"
                    f"   PnL: ${real_total_pnl:.2f}\n\n"
                )

                for bot in self.real_bots:
                    stats = bot.get_stats()
                    message += (
                        f"   • {stats['account_id']}: "
                        f"{stats['win_rate']:.1f}% | ${stats['total_pnl']:.2f}\n"
                    )

            self.log.i(self.tag, message)
            await self.tele_message.send_message(message, self.chat_id)

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error sending report: {e}")

    async def check_promotions(self):
        """Check promotions từ SIMULATED -> REAL"""
        while self.is_running:
            try:
                await asyncio.sleep(1800)  # Every 30 min

                if not self.is_running:
                    break

                # ... (giữ nguyên logic cũ)

            except Exception as e:
                self.log.e(self.tag, f"Error checking promotions: {e}")

    def get_stats(self) -> Dict:
        """Get overall stats"""
        session = self.db_manager.get_session()

        sim_configs = session.query(BotConfig).filter_by(
            is_active=True,
            trade_mode=TradeModeEnum.SIMULATED
        ).all()

        real_configs = session.query(BotConfig).filter_by(
            is_active=True,
            trade_mode=TradeModeEnum.REAL
        ).all()

        session.close()

        return {
            'total_sim_bots': len(sim_configs),
            'total_real_bots': len(self.real_bots),
            'sim_total_trades': sum(b.total_trades for b in sim_configs),
            'sim_total_pnl': sum(b.total_pnl for b in sim_configs),
            'real_total_trades': sum(b.total_trades for b in self.real_bots),
            'real_total_pnl': sum(b.total_pnl for b in self.real_bots),
        }

    async def stop(self):
        """Stop all bots"""
        try:
            self.is_running = False

            # Stop real bots
            for bot in self.real_bots:
                await bot.stop()

            self.log.i(self.tag, "BotManager stopped")

        except Exception as e:
            self.log.e(self.tag, f"Error stopping: {e}")