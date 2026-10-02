# stop-out-model

使用 Python 3 研究加密货币合约的潜在强平区间。项目方向和分阶段计划见 [AGENT.md](AGENT.md)。

当前已实现模块一：**逐仓状态 → 强平触发价格**。模块二计划用机器学习估计仓位分布，暂不实施。

## 快速运行

Python 3.11+，运行和测试仅使用标准库。在仓库根目录执行，无需安装依赖：

```powershell
python -m stop_out_model examples/long.json
python -m unittest discover -s tests -v
```

示例代表标记价格为 3,000、持有 1 ETH、当前权益为 150 USDT 的逐仓多仓。**示例中的维持保证金率 0.5% 是演示参数，不代表 ETH 的实时交易所规则。** 不计费用时结果约为：

```json
{
  "status": "pending",
  "liquidation_price": "2864.321608040201...",
  "distance_fraction": "0.045226130653...",
  "equity_buffer": "135.000"
}
```

上面省略了后续小数位；实际 CLI 输出完整计算结果。金额、价格和比例输出为十进制字符串，`distance_fraction` 的 `0.05` 表示 5%。

## Python 接口

```python
from stop_out_model import (
    IsolatedPosition,
    MaintenanceSchedule,
    calculate_liquidation,
)

position = IsolatedPosition.from_entry(
    side="long",
    quantity="1",             # 标的资产数量，非合约张数或 USDT 金额
    entry_price="3000",
    mark_price="3300",
    margin_balance="150",     # 不含未实现盈亏的逐仓余额
)
assert position.equity == 450  # 当前权益包含 300 USDT 浮盈

rules = MaintenanceSchedule.fixed("0.005")  # 演示规则
result = calculate_liquidation(position, rules)
print(result.liquidation_price)

# 对接未来的仓位分布模块：这里是当前有效杠杆，不是开仓设置杠杆。
estimated = IsolatedPosition.from_effective_leverage(
    side="short", mark_price="3000", equity="150", effective_leverage="20",
)
print(calculate_liquidation(estimated, rules))

# 在数量不变时更新标记价格；equity_adjustment 可计入已发生的补撤保证金／费用。
updated = position.at_mark("3200", equity_adjustment="50")
```

也可直接构造 `IsolatedPosition(side, quantity, mark_price, equity)`。它允许权益为零或负数，以识别已经满足强平条件的输入。通过有效杠杆构造时权益和杠杆必须为正。

所有数值支持字符串、`Decimal`、整数和有限浮点数；推荐字符串或 `Decimal`，避免调用前的二进制浮点运算损失精度。内部运算使用 50 位有效数字，不依赖调用方设置的 Decimal 精度。

## 计算口径

范围限定为报价币结算的线性逐仓合约。`P0` 是当前**标记价格**，`q > 0` 是标的数量，`C` 是当前权益，`s` 多头为 `+1`、空头为 `-1`：

```text
当前名义金额 N0 = q × P0
当前有效杠杆 = N0 / C                     （C > 0）
候选价格 P 下的权益 = C + s × q × (P - P0)
候选价格 P 下的维持保证金 = q × P × m - d
候选价格 P 下的费用准备金 = q × P × f
触发条件：权益 <= 维持保证金 + 费用准备金

多仓候选边界 P = (N0 - C - d) / [q × (1 - m - f)]
空仓候选边界 P = (N0 + C + d) / [q × (1 + m + f)]
```

`m` 为候选价格所处档位的维持保证金率，`d` 为该档维持保证金扣减额，`f` 为可选费用准备金率，默认 0。费用准备金是显式的建模选项，不等同于所有交易所的实际平仓手续费／强平费用。

`margin_balance` 已包括调用方计入的实际费用、资金费和保证金变更，不能再重复扣减。一次求解期间数量、权益现金调整和规则保持不变；模型不预测未来现金流或交易行为。

## 阶梯规则

```python
from stop_out_model import MaintenanceSchedule, MaintenanceTier

rules = MaintenanceSchedule((
    MaintenanceTier("0", "0.005", "0"),
    MaintenanceTier("100000", "0.01", "500"),
))
```

以上也是演示参数。档位按**候选触发价格下的仓位名义金额**选择，不能只用当前所在档位计算。每档包含下限、不含下一档下限，最后一档无上限。

规则必须从 0 开始、费率非递减，扣减额使维持保证金连续。例如上例在 100,000 处两档的维持保证金均为 500。费率加费用准备金率必须小于 1。当前不支持不连续规则、有限名义金额上限或基于挂单／其他仓位合并计算的档位。

## 返回状态

| status | 意义 | liquidation_price | distance_fraction |
| --- | --- | --- | --- |
| `pending` | 当前健康，在不利方向存在正价格触发边界 | 计算出的标记价格 | 非负相对距离 |
| `already_liquidatable` | 当前权益已不高于规则要求，不代表确认已被交易所执行强平 | `null` | `0` |
| `no_positive_liquidation_price` | 当前规则下无正价格触发点，例如充分抵押的多仓 | `null` | `null` |

`equity_buffer` 是当前权益减去当前规则要求；`liquidation_tier_floor` 在 `pending` 时返回触发档位下限，其余状态为 `null`。没有有效边界的状态不能被当作零价格价档参与未来区间汇总。

## 验证与边界

测试覆盖固定规则的多空案例、零维持保证金时的破产边界、跨档位、档位边界、费用准备金、补撤保证金、当前有效杠杆、已触发／无正根状态和非法输入。另外用多组确定性样本验证：在计算价格附近，权益与要求的大小关系应正确翻转；在仓位与规则不变时，更新标记价格和浮盈亏不应改变绝对强平价。

这是通用的显式规则引擎，尚未与具体交易所逐仓账户做端到端对账。实际使用前需要提供对应合约、账户和生效时间的规则。未实现全仓、反向合约、部分强平、ADL、交易所价格精度取整或最终成交模拟。输出不代表真实挂单。

规则背景参考：[逐仓保证金与价格计算](https://www.bybit.com/en/help-center/article/Understanding-the-Adjustment-and-Impact-of-the-New-Margin-Calculation)、[阶梯维持保证金](https://www.bybit.com/en/help-center/article/Maintenance-Margin-USDT-Contract)。这些文档帮助说明规则结构，代码不自动抓取或固化其示例参数。
