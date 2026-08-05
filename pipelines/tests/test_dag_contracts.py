"""DAG 与业务模块的契约测试。

DAG 里调用 ``repo.fetch_unprocessed(...)`` 这类方法，方法名写错不会有任何
静态报错——只会在 Airflow 里跑到那一步才 AttributeError，而那时通常已是凌晨两点。
这个测试把漂移提前到 CI。

不需要安装 Airflow：只做 AST 解析，不导入 DAG 模块。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from smartmall_pipeline import repository
from smartmall_pipeline.gates import gate3_model, gate4_human

DAGS_DIR = Path(__file__).resolve().parents[1] / "dags"

# DAG 里会被 X.from_env() / X(...) 实例化的类
KNOWN_CLASSES = {
    "OdsRepository": repository.OdsRepository,
    "DwsRepository": repository.DwsRepository,
    "HttpLabelStudioClient": gate4_human.HttpLabelStudioClient,
    "LiteLlmClient": gate3_model.LiteLlmClient,
}


def _own_body(scope: ast.AST):
    """遍历一个函数自身的语句，**不进入嵌套函数**。

    @task 装饰的函数嵌套在 @dag 函数里，各自有独立的局部变量——
    ``repo`` 在一个 task 里绑 OdsRepository，在另一个里绑 DwsRepository。
    按文件或按外层函数收集别名会让后者覆盖前者，产生假报错。
    """
    for node in ast.iter_child_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        yield from _own_body(node)


def _iter_scopes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            yield node


def _collect_calls(path: Path) -> list[tuple[type, str]]:
    """返回 (类, 被调用的方法名) 列表。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[type, str]] = []

    for scope in _iter_scopes(tree):
        aliases: dict[str, type] = {}
        body = list(_own_body(scope))

        for node in body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            fn = node.value.func
            cls = None
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                cls = KNOWN_CLASSES.get(fn.value.id)      # X.from_env()
            elif isinstance(fn, ast.Name):
                cls = KNOWN_CLASSES.get(fn.id)            # X(...)
            if cls is not None:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        aliases[t.id] = cls

        for node in body:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                v = node.func.value
                if isinstance(v, ast.Name) and v.id in aliases:
                    found.append((aliases[v.id], node.func.attr))

    return found


DAG_FILES = sorted(DAGS_DIR.glob("*.py"))


def test_dags_exist():
    assert DAG_FILES, "dags/ 下没有 DAG 文件"


@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_is_parseable(path: Path):
    ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_calls_existing_methods(path: Path):
    """DAG 调用的每个方法都必须真实存在于对应的类上。"""
    for cls, method in _collect_calls(path):
        assert hasattr(cls, method), (
            f"{path.name} 调用了 {cls.__name__}.{method}()，但该方法不存在"
        )


def test_at_least_some_calls_are_checked():
    """防止扫描器因解析失败而空转，假装通过。"""
    total = sum(len(_collect_calls(p)) for p in DAG_FILES)
    assert total >= 5, f"只解析到 {total} 处调用，扫描器可能失效了"


def test_scope_isolation():
    """扫描器必须按函数作用域隔离别名。

    dag_clean_dialogue 里 repo 在 load_batch 绑 OdsRepository、
    在 persist 绑 DwsRepository。若作用域没隔离，会误报
    DwsRepository.fetch_unprocessed 不存在。
    """
    calls = _collect_calls(DAGS_DIR / "dag_clean_dialogue.py")
    pairs = {(c.__name__, m) for c, m in calls}
    assert ("OdsRepository", "fetch_unprocessed") in pairs
    assert ("DwsRepository", "save_knowledge_items") in pairs
    assert ("DwsRepository", "fetch_unprocessed") not in pairs
