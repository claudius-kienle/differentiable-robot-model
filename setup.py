from distutils.core import setup

setup(
    name='differentiable-robot-model',
    version='1.0',
    description='Differentiable Robot Model',
    author='ArtiMinds',
    author_email='claudius.kienle@artiminds.com',
    url='https://artiminds.com',
    packages=[
        'differentiable_robot_model'
    ],
    install_requires=[
        'numpy',
        'torch',
        'pyquaternion',
        'urdf_parser_py',
        'tqdm',
        'matplotlib',
    ],
)
