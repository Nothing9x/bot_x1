from x1.bot.exchange.trade.trade_side import TradeSide
from x1.bot.model.reposonse.i_order_response import IOrderResponse
from x1.bot.model.state.order_state import OrderState


class GateOrderResponse(IOrderResponse):
    def __init__(self, data: dict):
        self._data = data
        self._dealAvgPrice = float(data.get("fill_price", 0))
        self._dealVol = float(data.get("size", 0))
        self._vol = float(data.get("size", 0))
        self._orderId = data.get("id_string")
        self._price = float(data.get("price", 0))
        self._state = OrderState.UNCOMPLETED if data.get("status") == "open" else OrderState.COMPLETED
        self._symbol = data.get("contract")
        self._positionId = data.get("text", "").replace("ao-", "")
        self._side = TradeSide.OPEN_LONG if data.get("size") > 0 else TradeSide.OPEN_SHORT

    @property
    def dealAvgPrice(self): return self._dealAvgPrice

    @property
    def dealVol(self): return self._dealVol

    @property
    def vol(self): return self._vol

    @property
    def orderId(self): return self._orderId

    @property
    def price(self): return self._price

    @property
    def state(self): return self._state

    @property
    def symbol(self): return self._symbol

    @property
    def positionId(self): return self._positionId

    @property
    def side(self): return self._side

    def to_dict(self) -> dict:
        return {
            "dealAvgPrice": self.dealAvgPrice,
            "dealVol": self.dealVol,
            "vol": self.vol,
            "orderId": self.orderId,
            "price": self.price,
            "state": self.state,
            "symbol": self.symbol,
            "positionId": self.positionId,
            "side": self.side,
            "bitget": False
        }

    def __str__(self): return f"<MexcOrderResponse {self.to_dict()}>"
