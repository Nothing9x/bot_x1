#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pump Trading Bot
Main bot với Backtest + Production Trading + Real Trading
- Hỗ trợ cả MEXC và Gate.io qua config
- Real bots follow best strategy từ database
"""

import asyncio
import os
import time
import traceback
from datetime import datetime
from typing import List, Dict
import json

import requests

from x1.bot.ai.pump_detector import PumpDetector
from x1.bot.ai.strategy_manager import StrategyManager
from x1.bot.config.exchange_config import ExchangeConfig, ExchangeType
from x1.bot.market.socket_factory import SocketFactory
from x1.bot.model.symbol import Symbol
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils import Utils
from x1.bot.utils.Log import Log
from x1.bot.utils.black_list_symbol import BLACK_LIST_SYMBOL

# ✨ THÊM IMPORT - PnL Tracking
try:
    from x1.bot.utils.enhanced_pnl_tracking import integrate_pnl_tracking

    PNL_TRACKING_AVAILABLE = True
except ImportError:
    PNL_TRACKING_AVAILABLE = False

# ✨ THÊM IMPORT - Bot Config Auto Updater
try:
    from x1.bot.trading.bot_config_updater import BotConfigUpdater

    CONFIG_UPDATER_AVAILABLE = True
except ImportError:
    CONFIG_UPDATER_AVAILABLE = False

# ✨ THÊM IMPORT - Real Trading
try:
    from x1.bot.trading.config_loader import ConfigLoader
    from x1.bot.trading.real_bot_live import RealBotLive

    REAL_TRADING_AVAILABLE = True
except ImportError:
    REAL_TRADING_AVAILABLE = False


class MexcPumpBot:
    """
    Bot phát hiện pump và backtest strategies để tìm config tốt nhất
    - Hỗ trợ cả MEXC và Gate.io thông qua BotConfig
    - Real bots trade thật follow best strategy
    """

    def __init__(self, api_key: str = None, api_secret: str = None, proxy=None,
                 real_accounts_path: str = None):
        """
        Args:
            api_key: API key (optional)
            api_secret: API secret (optional)
            proxy: Proxy string (optional)
            real_accounts_path: Path to real_accounts.json config file
        """
        self.db_manager = None
        self.bot_manager = None
        # ✨ THÊM ATTRIBUTES - PnL Tracking
        self.enhanced_bot_mgr = None
        self.enhanced_strat_mgr = None
        # ✨ THÊM ATTRIBUTE - Config Auto Updater
        self.config_updater = None
        # ✨ THÊM ATTRIBUTES - Real Trading
        self.config_loader = None
        self.real_bots: List[RealBotLive] = [] if REAL_TRADING_AVAILABLE else []
        self.real_accounts_path = real_accounts_path or "config/real_accounts.json"

        bot_token = ExchangeConfig.TELEGRAM_BOT_TOKEN
        self.admin_proxy = proxy or ExchangeConfig.PROXY

        self.tag = "PumpBot"
        self.chat_id = ExchangeConfig.TELEGRAM_CHAT_ID

        self.log = Log().init('main', ExchangeConfig.LOG_LEVEL)

        # Setup Telegram notification
        self.tele_message = TelegramMessageQueue(log=self.log, bot_token=bot_token)

        # ========== DYNAMIC SOCKET CREATION ==========
        # Tạo exchange socket dựa trên config (MEXC hoặc GATE)
        self.market_socket = SocketFactory.create_socket(
            log=self.log,
            proxy=self.admin_proxy,
            tele_message=self.tele_message,
            chat_id=self.chat_id
        )

        # Alias để tương thích với code cũ
        self.mexc_socket = self.market_socket

        # Setup Pump Detector
        self.pump_detector = PumpDetector(self.log, self.tele_message, self.chat_id)

        # Setup Strategy Manager (MAIN FEATURE)
        self.strategy_manager = StrategyManager(self.log, self.tele_message, self.chat_id)

        # Symbols to monitor
        self.symbols: List[Symbol] = []

        # Stats tracking
        self.start_time = None
        self.total_signals_detected = 0

        # Log exchange being used
        self.log.i(self.tag, f"📊 Using exchange: {ExchangeConfig.get_exchange_name()}")

    async def initialize(self):
        """Khởi tạo bot"""
        try:
            exchange_name = ExchangeConfig.get_exchange_name()
            self.log.i(self.tag, f"🚀 Initializing Pump Bot with {exchange_name} + BotManager...")

            self.start_time = datetime.now()

            # Send startup message
            await self.tele_message.send_message(
                f"🤖 Pump Bot Starting...\n"
                f"📊 Exchange: {exchange_name}\n"
                f"Mode: Backtest + Production Trading\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━",
                self.chat_id
            )

            # Load symbols to monitor
            self.symbols = self.init_symbols()
            self.log.i(self.tag, f"✅ Loaded {len(self.symbols)} symbols from {exchange_name}")

            # Configure pump detector
            self.configure_detector()

            # Generate strategies for backtesting
            num_strategies = ExchangeConfig.NUM_STRATEGIES
            self.strategy_manager.generate_strategies(max_strategies=num_strategies)

            # Initialize Database & BotManager
            try:
                from x1.bot.database.database_models import DatabaseManager
                from x1.bot.trading.bot_manager import BotManager

                self.db_manager = DatabaseManager('sqlite:///trading_bot.db')
                self.db_manager.create_tables()
                self.log.i(self.tag, "✅ Database initialized")

                self.bot_manager = BotManager(
                    self.db_manager,
                    self.strategy_manager,
                    self.log,
                    self.tele_message,
                    None, self.chat_id
                )

                await self.bot_manager.initialize()

                # ✨ THÊM - INTEGRATE PNL TRACKING
                if PNL_TRACKING_AVAILABLE:
                    try:
                        self.enhanced_bot_mgr, self.enhanced_strat_mgr = integrate_pnl_tracking(
                            self.bot_manager,
                            self.strategy_manager,
                            self.db_manager,
                            self.log,
                            self.tele_message,
                            self.chat_id
                        )

                        self.strategy_manager.enhanced_manager = self.enhanced_strat_mgr
                        self.log.i(self.tag, "✅ Enhanced PnL tracking initialized")
                    except Exception as e:
                        self.log.w(self.tag, f"⚠️ PnL tracking init failed: {e}")
                else:
                    self.log.w(self.tag, "⚠️ PnL tracking not available")

                # ✨ THÊM - INTEGRATE CONFIG AUTO UPDATER
                if CONFIG_UPDATER_AVAILABLE:
                    try:
                        self.config_updater = BotConfigUpdater(
                            db_manager=self.db_manager,
                            strategy_manager=self.strategy_manager,
                            log=self.log,
                            tele_message=self.tele_message,
                            chat_id=self.chat_id
                        )
                        # Config: update mỗi 1 giờ
                        self.config_updater.set_update_interval_hours(1)
                        self.log.i(self.tag, "✅ Bot Config Auto Updater initialized (interval: 1h)")
                    except Exception as e:
                        self.log.w(self.tag, f"⚠️ Config updater init failed: {e}")
                else:
                    self.log.w(self.tag, "⚠️ Config updater not available")

                # ✨ THÊM - INITIALIZE REAL TRADING
                await self._init_real_trading()

                bot_stats = self.bot_manager.get_stats()

                # Build init message
                init_msg = (
                    f"✅ Initialization complete\n"
                    f"📊 Exchange: {exchange_name}\n"
                    f"📈 Backtest: {num_strategies} strategies\n"
                    f"🤖 Production: {bot_stats['total_bots']} bots "
                    f"({bot_stats['real_bots']} REAL, {bot_stats['simulated_bots']} SIM)\n"
                    f"💰 Monitoring {len(self.symbols)} symbols"
                )

                # Add real bots info
                if self.real_bots:
                    init_msg += f"\n🔴 Real Trading: {len(self.real_bots)} bot(s) active"

                await self.tele_message.send_message(init_msg, self.chat_id)

            except ImportError as e:
                self.log.w(self.tag, f"⚠️ BotManager not available (missing modules): {e}")
                self.log.w(self.tag, "   Running in BACKTEST-ONLY mode")

                await self.tele_message.send_message(
                    f"✅ Initialization complete (Backtest-only mode)\n"
                    f"📊 Exchange: {exchange_name}\n"
                    f"📈 Backtest: {num_strategies} strategies\n"
                    f"💰 Monitoring {len(self.symbols)} symbols\n\n"
                    f"⚠️ Production trading not available",
                    self.chat_id
                )

            # Setup callbacks
            # 1. PumpDetector nhận candles
            self.market_socket.register_callback(self.pump_detector.on_candle_update)

            # 2. StrategyManager nhận candles (backtest)
            self.market_socket.register_callback(self.strategy_manager.on_candle_update)

            # 3. BotManager nhận candles (production) - nếu có
            if self.bot_manager:
                self.market_socket.register_callback(self.bot_manager.on_candle_update)

            # 4. ✨ THÊM - Real bots nhận candles (price updates)
            if self.real_bots:
                self.market_socket.register_callback(self._on_candle_for_real_bots)

            # 5. Pump signal → StrategyManager (backtest) + BotManager (production)
            self.pump_detector.set_on_pump_detected(self.on_pump_signal_detected)

            self.log.i(self.tag, f"✅ Bot initialized successfully with {exchange_name}")

        except Exception as e:
            self.log.e(self.tag, f"❌ Error initializing bot: {e}\n{traceback.format_exc()}")
            await self.tele_message.send_message(f"❌ Bot initialization failed: {e}", self.chat_id)
            raise

    # ✨ THÊM METHOD - Initialize Real Trading
    async def _init_real_trading(self):
        """Initialize Real Trading từ config file"""
        if not REAL_TRADING_AVAILABLE:
            self.log.w(self.tag, "⚠️ Real trading modules not available")
            return

        try:
            # Check if config file exists
            if not os.path.exists(self.real_accounts_path):
                self.log.w(self.tag, f"⚠️ Real accounts config not found: {self.real_accounts_path}")
                return

            # Load configs
            self.config_loader = ConfigLoader(self.real_accounts_path)
            active_accounts = self.config_loader.get_active_accounts()

            if not active_accounts:
                self.log.w(self.tag, "⚠️ No active accounts in config")
                return

            self.log.i(self.tag, f"📥 Loading {len(active_accounts)} real account(s)...")

            # Create RealBotLive for each account
            for account in active_accounts:
                try:
                    real_bot = RealBotLive(
                        account_config=account,
                        db_manager=self.db_manager,
                        log=self.log,
                    )

                    # Start the bot
                    await real_bot.start()

                    self.real_bots.append(real_bot)

                    self.log.i(self.tag,
                               f"✅ Real bot started: {account.account_id} | "
                               f"Exchange: {account.exchange} | "
                               f"Chat: {account.chat_id}"
                               )

                except Exception as e:
                    self.log.e(self.tag, f"❌ Error starting real bot {account.account_id}: {e}")

            self.log.i(self.tag, f"✅ {len(self.real_bots)} real bot(s) started")

        except Exception as e:
            self.log.e(self.tag, f"Error initializing real trading: {e}")

    # ✨ THÊM METHOD - Forward candles to real bots
    async def _on_candle_for_real_bots(self, symbol: str, interval: str, candle: Dict):
        """Forward candle updates to real bots for TP/SL/Reduce checking"""
        try:
            price = candle.get('close', candle.get('c', 0))
            for real_bot in self.real_bots:
                await real_bot.on_price_update(symbol, price, candle)
        except Exception as e:
            self.log.e(self.tag, f"Error forwarding candle to real bots: {e}")

    def init_symbols(self) -> List[Symbol]:
        """
        Lấy danh sách symbols dựa trên exchange đang dùng
        Sử dụng SocketFactory để centralize logic
        """
        return SocketFactory.init_symbols(self.log)

    def configure_detector(self):
        """Cấu hình pump detector - ĐIỀU KIỆN DỄ để vào NHIỀU lệnh"""
        self.pump_detector.config = ExchangeConfig.PUMP_CONFIG.copy()
        self.pump_detector.config.update({
            'rsi_period': 14,
            'rsi_overbought': 50,
            'momentum_threshold': 1.0,
            'recent_pump_price_threshold': 10.0,
            'recent_pump_volume_threshold': 5.0,
        })

        # Cập nhật cooldown CỰC NGẮN
        self.pump_detector.pump_lookback_candles = 10
        self.pump_detector.pump_cooldown_seconds = 120

        self.log.i(self.tag, "⚙️  Pump Detector Config (EASY MODE - Many signals):")
        self.log.i(self.tag, f"   {json.dumps(self.pump_detector.config, indent=4)}")

    async def send_test_signal(self):
        """Gửi một test signal để verify logic"""
        try:
            self.log.i(self.tag, "\n" + "=" * 60)
            self.log.i(self.tag, "🧪 SENDING TEST SIGNAL to verify logic")
            self.log.i(self.tag, "=" * 60)

            test_signal = {
                'symbol': 'TEST_USDT',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'candle_timestamp': int(time.time()),
                'price': 100.0,
                'price_change_1m': 0.8,
                'price_change_5m': 1.5,
                'volume_ratio': 2.0,
                'volume_usdt': 50000,
                'rsi': 55,
                'momentum': 1.5,
                'buy_pressure': 70.0,
                'trend_strength': 0.5,
                'volume_consistency': 0.6,
                'is_breakout': False,
                'confidence': 50,
                'is_new_candle': True,
                'timeframe': '1m',
            }

            self.log.i(self.tag, f"Test signal: {json.dumps(test_signal, indent=2)}")

            # Send to strategy manager
            await self.on_pump_signal_detected(test_signal)

            # Wait a bit
            await asyncio.sleep(2)

            # Check results
            strategies_entered = sum(1 for s in self.strategy_manager.strategies if 'TEST_USDT' in s.active_positions)

            self.log.i(self.tag, "=" * 60)
            if strategies_entered > 0:
                self.log.i(self.tag, f"✅ TEST PASSED: {strategies_entered} strategies entered TEST signal")
            else:
                self.log.w(self.tag, "⚠️ TEST FAILED: NO strategies entered TEST signal")
                self.log.w(self.tag, "This means there's a problem with the matching logic!")
            self.log.i(self.tag, "=" * 60 + "\n")

        except Exception as e:
            self.log.e(self.tag, f"Error sending test signal: {e}")

    async def start(self):
        """Start bot"""
        try:
            # Start Telegram
            await self.tele_message.start()

            # Initialize
            await self.initialize()

            # Start WebSocket
            await self.market_socket.start(self.symbols)

            # Start monitoring tasks
            asyncio.create_task(self.periodic_report())
            asyncio.create_task(self.status_monitor())

            # ✨ THÊM - Start Config Auto Updater
            if self.config_updater:
                asyncio.create_task(self.config_updater.start())
                self.log.i(self.tag, "✅ Config Auto Updater started")

            exchange_name = ExchangeConfig.get_exchange_name()
            self.log.i(self.tag, f"✅ Bot is running with {exchange_name}!")

            # Build running message
            running_msg = (
                f"✅ Bot is running!\n"
                f"📊 Exchange: {exchange_name}\n"
                f"🔍 Detecting pumps and backtesting strategies..."
            )
            if self.real_bots:
                running_msg += f"\n🔴 Real bots: {len(self.real_bots)} active"

            await self.tele_message.send_message(running_msg, self.chat_id)

            # Keep running
            while True:
                await asyncio.sleep(60)

        except Exception as e:
            self.log.e(self.tag, f"❌ Bot crashed: {e}\n{traceback.format_exc()}")
            await self.tele_message.send_message(f"❌ Bot crashed: {e}", self.chat_id)

    async def on_pump_signal_detected(self, signal: Dict):
        """
        Callback khi PumpDetector phát hiện pump
        Gửi signal cho:
        1. StrategyManager (backtest)
        2. BotManager (production trading) - nếu có
        3. ✨ Real bots (real trading) - nếu có
        """
        try:
            symbol = signal['symbol']
            price = signal['price']
            confidence = signal['confidence']
            price_change_1m = signal.get('price_change_1m', 0)
            price_change_5m = signal.get('price_change_5m', 0)
            volume_ratio = signal.get('volume_ratio', 0)

            # ✨ THÊM - UPDATE PRICES FOR PNL TRACKING
            if hasattr(self, 'enhanced_bot_mgr') and self.enhanced_bot_mgr:
                self.enhanced_bot_mgr.update_price(symbol, price)

            if hasattr(self, 'enhanced_strat_mgr') and self.enhanced_strat_mgr:
                self.enhanced_strat_mgr.update_price(symbol, price)

            self.total_signals_detected += 1

            self.log.i(self.tag,
                       f"🚀 PUMP #{self.total_signals_detected}: {symbol} | "
                       f"Price: ${price:.6f} | 1m: +{price_change_1m:.2f}% | "
                       f"5m: +{price_change_5m:.2f}% | "
                       f"Vol: {volume_ratio:.1f}x | Conf: {confidence}%"
                       )

            # Gửi notification cho high confidence signals
            if confidence >= 70:
                await self.tele_message.send_message(
                    f"🚀 PUMP: {symbol}\n"
                    f"💰 ${price:.6f} (+{price_change_1m:.2f}%)\n"
                    f"🔥 Confidence: {confidence}%",
                    self.chat_id
                )

            # 1. Gửi cho StrategyManager (backtest)
            signal_1m = signal.copy()
            signal_1m['timeframe'] = '1m'
            await self.strategy_manager.on_pump_signal(signal_1m)

            if price_change_5m > 0:
                signal_5m = signal.copy()
                signal_5m['timeframe'] = '5m'
                await self.strategy_manager.on_pump_signal(signal_5m)

            # 2. Gửi cho BotManager (production trading) - nếu có
            if self.bot_manager:
                await self.bot_manager.on_signal(signal)

            # 3. ✨ THÊM - Gửi cho Real bots
            for real_bot in self.real_bots:
                await real_bot.on_pump_signal(signal)

            # DEBUG: Log progress
            if self.total_signals_detected % 10 == 0:
                # Backtest stats
                strategies_with_positions = sum(
                    1 for s in self.strategy_manager.strategies if len(s.active_positions) > 0)
                total_positions = sum(len(s.active_positions) for s in self.strategy_manager.strategies)

                log_msg = (
                    f"📊 After {self.total_signals_detected} signals:\n"
                    f"   Backtest: {strategies_with_positions} strategies, {total_positions} positions"
                )

                # Production stats - nếu có
                if self.bot_manager:
                    bot_stats = self.bot_manager.get_stats()
                    log_msg += f"\n   Production: {bot_stats['total_bots']} bots, {bot_stats['total_trades']} trades"

                # ✨ THÊM - Real bots stats
                if self.real_bots:
                    total_real_trades = sum(b.total_trades for b in self.real_bots)
                    total_real_pnl = sum(b.total_pnl for b in self.real_bots)
                    log_msg += f"\n   Real: {len(self.real_bots)} bots, {total_real_trades} trades, ${total_real_pnl:.2f}"

                self.log.i(self.tag, log_msg)

        except Exception as e:
            self.log.e(self.tag, f"Error handling pump signal: {e}\n{traceback.format_exc()}")

    async def periodic_report(self):
        """Report kết quả strategies định kỳ"""
        while True:
            try:
                # Report mỗi 1 giờ
                await asyncio.sleep(3600)

                self.log.i(self.tag, "📊 Generating periodic strategy report...")
                await self.strategy_manager.report_results()

                # ✨ THÊM - Report real bots stats
                if self.real_bots:
                    await self._report_real_bots_stats()

            except Exception as e:
                self.log.e(self.tag, f"Error in periodic report: {e}")

    # ✨ THÊM METHOD - Report real bots stats
    async def _report_real_bots_stats(self):
        """Report stats của real bots"""
        try:
            if not self.real_bots:
                return

            total_trades = sum(b.total_trades for b in self.real_bots)
            total_pnl = sum(b.total_pnl for b in self.real_bots)
            total_winning = sum(b.winning_trades for b in self.real_bots)
            win_rate = (total_winning / total_trades * 100) if total_trades > 0 else 0

            message = (
                f"🔴 REAL BOTS HOURLY REPORT\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Bots: {len(self.real_bots)}\n"
                f"Total Trades: {total_trades}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Total PnL: ${total_pnl:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
            )

            for bot in self.real_bots:
                stats = bot.get_stats()
                message += (
                    f"• {stats['account_id']}: "
                    f"{stats['total_trades']}T | "
                    f"{stats['win_rate']:.1f}% | "
                    f"${stats['total_pnl']:.2f}\n"
                )

            self.log.i(self.tag, message)

        except Exception as e:
            self.log.e(self.tag, f"Error reporting real bots: {e}")

    async def status_monitor(self):
        """Monitor và report status bot"""
        while True:
            try:
                await asyncio.sleep(1800)  # Mỗi 30 phút

                runtime = datetime.now() - self.start_time
                hours = runtime.total_seconds() / 3600

                exchange_name = ExchangeConfig.get_exchange_name()

                # Get quick stats
                total_strategies = len(self.strategy_manager.strategies)
                strategies_with_positions = sum(
                    1 for s in self.strategy_manager.strategies if len(s.active_positions) > 0)
                strategies_with_trades = sum(1 for s in self.strategy_manager.strategies if s.stats['total_trades'] > 0)
                total_open_positions = sum(len(s.active_positions) for s in self.strategy_manager.strategies)
                total_completed_trades = sum(s.stats['total_trades'] for s in self.strategy_manager.strategies)

                message = (
                    f"📊 STATUS UPDATE ({exchange_name})\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⏱️ Runtime: {hours:.1f}h\n"
                    f"🔍 Signals Detected: {self.total_signals_detected}\n"
                    f"📈 Strategies Testing: {strategies_with_positions}/{total_strategies}\n"
                    f"✅ Strategies w/ Trades: {strategies_with_trades}/{total_strategies}\n"
                    f"💼 Open Positions: {total_open_positions}\n"
                    f"📊 Completed Trades: {total_completed_trades}\n"
                    f"💰 Symbols Monitored: {len(self.symbols)}"
                )

                # ✨ THÊM - Real bots info
                if self.real_bots:
                    total_real_pnl = sum(b.total_pnl for b in self.real_bots)
                    active_real_positions = sum(len(b.active_positions) for b in self.real_bots)
                    message += f"\n🔴 Real Bots: {len(self.real_bots)} | ${total_real_pnl:.2f}"

                self.log.i(self.tag, message)
                await self.tele_message.send_message(message, self.chat_id)

            except Exception as e:
                self.log.e(self.tag, f"Error in status monitor: {e}")

    async def get_best_strategy(self) -> Dict:
        """Lấy strategy tốt nhất hiện tại"""
        try:
            self.strategy_manager.calculate_rankings()
            if self.strategy_manager.best_strategy:
                return self.strategy_manager.best_strategy.get_summary()
            return None
        except Exception as e:
            self.log.e(self.tag, f"Error getting best strategy: {e}")
            return None

    async def create_production_bots(self, top_n: int = 5, mode: str = 'SIMULATED'):
        """
        Tạo production bots từ top backtest results
        Command để gọi thủ công hoặc tự động
        """
        try:
            if not self.bot_manager:
                self.log.e(self.tag, "❌ BotManager not available. Cannot create production bots.")
                return

            from x1.bot.database.database_models import TradeModeEnum

            trade_mode = TradeModeEnum.REAL if mode.upper() == 'REAL' else TradeModeEnum.SIMULATED

            self.log.i(self.tag, f"🤖 Creating {top_n} production bots in {mode} mode...")

            await self.bot_manager.create_bots_from_backtest(top_n=top_n, mode=trade_mode)

            bot_stats = self.bot_manager.get_stats()

            self.log.i(self.tag,
                       f"✅ Production bots created! "
                       f"Total: {bot_stats['total_bots']} "
                       f"({bot_stats['real_bots']} REAL, {bot_stats['simulated_bots']} SIM)"
                       )

        except Exception as e:
            self.log.e(self.tag, f"Error creating production bots: {e}\n{traceback.format_exc()}")

    # ✨ THÊM - CONFIG UPDATER CONTROL METHODS

    async def force_update_bot_configs(self):
        """
        Force update bot configs ngay lập tức
        Gọi từ command hoặc API
        """
        if not self.config_updater:
            self.log.e(self.tag, "❌ Config updater not available")
            return None

        self.log.i(self.tag, "⚡ Force updating bot configs...")
        result = await self.config_updater.force_update()

        await self.tele_message.send_message(
            f"⚡ Force update completed!\n"
            f"Updated: {result['updated_count']}, Created: {result['created_count']}",
            self.chat_id
        )

        return result

    def set_config_update_interval(self, hours: float):
        """
        Set interval cho auto update (in hours)

        Args:
            hours: Số giờ giữa mỗi lần update (ví dụ: 1, 2, 0.5 cho 30 phút)
        """
        if not self.config_updater:
            self.log.e(self.tag, "❌ Config updater not available")
            return

        self.config_updater.set_update_interval_hours(hours)
        self.log.i(self.tag, f"✅ Config update interval set to {hours}h")

    def get_config_updater_stats(self) -> Dict:
        """Lấy thống kê của config updater"""
        if not self.config_updater:
            return {'error': 'Config updater not available'}

        return self.config_updater.get_stats()

    # ✨ THÊM - REAL TRADING CONTROL METHODS

    async def add_real_account(self, account_data: Dict):
        """
        Thêm real account runtime (không cần restart bot)

        Args:
            account_data: Dict chứa thông tin account
                {
                    'account_id': 'bot_003',
                    'api_key': 'xxx',
                    'secret_key': 'xxx',
                    'chat_id': '@channel',
                    'position_size_usdt': 15,
                    'leverage': 10,
                }
        """
        if not REAL_TRADING_AVAILABLE:
            self.log.e(self.tag, "❌ Real trading modules not available")
            return None

        try:
            if not self.config_loader:
                self.config_loader = ConfigLoader()

            account = self.config_loader.add_account_from_dict(account_data)

            real_bot = RealBotLive(
                account_config=account,
                db_manager=self.db_manager,
                log=self.log,
            )

            await real_bot.start()
            self.real_bots.append(real_bot)

            self.log.i(self.tag, f"✅ Added real bot: {account.account_id}")

            await self.tele_message.send_message(
                f"✅ Real bot added: {account.account_id}\n"
                f"Exchange: {account.exchange}\n"
                f"Chat: {account.chat_id}",
                self.chat_id
            )

            return account

        except Exception as e:
            self.log.e(self.tag, f"Error adding real account: {e}")
            return None

    async def remove_real_account(self, account_id: str) -> bool:
        """Remove real account by ID"""
        try:
            for i, bot in enumerate(self.real_bots):
                if bot.account_config.account_id == account_id:
                    await bot.stop()
                    self.real_bots.pop(i)
                    self.log.i(self.tag, f"✅ Removed real bot: {account_id}")
                    return True

            self.log.w(self.tag, f"Real bot not found: {account_id}")
            return False

        except Exception as e:
            self.log.e(self.tag, f"Error removing real account: {e}")
            return False

    def get_real_bot_stats(self) -> List[Dict]:
        """Get stats của tất cả real bots"""
        return [bot.get_stats() for bot in self.real_bots]

    async def stop(self):
        """Stop bot gracefully"""
        try:
            self.log.i(self.tag, "🛑 Stopping bot...")

            # Stop real bots
            for real_bot in self.real_bots:
                await real_bot.stop()

            # Stop config updater
            if self.config_updater:
                self.config_updater.stop()

            self.log.i(self.tag, "✅ Bot stopped")

        except Exception as e:
            self.log.e(self.tag, f"Error stopping bot: {e}")


# ===== ENTRY POINT =====

async def main():
    """Main entry point"""

    # Create bot (không cần API key cho backtest mode)
    # Có thể truyền path đến config file cho real trading
    bot = MexcPumpBot(
        real_accounts_path="config/real_accounts.json"  # Optional
    )

    try:
        # Start bot
        await bot.start()

    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
        await bot.stop()


if __name__ == "__main__":
    try:
        exchange_name = ExchangeConfig.get_exchange_name()
        print("=" * 50)
        print(f"🚀 Pump Bot - Strategy Backtesting Mode")
        print(f"📊 Exchange: {exchange_name}")
        print("=" * 50)
        print("📊 This bot will:")
        print(f"  1. Monitor {exchange_name} perpetual contracts")
        print("  2. Detect pump signals in real-time")
        print("  3. Backtest strategies simultaneously")
        print("  4. Report best strategies every hour")
        print("  5. Trade with real bots (if configured)")
        print("=" * 50)
        print()

        asyncio.run(main())

    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        traceback.print_exc()