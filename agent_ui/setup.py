from setuptools import setup


setup(
    name="agent-ui",
    version="0.1.0",
    py_modules=["agent_ui"],
    entry_points={
        "console_scripts": [
            "agent-ui=agent_ui:main",
        ],
    },
)
