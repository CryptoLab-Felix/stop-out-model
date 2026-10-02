"""求解固定仓位在不同标记价格下首次达到维持保证金要求的边界。"""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum

from .models import PRECISION, IsolatedPosition, MaintenanceSchedule, Side


class LiquidationStatus(str, Enum):
    PENDING = "pending"
    ALREADY_LIQUIDATABLE = "already_liquidatable"
    NO_POSITIVE_LIQUIDATION_PRICE = "no_positive_liquidation_price"


@dataclass(frozen=True, slots=True)
class LiquidationResult:
    status: LiquidationStatus
    liquidation_price: Decimal | None
    distance_fraction: Decimal | None
    equity_buffer: Decimal
    liquidation_tier_floor: Decimal | None


def calculate_liquidation(
    position: IsolatedPosition, schedule: MaintenanceSchedule,
) -> LiquidationResult:
    """求首次触发边界，不模拟强平成交、部分减仓或未来保证金变动。

    distance_fraction 是距当前标记价格的相对距离，0.05 表示 5%。
    已满足条件时返回 None 价格和 0 距离，不虚构未来触发价。
    无正价格触发点时价格和距离均为 None，不把负价格当作有效价档。
    """
    with localcontext() as ctx:
        ctx.prec = PRECISION
        notional = position.notional
        buffer = position.equity - schedule.required_equity(notional)
        if buffer <= 0:
            return LiquidationResult(
                LiquidationStatus.ALREADY_LIQUIDATABLE, None, Decimal(0), buffer, None,
            )

        for index, tier in enumerate(schedule.tiers):
            rate = tier.maintenance_margin_rate + schedule.close_fee_rate
            if position.side is Side.LONG:
                numerator = notional - position.equity - tier.maintenance_amount
                denominator = 1 - rate
            else:
                numerator = notional + position.equity + tier.maintenance_amount
                denominator = 1 + rate

            # 先比较交叉相乘后的名义金额边界，避免除法舍入改变档位归属。
            if numerator <= 0 or numerator < tier.notional_floor * denominator:
                continue
            if index + 1 < len(schedule.tiers):
                upper = schedule.tiers[index + 1].notional_floor
                if numerator >= upper * denominator:
                    continue

            price = numerator / (denominator * position.quantity)
            distance = abs(price - position.mark_price) / position.mark_price
            return LiquidationResult(
                LiquidationStatus.PENDING, price, distance, buffer, tier.notional_floor,
            )

        # 连续规则下，健康空仓必有上涨触发点；充分抵押的多仓可能无正根。
        if position.side is Side.SHORT:
            raise ArithmeticError("no valid boundary found for a short position")
        return LiquidationResult(
            LiquidationStatus.NO_POSITIVE_LIQUIDATION_PRICE, None, None, buffer, None,
        )
