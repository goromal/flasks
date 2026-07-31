from setuptools import setup

setup(
    name='wormhole',
    version='0.0.0',
    py_modules=['wormhole'],
    entry_points={
        'console_scripts': ['wormhole = wormhole:main']
    },
)
