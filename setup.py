import os
import sys

from setuptools import setup

# PEP 517 build isolation does not always put the project root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup.build import b  # noqa: E402


setup(package_data={"retype": ["py.typed"]},
      packages=['qt', 'retype'],
      zip_safe=False,
      cmdclass={
          'b': b,  # custom build command for building retype with pyinstaller
      },)
