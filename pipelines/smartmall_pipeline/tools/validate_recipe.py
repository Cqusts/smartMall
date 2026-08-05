"""校验 Data-Juicer 配方里的算子名与参数名是否与已安装版本匹配。

**为什么需要这个**：Data-Juicer 的算子参数名在版本间会变
（比如 text_length_filter 有的版本用 min_len，有的用 min_ratio）。
配方写错了不会立刻报错，而是在跑到一半、已经处理了几十万条之后才炸。
这个脚本把失败提前到提交时，可以放进 CI。

用法::

    python -m smartmall_pipeline.tools.validate_recipe recipes/dialogue_clean_v1.yaml

未安装 data-juicer 时退出码为 0 并给出提示——不阻塞不需要跑批的开发环境。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import yaml


def load_recipe(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    process = cfg.get("process") or []
    if not isinstance(process, list):
        raise SystemExit(f"✗ {path}: process 必须是列表")
    return process


def _op_entries(process: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    """把 process 列表展开成 (算子名, 参数) 对。"""
    entries: list[tuple[str, dict[str, Any]]] = []
    for node in process:
        if not isinstance(node, dict) or len(node) != 1:
            raise SystemExit(f"✗ process 项格式不对: {node!r}")
        name, params = next(iter(node.items()))
        entries.append((name, params or {}))
    return entries


def validate(path: Path) -> int:
    entries = _op_entries(load_recipe(path))
    print(f"==> 配方 {path.name}，共 {len(entries)} 个算子")

    try:
        from data_juicer.ops import OPERATORS  # type: ignore
    except ImportError:
        print("  ⚠ 未安装 data-juicer，跳过算子校验")
        print("    需要跑批时执行：pip install py-data-juicer")
        # 仍然做结构校验：重复的 replace_content_mapper 是合法的，但空名字不是
        for name, _ in entries:
            if not name or not isinstance(name, str):
                print(f"  ✗ 非法算子名: {name!r}")
                return 1
        print("  ✓ 结构校验通过")
        return 0

    known = set(OPERATORS.modules.keys())
    failed = False

    for name, params in entries:
        if name not in known:
            print(f"  ✗ 未知算子: {name}")
            # 给出最接近的候选，省去翻文档
            import difflib

            close = difflib.get_close_matches(name, known, n=3)
            if close:
                print(f"      是否想用: {', '.join(close)}")
            failed = True
            continue

        op_cls = OPERATORS.modules[name]
        try:
            sig = inspect.signature(op_cls.__init__)
        except (TypeError, ValueError):
            print(f"  ✓ {name}（无法内省签名，仅校验算子名）")
            continue

        accepts_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        valid = set(sig.parameters) - {"self"}
        unknown = [k for k in params if k not in valid]

        if unknown and not accepts_kwargs:
            print(f"  ✗ {name} 不接受参数: {', '.join(unknown)}")
            print(f"      可用参数: {', '.join(sorted(valid))}")
            failed = True
        else:
            print(f"  ✓ {name}")

    if failed:
        print("\n✗ 配方与已安装的 data-juicer 不匹配")
        return 1

    print(f"\n✅ 配方校验通过（data-juicer 已安装，{len(known)} 个可用算子）")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    code = 0
    for arg in sys.argv[1:]:
        code |= validate(Path(arg))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
