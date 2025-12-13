#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Config Loader
Load config của các tài khoản thật/bot thật từ file JSON hoặc dict
"""

import json
import os
from typing import List, Dict, Optional
from dataclasses import dataclass, field


@dataclass
class RealAccountConfig:
    """Config cho một tài khoản thật"""

    # Required fields
    account_id: str
    api_key: str
    secret_key: str
    chat_id: str  # Telegram chat ID riêng

    # Optional fields with defaults
    exchange: str = "GATE"
    proxy: str = ""
    leverage: int = 10
    position_size_usdt: float = 10.0
    max_positions: int = 3

    # Account settings
    future_balance: float = 100.0
    spot_balance: float = 10.0
    withdraw_address: str = ""

    # Trading settings
    auto_place_sl_market: bool = True
    reduce_only_tp: bool = True  # TP sử dụng reduce only

    # Metadata
    is_active: bool = True
    description: str = ""

    def to_bot_config(self):
        """
        Convert sang BotConfig format cho GateTradeClient
        Cần import từ project của bạn
        """
        try:
            from com.BotConfig import BotConfig

            config = BotConfig()
            config.EXCHANGE = self.exchange
            config.API_KEY = self.api_key
            config.SECRET_KEY = self.secret_key
            config.PROXY = self.proxy
            config.LEVERAGE = self.leverage
            config.FUTURE_BALANCE = self.future_balance
            config.SPOT_BALANCE = self.spot_balance
            config.WITHDRAW_ADDRESS = self.withdraw_address
            config.USER_NAME = self.account_id
            config.BOT_USER_ID = self.account_id
            config.CHAT_ID = self.chat_id
            config.AUTO_PLACE_SL_MARKET = self.auto_place_sl_market

            return config
        except ImportError:
            # Return dict if BotConfig not available
            return {
                'exchange': self.exchange,
                'api_key': self.api_key,
                'secret_key': self.secret_key,
                'proxy': self.proxy,
                'leverage': self.leverage,
                'chat_id': self.chat_id,
                'account_id': self.account_id,
            }

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'account_id': self.account_id,
            'api_key': self.api_key,
            'secret_key': self.secret_key,
            'chat_id': self.chat_id,
            'exchange': self.exchange,
            'proxy': self.proxy,
            'leverage': self.leverage,
            'position_size_usdt': self.position_size_usdt,
            'max_positions': self.max_positions,
            'future_balance': self.future_balance,
            'spot_balance': self.spot_balance,
            'withdraw_address': self.withdraw_address,
            'auto_place_sl_market': self.auto_place_sl_market,
            'reduce_only_tp': self.reduce_only_tp,
            'is_active': self.is_active,
            'description': self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'RealAccountConfig':
        """Create from dictionary"""
        return cls(
            account_id=data['account_id'],
            api_key=data['api_key'],
            secret_key=data['secret_key'],
            chat_id=data['chat_id'],
            exchange=data.get('exchange', 'GATE'),
            proxy=data.get('proxy', ''),
            leverage=data.get('leverage', 10),
            position_size_usdt=data.get('position_size_usdt', 10.0),
            max_positions=data.get('max_positions', 3),
            future_balance=data.get('future_balance', 100.0),
            spot_balance=data.get('spot_balance', 10.0),
            withdraw_address=data.get('withdraw_address', ''),
            auto_place_sl_market=data.get('auto_place_sl_market', True),
            reduce_only_tp=data.get('reduce_only_tp', True),
            is_active=data.get('is_active', True),
            description=data.get('description', ''),
        )


class ConfigLoader:
    """
    Load và quản lý config cho các tài khoản thật
    """

    def __init__(self, config_path: str = None):
        """
        Args:
            config_path: Path to JSON config file (optional)
        """
        self.config_path = config_path
        self.accounts: List[RealAccountConfig] = []

        if config_path and os.path.exists(config_path):
            self.load_from_file(config_path)

    def load_from_file(self, path: str) -> List[RealAccountConfig]:
        """
        Load configs từ file JSON

        Expected format:
        {
            "accounts": [
                {
                    "account_id": "bot_001",
                    "api_key": "xxx",
                    "secret_key": "xxx",
                    "chat_id": "@channel",
                    ...
                }
            ]
        }
        """
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            accounts_data = data.get('accounts', [])
            self.accounts = [RealAccountConfig.from_dict(acc) for acc in accounts_data]

            return self.accounts

        except Exception as e:
            print(f"Error loading config from {path}: {e}")
            return []

    def load_from_dict(self, data: Dict) -> List[RealAccountConfig]:
        """Load từ dictionary"""
        accounts_data = data.get('accounts', [])
        self.accounts = [RealAccountConfig.from_dict(acc) for acc in accounts_data]
        return self.accounts

    def load_from_list(self, accounts_list: List[Dict]) -> List[RealAccountConfig]:
        """Load từ list of dictionaries"""
        self.accounts = [RealAccountConfig.from_dict(acc) for acc in accounts_list]
        return self.accounts

    def add_account(self, account: RealAccountConfig):
        """Thêm một account"""
        self.accounts.append(account)

    def add_account_from_dict(self, data: Dict):
        """Thêm account từ dict"""
        account = RealAccountConfig.from_dict(data)
        self.accounts.append(account)
        return account

    def get_account(self, account_id: str) -> Optional[RealAccountConfig]:
        """Get account by ID"""
        for acc in self.accounts:
            if acc.account_id == account_id:
                return acc
        return None

    def get_active_accounts(self) -> List[RealAccountConfig]:
        """Get tất cả accounts đang active"""
        return [acc for acc in self.accounts if acc.is_active]

    def get_accounts_by_exchange(self, exchange: str) -> List[RealAccountConfig]:
        """Get accounts theo exchange"""
        return [acc for acc in self.accounts if acc.exchange.upper() == exchange.upper()]

    def save_to_file(self, path: str = None):
        """Save configs ra file JSON"""
        save_path = path or self.config_path
        if not save_path:
            raise ValueError("No path specified for saving")

        data = {
            'accounts': [acc.to_dict() for acc in self.accounts]
        }

        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def __len__(self):
        return len(self.accounts)

    def __iter__(self):
        return iter(self.accounts)

    def __repr__(self):
        return f"ConfigLoader({len(self.accounts)} accounts)"


# ===== SAMPLE CONFIG FILE =====
SAMPLE_CONFIG = {
    "accounts": [
        {
            "account_id": "bot_001",
            "api_key": "f83dd15ee42e9b3b74321d96a4ea08da",
            "secret_key": "228b3c50eaf8865ebc62b5a302471289172e8534d2737d9353c17d54b4b90c38",
            "chat_id": "@gateio_auto",
            "exchange": "GATE",
            "proxy": "",
            "leverage": 10,
            "position_size_usdt": 10,
            "max_positions": 3,
            "is_active": True,
            "description": "Main trading bot"
        },
        {
            "account_id": "bot_002",
            "api_key": "another_api_key",
            "secret_key": "another_secret",
            "chat_id": "@gateio_bot2",
            "exchange": "GATE",
            "proxy": "",
            "leverage": 15,
            "position_size_usdt": 20,
            "max_positions": 5,
            "is_active": True,
            "description": "Secondary bot with higher leverage"
        }
    ]
}


def create_sample_config(path: str = "real_accounts.json"):
    """Tạo sample config file"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(SAMPLE_CONFIG, f, indent=2, ensure_ascii=False)
    print(f"Created sample config at: {path}")


# ===== USAGE EXAMPLE =====
if __name__ == "__main__":
    # Tạo sample config file
    create_sample_config("real_accounts.json")

    # Load từ file
    loader = ConfigLoader("real_accounts.json")
    print(f"Loaded {len(loader)} accounts")

    # Get active accounts
    active = loader.get_active_accounts()
    print(f"Active accounts: {len(active)}")

    for acc in active:
        print(f"  - {acc.account_id}: {acc.exchange} | {acc.chat_id}")