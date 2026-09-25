#! /usr/bin/env python3

import sys
import subprocess

packagename = sys.argv[1]

def install_python_package(packagename):
    pip_install_cmd = f"python3 -m pip --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org install {packagename}" 
    subprocess.run(pip_install_cmd.split(' '))

install_python_package(packagename)
