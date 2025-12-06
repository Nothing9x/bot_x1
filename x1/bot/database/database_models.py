"""
Database models cho MEXC Trading Bot System
Using SQLAlchemy ORM
"""

from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import json
import enum

Base = declarative_base()


class TradeModeEnum(enum.Enum):
    REAL = "REAL"
    SIMULATED = "SIMULATED"


class DirectionEnum(enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderStatusEnum(enum.Enum):
    PENDING = "PENDING"  # Đang chờ khớp
    FILLED = "FILLED"  # Đã khớp
    CANCELLED = "CANCELLED"  # Đã hủy
    REJECTED = "REJECTED"  # Bị từ chối


class TradeStatusEnum(enum.Enum):
    OPEN = "OPEN"  # Đang mở
    CLOSED = "CLOSED"  # Đã đóng
    CANCELLED = "CANCELLED"  # Đã hủy


class BotConfig(Base):
    """Config cho mỗi bot instance"""
    __tablename__ = 'bot_configs'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)

    # Trading parameters
    direction = Column(Enum(DirectionEnum), nullable=False)
    take_profit = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    position_size_usdt = Column(Float, nullable=False)

    # Entry conditions
    price_increase_threshold = Column(Float, nullable=False)
    volume_multiplier = Column(Float, nullable=False)
    rsi_threshold = Column(Float, nullable=False)
    min_confidence = Column(Float, nullable=False)

    # Advanced settings
    trailing_stop = Column(Boolean, default=False)
    min_trend_strength = Column(Float, default=0.0)
    require_breakout = Column(Boolean, default=False)
    min_volume_consistency = Column(Float, default=0.0)
    timeframe = Column(String(10), default='1m')

    # Mode
    trade_mode = Column(Enum(TradeModeEnum), nullable=False, default=TradeModeEnum.SIMULATED)
    is_active = Column(Boolean, default=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Performance stats
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)

    # Source (from backtest strategy ID)
    source_strategy_id = Column(Integer, nullable=True)

    # Relationships
    trades = relationship("Trade", back_populates="bot_config", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'direction': self.direction.value,
            'take_profit': self.take_profit,
            'stop_loss': self.stop_loss,
            'position_size_usdt': self.position_size_usdt,
            'price_increase_threshold': self.price_increase_threshold,
            'volume_multiplier': self.volume_multiplier,
            'rsi_threshold': self.rsi_threshold,
            'min_confidence': self.min_confidence,
            'trailing_stop': self.trailing_stop,
            'trade_mode': self.trade_mode.value,
            'is_active': self.is_active,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'total_pnl': self.total_pnl,
            'win_rate': self.win_rate,
        }


class Trade(Base):
    """Trade record - cả real và simulated"""
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True)

    # Bot reference
    bot_config_id = Column(Integer, ForeignKey('bot_configs.id'), nullable=False)
    bot_config = relationship("BotConfig", back_populates="trades")

    # Trade info
    symbol = Column(String(50), nullable=False)
    direction = Column(Enum(DirectionEnum), nullable=False)
    trade_mode = Column(Enum(TradeModeEnum), nullable=False)

    # Entry
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    quantity = Column(Float, nullable=False)

    # Exit
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)

    # Targets
    take_profit = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)

    # Tracking
    highest_price = Column(Float, nullable=True)
    lowest_price = Column(Float, nullable=True)

    # Result
    pnl_usdt = Column(Float, nullable=True)
    pnl_percent = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)  # TP, SL, MANUAL, CANCELLED

    # Status
    status = Column(Enum(TradeStatusEnum), nullable=False, default=TradeStatusEnum.OPEN)

    # Signal data (JSON)
    signal_data = Column(Text, nullable=True)  # Store full signal as JSON

    # Metadata
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationship
    orders = relationship("Order", back_populates="trade", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id,
            'bot_config_id': self.bot_config_id,
            'symbol': self.symbol,
            'direction': self.direction.value,
            'trade_mode': self.trade_mode.value,
            'entry_price': self.entry_price,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'exit_price': self.exit_price,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'quantity': self.quantity,
            'take_profit': self.take_profit,
            'stop_loss': self.stop_loss,
            'pnl_usdt': self.pnl_usdt,
            'pnl_percent': self.pnl_percent,
            'exit_reason': self.exit_reason,
            'status': self.status.value,
        }


