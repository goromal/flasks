"""setup.py enumerates py_modules by hand, so a new module is easy to forget.

The consequence is invisible to every other test: pytest imports from the
source tree, where the file is present, while the installed package omits it
and fails at import time on the deployed machine. Guard the list itself.
"""
import ast
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _declared_modules():
    with open(os.path.join(_HERE, "setup.py")) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "py_modules":
            return set(ast.literal_eval(node.value))
    raise AssertionError("setup.py declares no py_modules")


def _source_modules():
    return {n[:-3] for n in os.listdir(_HERE)
            if n.endswith(".py") and n != "setup.py"}


def test_every_module_is_packaged():
    missing = _source_modules() - _declared_modules()
    assert not missing, (
        "modules present in the source tree but absent from setup.py's "
        "py_modules, so they would not be installed: %s" % sorted(missing))


def test_no_packaged_module_is_missing_from_the_tree():
    stale = _declared_modules() - _source_modules()
    assert not stale, "setup.py lists modules that no longer exist: %s" % sorted(stale)
