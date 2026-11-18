from glob import glob
from setuptools import setup


PACKAGE_NAME = 'orchestrator'

setup(
    name=PACKAGE_NAME,
    version='0.0.0',
    packages=[PACKAGE_NAME],
    data_files=[
        ('share/ament_index/resource_index/packages', [f'resource/{PACKAGE_NAME}']),
        (f'share/{PACKAGE_NAME}', ['package.xml']),
        (f'share/{PACKAGE_NAME}/launch', glob('launch/*.launch.py')),
        (f'share/{PACKAGE_NAME}/config', glob('config/*.yaml')),
        (f'share/{PACKAGE_NAME}/config/sites', glob('config/sites/*.yaml')),
        (f'share/{PACKAGE_NAME}/test_trees', glob('test_trees/*.xml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Karan Khanna',
    maintainer_email='karankha@andrew.cmu.edu',
    description='Agentic orchestrator for the G1: tree executor, LLM planner and trace',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'executor_node = orchestrator.executor_node:main',
            'planner_node = orchestrator.planner_node:main',
            'fake_robot = orchestrator.fake_robot:main',
            'send_tree = orchestrator.send_tree:main',
            'planner_cli = orchestrator.planner_cli:main',
            'cosmos_vlm_server = orchestrator.cosmos_vlm_server:main',
            'run_recorder = orchestrator.run_recorder:main',
        ],
    },
)