class Order(Base):
    """Order record - chi tiết lệnh (cho REAL mode)"""
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True)

    # Trade reference
    trade_id = Column(Integer, ForeignKey('trades.id'), nullable=False)
    trade = relationship("Trade", back_populates="orders")

    # Order info
    exchange_order_id = Column(String(100), nullable=True)  # ID từ exchange
    symbol = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)  # BUY, SELL
    order_type = Column(String(20), nullable=False)  # MARKET, LIMIT

    # Price & Quantity
    price = Column(Float, nullable=True)  # Null for MARKET orders
    quantity = Column(Float, nullable=False)
    filled_quantity = Column(Float, default=0.0)
    avg_fill_price = Column(Float, nullable=True)

    # Status
    status = Column(Enum(OrderStatusEnum), nullable=False, default=OrderStatusEnum.PENDING)

    # Timing
    created_at = Column(DateTime, default=datetime.now)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    # Error info
    error_message = Column(Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'trade_id': self.trade_id,
            'exchange_order_id': self.exchange_order_id,
            'symbol': self.symbol,
            'side': self.side,
            'order_type': self.order_type,
            'price': self.price,
            'quantity': self.quantity,
            'filled_quantity': self.filled_quantity,
            'avg_fill_price': self.avg_fill_price,
            'status': self.status.value,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class BacktestResult(Base):
    """Kết quả backtest"""
    __tablename__ = 'backtest_results'

    id = Column(Integer, primary_key=True)

    # Strategy info
    strategy_id = Column(Integer, nullable=False)
    strategy_name = Column(String(200), nullable=False)

    # Config (JSON)
    config_json = Column(Text, nullable=False)

    # Results
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)

    total_pnl = Column(Float, default=0.0)
    roi = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)

    avg_win = Column(Float, default=0.0)
    avg_loss = Column(Float, default=0.0)

    # Ranking
    rank = Column(Integer, nullable=True)

    # Metadata
    backtest_start = Column(DateTime, nullable=True)
    backtest_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'strategy_id': self.strategy_id,
            'strategy_name': self.strategy_name,
            'config': json.loads(self.config_json),
            'total_trades': self.total_trades,
            'win_rate': self.win_rate,
            'total_pnl': self.total_pnl,
            'roi': self.roi,
            'profit_factor': self.profit_factor,
            'rank': self.rank,
        }


# Database manager
class DatabaseManager:
    """Quản lý database connection và operations"""

    def __init__(self, db_url='sqlite:///mexc_trading_bot.db'):
        self.engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def get_session(self):
        return self.Session()

    def create_tables(self):
        """Tạo tất cả tables"""
        Base.metadata.create_all(self.engine)

    def drop_tables(self):
        """Xóa tất cả tables (cẩn thận!)"""
        Base.metadata.drop_all(self.engine)


# Example usage
if __name__ == "__main__":
    # Initialize database
    db = DatabaseManager()
    db.create_tables()

    # Create session
    session = db.get_session()

    # Create a bot config
    bot_config = BotConfig(
        name="Bot-LONG-Aggressive",
        direction=DirectionEnum.LONG,
        take_profit=5.0,
        stop_loss=2.0,
        position_size_usdt=50,
        price_increase_threshold=1.0,
        volume_multiplier=2.0,
        rsi_threshold=60,
        min_confidence=70,
        trade_mode=TradeModeEnum.SIMULATED
    )

    session.add(bot_config)
    session.commit()

    print(f"Created bot config: {bot_config.to_dict()}")

    session.close()