from abc import ABC, abstractmethod

class IOrderResponse(ABC):
    @property
    @abstractmethod
    def dealAvgPrice(self) -> float: pass

    @property
    @abstractmethod
    def dealVol(self) -> float: pass

    @property
    @abstractmethod
    def vol(self) -> float: pass

    @property
    @abstractmethod
    def orderId(self) -> str: pass

    @property
    @abstractmethod
    def price(self) -> float: pass

    @property
    @abstractmethod
    def state(self) -> int: pass

    @property
    @abstractmethod
    def symbol(self) -> str: pass

    @property
    @abstractmethod
    def positionId(self) -> str: pass

    @property
    @abstractmethod
    def side(self) -> int: pass

    @abstractmethod
    def to_dict(self) -> dict: pass

    @abstractmethod
    def __str__(self) -> str: pass
