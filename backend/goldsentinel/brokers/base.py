from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain import AccountSnapshot, BrokerResult, Candle, OrderRequest, Position, Quote, Side, Timeframe


class Broker(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def candles(self, symbol: str, timeframe: Timeframe, count: int, offset: int = 0) -> list[Candle]: ...

    @abstractmethod
    def positions(self, symbol: str | None = None) -> list[Position]: ...

    @abstractmethod
    def pending_orders(self, symbol: str | None = None) -> list[dict]: ...

    @abstractmethod
    def submit_market_order(self, symbol: str, order: OrderRequest) -> BrokerResult: ...

    @abstractmethod
    def close_position(self, ticket: str, volume: float | None = None) -> BrokerResult: ...

    @abstractmethod
    def modify_position(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult: ...

    @abstractmethod
    def cancel_pending(self, ticket: str) -> BrokerResult: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> dict: ...

    def loss_at_stop(self, symbol: str, side: Side, volume: float, entry: float, stop_loss: float) -> float:
        spec = self.symbol_spec(symbol)
        return abs(entry - stop_loss) * volume * float(spec["contract_size"])

    def normalize_protective_levels(self, symbol: str, side: Side, quote: Quote, stop_loss: float, take_profit: float) -> tuple[float, float]:
        return stop_loss, take_profit

    def is_owned_position(self, position: Position) -> bool:
        return position.magic == 260115 or position.comment.startswith("GS:")

