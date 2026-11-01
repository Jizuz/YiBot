"""依赖方向静态检查（T21，design: 架构设计——单向依赖强制）。

层级：api(1) → orchestration(2) → model/tools(3) → data(4)；
core / config 为横切层：可被任何业务层引用，但自身禁止引用业务层。
main.py 为装配根，不受限。

用法：.venv/bin/python scripts/check_layering.py（违规退出码 1）
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 模块前缀 → 层级（数字越大层级越低）
LAYERS = {
    "api": 1,
    "orchestration": 2,
    "model": 3,
    "tools": 3,
    "data": 4,
}
CROSS = ("core", "config")  # 横切层


def layer_of(module: str) -> int | None:
    top = module.split(".")[0]
    return LAYERS.get(top)


def is_cross(module: str) -> bool:
    return module.split(".")[0] in CROSS


def check_file(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if parts[0] == "main.py":
        return []
    if parts[0] in CROSS:
        owner_layer = None  # 横切层：禁止引用任何业务层
        owner_name = parts[0]
    else:
        owner_layer = LAYERS.get(parts[0])
        owner_name = parts[0]
        if owner_layer is None:
            return []

    violations = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            target_layer = layer_of(name)
            if owner_layer is None:  # 横切层规则
                if target_layer is not None and name.split(".")[0] != owner_name:
                    violations.append(
                        f"{rel}: 横切层 {owner_name} 禁止依赖业务层 -> {name}"
                    )
            else:
                if target_layer is None or is_cross(name):
                    continue  # 横切层/外部库可引用
                if target_layer < owner_layer:
                    violations.append(
                        f"{rel}: 层级{owner_layer}({owner_name}) 不得反向依赖"
                        f" 层级{target_layer}({name.split('.')[0]})"
                    )
    return violations


def main() -> int:
    violations = []
    for top in [*LAYERS, *CROSS]:
        path = ROOT / top
        if path.is_dir():
            for py in path.rglob("*.py"):
                if "mcp_gateway" in str(py) or "__pycache__" in str(py):
                    continue
                violations.extend(check_file(py))
    if violations:
        print(f"❌ 依赖方向违规 {len(violations)} 处：")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("✓ 依赖方向检查通过（api → orchestration → model/tools → data；横切层无反向依赖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
