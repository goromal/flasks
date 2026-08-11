from setuptools import setup

setup(
    name='cozy',
    version='0.0.0',
    py_modules=['cozy', 'job_store', 'workflows', 'comfyui_client',
                'runner', 'eta', 'fit', 'image_size', 'image_refs', 'crop',
                'queue_store'],
    entry_points={
        'console_scripts': ['cozy = cozy:run']
    },
)
