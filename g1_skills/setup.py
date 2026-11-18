from setuptools import find_packages, setup


PACKAGE_NAME = 'g1_skills'

setup(
    name=PACKAGE_NAME,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    package_data={'': ['contract.yaml', '*.md'], PACKAGE_NAME: ['*/contract.yaml', '*/*.md']},
    data_files=[
        ('share/ament_index/resource_index/packages', [f'resource/{PACKAGE_NAME}']),
        (f'share/{PACKAGE_NAME}', ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=False,
    maintainer='Karan Khanna',
    maintainer_email='karankha@andrew.cmu.edu',
    description='The G1 skills: contracts, skill code, agent skills and their prompts',
    license='MIT',
    tests_require=['pytest'],
)
