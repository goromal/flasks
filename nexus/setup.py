from setuptools import setup

setup(
    name="nexus-ui",
    version="0.0.1",
    py_modules=["nexus", "roster", "spokes"],
    entry_points={
        "console_scripts": [
            "nexus-ui=nexus:main",
        ],
    },
)
