#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script để chạy MEXC Pump Bot
Đặt file này ở root folder: Mexc_Bot/x1/
Chạy: python run_bot.py
"""

import sys
import os

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import traceback
from x1.bot.mexc_pump_bot import MexcPumpBot


async def main():
    """Main entry point"""

    print("=" * 70)
    print("🚀 MEXC PUMP BOT - Strategy Backtesting & Production Trading")
    print("=" * 70)

    # Config
    API_KEY = None  # Nếu muốn trade REAL, điền API key
    API_SECRET = None  # Nếu muốn trade REAL, điền API secret

    print("\n📋 Configuration:")
    print(f"  API Key: {'✅ Set' if API_KEY else '❌ Not set (Backtest only)'}")
    print(f"  Mode: {'Full System' if API_KEY else 'Backtest Only'}")
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