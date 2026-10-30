from setuptools import setup

setup(
    name="rcrsync_ui",
    version="0.0.1",
    py_modules=["rcrsync_ui"],
    entry_points={"console_scripts": ["rcrsync_ui = rcrsync_ui:main"]},
)
