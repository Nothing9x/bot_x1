#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script để chạy Pump Bot
- Hỗ trợ cả MEXC và Gate.io
- Chuyển đổi exchange trong exchange_config.py

Đặt file này ở root folder: Mexc_Bot/x1/
Chạy: python run_bot.py
"""

import sys
import os

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import traceback

# Import config để hiển thị exchange đang dùng
from x1.bot.config.exchange_config import ExchangeConfig

# Import bot từ pump_bot.py (hỗ trợ cả MEXC và Gate)
from x1.bot.pump_bot import MexcPumpBot


async def main():
    """Main entry point"""

    exchange_name = ExchangeConfig.get_exchange_name()

    print("=" * 70)
    print(f"🚀 PUMP BOT - Strategy Backtesting & Production Trading")
    print(f"📊 Exchange: {exchange_name}")
    print("=" * 70)

    # Config
    API_KEY = None  # Nếu muốn trade REAL, điền API key
    API_SECRET = None  # Nếu muốn trade REAL, điền API secret

    print("\n📋 Configuration:")
    print(f"  Exchange: {exchange_name}")
    print(f"  API Key: {'✅ Set' if API_KEY else '❌ Not set (Backtest only)'}")
    print(f"  Mode: {'Full System' if API_KEY else 'Backtest Only'}")

    if ExchangeConfig.is_gate():
        print(f"  Gate Testnet: {'✅ Yes' if ExchangeConfig.GATE_TESTNET else '❌ No (Mainnet)'}")

    print()

    # Create bot
    try:
        bot = MexcPumpBot(api_key=API_KEY, api_secret=API_SECRET)

        # Start bot
        await bot.start()

    except KeyboardInterrupt:
        print("\n\n👋 Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Bot crashed: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    # Run bot
    asyncio.run(main())