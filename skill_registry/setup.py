from glob import glob
from setuptools import setup


PACKAGE_NAME = 'skill_registry'

setup(
    name=PACKAGE_NAME,
    version='0.0.0',
    packages=[PACKAGE_NAME],
    package_data={PACKAGE_NAME: ['deny.yaml']},
    data_files=[
        ('share/ament_index/resource_index/packages', [f'resource/{PACKAGE_NAME}']),
        (f'share/{PACKAGE_NAME}', ['package.xml']),
        (f'share/{PACKAGE_NAME}/contracts', glob('contracts/*.yaml')),
        (f'share/{PACKAGE_NAME}/trees', glob('trees/*.xml')),
        (f'share/{PACKAGE_NAME}/agent_skills', glob('agent_skills/*.md')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Karan Khanna',
    maintainer_email='karankha@andrew.cmu.edu',
    description='Skill contracts, online skill discovery, and a validator for behavior trees before they run',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'registry_node = skill_registry.registry_node:main',
            'validate_tree = skill_registry.validate_tree:main',
        ],
    },
)
