# -*- coding: utf-8 -*-
"""
BotManager - Quản lý nhiều trading bots
- Tạo bots từ best backtest results
- Update config từ backtest mới
- Monitor performance
- Detailed report với config

FIX:
1. Tạo 5 LONG + 5 SHORT (thay vì top 5 chung)
2. Thêm reduce vào notification
3. Đọc reduce từ strategy config
"""

import asyncio
import traceback
from typing import List, Dict
import json
from datetime import datetime

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
                 log, tele_message, exchange=None, chat_id=""):
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
            'max_bots': 10,
            'min_trades_for_promotion': 20,
            'min_win_rate_for_promotion': 60,
            'min_profit_factor': 1.5,
            'auto_update_interval': 3600,
            'auto_create_from_backtest': True,
        }

    async def initialize(self):
        """Khởi tạo BotManager"""
        try:
            self.log.i(self.tag, "🤖 Initializing BotManager...")

            # Load existing bots from database
            await self.load_bots_from_db()

            # Auto create bots nếu chưa có
            if len(self.bots) == 0 and self.config['auto_create_from_backtest']:
                self.log.i(self.tag, "📊 No bots found, will auto-create from backtest after warm-up...")
                # Schedule auto-create sau 1 tiếng để có backtest data
                asyncio.create_task(self._delayed_auto_create_bots())

            # Start monitoring tasks
            asyncio.create_task(self.auto_update_configs())
            asyncio.create_task(self.monitor_performance())
            asyncio.create_task(self.check_promotions())

            self.log.i(self.tag, f"✅ BotManager initialized with {len(self.bots)} bots")

        except Exception as e:
            self.log.e(self.tag, f"Error initializing: {e}\n{traceback.format_exc()}")

    async def _delayed_auto_create_bots(self):
        """Tự động tạo bots sau khi có đủ backtest data - ĐỢI 1 TIẾNG"""
        try:
            # ✨ FIX: Đợi 1 tiếng (3600s) thay vì 30 phút
            await asyncio.sleep(3600)

            # Check nếu đã có bots thì skip
            if len(self.bots) > 0:
                return

            # Check nếu backtest có results
            self.strategy_manager.calculate_rankings()
            if not self.strategy_manager.top_strategies:
                self.log.w(self.tag, "⚠️ No backtest results yet, will retry in 1 hour...")
                asyncio.create_task(self._delayed_auto_create_bots())
                return

            # ✨ FIX: Tạo 5 LONG + 5 SHORT bots (SIMULATED mode)
            self.log.i(self.tag, "🤖 Auto-creating 5 LONG + 5 SHORT bots from backtest results...")
            await self.create_bots_from_backtest(top_n=5, mode=TradeModeEnum.SIMULATED)

        except Exception as e:
            self.log.e(self.tag, f"Error in delayed auto-create: {e}")

    async def load_bots_from_db(self):
        """Load bots từ database"""
        try:
            session = self.db_manager.get_session()

            bot_configs = session.query(BotConfig).filter_by(is_active=True).all()

            for bot_config in bot_configs:
                bot = TradingBot(
                    bot_config=bot_config,
                    db_manager=self.db_manager,
                    log=self.log,
                    tele_message=self.tele_message,
                    exchange=self.exchange,
                    chat_id=self.chat_id
                )
                self.bots.append(bot)

            session.close()

            self.log.i(self.tag, f"Loaded {len(self.bots)} bots from database")

        except Exception as e:
            self.log.e(self.tag, f"Error loading bots: {e}")

    async def create_bots_from_backtest(self, top_n: int = 5, mode: TradeModeEnum = TradeModeEnum.SIMULATED):
        """
        ✨ FIX: Tạo top_n LONG + top_n SHORT bots từ backtest
        """
        try:
            self.log.i(self.tag, f"📊 Creating {top_n} LONG + {top_n} SHORT bots from backtest results...")

            self.strategy_manager.calculate_rankings()

            # ✨ FIX: Tách riêng LONG và SHORT strategies
            all_strategies = self.strategy_manager.top_strategies
            long_strategies = [s for s in all_strategies if s.config['direction'] == 'LONG'][:top_n]
            short_strategies = [s for s in all_strategies if s.config['direction'] == 'SHORT'][:top_n]

            if not long_strategies and not short_strategies:
                self.log.w(self.tag, "No strategies available from backtest")
                return

            session = self.db_manager.get_session()
            created_count = 0
            created_bots_info = []

            # ✨ Tạo LONG bots
            for rank, strategy in enumerate(long_strategies, 1):
                result = await self._create_single_bot(session, strategy, rank, 'LONG', mode)
                if result:
                    created_bots_info.append(result)
                    created_count += 1

            # ✨ Tạo SHORT bots
            for rank, strategy in enumerate(short_strategies, 1):
                result = await self._create_single_bot(session, strategy, rank, 'SHORT', mode)
                if result:
                    created_bots_info.append(result)
                    created_count += 1

            session.commit()
            session.close()

            # Send detailed notification
            if created_count > 0:
                await self._send_bots_created_notification(created_bots_info, mode)

            self.log.i(self.tag, f"✅ Created {created_count} bots from backtest")

        except Exception as e:
            self.log.e(self.tag, f"Error creating bots from backtest: {e}\n{traceback.format_exc()}")

    async def _create_single_bot(self, session, strategy, rank: int, direction: str,
                                 mode: TradeModeEnum) -> Dict:
        """Tạo một bot từ strategy - helper method"""
        try:
            config = strategy.config
            stats = strategy.stats

            # Tạo bot name với config details
            bot_name = f"Bot-{direction}-R{rank}_TP{config['take_profit']}_SL{config['stop_loss']}"

            # Check if bot already exists
            existing = session.query(BotConfig).filter_by(name=bot_name).first()
            if existing:
                self.log.d(self.tag, f"Bot {bot_name} already exists, skipping")
                return None

            # ✨ FIX: Lấy reduce từ strategy config
            reduce_value = config.get('reduce', 0)

            # Create bot config
            bot_config = BotConfig(
                name=bot_name,
                direction=DirectionEnum.LONG if direction == 'LONG' else DirectionEnum.SHORT,
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

            # ✨ FIX: Set reduce nếu column tồn tại trong database
            if hasattr(bot_config, 'reduce'):
                bot_config.reduce = reduce_value

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
                exchange=self.exchange,
                chat_id=self.chat_id
            )

            self.bots.append(bot)

            self.log.i(self.tag,
                       f"✅ Created {bot_name}: {direction} | "
                       f"TP={config['take_profit']}% SL={config['stop_loss']}% R={reduce_value}%/m | "
                       f"Backtest: {stats['total_trades']} trades, {stats['win_rate']:.1f}% WR"
                       )

            return {
                'name': bot_name,
                'config': config,
                'stats': stats,
                'rank': rank,
                'direction': direction,
                'reduce': reduce_value
            }

        except Exception as e:
            self.log.e(self.tag, f"Error creating single bot: {e}")
            return None

    async def _send_bots_created_notification(self, bots_info: List[Dict], mode: TradeModeEnum):
        """
        ✨ FIX: Gửi notification khi tạo bots mới với chi tiết config + REDUCE
        """
        try:
            mode_emoji = "🔴" if mode == TradeModeEnum.REAL else "🔵"
            mode_str = mode.value

            # ✨ FIX: Đếm LONG và SHORT riêng
            long_count = sum(1 for b in bots_info if b['direction'] == 'LONG')
            short_count = sum(1 for b in bots_info if b['direction'] == 'SHORT')

            message = (
                f"🤖 <b>NEW BOTS CREATED</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Mode: {mode_emoji} {mode_str}\n"
                f"Count: {len(bots_info)} ({long_count} LONG + {short_count} SHORT)\n\n"
            )

            for i, bot_info in enumerate(bots_info, 1):
                config = bot_info['config']
                stats = bot_info['stats']
                direction = bot_info['direction']
                direction_emoji = "📈" if direction == 'LONG' else "📉"

                # ✨ FIX: Lấy reduce value
                reduce_value = bot_info.get('reduce', config.get('reduce', 0))

                message += (
                    f"{i}. {direction_emoji} <b>{bot_info['name']}</b>\n"
                    f"   📊 Backtest: {stats['total_trades']}T | "
                    f"WR:{stats['win_rate']:.0f}% | ${stats['total_pnl']:.2f}\n"
                    f"   ⚙️ TP{config['take_profit']}% SL{config['stop_loss']}% "
                    f"Vol{config['volume_multiplier']}x Conf{config['min_confidence']}% "
                    f"R{reduce_value}%/m\n"  # ✨ FIX: Thêm reduce
                )

            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error sending notification: {e}")

    async def on_signal(self, signal: Dict):
        """Broadcast signal đến tất cả bots"""
        try:
            tasks = []
            for bot in self.bots:
                if bot.is_active:  # Dùng cached value
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
                if bot.is_active:  # Dùng cached value
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

                self.strategy_manager.calculate_rankings()
                await self.update_bot_configs()

            except Exception as e:
                self.log.e(self.tag, f"Error in auto update: {e}")

    async def update_bot_configs(self):
        """Update bot configs từ backtest results mới"""
        try:
            session = self.db_manager.get_session()

            long_strategies = [s for s in self.strategy_manager.top_strategies
                               if s.config['direction'] == 'LONG'][:3]
            short_strategies = [s for s in self.strategy_manager.top_strategies
                                if s.config['direction'] == 'SHORT'][:3]

            update_count = 0

            for rank, strategy in enumerate(long_strategies, 1):
                bot_name = f"Bot-LONG-Top{rank}"
                bot_config = session.query(BotConfig).filter_by(name=bot_name).first()

                if bot_config:
                    config = strategy.config
                    bot_config.take_profit = config['take_profit']
                    bot_config.stop_loss = config['stop_loss']
                    bot_config.volume_multiplier = config['volume_multiplier']
                    bot_config.min_confidence = config['min_confidence']
                    # ✨ FIX: Update reduce nếu có
                    if hasattr(bot_config, 'reduce'):
                        bot_config.reduce = config.get('reduce', 0)
                    update_count += 1

            for rank, strategy in enumerate(short_strategies, 1):
                bot_name = f"Bot-SHORT-Top{rank}"
                bot_config = session.query(BotConfig).filter_by(name=bot_name).first()

                if bot_config:
                    config = strategy.config
                    bot_config.take_profit = config['take_profit']
                    bot_config.stop_loss = config['stop_loss']
                    bot_config.volume_multiplier = config['volume_multiplier']
                    bot_config.min_confidence = config['min_confidence']
                    # ✨ FIX: Update reduce nếu có
                    if hasattr(bot_config, 'reduce'):
                        bot_config.reduce = config.get('reduce', 0)
                    update_count += 1

            if update_count > 0:
                session.commit()
                self.log.i(self.tag, f"✅ Updated {update_count} bot configs from backtest")

            session.close()

        except Exception as e:
            self.log.e(self.tag, f"Error updating configs: {e}")

    async def check_promotions(self):
        """Check xem bot nào đủ điều kiện promote từ SIMULATED -> REAL"""
        while True:
            try:
                await asyncio.sleep(1800)

                session = self.db_manager.get_session()

                sim_configs = session.query(BotConfig).filter_by(
                    trade_mode=TradeModeEnum.SIMULATED,
                    is_active=True
                ).all()

                for bot_config in sim_configs:
                    if (bot_config.total_trades >= self.config['min_trades_for_promotion'] and
                            bot_config.win_rate >= self.config['min_win_rate_for_promotion']):

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

            self.log.i(self.tag, f"🎉 PROMOTED {bot_config.name} to REAL mode!")

            # ✨ FIX: Thêm reduce vào notification
            reduce_val = getattr(bot_config, 'reduce', 0) or 0

            message = (
                f"🎉 <b>BOT PROMOTED TO REAL</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot: <b>{bot_config.name}</b>\n"
                f"📊 Stats: {bot_config.total_trades}T | WR:{bot_config.win_rate:.1f}%\n"
                f"💰 PnL: ${bot_config.total_pnl:.2f}\n\n"
                f"⚙️ <b>Config:</b>\n"
                f"├ Direction: {bot_config.direction.value}\n"
                f"├ TP: {bot_config.take_profit}% | SL: {bot_config.stop_loss}%\n"
                f"├ Vol: {bot_config.volume_multiplier}x | Conf: {bot_config.min_confidence}%\n"
                f"├ Reduce: {reduce_val}%/min\n"
                f"└ Trail: {'✅' if bot_config.trailing_stop else '❌'}"
            )

            await self.tele_message.send_message(message, self.chat_id)

        except Exception as e:
            self.log.e(self.tag, f"Error promoting bot: {e}")

    async def monitor_performance(self):
        """Monitor performance của tất cả bots"""
        while True:
            try:
                await asyncio.sleep(3600)

                session = self.db_manager.get_session()
                bot_configs = session.query(BotConfig).filter_by(is_active=True).all()

                if not bot_configs:
                    session.close()
                    continue

                # Build detailed report
                message = (
                    f"📊 <b>BOTS PERFORMANCE REPORT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                )

                real_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.REAL]
                sim_bots = [b for b in bot_configs if b.trade_mode == TradeModeEnum.SIMULATED]

                if real_bots:
                    message += f"🔴 <b>REAL BOTS ({len(real_bots)}):</b>\n"
                    for bot in real_bots:
                        pnl_emoji = "✅" if bot.total_pnl > 0 else "❌"
                        reduce_val = getattr(bot, 'reduce', 0) or 0
                        message += (
                            f"  {pnl_emoji} {bot.name}\n"
                            f"     {bot.total_trades}T | WR:{bot.win_rate:.0f}% | ${bot.total_pnl:.2f}\n"
                            f"     TP{bot.take_profit}% SL{bot.stop_loss}% R{reduce_val}%/m\n"
                        )
                    message += "\n"

                if sim_bots:
                    # Sort by PnL
                    sim_bots_sorted = sorted(sim_bots, key=lambda b: b.total_pnl, reverse=True)
                    message += f"🔵 <b>SIM BOTS ({len(sim_bots)}):</b>\n"
                    for bot in sim_bots_sorted[:5]:  # Top 5
                        pnl_emoji = "✅" if bot.total_pnl > 0 else "❌"
                        reduce_val = getattr(bot, 'reduce', 0) or 0
                        message += (
                            f"  {pnl_emoji} {bot.name}\n"
                            f"     {bot.total_trades}T | WR:{bot.win_rate:.0f}% | ${bot.total_pnl:.2f} | R{reduce_val}%/m\n"
                        )

                # Summary
                total_pnl = sum(b.total_pnl for b in bot_configs)
                total_trades = sum(b.total_trades for b in bot_configs)
                message += (
                    f"\n📊 <b>SUMMARY:</b>\n"
                    f"Total Bots: {len(bot_configs)} ({len(real_bots)} REAL, {len(sim_bots)} SIM)\n"
                    f"Total Trades: {total_trades}\n"
                    f"Total PnL: ${total_pnl:.2f}"
                )

                self.log.i(self.tag, message)
                await self.tele_message.send_message(message, self.chat_id)

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