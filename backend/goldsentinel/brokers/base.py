from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain import AccountSnapshot, BrokerResult, Candle, OrderRequest, Position, Quote, Timeframe


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

