from setuptools import setup

setup(
    name='media2notes',
    version='0.1.0',
    packages=['media2notes'],
    install_requires=['pillow'],
    entry_points={
        'console_scripts': ['media2notes = media2notes.cli:main']
    },
)
