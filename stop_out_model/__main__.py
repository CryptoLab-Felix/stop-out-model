"""读取本地 JSON 仓位快照，输出 JSON 计算结果。"""

import argparse
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from . import IsolatedPosition, MaintenanceSchedule, MaintenanceTier, calculate_liquidation


def main() -> None:
    parser = argparse.ArgumentParser(description="计算逐仓线性合约的强平边界")
    parser.add_argument("input", type=Path, help="包含 position 和 maintenance_tiers 的 JSON 文件")
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        unknown = set(payload) - {"position", "maintenance_tiers", "close_fee_rate"}
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        position = IsolatedPosition(**payload["position"])
        schedule = MaintenanceSchedule(
            tuple(MaintenanceTier(**tier) for tier in payload["maintenance_tiers"]),
            payload.get("close_fee_rate", 0),
        )
        result = calculate_liquidation(position, schedule)
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as error:
        parser.error(str(error))
    output = {
        "position": asdict(position),
        "notional": position.notional,
        "effective_leverage": position.effective_leverage,
        "result": asdict(result),
    }
    print(json.dumps(output, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
