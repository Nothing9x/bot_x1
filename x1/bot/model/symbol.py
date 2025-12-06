class Symbol:
    def __init__(self, symbol: str, price_scale: int, contract_size: float, max_vol: float, max_leverage: int):
        self.symbol = symbol
        self.price_scale = price_scale
        self.contract_size = contract_size
        self.max_vol = max_vol
        self.max_leverage = max_leverage

    def __repr__(self):
        return (
            f"Symbol(symbol={self.symbol}, price_scale={self.price_scale}, "
            f"contract_size={self.contract_size}, max_vol={self.max_vol}, "
            f"max_leverage={self.max_leverage})"
        )
