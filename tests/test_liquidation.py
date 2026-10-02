import json
import random
import subprocess
import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from stop_out_model import (
    IsolatedPosition,
    LiquidationStatus,
    MaintenanceSchedule,
    MaintenanceTier,
    calculate_liquidation,
)

D = Decimal
ROOT = Path(__file__).resolve().parents[1]


class LiquidationTests(unittest.TestCase):
    def setUp(self):
        self.fixed = MaintenanceSchedule.fixed("0.005")
        self.tiered = MaintenanceSchedule((
            MaintenanceTier("0", "0.005"),
            MaintenanceTier("100000", "0.01", "500"),
            MaintenanceTier("500000", "0.02", "5500"),
        ))

    def assertClose(self, actual, expected, tolerance="1e-35"):
        self.assertLessEqual(abs(actual - D(expected)), D(tolerance))

    def test_long_known_boundary(self):
        position = IsolatedPosition("long", "1", "3000", "150")
        result = calculate_liquidation(position, self.fixed)
        self.assertEqual(result.status, LiquidationStatus.PENDING)
        self.assertClose(result.liquidation_price, "2864.32160804020100502512562814070351758794")
        self.assertClose(result.distance_fraction, "0.04522613065326633165829145728643216080402")
        self.assertEqual(result.equity_buffer, 135)

    def test_short_known_boundary(self):
        result = calculate_liquidation(IsolatedPosition("short", 1, 3000, 150), self.fixed)
        self.assertClose(result.liquidation_price, "3134.32835820895522388059701492537313432836")
        self.assertGreater(result.distance_fraction, 0)

    def test_zero_maintenance_reaches_bankruptcy_prices(self):
        rules = MaintenanceSchedule.fixed(0)
        for side, expected in (("long", 2850), ("short", 3150)):
            with self.subTest(side=side):
                result = calculate_liquidation(IsolatedPosition(side, 1, 3000, 150), rules)
                self.assertEqual(result.liquidation_price, expected)

    def test_current_equity_includes_pnl(self):
        for side, expected in (("long", 450), ("short", -150)):
            with self.subTest(side=side):
                position = IsolatedPosition.from_entry(
                    side=side, quantity=1, entry_price=3000, mark_price=3300, margin_balance=150,
                )
                self.assertEqual(position.equity, expected)
                self.assertEqual(position.notional, 3300)

    def test_effective_leverage_is_not_initial_leverage(self):
        position = IsolatedPosition.from_entry(
            side="long", quantity=1, entry_price=3000, mark_price=3300, margin_balance=150,
        )
        self.assertClose(position.effective_leverage, "7.333333333333333333333333333333333333333333")
        self.assertNotEqual(position.effective_leverage, 20)

    def test_ml_facing_constructor_matches_direct_position(self):
        position = IsolatedPosition.from_effective_leverage(
            side="short", mark_price="3000", equity="150", effective_leverage="20",
        )
        self.assertEqual(position, IsolatedPosition("short", 1, 3000, 150))
        self.assertEqual(position.effective_leverage, 20)

    def test_mark_rebasing_preserves_absolute_liquidation_price(self):
        for side in ("long", "short"):
            position = IsolatedPosition(side, 100, 1000, 20000)
            original = calculate_liquidation(position, self.tiered)
            for new_mark in (950, 1100):
                with self.subTest(side=side, mark=new_mark):
                    updated = calculate_liquidation(position.at_mark(new_mark), self.tiered)
                    self.assertEqual(updated.liquidation_price, original.liquidation_price)

    def test_extra_margin_moves_boundary_away_and_withdrawal_moves_it_closer(self):
        for side in ("long", "short"):
            position = IsolatedPosition(side, 1, 3000, 150)
            original = calculate_liquidation(position, self.fixed)
            added = calculate_liquidation(position.at_mark(3000, equity_adjustment=50), self.fixed)
            removed = calculate_liquidation(position.at_mark(3000, equity_adjustment=-50), self.fixed)
            with self.subTest(side=side):
                self.assertGreater(added.distance_fraction, original.distance_fraction)
                self.assertLess(removed.distance_fraction, original.distance_fraction)

    def test_fixed_rule_scaling_changes_amount_but_not_boundary(self):
        first = calculate_liquidation(IsolatedPosition("long", 1, 3000, 150), self.fixed)
        scaled = calculate_liquidation(IsolatedPosition("long", 10, 3000, 1500), self.fixed)
        self.assertEqual(first.liquidation_price, scaled.liquidation_price)
        self.assertEqual(scaled.equity_buffer, first.equity_buffer * 10)

    def test_fee_reserve_moves_both_boundaries_closer(self):
        fees = MaintenanceSchedule.fixed("0.005", close_fee_rate="0.001")
        for side in ("long", "short"):
            position = IsolatedPosition(side, 1, 3000, 150)
            with self.subTest(side=side):
                result = calculate_liquidation(position, fees)
                self.assertEqual(result.equity_buffer, 132)
                self.assertLess(result.distance_fraction, calculate_liquidation(position, self.fixed).distance_fraction)

    def test_at_or_below_maintenance_has_no_future_price(self):
        for side in ("long", "short"):
            for equity in (15, 14, 0, -20):
                with self.subTest(side=side, equity=equity):
                    result = calculate_liquidation(IsolatedPosition(side, 1, 3000, equity), self.fixed)
                    self.assertEqual(result.status, LiquidationStatus.ALREADY_LIQUIDATABLE)
                    self.assertIsNone(result.liquidation_price)
                    self.assertEqual(result.distance_fraction, 0)
                    self.assertLessEqual(result.equity_buffer, 0)

    def test_fully_collateralized_long_has_no_positive_boundary(self):
        for equity in (3000, 4000):
            with self.subTest(equity=equity):
                result = calculate_liquidation(IsolatedPosition("long", 1, 3000, equity), self.fixed)
                self.assertEqual(result.status, LiquidationStatus.NO_POSITIVE_LIQUIDATION_PRICE)
                self.assertIsNone(result.liquidation_price)
                self.assertIsNone(result.distance_fraction)

    def test_collateralized_short_still_has_an_upside_boundary(self):
        result = calculate_liquidation(IsolatedPosition("short", 1, 3000, 4000), self.fixed)
        self.assertEqual(result.status, LiquidationStatus.PENDING)
        self.assertGreater(result.liquidation_price, 3000)

    def test_nonpositive_equity_has_no_effective_leverage(self):
        for equity in (0, -1):
            self.assertIsNone(IsolatedPosition("long", 1, 3000, equity).effective_leverage)

    def test_long_boundary_uses_lower_tier_than_current_mark(self):
        position = IsolatedPosition("long", 100, 1100, 15000)
        result = calculate_liquidation(position, self.tiered)
        self.assertEqual(self.tiered.tier_at(position.notional).notional_floor, 100000)
        self.assertEqual(result.liquidation_tier_floor, 0)
        self.assertLess(result.liquidation_price, 1000)
        self.assertBoundary(position, self.tiered, result.liquidation_price)

    def test_short_boundary_uses_higher_tier_than_current_mark(self):
        position = IsolatedPosition("short", 100, 900, 15000)
        result = calculate_liquidation(position, self.tiered)
        self.assertEqual(self.tiered.tier_at(position.notional).notional_floor, 0)
        self.assertEqual(result.liquidation_tier_floor, 100000)
        self.assertGreater(result.liquidation_price, 1000)
        self.assertBoundary(position, self.tiered, result.liquidation_price)

    def test_exact_tier_boundary_belongs_to_upper_tier(self):
        for side, mark, equity in (("long", 1100, 10500), ("short", 900, 10500)):
            with self.subTest(side=side):
                result = calculate_liquidation(IsolatedPosition(side, 100, mark, equity), self.tiered)
                self.assertEqual(result.liquidation_price, 1000)
                self.assertEqual(result.liquidation_tier_floor, 100000)
        self.assertEqual(self.tiered.maintenance_margin(100000), 500)

    def test_external_decimal_precision_does_not_change_result(self):
        position = IsolatedPosition("short", "0.123456789", "3456.789", "20")
        expected = calculate_liquidation(position, self.fixed)
        with localcontext() as ctx:
            ctx.prec = 6
            self.assertEqual(calculate_liquidation(position, self.fixed), expected)

    def assertBoundary(self, position, rules, price):
        at_boundary = position.at_mark(price)
        self.assertClose(at_boundary.equity, rules.required_equity(at_boundary.notional), "1e-32")
        with localcontext() as ctx:
            ctx.prec = 50
            for direction in (-1, 1):
                shifted_price = price * (1 + D(direction) * D("0.000001"))
                shifted = position.at_mark(shifted_price)
                buffer = shifted.equity - rules.required_equity(shifted.notional)
                self.assertGreater(buffer * direction * position.side.sign, 0)

    def test_boundaries_satisfy_equity_equation_and_cross_in_correct_direction(self):
        rng = random.Random(42)
        for side in ("long", "short"):
            for _ in range(50):
                quantity = D(rng.randint(1, 1000))
                mark = D(rng.randint(100, 5000))
                notional = quantity * mark
                equity = self.tiered.required_equity(notional) + notional * D("0.15")
                position = IsolatedPosition(side, quantity, mark, equity)
                with self.subTest(side=side, quantity=quantity, mark=mark):
                    result = calculate_liquidation(position, self.tiered)
                    self.assertEqual(result.status, LiquidationStatus.PENDING)
                    self.assertGreater((mark - result.liquidation_price) * position.side.sign, 0)
                    self.assertBoundary(position, self.tiered, result.liquidation_price)


