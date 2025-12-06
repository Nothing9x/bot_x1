import asyncio
import time
import traceback
from datetime import datetime
from typing import List, Dict
import json

import requests

from x1.bot.ai.pump_detector import PumpDetector
from x1.bot.market.mexc_socket import MexcSocket
from x1.bot.model.symbol import Symbol
from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.ai.strategy_manager import StrategyManager
from x1.bot.utils import Utils
from x1.bot.utils.Log import Log
from x1.bot.utils.black_list_symbol import BLACK_LIST_SYMBOL


class MexcPumpBot:
    """
    Bot phát hiện pump và backtest strategies để tìm config tốt nhất
    """

    def __init__(self, api_key: str = None, api_secret: str = None, proxy=None):

        bot_token = "7519046021:AAER7iFwU2akFBZp111qCyZwBak_2NrT2lw"
        self.admin_proxy = "s5witiaf:rZPT4s9E@103.182.19.82:49295"

        self.tag = "MexcPumpBot"
        self.chat_id = "@xbot_x1"

        self.log = Log().init('main', 'DEBUG')

        # Setup Telegram notification
        self.tele_message = TelegramMessageQueue(log=self.log, bot_token=bot_token)

        # Setup WebSocket
        self.mexc_socket = MexcSocket(self.log, self.admin_proxy, self.tele_message, self.chat_id)

        # Setup Pump Detector
        self.pump_detector = PumpDetector(self.log, self.tele_message, self.chat_id)

        # Setup Strategy Manager (MAIN FEATURE)
        self.strategy_manager = StrategyManager(self.log, self.tele_message, self.chat_id)

        # Symbols to monitor
        self.symbols: List[Symbol] = []

        # Stats tracking
        self.start_time = None
        self.total_signals_detected = 0

    async def initialize(self):
        """Khởi tạo bot"""
        try:
            self.log.i(self.tag, "🚀 Initializing MEXC Pump Bot with Strategy Backtesting...")

            self.start_time = datetime.now()

            # Send startup message
            await self.tele_message.send_message(
                f"🤖 MEXC Pump Bot Starting (Backtest Mode)...\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━",
                self.chat_id
            )

            # Load symbols to monitor
            self.symbols = self.init_symbols()
            self.log.i(self.tag, f"✅ Loaded {len(self.symbols)} symbols")

            # Configure pump detector
            self.configure_detector()

            # Generate strategies for backtesting
            num_strategies = 100000  # Có thể điều chỉnh
            self.strategy_manager.generate_strategies(max_strategies=num_strategies)

            await self.tele_message.send_message(
                f"✅ Generated {num_strategies} strategies for backtesting\n"
                f"Monitoring {len(self.symbols)} symbols",
                self.chat_id
            )

            # Setup callbacks
            # Callback 1: PumpDetector nhận candles để phát hiện pump
            self.mexc_socket.register_callback(self.pump_detector.on_candle_update)

            # Callback 2: StrategyManager nhận candles để check TP/SL
            self.mexc_socket.register_callback(self.strategy_manager.on_candle_update)

            # Callback 3: Khi phát hiện pump, gửi signal cho strategy manager
            self.pump_detector.set_on_pump_detected(self.on_pump_signal_detected)

            self.log.i(self.tag, "✅ Bot initialized successfully")

        except Exception as e:
            self.log.e(self.tag, f"❌ Error initializing bot: {e}\n{traceback.format_exc()}")
            await self.tele_message.send_message(f"❌ Bot initialization failed: {e}", self.chat_id)
            raise

    def init_symbols(self):
        """Load danh sách symbols từ MEXC"""
        symbols: list[Symbol] = []

        try:
            url = "https://contract.mexc.com/api/v1/contract/detail"
            response = requests.get(url, proxies=Utils.get_proxies(self.admin_proxy), timeout=10)
            data = response.json()

            cur_time = int(time.time() * 1000)
            for d in data["data"]:
                if cur_time < d["openingTime"]:
                    continue
                if not d["symbol"].endswith("_USDT"):
                    continue
                if d['symbol'] in BLACK_LIST_SYMBOL:
                    self.log.d(self.tag, f"Symbol {d['symbol']} in black list, skipped")
                    continue

                symbols.append(Symbol(
                    d['symbol'],
                    d["priceScale"],
                    d["contractSize"],
                    d["maxVol"],
                    d["maxLeverage"]
                ))

            self.log.i(self.tag, f"✅ Loaded {len(symbols)} valid USDT symbols")

        except Exception as e:
            self.log.e(self.tag, f"Error loading symbols: {e}\n{traceback.format_exc()}")

        return symbols

    def configure_detector(self):
        """Cấu hình pump detector - dùng config rộng để bắt nhiều signals"""
        self.pump_detector.config = {
            'price_increase_1m': 2.0,  # Giảm threshold để bắt nhiều tín hiệu hơn
            'price_increase_5m': 5.0,  # Giảm threshold
            'volume_spike_multiplier': 2.0,  # Giảm để bắt nhiều signals
            'min_volume_usdt': 500,  # Volume tối thiểu thấp
            'rsi_period': 14,
            'rsi_overbought': 60,  # Giảm threshold
            'momentum_threshold': 1.5,  # Giảm threshold
            'min_confidence': 60,  # Giảm để bắt nhiều signals

            # Phát hiện pump cũ (để không bắt lại)
            'recent_pump_price_threshold': 5.0,  # Nếu đã tăng >5% trong 20 nến gần đây
            'recent_pump_volume_threshold': 3.0,  # Nếu đã có volume spike >3x
        }

        # Cập nhật lookback và cooldown
        self.pump_detector.pump_lookback_candles = 20  # Kiểm tra 20 nến gần đây
        self.pump_detector.pump_cooldown_seconds = 600  # Cooldown 10 phút

        self.log.i(self.tag, "⚙️  Pump Detector Config (Relaxed for more signals):")
        self.log.i(self.tag, f"   {json.dumps(self.pump_detector.config, indent=4)}")

    async def start(self):
        """Start bot"""
        try:
            # Start Telegram
            await self.tele_message.start()

            # Initialize
            await self.initialize()

            # Start WebSocket
            await self.mexc_socket.start(self.symbols)

            # Start monitoring tasks
            asyncio.create_task(self.periodic_report())
            asyncio.create_task(self.status_monitor())

            self.log.i(self.tag, "✅ Bot is running in backtest mode!")
            await self.tele_message.send_message(
                "✅ Bot is running!\n"
                "🔍 Detecting pumps and backtesting strategies...",
                self.chat_id
            )

            # Keep running
            while True:
                await asyncio.sleep(60)

        except Exception as e:
            self.log.e(self.tag, f"❌ Bot crashed: {e}\n{traceback.format_exc()}")
            await self.tele_message.send_message(f"❌ Bot crashed: {e}", self.chat_id)

    async def on_pump_signal_detected(self, signal: Dict):
        """
        Callback khi PumpDetector phát hiện pump
        Gửi signal cho StrategyManager để test
        """
        try:
            symbol = signal['symbol']
            price = signal['price']
            confidence = signal['confidence']
            price_change_1m = signal.get('price_change_1m', 0)
            price_change_5m = signal.get('price_change_5m', 0)
            volume_ratio = signal.get('volume_ratio', 0)

            self.total_signals_detected += 1

            self.log.i(self.tag,
                       f"🚀 PUMP DETECTED #{self.total_signals_detected}: {symbol} | "
                       f"Price: ${price:.6f} | Change 1m: +{price_change_1m:.2f}% | "
                       f"Change 5m: +{price_change_5m:.2f}% | "
                       f"Volume: {volume_ratio:.1f}x | "
                       f"Confidence: {confidence}%"
                       )

            # Gửi notification cho high confidence signals
            if confidence >= 80:
                await self.tele_message.send_message(
                    f"🚀 PUMP: {symbol}\n"
                    f"💰 ${price:.6f} (+{price_change_1m:.2f}%)\n"
                    f"🔥 Confidence: {confidence}%",
                    self.chat_id
                )

            # Tạo signals cho cả 1m và 5m timeframes
            # Signal cho 1m timeframe
            signal_1m = signal.copy()
            signal_1m['timeframe'] = '1m'
            await self.strategy_manager.on_pump_signal(signal_1m)

            # Signal cho 5m timeframe (nếu có đủ data)
            if price_change_5m > 0:
                signal_5m = signal.copy()
                signal_5m['timeframe'] = '5m'
                await self.strategy_manager.on_pump_signal(signal_5m)

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

            except Exception as e:
                self.log.e(self.tag, f"Error in periodic report: {e}")

    async def status_monitor(self):
        """Monitor và report status bot"""
        while True:
            try:
                await asyncio.sleep(1800)  # Mỗi 30 phút

                runtime = datetime.now() - self.start_time
                hours = runtime.total_seconds() / 3600

                # Get quick stats
                total_strategies = len(self.strategy_manager.strategies)
                strategies_with_positions = sum(
                    1 for s in self.strategy_manager.strategies if len(s.active_positions) > 0)
                strategies_with_trades = sum(1 for s in self.strategy_manager.strategies if s.stats['total_trades'] > 0)
                total_open_positions = sum(len(s.active_positions) for s in self.strategy_manager.strategies)
                total_completed_trades = sum(s.stats['total_trades'] for s in self.strategy_manager.strategies)

                message = (
                    f"📊 STATUS UPDATE\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⏱️ Runtime: {hours:.1f}h\n"
                    f"🔍 Signals Detected: {self.total_signals_detected}\n"
                    f"📈 Strategies Testing: {strategies_with_positions}/{total_strategies}\n"
                    f"✅ Strategies w/ Trades: {strategies_with_trades}/{total_strategies}\n"
                    f"💼 Open Positions: {total_open_positions}\n"
                    f"📊 Completed Trades: {total_completed_trades}\n"
                    f"💰 Symbols Monitored: {len(self.symbols)}"
                )

                self.log.i(self.tag, message)

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

    async def export_results(self):
        """Export kết quả ra file"""
        try:
            self.log.i(self.tag, "💾 Exporting results...")
            self.strategy_manager.save_results_to_file()
            await self.tele_message.send_message(
                "💾 Strategy results exported to file",
                self.chat_id
            )
        except Exception as e:
            self.log.e(self.tag, f"Error exporting results: {e}")


# ===== ENTRY POINT =====

async def main():
    """Main entry point"""

    # Create bot (không cần API key cho backtest mode)
    bot = MexcPumpBot()

    # Start bot
    await bot.start()


if __name__ == "__main__":
    try:
        print("=" * 50)
        print("🚀 MEXC Pump Bot - Strategy Backtesting Mode")
        print("=" * 50)
        print("📊 This bot will:")
        print("  1. Monitor MEXC perpetual contracts")
        print("  2. Detect pump signals in real-time")
        print("  3. Backtest 100+ strategies simultaneously")
        print("  4. Report best strategies every hour")
        print("=" * 50)
        print()

        asyncio.run(main())

    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        traceback.print_exc()