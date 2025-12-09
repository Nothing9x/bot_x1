#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Bot Config Auto Updater
- Tự động tạo/update 5 LONG + 5 SHORT simulated bots
- Update config mỗi giờ dựa trên best backtest strategies
- Hiển thị đầy đủ: TP, SL, Vol, Conf, Reduce, RSI, Price Increase
"""

import asyncio
import traceback
from datetime import datetime
from typing import List, Dict, Optional
import json

from x1.bot.database.database_models import (
    DatabaseManager, BotConfig, BacktestResult,
    DirectionEnum, TradeModeEnum
)
from x1.bot.ai.strategy_manager import StrategyManager


class BotConfigUpdater:
    """
    Tự động tạo và update simulated bots từ best backtest strategies
    """

    def __init__(self, db_manager: DatabaseManager, strategy_manager: StrategyManager,
                 log, tele_message=None, chat_id: str = ""):
        self.tag = "ConfigUpdater"
        self.db_manager = db_manager
        self.strategy_manager = strategy_manager
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        # Config
        self.config = {
            'update_interval_seconds': 3600,  # 1 giờ
            'enabled': True,
            'num_long_bots': 5,
            'num_short_bots': 5,
            'min_trades': 5,
            'min_win_rate': 40.0,
        }

        # Stats
        self.last_update_time = None
        self.total_updates = 0
        self.is_first_run = True

    def set_update_interval_hours(self, hours: float):
        """Set update interval in hours"""
        self.config['update_interval_seconds'] = int(hours * 3600)
        self.log.i(self.tag, f"⏰ Update interval: {hours}h")

    async def start(self):
        """Bắt đầu auto-update loop"""
        interval = self.config['update_interval_seconds']
        self.log.i(self.tag, f"🚀 Starting ConfigUpdater - Update every {interval / 3600:.1f}h")

        while self.config['enabled']:
            try:
                if self.is_first_run:
                    self.log.i(self.tag, f"⏳ Waiting {interval / 3600:.1f}h before first update...")
                    await asyncio.sleep(interval)
                    self.is_first_run = False

                self.log.i(self.tag, "=" * 70)
                self.log.i(self.tag, "🔄 SCHEDULED CONFIG UPDATE")
                self.log.i(self.tag, "=" * 70)

                await self.update_all_bots()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                self.log.i(self.tag, "🛑 ConfigUpdater stopped")
                break
            except Exception as e:
                self.log.e(self.tag, f"Error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(60)

    async def update_all_bots(self) -> Dict:
        """Tạo/Update tất cả simulated bots"""
        result = {
            'long_updates': [],
            'short_updates': [],
            'created_count': 0,
            'updated_count': 0,
        }

        try:
            self.strategy_manager.calculate_rankings()
            all_strategies = self.strategy_manager.top_strategies

            if not all_strategies:
                self.log.w(self.tag, "⚠️ No strategies available!")
                return result

            # Lọc strategies
            long_strategies = [
                s for s in all_strategies
                if s.config.get('direction') == 'LONG'
                   and s.stats.get('total_trades', 0) >= self.config['min_trades']
                   and s.stats.get('win_rate', 0) >= self.config['min_win_rate']
            ]

            short_strategies = [
                s for s in all_strategies
                if s.config.get('direction') == 'SHORT'
                   and s.stats.get('total_trades', 0) >= self.config['min_trades']
                   and s.stats.get('win_rate', 0) >= self.config['min_win_rate']
            ]

            # Sort by PnL
            long_strategies.sort(key=lambda s: s.stats.get('total_pnl', 0), reverse=True)
            short_strategies.sort(key=lambda s: s.stats.get('total_pnl', 0), reverse=True)

            self.log.i(self.tag,
                       f"📊 Found {len(long_strategies)} LONG, {len(short_strategies)} SHORT qualified strategies")

            # Update LONG bots
            self.log.i(self.tag, "")
            self.log.i(self.tag, "━━━━━━━━━━━━━━━━━━ LONG BOTS ━━━━━━━━━━━━━━━━━━")

            for rank in range(1, self.config['num_long_bots'] + 1):
                if rank <= len(long_strategies):
                    strategy = long_strategies[rank - 1]
                    update_info = await self._update_or_create_bot(strategy, rank, 'LONG')
                    if update_info:
                        result['long_updates'].append(update_info)
                        if update_info['action'] == 'created':
                            result['created_count'] += 1
                        elif update_info['action'] == 'updated':
                            result['updated_count'] += 1
                else:
                    self.log.w(self.tag, f"  Bot-LONG-Top{rank}: No strategy available")

            # Update SHORT bots
            self.log.i(self.tag, "")
            self.log.i(self.tag, "━━━━━━━━━━━━━━━━━━ SHORT BOTS ━━━━━━━━━━━━━━━━━━")

            for rank in range(1, self.config['num_short_bots'] + 1):
                if rank <= len(short_strategies):
                    strategy = short_strategies[rank - 1]
                    update_info = await self._update_or_create_bot(strategy, rank, 'SHORT')
                    if update_info:
                        result['short_updates'].append(update_info)
                        if update_info['action'] == 'created':
                            result['created_count'] += 1
                        elif update_info['action'] == 'updated':
                            result['updated_count'] += 1
                else:
                    self.log.w(self.tag, f"  Bot-SHORT-Top{rank}: No strategy available")

            # Summary
            self.last_update_time = datetime.now()
            self.total_updates += 1

            self.log.i(self.tag, "")
            self.log.i(self.tag, "━━━━━━━━━━━━━━━━━━ SUMMARY ━━━━━━━━━━━━━━━━━━")
            self.log.i(self.tag, f"✅ Created: {result['created_count']} | 📝 Updated: {result['updated_count']}")
            self.log.i(self.tag, "=" * 70)

            await self._send_detailed_notification(result)

        except Exception as e:
            self.log.e(self.tag, f"Error updating bots: {e}\n{traceback.format_exc()}")

        return result

    async def _update_or_create_bot(self, strategy, rank: int, direction: str) -> Optional[Dict]:
        """Update hoặc create bot với đầy đủ thông số"""
        try:
            session = self.db_manager.get_session()

            bot_name = f"Bot-{direction}-Top{rank}"
            cfg = strategy.config
            stats = strategy.stats

            existing_bot = session.query(BotConfig).filter_by(name=bot_name).first()

            if existing_bot:
                # ===== UPDATE =====
                old = {
                    'take_profit': existing_bot.take_profit,
                    'stop_loss': existing_bot.stop_loss,
                    'price_increase_threshold': existing_bot.price_increase_threshold,
                    'volume_multiplier': existing_bot.volume_multiplier,
                    'min_confidence': existing_bot.min_confidence,
                    'rsi_threshold': existing_bot.rsi_threshold,
                    'reduce': getattr(existing_bot, 'reduce', 0.0) or 0.0,
                    'timeframe': existing_bot.timeframe,
                }

                new = {
                    'take_profit': cfg['take_profit'],
                    'stop_loss': cfg['stop_loss'],
                    'price_increase_threshold': cfg['price_increase_threshold'],
                    'volume_multiplier': cfg['volume_multiplier'],
                    'min_confidence': cfg['min_confidence'],
                    'rsi_threshold': cfg['rsi_threshold'],
                    'reduce': cfg.get('reduce', 0.0),
                    'timeframe': cfg.get('timeframe', '1m'),
                }

                # Check changes
                has_changes = any(old[k] != new[k] for k in old.keys())

                if not has_changes:
                    self.log.i(self.tag, f"  {bot_name}: ✓ No changes")
                    session.close()
                    return {'action': 'no_change', 'bot_name': bot_name}

                # Apply updates
                existing_bot.take_profit = new['take_profit']
                existing_bot.stop_loss = new['stop_loss']
                existing_bot.price_increase_threshold = new['price_increase_threshold']
                existing_bot.volume_multiplier = new['volume_multiplier']
                existing_bot.min_confidence = new['min_confidence']
                existing_bot.rsi_threshold = new['rsi_threshold']
                existing_bot.timeframe = new['timeframe']
                existing_bot.source_strategy_id = strategy.strategy_id

                # Update reduce if column exists
                if hasattr(existing_bot, 'reduce'):
                    existing_bot.reduce = new['reduce']

                session.commit()
                session.close()

                # Log detailed changes
                self._log_update(bot_name, strategy.strategy_id, stats, old, new)

                return {
                    'action': 'updated',
                    'bot_name': bot_name,
                    'strategy_id': strategy.strategy_id,
                    'old': old,
                    'new': new,
                    'stats': stats,
                }

            else:
                # ===== CREATE =====
                new_bot = BotConfig(
                    name=bot_name,
                    direction=DirectionEnum.LONG if direction == 'LONG' else DirectionEnum.SHORT,
                    take_profit=cfg['take_profit'],
                    stop_loss=cfg['stop_loss'],
                    position_size_usdt=cfg.get('position_size_usdt', 50),
                    price_increase_threshold=cfg['price_increase_threshold'],
                    volume_multiplier=cfg['volume_multiplier'],
                    rsi_threshold=cfg['rsi_threshold'],
                    min_confidence=cfg['min_confidence'],
                    min_trend_strength=cfg.get('min_trend_strength', 0.0),
                    require_breakout=cfg.get('require_breakout', False),
                    min_volume_consistency=cfg.get('min_volume_consistency', 0.0),
                    timeframe=cfg.get('timeframe', '1m'),
                    trade_mode=TradeModeEnum.SIMULATED,
                    is_active=True,
                    source_strategy_id=strategy.strategy_id
                )

                # Set reduce if column exists
                if hasattr(new_bot, 'reduce'):
                    new_bot.reduce = cfg.get('reduce', 0.0)

                session.add(new_bot)
                session.commit()
                session.close()

                new_cfg = {
                    'take_profit': cfg['take_profit'],
                    'stop_loss': cfg['stop_loss'],
                    'price_increase_threshold': cfg['price_increase_threshold'],
                    'volume_multiplier': cfg['volume_multiplier'],
                    'min_confidence': cfg['min_confidence'],
                    'rsi_threshold': cfg['rsi_threshold'],
                    'reduce': cfg.get('reduce', 0.0),
                    'timeframe': cfg.get('timeframe', '1m'),
                }

                # Log creation
                self._log_create(bot_name, strategy.strategy_id, stats, new_cfg)

                return {
                    'action': 'created',
                    'bot_name': bot_name,
                    'strategy_id': strategy.strategy_id,
                    'config': new_cfg,
                    'stats': stats,
                }

        except Exception as e:
            self.log.e(self.tag, f"Error {bot_name}: {e}")
            return None

    def _log_update(self, bot_name: str, strategy_id: int, stats: Dict, old: Dict, new: Dict):
        """Log chi tiết update"""
        self.log.i(self.tag, "")
        self.log.i(self.tag, f"  📝 {bot_name} - UPDATED")
        self.log.i(self.tag,
                   f"     Strategy #{strategy_id}: {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}")
        self.log.i(self.tag, f"     ┌───────────────────┬────────────┬────────────┐")
        self.log.i(self.tag, f"     │ Parameter         │    Old     │    New     │")
        self.log.i(self.tag, f"     ├───────────────────┼────────────┼────────────┤")
        self.log.i(self.tag,
                   f"     │ Take Profit       │ {old['take_profit']:>8.1f}%  │ {new['take_profit']:>8.1f}%  │")
        self.log.i(self.tag, f"     │ Stop Loss         │ {old['stop_loss']:>8.1f}%  │ {new['stop_loss']:>8.1f}%  │")
        self.log.i(self.tag,
                   f"     │ Price Increase    │ {old['price_increase_threshold']:>8.1f}%  │ {new['price_increase_threshold']:>8.1f}%  │")
        self.log.i(self.tag,
                   f"     │ Volume Multiplier │ {old['volume_multiplier']:>8.1f}x  │ {new['volume_multiplier']:>8.1f}x  │")
        self.log.i(self.tag,
                   f"     │ Min Confidence    │ {old['min_confidence']:>8.0f}%  │ {new['min_confidence']:>8.0f}%  │")
        self.log.i(self.tag,
                   f"     │ RSI Threshold     │ {old['rsi_threshold']:>8.0f}   │ {new['rsi_threshold']:>8.0f}   │")
        self.log.i(self.tag, f"     │ Reduce TP/min     │ {old['reduce']:>8.1f}%  │ {new['reduce']:>8.1f}%  │")
        self.log.i(self.tag, f"     └───────────────────┴────────────┴────────────┘")

    def _log_create(self, bot_name: str, strategy_id: int, stats: Dict, cfg: Dict):
        """Log chi tiết create"""
        self.log.i(self.tag, "")
        self.log.i(self.tag, f"  🆕 {bot_name} - CREATED")
        self.log.i(self.tag,
                   f"     Strategy #{strategy_id}: {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}")
        self.log.i(self.tag, f"     ┌───────────────────┬────────────┐")
        self.log.i(self.tag, f"     │ Parameter         │   Value    │")
        self.log.i(self.tag, f"     ├───────────────────┼────────────┤")
        self.log.i(self.tag, f"     │ Take Profit       │ {cfg['take_profit']:>8.1f}%  │")
        self.log.i(self.tag, f"     │ Stop Loss         │ {cfg['stop_loss']:>8.1f}%  │")
        self.log.i(self.tag, f"     │ Price Increase    │ {cfg['price_increase_threshold']:>8.1f}%  │")
        self.log.i(self.tag, f"     │ Volume Multiplier │ {cfg['volume_multiplier']:>8.1f}x  │")
        self.log.i(self.tag, f"     │ Min Confidence    │ {cfg['min_confidence']:>8.0f}%  │")
        self.log.i(self.tag, f"     │ RSI Threshold     │ {cfg['rsi_threshold']:>8.0f}   │")
        self.log.i(self.tag, f"     │ Reduce TP/min     │ {cfg['reduce']:>8.1f}%  │")
        self.log.i(self.tag, f"     │ Mode              │ SIMULATED  │")
        self.log.i(self.tag, f"     └───────────────────┴────────────┘")

    async def _send_detailed_notification(self, result: Dict):
        """Gửi Telegram notification chi tiết"""
        if not self.tele_message:
            return

        if result['created_count'] == 0 and result['updated_count'] == 0:
            try:
                await self.tele_message.send_message(
                    f"🔄 Config Update Check\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
                    f"✅ All bots up-to-date",
                    self.chat_id
                )
            except:
                pass
            return

        lines = [
            f"🔄 BOT CONFIG UPDATE",
            f"━━━━━━━━━━━━━━━━━━",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"🆕 Created: {result['created_count']} | 📝 Updated: {result['updated_count']}",
            "",
        ]

        # LONG updates
        long_changes = [u for u in result['long_updates'] if u['action'] in ['created', 'updated']]
        if long_changes:
            lines.append("📈 LONG BOTS:")
            for u in long_changes:
                emoji = "🆕" if u['action'] == 'created' else "📝"
                stats = u['stats']

                if u['action'] == 'updated':
                    old, new = u['old'], u['new']
                    lines.append(
                        f"{emoji} {u['bot_name']}\n"
                        f"   TP: {old['take_profit']}→{new['take_profit']}% | "
                        f"SL: {old['stop_loss']}→{new['stop_loss']}%\n"
                        f"   Vol: {old['volume_multiplier']}→{new['volume_multiplier']}x | "
                        f"Conf: {old['min_confidence']}→{new['min_confidence']}%\n"
                        f"   Reduce: {old['reduce']}→{new['reduce']}%/min\n"
                        f"   📊 {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}"
                    )
                else:
                    cfg = u['config']
                    lines.append(
                        f"{emoji} {u['bot_name']}\n"
                        f"   TP: {cfg['take_profit']}% | SL: {cfg['stop_loss']}%\n"
                        f"   Vol: {cfg['volume_multiplier']}x | Conf: {cfg['min_confidence']}%\n"
                        f"   Reduce: {cfg['reduce']}%/min\n"
                        f"   📊 {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}"
                    )
            lines.append("")

        # SHORT updates
        short_changes = [u for u in result['short_updates'] if u['action'] in ['created', 'updated']]
        if short_changes:
            lines.append("📉 SHORT BOTS:")
            for u in short_changes:
                emoji = "🆕" if u['action'] == 'created' else "📝"
                stats = u['stats']

                if u['action'] == 'updated':
                    old, new = u['old'], u['new']
                    lines.append(
                        f"{emoji} {u['bot_name']}\n"
                        f"   TP: {old['take_profit']}→{new['take_profit']}% | "
                        f"SL: {old['stop_loss']}→{new['stop_loss']}%\n"
                        f"   Vol: {old['volume_multiplier']}→{new['volume_multiplier']}x | "
                        f"Conf: {old['min_confidence']}→{new['min_confidence']}%\n"
                        f"   Reduce: {old['reduce']}→{new['reduce']}%/min\n"
                        f"   📊 {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}"
                    )
                else:
                    cfg = u['config']
                    lines.append(
                        f"{emoji} {u['bot_name']}\n"
                        f"   TP: {cfg['take_profit']}% | SL: {cfg['stop_loss']}%\n"
                        f"   Vol: {cfg['volume_multiplier']}x | Conf: {cfg['min_confidence']}%\n"
                        f"   Reduce: {cfg['reduce']}%/min\n"
                        f"   📊 {stats['total_trades']}T | {stats['win_rate']:.1f}%WR | ${stats['total_pnl']:.2f}"
                    )

        try:
            await self.tele_message.send_message("\n".join(lines), self.chat_id)
        except Exception as e:
            self.log.e(self.tag, f"Error sending notification: {e}")

    async def force_update(self) -> Dict:
        """Force update ngay lập tức"""
        self.log.i(self.tag, "⚡ FORCE UPDATE")
        self.is_first_run = False
        return await self.update_all_bots()

    def get_stats(self) -> Dict:
        return {
            'enabled': self.config['enabled'],
            'interval_hours': self.config['update_interval_seconds'] / 3600,
            'num_long_bots': self.config['num_long_bots'],
            'num_short_bots': self.config['num_short_bots'],
            'last_update': self.last_update_time.isoformat() if self.last_update_time else None,
            'total_updates': self.total_updates,
        }

    def stop(self):
        self.config['enabled'] = False
        self.log.i(self.tag, "🛑 Stopped")