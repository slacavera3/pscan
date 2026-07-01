from setuptools import setup, find_packages
from pathlib import Path

# Read the contents of your README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    name='pscan-lab', 
    version='1.0.0',
    description='Automated multi-sensor acquisition and Thorlabs stage control',
    long_description=long_description,
    long_description_content_type='text/markdown',
    packages=find_packages(),
    install_requires=[
        'pyserial>=3.5',
        'opencv-python>=4.5',
        'numpy>=1.19',
        'python-vxi11>=0.9',
        'standard-xdrlib'
    ],
    entry_points={
        'console_scripts': [
            # Links the terminal command 'pscan' to main() in main.py
            'pscan=pscan.main:main',
            # Links the terminal command 'pystage' to main() in control_stages.py
            'pystage=pscan.control_stages:main',
        ],
    },
)
