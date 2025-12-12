from abc import ABC, abstractmethod

class ITradeClient(ABC):

    @abstractmethod
    async def start(self):
        pass

    @abstractmethod
    async def stop(self):
        pass

    @abstractmethod
    async def send_order(self, orderId=-1, symbol='', price=0, quantity=0, side=0, leverage=None, ps=1,
                         take_profit=None, stop_loss=None, tag=""):
        pass

    @abstractmethod
    async def place_order(self, symbol, price, quantity, side, leverage, ps, take_profit=None, stop_loss=None,
                          retry_count=3):
        pass

    @abstractmethod
    async def cancel_order(self, orderId):
        pass

    @abstractmethod
    async def close_all_positions_and_orders(self):
        """Đóng toàn bộ vị thế và lệnh chờ."""
        pass

