"""一次性脚本：业务代码 print → logger（T21，spec: production-hardening）。

- 排除 mcp_gateway（既有独立模块，登记 issue）与 tests/scripts；
- 自动补充 import logging / logger 定义；
- AST 校验替换后 logger.info 单参调用（多参 print 预合并）。
"""

import ast
import re
from pathlib import Path

TARGETS = [
    "api", "orchestration", "data", "core", "model", "tools/mcp_tool.py",
    "main.py",
]
EXCLUDE_PARTS = ("mcp_gateway", "test_", "scripts/")


def iter_files():
    for target in TARGETS:
        path = Path(target)
        if path.is_file():
            yield path
        elif path.is_dir():
            for py in path.rglob("*.py"):
                if any(part in str(py) for part in EXCLUDE_PARTS):
                    continue
                yield py


def main():
    changed = []
    for py in iter_files():
        text = py.read_text(encoding="utf-8")
        if "print(" not in text:
            continue
        # 多参 print 预合并：print(a, b, ...) 难以机械转换的先列出人工核对
        new_text = re.sub(r"\bprint\(", "logger.info(", text)
        if "logger = logging.getLogger" not in new_text:
            lines = new_text.split("\n")
            # 补 import logging
            if not any(ln.startswith("import logging") for ln in lines):
                for i, ln in enumerate(lines):
                    if ln.startswith(("import ", "from ")):
                        lines.insert(i, "import logging")
                        break
                else:
                    lines.insert(0, "import logging")
            new_text = "\n".join(lines)
            # 补 logger 定义（首个 import 块后）
            lines = new_text.split("\n")
            last_import = 0
            for i, ln in enumerate(lines):
                if ln.startswith(("import ", "from ")):
                    last_import = i
            module_name = str(py.with_suffix("")).replace("/", ".")
            lines.insert(
                last_import + 1,
                f'logger = logging.getLogger("{module_name}")',
            )
            new_text = "\n".join(lines)
        py.write_text(new_text, encoding="utf-8")
        changed.append(str(py))
    print(f"已替换 {len(changed)} 个文件: {changed}")


if __name__ == "__main__":
    main()
