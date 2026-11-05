from setuptools import setup, find_packages
from pathlib import Path

# Read the contents of your README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    # One build, one name. The separate 'pscan-lab-dev' package and its
    # virtualenv are gone: having a stable and an experimental install on one
    # machine meant two package names, two command sets, a venv, two
    # installers, and a different failure mode in each combination. The
    # fragmentation cost more than the isolation bought.
    name='pscan-lab',
    version='2.0.0',
    description='Scanning microscope orchestrator: stages, DAQ cards, scopes and cameras',
    long_description=long_description,
    long_description_content_type='text/markdown',
    # tests/ carries an __init__.py so unittest can discover it, which would
    # otherwise make find_packages() install a top-level 'tests' package into
    # site-packages and collide with every other project that has one.
    packages=find_packages(exclude=['tests', 'tests.*', 'dev', 'dev.*']),
    install_requires=[
        'pyserial>=3.5',
        'opencv-python>=4.5',
        'numpy>=1.19',
        'python-vxi11>=0.9',
        'standard-xdrlib'
        # Phidget22 removed: using ctypes + libphidget21.so instead
    ],
    extras_require={
        # NI-DAQmx, for the cards comedi cannot reach - every NI X Series
        # board, the PCIe-6351 included. Deliberately optional: the rigs
        # running comedi cards have no use for it, and the Python package is
        # useless without NI's driver installed system-wide anyway, so making
        # it a hard requirement would mean a failing install on machines where
        # nothing needs it. pscan.daq imports it lazily and says exactly what
        # to install when a config asks for the daqmx backend without it.
        #
        #   pip install '.[daqmx]'
        'daqmx': ['nidaqmx>=0.9'],
    },
    entry_points={
        'console_scripts': [
            # No -dev suffix any more: there is only one build, so there is
            # nothing to disambiguate from.
            'pscan=pscan.main:main',            # the scan orchestrator
            'pystage=pscan.control_stages:main',  # jog the Thorlabs stages
            'pypiezo=pscan.control_piezo:main',   # drive the Phidget piezo
            'pydaq=pscan.control_daq:main',       # inspect and poke DAQ cards
            'flyscan=flyscan.cli:main',           # the fly-scan orchestrator
        ],
    },
)
