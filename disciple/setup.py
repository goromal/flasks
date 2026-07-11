from setuptools import setup

setup(
    name='disciple',
    version='0.0.1',
    py_modules=['disciple', 'disciple_report'],
    entry_points={
        'console_scripts': [
            'disciple = disciple:run',
            'disciple-ingest = disciple:ingest',
            'disciple-report = disciple_report:main',
        ]
    },
)