class ValidationTests(unittest.TestCase):
    def test_invalid_position_numbers_and_side(self):
        good = dict(side="long", quantity="1", mark_price="3000", equity="150")
        for field in ("quantity", "mark_price", "equity"):
            for value in ("NaN", "Infinity", "-Infinity", "bad", True, None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    IsolatedPosition(**(good | {field: value}))
        for field in ("quantity", "mark_price"):
            for value in (0, -1):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    IsolatedPosition(**(good | {field: value}))
        with self.assertRaises(ValueError):
            IsolatedPosition(**(good | {"side": "buy"}))

    def test_invalid_effective_leverage_inputs(self):
        good = dict(side="long", mark_price=3000, equity=150, effective_leverage=20)
        for field in ("mark_price", "equity", "effective_leverage"):
            for value in (0, -1, "NaN"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    IsolatedPosition.from_effective_leverage(**(good | {field: value}))

    def test_invalid_entry_price(self):
        for entry in (0, -1, "NaN"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                IsolatedPosition.from_entry(
                    side="long", quantity=1, entry_price=entry, mark_price=3000, margin_balance=150,
                )

    def test_invalid_tier_fields(self):
        for args in ((-1, ".01", 0), (0, -1, 0), (0, 1, 0), (0, ".01", -1), (0, "NaN", 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                MaintenanceTier(*args)

    def test_invalid_schedules(self):
        cases = (
            (),
            (MaintenanceTier(1, ".01"),),
            (MaintenanceTier(0, ".01", 1),),
            (MaintenanceTier(0, ".005"), MaintenanceTier(0, ".01")),
            (MaintenanceTier(0, ".01"), MaintenanceTier(100000, ".005")),
            (MaintenanceTier(0, ".005"), MaintenanceTier(100000, ".01", 0)),
        )
        for tiers in cases:
            with self.subTest(tiers=tiers), self.assertRaises(ValueError):
                MaintenanceSchedule(tiers)

    def test_invalid_fee_rate(self):
        for fee in (-1, 1, ".995", "NaN"):
            with self.subTest(fee=fee), self.assertRaises(ValueError):
                MaintenanceSchedule.fixed(".005", close_fee_rate=fee)


class CommandLineTests(unittest.TestCase):
    def test_example_runs_without_dependencies(self):
        completed = subprocess.run(
            [sys.executable, "-m", "stop_out_model", "examples/long.json"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["result"]["status"], "pending")
        self.assertEqual(payload["effective_leverage"], "20")
        self.assertEqual(D(payload["result"]["liquidation_price"]).quantize(D("0.01")), D("2864.32"))

    def test_missing_input_fails_without_traceback(self):
        completed = subprocess.run(
            [sys.executable, "-m", "stop_out_model", "examples/does-not-exist.json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
