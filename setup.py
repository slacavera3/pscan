from setuptools import setup, find_packages
from pathlib import Path

# Read the contents of your README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    # Deliberately distinct from the stable 'pscan-lab' so the two can sit on
    # one machine without pip treating an install of either as an upgrade of
    # the other. They still both ship a top-level 'pscan' package, so this
    # branch must go in its own venv - see install-dev.sh.
    name='pscan-lab-dev',
    version='1.0.0.dev0',
    description='Experimental build of pscan: flyscan plus untested hardware changes',
    long_description=long_description,
    long_description_content_type='text/markdown',
    packages=find_packages(),
    install_requires=[
        'pyserial>=3.5',
        'opencv-python>=4.5',
        'numpy>=1.19',
        'python-vxi11>=0.9',
        'standard-xdrlib'
        # Phidget22 removed: using ctypes + libphidget21.so instead
    ],
    entry_points={
        'console_scripts': [
            # Every command carries the -dev suffix so that running one is
            # always an explicit choice: there is no name an experimental
            # build and the stable one both answer to, on PATH or inside an
            # activated venv.
            # Links the terminal command 'pscan-dev' to main() in main.py
            'pscan-dev=pscan.main:main',
            # Links the terminal command 'pystage-dev' to main() in control_stages.py
            'pystage-dev=pscan.control_stages:main',
            # Links the terminal command 'pypiezo-dev' to main() in control_piezo.py
            'pypiezo-dev=pscan.control_piezo:main',
            # Links the terminal command 'flyscan-dev' to main() in flyscan/cli.py
            'flyscan-dev=flyscan.cli:main',
        ],
    },
)
