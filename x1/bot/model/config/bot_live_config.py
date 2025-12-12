from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True) # frozen=True làm cho class là immutable (bất biến), một thực hành tốt cho cấu hình.
class BotLiveConfig:
    """
    Cấu hình cho bot giao dịch trực tiếp.
    """
    NAME: str
    API_KEY: str
    SECRET_KEY: str
    CHAT_ID: str
    PROXY: Optional[str] = ""