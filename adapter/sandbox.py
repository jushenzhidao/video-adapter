"""AST 沙箱：白名单 stdlib + 受限 builtins。

脚本是"一个厂商契约的翻译层"，只需要字符串/字典/正则/日期这些能力。
文件、网络、动态求值一律不给 —— 基础设施只能经 `ctx` 触达。
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

#: 允许 import 的模块（按**根模块**判定）。
ALLOWED_IMPORTS = frozenset(
    {
        "re", "json", "math", "datetime", "time", "typing", "functools", "itertools",
        "collections", "dataclasses", "enum", "copy", "decimal", "random", "statistics",
        "string", "textwrap", "unicodedata", "uuid", "base64", "binascii", "hashlib",
        "hmac", "urllib", "__future__",
    }
)

#: 禁止出现的名字（哪怕只是引用）。
FORBIDDEN_NAMES = frozenset(
    {
        "exec", "eval", "compile", "open", "input", "__import__", "getattr", "setattr",
        "delattr", "globals", "locals", "vars", "breakpoint", "exit", "quit", "memoryview",
        "object", "super", "classmethod", "staticmethod",
    }
)

#: 允许暴露给脚本的内建函数。
_SAFE_BUILTINS = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset", "hash", "hex", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "oct",
    "ord", "pow", "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip", "Exception", "ValueError", "TypeError",
    "KeyError", "IndexError", "LookupError", "RuntimeError", "StopIteration",
    "NotImplementedError", "AssertionError", "ArithmeticError", "ZeroDivisionError",
    "AttributeError", "NameError", "UnicodeDecodeError", "UnicodeEncodeError", "True",
    "False", "None",
)


class ScriptSecurityError(Exception):
    """脚本试图做沙箱外的事 —— 这类脚本不该被装载，更不能被运行。"""


def _root(name: str) -> str:
    return str(name or "").split(".", 1)[0]


def audit(tree: ast.AST, origin: str = "<script>") -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root(alias.name) not in ALLOWED_IMPORTS:
                    raise ScriptSecurityError(
                        f"{origin}: import {alias.name!r} is not allowed in the sandbox"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入 = 逃出沙箱
                raise ScriptSecurityError(f"{origin}: relative imports are not allowed")
            if _root(node.module) not in ALLOWED_IMPORTS:
                raise ScriptSecurityError(
                    f"{origin}: from {node.module!r} import … is not allowed in the sandbox"
                )
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise ScriptSecurityError(f"{origin}: name {node.id!r} is forbidden")
        elif isinstance(node, ast.Attribute):
            if str(node.attr).startswith("__"):
                raise ScriptSecurityError(
                    f"{origin}: dunder attribute access ({node.attr!r}) is forbidden"
                )


def safe_builtins() -> dict:
    table = {name: getattr(builtins, name) for name in _SAFE_BUILTINS if hasattr(builtins, name)}

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if level:
            raise ScriptSecurityError("relative imports are not allowed")
        if _root(name) not in ALLOWED_IMPORTS:
            raise ScriptSecurityError(f"import {name!r} is not allowed in the sandbox")
        return builtins.__import__(name, globals, locals, fromlist, level)

    table["__import__"] = _guarded_import
    return table


def load_module(path: Path, ref: str) -> dict:
    """读源码 → 审计 → 在受限命名空间里执行 → 返回模块命名空间。"""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ScriptSecurityError(f"{ref}: syntax error at {exc.lineno}: {exc.msg}") from exc
    audit(tree, origin=ref)
    namespace: dict = {"__name__": ref, "__file__": str(path), "__builtins__": safe_builtins()}
    code = compile(tree, str(path), "exec")
    exec(code, namespace)  # noqa: S102 - 已过 AST 审计 + 受限 builtins
    return namespace
