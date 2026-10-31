from setuptools import setup

setup(
    name='chat_ui',
    version='0.0.0',
    py_modules=['chat_ui'],
    entry_points={
        'console_scripts': ['chat_ui = chat_ui:run']
    },
)
