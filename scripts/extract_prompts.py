"""一次性脚本：将 orchestration 各文件的 *_PROMPT 常量集中到 prompts/ 目录。"""

import re
from pathlib import Path

FILES = {
    "orchestration/supervisor.py": "supervisor",
    "orchestration/sub_agent/triage_agent.py": "triage",
    "orchestration/sub_agent/symptom_agent.py": "symptom",
    "orchestration/sub_agent/education_agent.py": "education",
    "orchestration/sub_agent/rights_agent.py": "rights",
    "orchestration/sub_agent/search_agent.py": "search",
    "orchestration/sub_agent/chit_chat_agent.py": "chit_chat",
}

PATTERN = re.compile(
    r'^(?P<name>[A-Z][A-Z0-9_]*_PROMPT) = """.*?"""\n',
    re.DOTALL | re.MULTILINE,
)


def main():
    Path("orchestration/prompts").mkdir(parents=True, exist_ok=True)
    init_file = Path("orchestration/prompts/__init__.py")
    if not init_file.exists():
        init_file.write_text(
            '"""编排层 prompts 集中管理（T19：医疗审计快照基础）。"""\n',
            encoding="utf-8",
        )
    for path, mod in FILES.items():
        src = Path(path)
        text = src.read_text(encoding="utf-8")
        matches = list(PATTERN.finditer(text))
        if not matches:
            print(f"{path}: 无匹配（跳过）")
            continue
        parts = [
            f'"""{mod} Agent prompts（T19 集中管理：内容与原文逐字一致）。"""\n\n'
        ]
        names = []
        for m in matches:
            names.append(m.group("name"))
            parts.append(m.group(0))  # 逐字保留原文定义块
            parts.append("\n")
        Path(f"orchestration/prompts/{mod}.py").write_text(
            "".join(parts), encoding="utf-8"
        )
        new_text = PATTERN.sub("", text)
        import_line = (
            f'from orchestration.prompts.{mod} import {", ".join(names)}'
        )
        lines = new_text.split("\n")
        last_import = 0
        for i, ln in enumerate(lines[:80]):
            if ln.startswith(("import ", "from ")):
                last_import = i
        lines.insert(last_import + 1, import_line)
        src.write_text("\n".join(lines), encoding="utf-8")
        print(f"{path}: 提取 {len(names)} 个 -> prompts/{mod}.py")


if __name__ == "__main__":
    main()
