import ast
import pathlib
import unittest

FLASHBACK_DIR = pathlib.Path(__file__).resolve().parent.parent / "flashback"


class _UnionAnnotationVisitor(ast.NodeVisitor):
    """Finds `X | Y` used as a type annotation (PEP 604), not as an ordinary
    runtime bitwise-or expression elsewhere in the code."""

    def __init__(self):
        self.found = []

    def _check(self, node):
        if node is None:
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            self.found.append(node)
        for child in ast.iter_child_nodes(node):
            self._check(child)

    def _visit_function(self, node):
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            self._check(arg.annotation)
        if args.vararg is not None:
            self._check(args.vararg.annotation)
        if args.kwarg is not None:
            self._check(args.kwarg.annotation)
        self._check(node.returns)
        self.generic_visit(node)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_AnnAssign(self, node):
        self._check(node.annotation)
        self.generic_visit(node)


class TestPython39AnnotationCompat(unittest.TestCase):
    """pyproject.toml declares `requires-python = ">=3.9"`. PEP 604's `X | Y`
    union syntax (e.g. `str | None`) is only valid *at runtime* from Python
    3.10 onward — used in a plain annotation, it crashes the whole package on
    import under 3.9, unless the module has `from __future__ import
    annotations` to defer evaluation. Session 102 found exactly this in
    `parser.py`'s `edit_card` signature: every `flashback` invocation on a
    real Python 3.9 install raised `TypeError: unsupported operand type(s)
    for |: 'type' and 'NoneType'` at import time, never caught by this suite
    since it always runs on a newer interpreter. Use `typing.Optional`/
    `typing.Union` instead (already the convention elsewhere in this file),
    or add `from __future__ import annotations` to the module.
    """

    def test_no_runtime_pep604_unions_without_future_annotations(self):
        offenders = []
        for path in sorted(FLASHBACK_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            has_future_annotations = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
                for node in tree.body
            )
            if has_future_annotations:
                continue
            visitor = _UnionAnnotationVisitor()
            visitor.visit(tree)
            if visitor.found:
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
