#! /usr/bin/env python3

import sys
import subprocess
import os
import zipfile
import ssl
from urllib.request import urlopen
from io import BytesIO
from fix_proto_imports import update_pb2_imports
import glob
import argparse
import parse_version

API_CHANNEL = sys.argv[2] 
CASE_NAME = sys.argv[1] 
PATH = os.path.expanduser(f"~/docs/api/cases/{CASE_NAME}")
#DOWNLOAD_LINK = f"https://interactivebrokers.github.io/downloads/twsapi_macunix.{API_VERSION}.01.zip"
SOURCE_PATH = os.path.join(PATH, "/IBJts/source")
if API_CHANNEL == 'latest':
    SOURCE_PATH = "/source"
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

print(PATH)
PROTO_DIR = os.path.join(PATH, SOURCE_PATH, "proto")
print("PROTO DIR: ", PROTO_DIR)
PROTO_FILES_PATH = os.path.join(SOURCE_PATH, "pythonclient", "ibapi", "protobuf")
print("PROTO FILES PATH: ", PROTO_FILES_PATH)
PROTO_FILES = glob.glob(os.path.join(PROTO_DIR, "*.proto"))
print("Proto files: ", PROTO_FILES)

#print("proto files dir: ", PROTO_FILES_DIR)

def create_case_dir():
    try:
        if not os.path.isdir(PATH):
            os.makedirs(PATH, exist_ok=True)
            print(f"Directory created: {PATH}")
        else:
            print(f"Directory {PATH} already exists")
    except Exception as err:
        print(f"Error {err}")

def get_download_url():
    versions = {}
    try:
        rels = parse_version.fetch_linux_api_versions()
        print(rels)
    except Exception as e:
        print(f"Failed to fetch {e}", file=sys.stderr)
    if not rels:
        print("No matching linux TWS API downloads found", file=sys.stderr)
    for r in rels:
        versions[r.channel] = r.url
    return versions

def download_source(tartget_dir=PATH):
    urls = get_download_url()
    print(urls)
    allowed = ['latest', 'stable']
    if API_CHANNEL not in allowed:
        print(f"[+] allowed args are 'latest' and 'stable'")
    url = urls[API_CHANNEL]
    try:
        with urlopen(url, context=ssl_context) as response:
            total_size = response.headers.get('content-length')
            if total_size:
                total_size = int(total_size)
                print(f"API package total size: {total_size}")
            zip_data = BytesIO(response.read())
            print(f"Extracting to {PATH}")
            with zipfile.ZipFile(zip_data, 'r') as zip_ref:
                zip_ref.extractall(PATH)
            print(f"Extraction completed")
            
            total_files = sum([len(files) for _, _, files in os.walk(tartget_dir)])
            print(f"Downaloded and extracted {total_files} files")
            print(f"Target dir: {PATH}")

            return True
    except Exception as err:
        print(f"Error: {err}")
        return False

def fix_proto_files():
    
    return

def compile_proto():
    proto_files = os.listdir(PATH + SOURCE_PATH + '/proto')
    cmd = [
            'protoc',
            f"--proto_path={PATH + PROTO_DIR}",
            f"--python_out={PATH + PROTO_FILES_PATH}",
            ] + proto_files

    try:
        subprocess.run(cmd)
    except Exception as err:
        print(err)

def fix_proto_imports():
    PYTHON_FILES_PATH = PATH + PROTO_FILES_PATH
    update_pb2_imports(PYTHON_FILES_PATH)

def check_proto_version():
   try:
       result = subprocess.run(
       ['protoc', '--version'],
       capture_output=True,
       text=True,
       check=True
       )
       version = result.stdout.strip()
       print(f"protoc veresion is {version}")
       # Generate proto files for pytohn
       return True
   except FileNotFoundError:
       print("protoc compiler not found")
       print("Install with apt-get install protobuf-compiler")
       return False
   except Exception as err:
       print(f"Error: {err}")
       return False

def install_ibapi():
    # install setuptools:
    setup_location = PATH + SOURCE_PATH + "/pythonclient"
    print(setup_location)
    package_or_path = setup_location
    packages = ['protobuf==5.29.5', setup_location]
    for pack in packages:
        pip_install_cmd = f"python3 -m pip --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org install {pack}" 
        subprocess.run(pip_install_cmd.split(' '))

def main():
#    parser = argparse.ArgumentParser(
#            description="Download and install ibapi",
#            formatter_class=argparse.RawDescriptionHelpFormatter, 
#            epilog="""
#            Examples:
#                python3 setup-script.py --api-version 1048 --case test_case
#            """
#            )
#    parser.add_argument('--api-version', '-a', type=str, required=True, help="API version(e.g 1048#)")
#    parser.add_argument("--case-name", "-c", type=str, required=True, help="Case name (e.g Test case)")
#
#    args = parser.parse_args()
#
    create_case_dir()
    protoc_compiler_installed = check_proto_version()
    if os.path.isdir(PATH):
        print(f"Directory {PATH} exists")
        download_source()
        if protoc_compiler_installed:
            compile_proto()
            fix_proto_imports()
            install_ibapi()

if __name__ == "__main__":
    main()
