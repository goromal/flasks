from setuptools import setup

setup(
    name='brom',
    version='0.0.0',
    py_modules=['bromserver', 'providers', 'provider_tpb', 'aria2rpc', 'store'],
    entry_points={
        'console_scripts': ['brom = bromserver:run']
    },
)
