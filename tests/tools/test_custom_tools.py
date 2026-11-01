"""T1：calculate 工具安全求值测试（spec: security-hardening）。

红阶段断言口径：
- 合法算术（四则/括号/小数/负号/整除/取模/有界幂）正常计算；
- 注入与非算术样例一律返回"计算错误"，且不得出现"计算结果"（零求值）；
- 边界：除零、空串、超长表达式拒绝。
"""

import pytest

from tools.custom_tools import calculate


def _run(expression: str) -> str:
    return str(calculate.invoke({"expression": expression}))


# ---------------- 正向：合法算术 ----------------

@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("3 * 7 + 2", 23),
        ("10 / 4", 2.5),
        ("(1.5 + 2.5) * 2", 8),
        ("-5 + 3", -2),
        ("2 ** 10", 1024),
        ("7 // 2", 3),
        ("7 % 3", 1),
        ("1 + 2 * 3 - 4 / 2", 5),
    ],
)
def test_valid_arithmetic(expression: str, expected):
    result = _run(expression)
    assert result.startswith("计算结果"), result
    assert str(expected) in result, result


# ---------------- 异常：注入样例（零求值） ----------------

INJECTION_SAMPLES = [
    # 对象链绕过 builtins 清空（现状 eval 可执行 → 红阶段主证）
    "().__class__.__mro__[1].__subclasses__()",
    "().__class__.__bases__[0]",
    # lambda / 逗号表达式 / 下标 / 字符串字面量（非算术）
    "(lambda: 1)()",
    "1, 2",
    "'abc'[0]",
    "'abc' + 'd'",
    # 直接 import / 内建（现状被 builtins 清空挡住，仍需保持拒绝）
    "__import__('os').system('id')",
    "open('/etc/passwd')",
    # 变量名（含数学常量伪装 e / pi）
    "e ** 2",
    "pi",
    # 资源滥用：超大幂导致超长结果
    "2 ** 10000",
]


@pytest.mark.parametrize("expression", INJECTION_SAMPLES)
def test_injection_rejected(expression: str):
    result = _run(expression)
    assert result.startswith("计算错误"), f"注入样例未被拒绝: {expression!r} -> {result!r}"
    assert "计算结果" not in result, result
    assert "<class" not in result, result


# ---------------- 边界 ----------------

@pytest.mark.parametrize(
    "expression",
    ["1 / 0", "1 // 0", "", "   ", "3 +", "()"],
)
def test_invalid_or_edge(expression: str):
    result = _run(expression)
    assert result.startswith("计算错误"), result


def test_expression_too_long():
    result = _run("1 + " * 60 + "1")
    assert result.startswith("计算错误"), result
