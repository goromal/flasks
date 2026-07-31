from setuptools import setup

setup(
    name='self-tester-app',
    version='0.0.1',
    py_modules=['tester', 'folio_source', 'folio_fetch'],
    entry_points={
        'console_scripts': ['tester = tester:run']
    },
)
