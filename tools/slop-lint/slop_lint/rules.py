"""AST helpers and rule predicates used by :mod:`slop_lint.engine`."""

from __future__ import annotations

import ast

COMPLEXITY_LIMIT = 10
BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})
MUTABLE_FACTORIES = frozenset({"list", "dict", "set"})
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def functions(tree: ast.AST) -> list[ast.AST]:
    """Return every function and method in *tree*, including nested ones."""
    return [node for node in ast.walk(tree) if isinstance(node, SCOPE_NODES)]


def default_scopes(tree: ast.AST) -> list[ast.AST]:
    """Return every node that may carry default arguments."""
    return [node for node in ast.walk(tree) if isinstance(node, (*SCOPE_NODES, ast.Lambda))]


def complexity(function: ast.AST) -> int:
    """Return the McCabe-style complexity of one function or method."""
    score = 1
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, NESTED_SCOPES):
            continue
        score += _decision_points(node)
        pending.extend(ast.iter_child_nodes(node))
    return score


def _decision_points(node: ast.AST) -> int:
    if isinstance(node, ast.If):
        return 1
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return 1
    if isinstance(node, (ast.ExceptHandler, ast.With, ast.AsyncWith, ast.Assert)):
        return 1
    if isinstance(node, ast.BoolOp):
        return max(0, len(node.values) - 1)
    if isinstance(node, COMPREHENSIONS):
        return sum(len(generator.ifs) for generator in node.generators)
    if isinstance(node, ast.match_case):
        return 1
    return 0


def is_swallowed(handler: ast.ExceptHandler) -> bool:
    """True when *handler* catches broadly and only ignores the error."""
    if not handler.body:
        return False
    if handler.type is not None and not _is_broad(handler.type):
        return False
    return all(_is_noop(statement) for statement in handler.body)


def _is_broad(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in BROAD_EXCEPTIONS
    if isinstance(node, ast.Attribute):
        return node.attr in BROAD_EXCEPTIONS
    return False


def _is_noop(statement: ast.AST) -> bool:
    if isinstance(statement, (ast.Pass, ast.Continue)):
        return True
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def mutable_defaults(scope: ast.AST) -> list[ast.AST]:
    """Return the default values of *scope* that are mutable containers."""
    arguments = scope.args
    defaults = list(arguments.defaults)
    defaults.extend(item for item in arguments.kw_defaults if item is not None)
    return [item for item in defaults if is_mutable_default(item)]


def is_mutable_default(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in MUTABLE_FACTORIES
    return False
