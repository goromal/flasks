"""Tests import modules from the source tree, so a module missing from
setup.py's py_modules passes every test and then fails to import once
installed. Catch that here."""
import ast
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_module_is_packaged():
    with open(os.path.join(HERE, "setup.py")) as f:
        tree = ast.parse(f.read())
    listed = next(
        ast.literal_eval(kw.value) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords if kw.arg == "py_modules")
    on_disk = {f[:-3] for f in os.listdir(HERE)
               if f.endswith(".py") and f != "setup.py"}
    assert on_disk - set(listed) == set()
