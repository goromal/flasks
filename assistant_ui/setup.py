from setuptools import setup
setup(
    name='assistant_ui',
    version='0.0.0',
    py_modules=['assistant_ui', 'agent_companion'],
    entry_points={
        'console_scripts': ['assistant_ui = assistant_ui:run']
    },
)
