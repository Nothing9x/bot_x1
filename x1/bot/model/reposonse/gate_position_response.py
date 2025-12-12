from x1.bot.database.database_models import DirectionEnum
from x1.bot.model.reposonse.i_position_response import IPositionResponse



class GatePositionResponse(IPositionResponse):
    def __init__(self, data: dict):
        self._data = data
        self.openAvgPrice = float(data.get("entry_price", 0))
        self.closeAvgPrice = float(data.get("entry_price", 0))
        self.closeVol = float(data.get("size", 0))
        self.holdVol = float(data.get("size", 0))
        self.pnl = float(data.get("realised_pnl", 0))
        self.updateTime = int(data.get("time_ms", 0))
        self.realised = float(data.get("realised_pnl", 0))
        self.positionType = DirectionEnum.LONG if data.get("mode") in ("dual_long", "long") else DirectionEnum.SHORT
        self.id = data.get("update_id")
        self.state = data.get("size")
        self.symbol = data.get("contract")

    @property
    def openAvgPrice(self): return self._openAvgPrice

    @openAvgPrice.setter
    def openAvgPrice(self, value): self._openAvgPrice = value

    @property
    def closeAvgPrice(self): return self._closeAvgPrice

    @closeAvgPrice.setter
    def closeAvgPrice(self, value): self._closeAvgPrice = value

    @property
    def closeVol(self): return self._closeVol

    @closeVol.setter
    def closeVol(self, value): self._closeVol = value

    @property
    def holdVol(self): return self._holdVol

    @holdVol.setter
    def holdVol(self, value): self._holdVol = value

    @property
    def pnl(self): return self._pnl

    @pnl.setter
    def pnl(self, value): self._pnl = value

    @property
    def updateTime(self): return self._updateTime

    @updateTime.setter
    def updateTime(self, value): self._updateTime = value

    @property
    def realised(self): return self._realised

    @realised.setter
    def realised(self, value): self._realised = value

    @property
    def positionType(self): return self._positionType

    @positionType.setter
    def positionType(self, value): self._positionType = value

    @property
    def id(self): return self._id

    @id.setter
    def id(self, value): self._id = value

    @property
    def state(self): return self._state

    @state.setter
    def state(self, value): self._state = value

    @property
    def symbol(self): return self._symbol

    @symbol.setter
    def symbol(self, value): self._symbol = value

    def is_bitget(self) -> bool: return False

    def to_dict(self) -> dict:
        return {
            "openAvgPrice": self.openAvgPrice,
            "closeAvgPrice": self.closeAvgPrice,
            "closeVol": self.closeVol,
            "holdVol": self.holdVol,
            "pnl": self.pnl,
            "updateTime": self.updateTime,
            "realised": self.realised,
            "positionType": self.positionType,
            "positionId": self.id,
            "state": self.state,
            "symbol": self.symbol,
            "bitget": False
        }

    def update(self, other: IPositionResponse):
        self.openAvgPrice = other.openAvgPrice
        self.closeAvgPrice = other.closeAvgPrice
        self.closeVol = other.closeVol
        self.holdVol = other.holdVol
        self.pnl = other.pnl
        self.updateTime = other.updateTime
        self.realised = other.realised
        self.positionType = other.positionType
        self.id = other.id
        self.state = other.state
        self.symbol = other.symbol

    def __str__(self): return str(self.to_dict())
