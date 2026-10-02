"""不可变仓位快照及显式维持保证金规则。"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum

Number = Decimal | str | int | float
PRECISION = 50


def _decimal(value: Number, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return number


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True, slots=True)
class IsolatedPosition:
    """数量始终为正；equity 是当前逐仓权益，已包含未实现盈亏。"""

    side: Side
    quantity: Decimal
    mark_price: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        for name in ("quantity", "mark_price", "equity"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.quantity <= 0 or self.mark_price <= 0:
            raise ValueError("quantity and mark_price must be positive")

    @property
    def notional(self) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self.quantity * self.mark_price

    @property
    def effective_leverage(self) -> Decimal | None:
        if self.equity <= 0:
            return None
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self.notional / self.equity

    @classmethod
    def from_entry(
        cls, *, side: Side | str, quantity: Number, entry_price: Number,
        mark_price: Number, margin_balance: Number,
    ) -> "IsolatedPosition":
        """margin_balance 不含浮动盈亏；允许已扣费后出现非正余额。"""
        side = Side(side)
        quantity = _decimal(quantity, "quantity")
        entry_price = _decimal(entry_price, "entry_price")
        mark_price = _decimal(mark_price, "mark_price")
        margin_balance = _decimal(margin_balance, "margin_balance")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            equity = margin_balance + side.sign * quantity * (mark_price - entry_price)
        return cls(side, quantity, mark_price, equity)

    @classmethod
    def from_effective_leverage(
        cls, *, side: Side | str, mark_price: Number,
        equity: Number, effective_leverage: Number,
    ) -> "IsolatedPosition":
        """这里接受当前有效杠杆，不接受开仓时设置杠杆的替代值。"""
        mark_price = _decimal(mark_price, "mark_price")
        equity = _decimal(equity, "equity")
        leverage = _decimal(effective_leverage, "effective_leverage")
        if mark_price <= 0 or equity <= 0 or leverage <= 0:
            raise ValueError("mark_price, equity and effective_leverage must be positive")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            quantity = equity * leverage / mark_price
        return cls(side, quantity, mark_price, equity)

    def at_mark(
        self, mark_price: Number, *, equity_adjustment: Number = 0,
    ) -> "IsolatedPosition":
        """更新标记价格及已发生的权益调整；不预测未来补仓或费用。"""
        mark_price = _decimal(mark_price, "mark_price")
        adjustment = _decimal(equity_adjustment, "equity_adjustment")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            equity = self.equity + self.side.sign * self.quantity * (mark_price - self.mark_price) + adjustment
        return IsolatedPosition(self.side, self.quantity, mark_price, equity)


@dataclass(frozen=True, slots=True)
class MaintenanceTier:
    """档位包含下限；上限为下一档下限。MM = notional * rate - amount。"""

    notional_floor: Decimal
    maintenance_margin_rate: Decimal
    maintenance_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("notional_floor", "maintenance_margin_rate", "maintenance_amount"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.notional_floor < 0 or self.maintenance_amount < 0:
            raise ValueError("notional_floor and maintenance_amount must be nonnegative")
        if not 0 <= self.maintenance_margin_rate < 1:
            raise ValueError("maintenance_margin_rate must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class MaintenanceSchedule:
    """连续非递减阶梯；最后一档无上限。费用准备金不代表通用交易所费用。"""

    tiers: tuple[MaintenanceTier, ...]
    close_fee_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "tiers", tuple(self.tiers))
        object.__setattr__(self, "close_fee_rate", _decimal(self.close_fee_rate, "close_fee_rate"))
        if not self.tiers or not all(isinstance(tier, MaintenanceTier) for tier in self.tiers):
            raise ValueError("tiers must contain MaintenanceTier objects")
        if not 0 <= self.close_fee_rate < 1:
            raise ValueError("close_fee_rate must be in [0, 1)")
        if self.tiers[0].notional_floor != 0 or self.tiers[0].maintenance_amount != 0:
            raise ValueError("first tier must start at zero with zero maintenance_amount")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            for index, tier in enumerate(self.tiers):
                if tier.maintenance_margin_rate + self.close_fee_rate >= 1:
                    raise ValueError("maintenance rate plus close fee rate must be below 1")
                if index == 0:
                    continue
                previous = self.tiers[index - 1]
                if tier.notional_floor <= previous.notional_floor:
                    raise ValueError("tier floors must be strictly increasing")
                if tier.maintenance_margin_rate < previous.maintenance_margin_rate:
                    raise ValueError("maintenance rates must be nondecreasing")
                expected = previous.maintenance_amount + tier.notional_floor * (
                    tier.maintenance_margin_rate - previous.maintenance_margin_rate
                )
                if tier.maintenance_amount != expected:
                    raise ValueError("maintenance_amount must keep the schedule continuous")

    @classmethod
    def fixed(
        cls, maintenance_margin_rate: Number, *, close_fee_rate: Number = 0,
    ) -> "MaintenanceSchedule":
        return cls((MaintenanceTier(0, maintenance_margin_rate),), close_fee_rate)

    def tier_at(self, notional: Number) -> MaintenanceTier:
        notional = _decimal(notional, "notional")
        if notional < 0:
            raise ValueError("notional must be nonnegative")
        for tier in reversed(self.tiers):
            if notional >= tier.notional_floor:
                return tier
        raise AssertionError("validated schedule must cover nonnegative notional")

    def maintenance_margin(self, notional: Number) -> Decimal:
        notional = _decimal(notional, "notional")
        tier = self.tier_at(notional)
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return notional * tier.maintenance_margin_rate - tier.maintenance_amount

    def required_equity(self, notional: Number) -> Decimal:
        notional = _decimal(notional, "notional")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self.maintenance_margin(notional) + notional * self.close_fee_rate
