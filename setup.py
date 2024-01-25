#!python3
# -*- coding: utf-8 -*-
"""
O2MConverter
Copyright 2020-2022 Aleksi Ikkala, Anton Sobinov
https://github.com/aikkala/O2MConverter
"""

from setuptools import setup, find_packages

setup(
    name='O2MConverter',
    version='0.2',
    description='OpenSim to MuJoCo XML converter.',
    long_description=open('README.md').read(),
    long_description_content_type="text/markdown",
    url='https://github.com/aikkala/O2MConverter',
    install_requires=[
        'pyquaternion',
        'vtk',
        'natsort',
        'xmltodict',
        'numpy',
        'scipy',
        'scikit-learn',
        'pandas'],
    author='Aleksi Ikkala, Anton R Sobinov',
    author_email='aleksi.ikkala@gmail.com',
    packages=find_packages())
