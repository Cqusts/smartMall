#!/usr/bin/env bash
# 校验 Java 与 Python 两侧的 Kafka Topic 契约是否一致。
#
# 两个语言各维护一份常量，漂移了不会编译报错，只会在运行时把消息投递到
# 不存在的 topic。这个检查放进 CI，把运行时故障提前到提交时。
#
# 比对两件事：
#   1. topic 全集
#   2. GPU 独占清单——分区数依赖它，写错会导致并发消费 OOM
#
# 用法：./deploy/scripts/check-contracts.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

exec python3 - "$REPO_ROOT" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
java_src = root / "apps/java/mall-common/src/main/java/com/smartmall/common/kafka/Topics.java"
py_src = root / "apps/python/ai-common/src/ai_common/kafka/topics.py"

for p in (java_src, py_src):
    if not p.exists():
        sys.exit(f"✗ 找不到 {p}")

java_text = java_src.read_text(encoding="utf-8")
py_text = py_src.read_text(encoding="utf-8")


def constants(text: str) -> dict[str, str]:
    """抽取 CONST_NAME = "topic.value" 形式的常量。两边语法不同但这个形状一致。"""
    pattern = re.compile(r'\b([A-Z][A-Z0-9_]*)\b[^=\n]*=\s*"([a-z][a-z0-9.]*)"')
    return {m.group(1): m.group(2) for m in pattern.finditer(text)}


def gpu_exclusive(text: str, consts: dict[str, str]) -> set[str]:
    """把 GPU_EXCLUSIVE 块里引用的常量名解析为实际 topic 值。

    只在该块内部查找已知常量名，因此注释里的大写词不会被误当成常量。
    锚定在赋值语句上——两边的文档字符串/注释里都提到了 GPU_EXCLUSIVE，
    按首次出现定位会框到整个文件。
    """
    assign = re.search(r"GPU_EXCLUSIVE[^\n=]*=", text)
    if assign is None:
        sys.exit("✗ 未找到 GPU_EXCLUSIVE 的赋值语句")
    start = assign.end()
    block = text[start : text.index(")", start)]
    return {
        consts[name]
        for name in consts
        if re.search(rf"\b{name}\b", block)
    }


java_consts = constants(java_text)
py_consts = constants(py_text)

# 排除 DLQ 后缀这类非 topic 常量
java_topics = {v for v in java_consts.values() if "." in v and not v.startswith(".")}
py_topics = {v for v in py_consts.values() if "." in v and not v.startswith(".")}

print(f"==> Java 侧 {len(java_topics)} 个 topic")
print(f"==> Python 侧 {len(py_topics)} 个 topic")

failed = False

if java_topics != py_topics:
    failed = True
    print("\n✗ Topic 全集不一致：")
    for t in sorted(java_topics - py_topics):
        print(f"    只在 Java：{t}")
    for t in sorted(py_topics - java_topics):
        print(f"    只在 Python：{t}")

java_gpu = gpu_exclusive(java_text, java_consts)
py_gpu = gpu_exclusive(py_text, py_consts)

if java_gpu != py_gpu:
    failed = True
    print("\n✗ GPU 独占 topic 清单不一致：")
    for t in sorted(java_gpu - py_gpu):
        print(f"    只在 Java：{t}")
    for t in sorted(py_gpu - java_gpu):
        print(f"    只在 Python：{t}")

if failed:
    sys.exit(1)

print(f"\n✅ Topic 契约一致（{len(java_topics)} 个 topic，"
      f"其中 GPU 独占 {len(java_gpu)} 个：{', '.join(sorted(java_gpu))}）")
PY
