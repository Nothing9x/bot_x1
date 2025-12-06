# MEXC Pump Trading Bot

Hệ thống trading tự động với 3 layers: Backtest → Simulated → Real Trading

## 📁 Cấu trúc Project

```
Mexc_Bot/x1/
├── run_bot.py                      # Script chạy bot (chạy file này)
├── x1/
│   └── bot/
│       ├── mexc_pump_bot.py        # Main bot
│       ├── ai/
│       │   ├── pump_detector.py    # Phát hiện pump
│       │   └── strategy_manager.py # Backtest strategies
│       ├── database/               # (Optional - cho Production Trading)
│       │   └── database_models.py  # Database schema
│       └── trading/                # (Optional - cho Production Trading)
│           ├── trading_bot.py      # Trading bot class
│           └── bot_manager.py      # Bot manager
```

## 🚀 Quick Start

### 1. Chạy Backtest Only (Không cần setup gì thêm)

```bash
cd ~/WORKSPACE/GIT/My/Mexc_Bot/x1
python run_bot.py
```

Bot sẽ:
- ✅ Phát hiện pump signals
- ✅ Test 100 strategies (50 LONG + 50 SHORT)
- ✅ Report kết quả mỗi 1 giờ
- ❌ Không trade thật

### 2. Chạy Full System (Backtest + Production Trading)

#### Step 1: Tạo các file cần thiết

Tạo folder structure:
```bash
mkdir -p x1/bot/database
mkdir -p x1/bot/trading
touch x1/bot/database/__init__.py
touch x1/bot/trading/__init__.py
```

#### Step 2: Copy code từ artifacts

**File 1: `x1/bot/database/database_models.py`**
- Copy toàn bộ code từ artifact "Database Schema & Models"

**File 2: `x1/bot/trading/trading_bot.py`**
- Copy toàn bộ code từ artifact "TradingBot Class"

**File 3: `x1/bot/trading/bot_manager.py`**
- Copy toàn bộ code từ artifact "BotManager - Quản lý nhiều Bots"

#### Step 3: Fix imports

Trong mỗi file vừa tạo, sửa imports:

```python
# Trong database_models.py - không cần sửa gì

# Trong trading_bot.py - sửa dòng import:
from x1.bot.database.database_models import (...)

# Trong bot_manager.py - sửa dòng import:
from x1.bot.database.database_models import (...)
from x1.bot.trading.trading_bot import TradingBot
```

#### Step 4: Install dependencies

```bash
pip install sqlalchemy
```

#### Step 5: Run

```bash
python run_bot.py
```

Bot sẽ:
- ✅ Phát hiện pump signals
- ✅ Test 100 strategies (backtest)
- ✅ Trade giả lập với config tốt nhất
- ✅ Tự động promote sang REAL nếu profitable

## 📊 Monitoring

### Xem logs realtime

```bash
tail -f logs/main.log
```

### Query database

```python
from x1.bot.database.database_models import DatabaseManager, BotConfig, Trade

db = DatabaseManager()
session = db.get_session()

# Xem tất cả bots
bots = session.query(BotConfig).all()
for bot in bots:
    print(f"{bot.name}: {bot.total_trades} trades, ${bot.total_pnl:.2f}")

# Xem trades gần đây
trades = session.query(Trade).order_by(Trade.created_at.desc()).limit(10).all()
for trade in trades:
    print(f"{trade.symbol}: {trade.pnl_usdt:.2f}")
```

### Tạo production bots từ backtest

Sau khi bot chạy 12-24h và có backtest results:

```python
# Trong Python console hoặc script
import asyncio
from x1.bot.mexc_pump_bot import MexcPumpBot

async def create_bots():
    bot = MexcPumpBot()
    await bot.initialize()
    
    # Tạo 5 bots từ top 5 strategies (SIMULATED mode)
    await bot.create_production_bots(top_n=5, mode='SIMULATED')

asyncio.run(create_bots())
```

## 🔧 Configuration

### Pump Detector Settings

Sửa trong `mexc_pump_bot.py` → `configure_detector()`:

```python
self.pump_detector.config = {
    'price_increase_1m': 0.5,       # % tăng trong 1 phút
    'volume_spike_multiplier': 1.5, # Volume tăng bao nhiêu lần
    'min_volume_usdt': 100,         # Volume tối thiểu
    'min_confidence': 40,           # Confidence tối thiểu
}
```

### Strategy Generation

Sửa trong `mexc_pump_bot.py` → `initialize()`:

```python
# Số lượng strategies
num_strategies = 100  # Có thể tăng lên 200, 500...

# Parameter ranges trong strategy_manager.py → generate_strategies()
```

### Bot Manager Settings

Sửa trong `bot_manager.py` → `__init__()`:

```python
self.config = {
    'max_bots': 10,                    # Số bots tối đa
    'min_trades_for_promotion': 20,    # Trades tối thiểu để promote
    'min_win_rate_for_promotion': 60,  # Win rate tối thiểu
    'min_profit_factor': 1.5,          # Profit factor tối thiểu
}
```

## 🐛 Troubleshooting

### Lỗi: `ModuleNotFoundError: No module named 'x1'`

**Fix:**
```bash
# Đảm bảo chạy từ đúng folder
cd ~/WORKSPACE/GIT/My/Mexc_Bot/x1
python run_bot.py
```

### Lỗi: `SyntaxError: Non-ASCII character`

**Fix:** Thêm vào đầu file Python:
```python
# -*- coding: utf-8 -*-
```

### Lỗi: `'MexcPumpBot' object has no attribute 'bot_manager'`

**Fix:** Bot đang chạy ở BACKTEST-ONLY mode (không có database modules). Không ảnh hưởng tới backtest.

Nếu muốn full system, tạo các file database_models.py, trading_bot.py, bot_manager.py theo hướng dẫn trên.

### Bot không phát hiện pump

**Check:**
1. WebSocket có kết nối không? → Xem log "Connected to MEXC WebSocket"
2. Có nhận candle data không? → Xem log "New candle added"
3. Threshold quá cao → Giảm `price_increase_1m`, `volume_spike_multiplier` trong config

### Strategies không vào lệnh

**Check:**
1. Xem debug logs để biết lý do fail
2. Giảm thresholds trong `generate_strategies()`
3. Xem `min_confidence`, `volume_multiplier`, `rsi_threshold`

## 📈 Performance Tips

1. **Backtest Period:** Chạy ít nhất 24-48h để có đủ data
2. **Number of Strategies:** 100-200 strategies là optimal
3. **Simulated Period:** Test SIM ít nhất 50 trades trước khi promote REAL
4. **Monitor:** Check database mỗi ngày để tracking performance

## ⚠️ Warnings

1. **REAL Trading = Real Money:** Chỉ promote sang REAL khi đã test kỹ
2. **Start Small:** Bắt đầu với position size nhỏ (10-50 USDT)
3. **Monitor 24/7:** Sử dụng VPS nếu muốn chạy liên tục
4. **Backup Database:** Backup file `mexc_trading_bot.db` thường xuyên

## 📞 Support

- Telegram: @xbot_x1
- Bot sẽ gửi notification qua Telegram channel

## 📝 License

Private use only.