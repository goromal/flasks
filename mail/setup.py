from setuptools import setup

setup(
    name="mail",
    version="0.0.1",
    py_modules=["mail", "run_store"],
    entry_points={
        "console_scripts": ["mail-ui = mail:main"],
    },
)
