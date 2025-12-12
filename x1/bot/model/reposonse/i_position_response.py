from abc import ABC, abstractmethod

class IPositionResponse(ABC):
    @property
    @abstractmethod
    def openAvgPrice(self) -> float: pass

    @openAvgPrice.setter
    @abstractmethod
    def openAvgPrice(self, value: float): pass

    @property
    @abstractmethod
    def closeAvgPrice(self) -> float: pass

    @closeAvgPrice.setter
    @abstractmethod
    def closeAvgPrice(self, value: float): pass

    @property
    @abstractmethod
    def closeVol(self) -> float: pass

    @closeVol.setter
    @abstractmethod
    def closeVol(self, value: float): pass

    @property
    @abstractmethod
    def holdVol(self) -> float: pass

    @holdVol.setter
    @abstractmethod
    def holdVol(self, value: float): pass

    @property
    @abstractmethod
    def pnl(self) -> float: pass

    @pnl.setter
    @abstractmethod
    def pnl(self, value: float): pass

    @property
    @abstractmethod
    def updateTime(self) -> int: pass

    @updateTime.setter
    @abstractmethod
    def updateTime(self, value: int): pass

    @property
    @abstractmethod
    def realised(self) -> float: pass

    @realised.setter
    @abstractmethod
    def realised(self, value: float): pass

    @property
    @abstractmethod
    def positionType(self): pass

    @positionType.setter
    @abstractmethod
    def positionType(self, value): pass

    @property
    @abstractmethod
    def id(self): pass

    @id.setter
    @abstractmethod
    def id(self, value): pass

    @property
    @abstractmethod
    def state(self): pass

    @state.setter
    @abstractmethod
    def state(self, value): pass

    @property
    @abstractmethod
    def symbol(self): pass

    @symbol.setter
    @abstractmethod
    def symbol(self, value): pass

    @abstractmethod
    def is_bitget(self) -> bool: pass

    @abstractmethod
    def to_dict(self) -> dict: pass

    @abstractmethod
    def update(self, other: 'IPositionResponse'): pass

    @abstractmethod
    def __str__(self) -> str: pass
