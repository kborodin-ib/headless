# ibapi-case-setup

Sets up an isolated Python environment with the Interactive Brokers TWS API (`ibapi`) for a support case, in one command, on any Debian-based Linux system (Debian, Ubuntu, Mint, Pop!_OS, …).

```bash
python3 ibapi_case_setup.py setup -c my_case -a stable
```

This gives you `~/docs/api/cases/my_case` with the API source, generated protobuf files and a ready-to-use virtualenv.

The script uses only the Python standard library. It merges three older helper scripts: the setup script, the pb2 import fixer and the pip install wrapper.

---

## Requirements

- Python 3.8 or newer
- `python3-venv`, needed to create the case virtualenv:
  ```bash
  sudo apt-get install -y python3-venv
  ```
- Internet access to `interactivebrokers.github.io` (version list and download) and to PyPI.
- Optional: `protobuf-compiler` (`protoc`). If it isn't installed, the script uses the `protoc` that comes with `grpcio-tools` instead, so no `sudo` is needed. See [How protoc is chosen](#how-protoc-is-chosen).

---

## Installation

You don't need to install anything; the script runs as-is:

```bash
python3 ibapi_case_setup.py --help
```

Or install it as a command, preferably into a virtualenv or with `pipx`:

```bash
pip install .
ibapi-case-setup --help
```

---

## Usage

### List the API versions you can install

```bash
python3 ibapi_case_setup.py versions
```

```
1050.02 (stable)     https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.02.zip
1051.01 (latest)     https://interactivebrokers.github.io/downloads/twsapi_macunix.1051.01.zip
```

The list is read live from the IBKR download page, so it changes when IBKR publishes a new release.

### Set up a case

```bash
python3 ibapi_case_setup.py setup -c my_case -a stable     # the Stable release
python3 ibapi_case_setup.py setup -c my_case -a latest     # the Latest release
python3 ibapi_case_setup.py setup -c my_case -a 1050       # version number; patch taken from the page
python3 ibapi_case_setup.py setup -c my_case -a 1051.01    # exact version
python3 ibapi_case_setup.py my_case 1050                   # short form, same as `setup`
```

The version must be one of those `versions` lists. Anything else is rejected before anything is created or downloaded:

```
ERROR   API version 1037 is not currently available. Available: 1050.02 (stable), 1051.01 (latest)
```

When it finishes:

```bash
source ~/docs/api/cases/my_case/.venv/bin/activate
python -c "import ibapi; print(ibapi.__file__)"
```

### Other commands

```bash
# Rewrite `import X_pb2 as X__pb2` -> `import ibapi.protobuf.X_pb2 as X__pb2` under a directory
python3 ibapi_case_setup.py fix-imports ~/docs/api/cases/my_case/IBJts/source/pythonclient/ibapi/protobuf

# pip install into a specific interpreter (default: the one running the script)
python3 ibapi_case_setup.py pip-install pandas --python ~/docs/api/cases/my_case/.venv/bin/python
```

---

## What `setup` does

1. **Checks available versions.** It reads the list from the IBKR download page and resolves `-a` against it. If the version isn't listed, or the page can't be reached, it stops here.
2. **Creates folders.** It creates the base directory (`~/docs/api/cases` by default) if it's missing, then the case folder.
3. **Downloads the API.** It downloads `twsapi_macunix.<version>.<patch>.zip` and extracts it into the case folder. If `IBJts/source` already exists, it skips this step unless you pass `--force-download`. Zip entries that would land outside the case folder are refused.
4. **Creates a virtualenv** at `<case>/.venv`. Debian 12+ and Ubuntu 23.04+ refuse system-wide `pip install` (PEP 668), so everything goes into the venv.
5. **Compiles protobuf files.** It compiles `IBJts/source/proto/*.proto` into `pythonclient/ibapi/protobuf/`.
6. **Fixes pb2 imports** so the generated files import each other through the `ibapi.protobuf` package.
7. **Installs** `protobuf==5.29.5` and the `pythonclient` package into the venv.
8. **Checks the install** by running `import ibapi.client`.

If any step fails, the script stops with a clear message and exit code `1`.

### Resulting layout

```
~/docs/api/cases/
└── my_case/
    ├── .venv/                          # case virtualenv
    └── IBJts/
        └── source/
            ├── proto/                  # .proto sources from IBKR
            └── pythonclient/
                └── ibapi/
                    └── protobuf/       # generated *_pb2.py files
```

---

## `setup` options

| Option | Description |
|---|---|
| `-c`, `--case CASE` | Case name, used as the folder name. Can't contain `/`. |
| `-a`, `--api-version VER` | `stable`, `latest`, `1050` or `1050.02`. Must be listed by `versions`. |
| `--base-dir DIR` | Where case folders go. Default: `~/docs/api/cases`. |
| `--python PATH` | Install into this interpreter instead of creating `<case>/.venv`. |
| `--protoc {auto,system,grpcio-tools}` | Which `protoc` to use. Default: `auto`. |
| `--install-protoc` | Run `apt-get install protobuf-compiler` if `protoc` is missing (uses `sudo` when not root). |
| `--force-download` | Download again even if the source is already in the case folder. |
| `--insecure` | Skip TLS checks and pass `--trusted-host` to pip. Only for intercepting corporate proxies. |
| `-v`, `--verbose` | Debug logging, including every command that runs. |

`--insecure` and `-v` also work with `versions`, `fix-imports` and `pip-install`.

---

## How protoc is chosen

| `--protoc` | Behaviour |
|---|---|
| `auto` (default) | Uses `protoc` from `PATH` if installed. Otherwise installs `grpcio-tools==1.71.0` into the venv and uses its bundled `protoc`. |
| `system` | Requires `protoc` on `PATH`. Add `--install-protoc` to have it installed via `apt-get`. |
| `grpcio-tools` | Always uses the `protoc` bundled with `grpcio-tools`. Its version matches `protobuf==5.29.5`. |

Older Debian and Ubuntu releases ship an old `protoc`. Files it generates may not import with `protobuf 5.29.5`. If the final import check fails, re-run with `--protoc grpcio-tools`.

---

## Troubleshooting

| Message | What to do |
|---|---|
| `could not load the list of API versions` | No access to `interactivebrokers.github.io`. Check your network or proxy. |
| `TLS verification failed … Re-run with --insecure` | You're behind a proxy that intercepts TLS. Re-run with `--insecure`. |
| `API version … is not currently available` | Pick one from `python3 ibapi_case_setup.py versions`. |
| `could not create a virtualenv` | `sudo apt-get install -y python3-venv` |
| `protoc not found` | Install `protobuf-compiler`, add `--install-protoc`, or use `--protoc grpcio-tools`. |
| `ibapi installed but failed to import` | Usually a protoc/protobuf version mismatch. Re-run with `--protoc grpcio-tools`. |
| `base path exists but is not a directory` | Something at `--base-dir` is a file. Move it or choose another `--base-dir`. |

Re-running `setup` on an existing case is safe: it reuses the venv and the downloaded source.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest
```

The tests don't need network access. Downloads, the IBKR version page, pip and protoc are all mocked.

### Project layout

```
ibapi-case-setup/
├── ibapi_case_setup.py          # the tool (single file, stdlib only)
├── setup.py                     # packaging; installs the `ibapi-case-setup` command
├── requirements.txt             # dev/test dependencies + pinned versions the tool installs
├── README.md
├── .gitignore
└── tests/
    └── test_ibapi_case_setup.py
```
