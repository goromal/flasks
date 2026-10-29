from setuptools import setup

setup(
    name='cozy',
    version='0.0.0',
    py_modules=['cozy', 'cozyctl', 'job_store', 'workflows', 'comfyui_client',
                'runner', 'eta', 'fit', 'image_size', 'image_refs', 'heif',
                'crop', 'queue_store', 'video_judge', 'video_media',
                'video_search', 'video_store'],
    entry_points={
        'console_scripts': ['cozy = cozy:run', 'cozyctl = cozyctl:run']
    },
)
