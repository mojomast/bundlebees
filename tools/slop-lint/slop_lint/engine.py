"""AST scanner for slop-lint."""

from __future__ import annotations

import ast
from collections import namedtuple

from slop_lint import rules

Finding = namedtuple("Finding", "path line rule message")


def scan_source(source: str, path: str) -> list[Finding]:
    """Scan one source string and return its findings, sorted by position."""
    try:
        tree = ast.parse(source, filename=path)
    except Exception as exc:
        return [Finding(path, _line_of(exc), "syntax-error", _syntax_message(exc))]

    findings: list[Finding] = []
    functions = rules.functions(tree)
    for function in functions:
        score = rules.complexity(function)
        if score > rules.COMPLEXITY_LIMIT:
            findings.append(
                Finding(
                    path,
                    function.lineno,
                    "complexity",
                    f"cyclomatic complexity {score} exceeds {rules.COMPLEXITY_LIMIT}",
                )
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and rules.is_swallowed(node):
            findings.append(
                Finding(path, node.lineno, "swallowed-exception", "exception handler swallows the error")
            )
    for scope in rules.default_scopes(tree):
        for _ in rules.mutable_defaults(scope):
            findings.append(
                Finding(path, scope.lineno, "mutable-default-arg", "default argument is a mutable container")
            )
    findings.sort(key=lambda finding: (finding.line, finding.rule, finding.message))
    return findings


def _line_of(exc: BaseException) -> int:
    line = getattr(exc, "lineno", 1)
    return line if isinstance(line, int) and line > 0 else 1


def _syntax_message(exc: BaseException) -> str:
    if isinstance(exc, SyntaxError):
        detail = exc.msg or "invalid syntax"
    else:
        detail = exc.__class__.__name__
    return f"cannot parse source: {detail}"
