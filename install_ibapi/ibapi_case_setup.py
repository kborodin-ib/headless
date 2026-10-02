#!/usr/bin/env python3
"""
ibapi_case_setup - prepare an isolated IBKR TWS API (ibapi) Python environment
for a support case, on any Debian-based Linux system.

Merges three former scripts:
  * fix_proto_imports.py  -> `fix-imports` command / update_pb2_imports()
  * pip install helper    -> `pip-install` command / pip_install()
  * setup-script.py       -> `setup` command (the default)

What `setup` does:
  1. creates ~/docs/api/cases (if missing) and ~/docs/api/cases/<case>
  2. checks the IBKR download page for the API versions currently offered,
     accepts only one of those (stable, latest, 1050 or 1050.02), then
     downloads and extracts twsapi_macunix.<version>.<patch>.zip
  3. creates a virtualenv in <case>/.venv
     (Debian 12+ / Ubuntu 23.04+ refuse system-wide pip installs, PEP 668)
  4. compiles IBJts/source/proto/*.proto into pythonclient/ibapi/protobuf
     using the system `protoc` if present, otherwise the protoc bundled
     with grpcio-tools (no apt/sudo needed)
  5. rewrites `import X_pb2 as X__pb2` -> `import ibapi.protobuf.X_pb2 as X__pb2`
  6. pip-installs protobuf + the pythonclient into the venv and verifies
     that `import ibapi.client` works

Usage:
  python3 ibapi_case_setup.py versions                    # list available API versions
  python3 ibapi_case_setup.py setup --case my_case --api-version stable
  python3 ibapi_case_setup.py my_case 1051                # legacy positional form
  python3 ibapi_case_setup.py fix-imports ./some/dir
  python3 ibapi_case_setup.py pip-install requests --python ~/docs/api/cases/my_case/.venv/bin/python

Running it needs only the Python standard library (Python 3.8+).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import venv
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Sequence, Union
from urllib.error import URLError
from urllib.request import urlopen

__version__ = "1.0.0"

log = logging.getLogger("ibapi_case_setup")

DEFAULT_BASE_DIR = "~/docs/api/cases"
DOWNLOAD_URL_TEMPLATE = (
    "https://interactivebrokers.github.io/downloads/twsapi_macunix.{version}.{patch}.zip"
)
DOWNLOAD_INDEX_URL = "https://interactivebrokers.github.io/"
# Package links on the download page; must stay in sync with DOWNLOAD_URL_TEMPLATE.
RELEASE_LINK_RE = re.compile(r"twsapi_macunix\.(\d{3,5})\.(\d{2})\.zip", re.IGNORECASE)
LABEL_RE = re.compile(r"\b(Stable|Latest)\b", re.IGNORECASE)
PROTOBUF_REQUIREMENT = "protobuf==5.29.5"
GRPCIO_TOOLS_REQUIREMENT = "grpcio-tools==1.71.0"  # bundles a protoc matching protobuf 5.29
TRUSTED_HOSTS = ("pypi.org", "pypi.python.org", "files.pythonhosted.org")
PB2_IMPORT_RE = re.compile(r"^(import\s+)(\w+_pb2)(\s+as\s+\w+__pb2)", re.MULTILINE)
COMMANDS = ("setup", "versions", "fix-imports", "pip-install")
VERSION_INPUT_RE = re.compile(r"(?i)stable|latest|\d{3,5}(\.\d{2})?")

PathLike = Union[str, Path]


class SetupError(RuntimeError):
    """A step failed in a way the user needs to act on."""


# --------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class CasePaths:
    """All filesystem locations for one case, derived from the case directory."""

    case_dir: Path

    @classmethod
    def for_case(cls, case_name: str, base_dir: PathLike = DEFAULT_BASE_DIR) -> "CasePaths":
        if not case_name or "/" in case_name or case_name in (".", ".."):
            raise ValueError(f"invalid case name: {case_name!r}")
        return cls(Path(base_dir).expanduser().resolve() / case_name)

    @property
    def source_dir(self) -> Path:
        return self.case_dir / "IBJts" / "source"

    @property
    def proto_dir(self) -> Path:
        return self.source_dir / "proto"

    @property
    def pythonclient_dir(self) -> Path:
        return self.source_dir / "pythonclient"

    @property
    def protobuf_out_dir(self) -> Path:
        return self.pythonclient_dir / "ibapi" / "protobuf"

    @property
    def venv_dir(self) -> Path:
        return self.case_dir / ".venv"


def ensure_base_dir(base_dir: PathLike = DEFAULT_BASE_DIR) -> Path:
    """Make sure the directory that holds all cases exists, creating it if needed."""
    base = Path(base_dir).expanduser().resolve()
    if base.is_dir():
        log.info("Base directory exists: %s", base)
        return base
    if base.exists():
        raise SetupError(f"base path exists but is not a directory: {base}")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise SetupError(f"could not create base directory {base}: {err}") from err
    log.info("Created base directory: %s", base)
    return base


def build_download_url(api_version: str, patch: str = "01") -> str:
    if not re.fullmatch(r"\d{3,5}", api_version or ""):
        raise ValueError(f"API version must be digits like 1037, got {api_version!r}")
    if not re.fullmatch(r"\d{2}", patch or ""):
        raise ValueError(f"patch must be two digits like 01, got {patch!r}")
    return DOWNLOAD_URL_TEMPLATE.format(version=api_version, patch=patch)


# ------------------------------------------------------- available API versions


@dataclass(frozen=True)
class ApiRelease:
    """One Mac/Linux API package offered on the IBKR download page."""

    version: str  # e.g. "1050"
    patch: str  # e.g. "02"
    label: Optional[str] = None  # "stable" / "latest" when the page says so

    @property
    def full_version(self) -> str:
        return f"{self.version}.{self.patch}"

    @property
    def url(self) -> str:
        return build_download_url(self.version, self.patch)

    def __str__(self) -> str:
        return f"{self.full_version} ({self.label})" if self.label else self.full_version


def _version_key(release: ApiRelease):
    return (int(release.version), int(release.patch))


def parse_available_releases(html: str) -> List[ApiRelease]:
    """Find every twsapi_macunix.<version>.<patch>.zip link on the download page.

    The Stable/Latest label is taken from the closest mention between the
    previous package link and this one.
    """
    releases = {}
    prev_end = 0
    for match in RELEASE_LINK_RE.finditer(html):
        version, patch = match.group(1), match.group(2)
        window = html[max(prev_end, match.start() - 2000): match.start()]
        labels = LABEL_RE.findall(window)
        label = labels[-1].lower() if labels else None
        prev_end = match.end()
        key = (version, patch)
        if key not in releases or (releases[key].label is None and label):
            releases[key] = ApiRelease(version, patch, label)
    return sorted(releases.values(), key=_version_key)


def fetch_available_releases(
    index_url: str = DOWNLOAD_INDEX_URL, insecure: bool = False, timeout: int = 30
) -> List[ApiRelease]:
    """Download the IBKR API download page and return the packages it offers."""
    log.info("Checking available API versions at %s", index_url)
    try:
        with urlopen(index_url, context=make_ssl_context(insecure), timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except URLError as err:
        if isinstance(getattr(err, "reason", None), ssl.SSLError):
            raise SetupError(
                f"TLS verification failed for {index_url}: {err.reason}. "
                "Behind an intercepting proxy? Re-run with --insecure."
            ) from err
        raise SetupError(f"could not load the list of API versions from {index_url}: {err}") from err
    except OSError as err:
        raise SetupError(f"could not load the list of API versions from {index_url}: {err}") from err

    releases = parse_available_releases(html)
    if not releases:
        raise SetupError(
            f"no twsapi_macunix packages found on {index_url}; "
            "the page layout or DOWNLOAD_URL_TEMPLATE may be out of date"
        )
    return releases


def select_release(requested: str, releases: Sequence[ApiRelease]) -> ApiRelease:
    """Match the user's input against the available packages.

    Accepts "stable", "latest", a version ("1050") or a full version ("1050.02").
    Anything not currently offered on the download page is rejected.
    """
    wanted = (requested or "").strip().lower()
    available = ", ".join(str(r) for r in releases)

    if wanted in ("stable", "latest"):
        labelled = [r for r in releases if r.label == wanted]
        if labelled:
            return max(labelled, key=_version_key)
        if wanted == "latest" and releases:
            return max(releases, key=_version_key)
        raise SetupError(f"no {wanted!r} package found. Available: {available}")

    match = re.fullmatch(r"(\d{3,5})(?:\.(\d{2}))?", wanted)
    if not match:
        raise SetupError(
            f"invalid API version {requested!r}: use stable, latest, a version like 1050 "
            f"or a full version like 1050.02. Available: {available}"
        )
    version, patch = match.groups()
    candidates = [r for r in releases if r.version == version and (patch is None or r.patch == patch)]
    if not candidates:
        raise SetupError(f"API version {requested} is not currently available. Available: {available}")
    return max(candidates, key=_version_key)


# ------------------------------------------------------------------ process helpers


def run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with check=True and errors turned into SetupError."""
    cmd = [str(c) for c in cmd]
    log.debug("$ %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except FileNotFoundError as err:
        raise SetupError(f"command not found: {cmd[0]}") from err
    except subprocess.CalledProcessError as err:
        detail = (err.stderr or "").strip() if isinstance(err.stderr, str) else ""
        msg = f"command failed (exit {err.returncode}): {' '.join(cmd)}"
        raise SetupError(f"{msg}\n{detail}" if detail else msg) from err


# ------------------------------------------------------------- 1. fix pb2 imports


def fix_pb2_import_text(text: str) -> str:
    """`import foo_pb2 as foo__pb2` -> `import ibapi.protobuf.foo_pb2 as foo__pb2`.

    Idempotent: already-qualified imports don't match the pattern.
    """
    return PB2_IMPORT_RE.sub(r"\1ibapi.protobuf.\2\3", text)


def update_pb2_imports(directory: PathLike) -> List[Path]:
    """Rewrite pb2 imports in every .py file under `directory`. Returns changed files."""
    directory = Path(directory)
    if not directory.is_dir():
        raise SetupError(f"not a directory: {directory}")
    changed: List[Path] = []
    for path in sorted(directory.rglob("*.py")):
        original = path.read_text(encoding="utf-8")
        updated = fix_pb2_import_text(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
            log.debug("Updated imports: %s", path)
    log.info("Fixed pb2 imports in %d file(s) under %s", len(changed), directory)
    return changed


# ------------------------------------------------------------------ 2. pip install


def build_pip_command(
    python: PathLike, packages: Sequence[PathLike], trusted_hosts: bool = False
) -> List[str]:
    cmd = [str(python), "-m", "pip", "install"]
    if trusted_hosts:
        for host in TRUSTED_HOSTS:
            cmd += ["--trusted-host", host]
    cmd += [str(p) for p in packages]
    return cmd


def pip_install(python: PathLike, packages: Sequence[PathLike], trusted_hosts: bool = False) -> None:
    log.info("pip install %s", " ".join(str(p) for p in packages))
    run(build_pip_command(python, packages, trusted_hosts))


# ------------------------------------------------------------ 3. download/extract


def make_ssl_context(insecure: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract, refusing members that would land outside `dest`. Returns file count."""
    dest = Path(dest).resolve()
    members = zf.infolist()
    for member in members:
        target = (dest / member.filename).resolve()
        if target != dest and dest not in target.parents:
            raise SetupError(f"refusing to extract outside {dest}: {member.filename}")
    zf.extractall(dest)
    return sum(1 for m in members if not m.is_dir())


def download_and_extract(url: str, dest: PathLike, insecure: bool = False, timeout: int = 120) -> int:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    try:
        with urlopen(url, context=make_ssl_context(insecure), timeout=timeout) as resp:
            size = resp.headers.get("Content-Length")
            if size:
                log.info("Package size: %.1f MB", int(size) / 1e6)
            data = resp.read()
    except URLError as err:
        if isinstance(getattr(err, "reason", None), ssl.SSLError):
            raise SetupError(
                f"TLS verification failed for {url}: {err.reason}. "
                "Behind an intercepting proxy? Re-run with --insecure."
            ) from err
        raise SetupError(f"download failed for {url}: {err} (check the API version)") from err
    except OSError as err:
        raise SetupError(f"download failed for {url}: {err}") from err

    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            count = safe_extract(zf, dest)
    except zipfile.BadZipFile as err:
        raise SetupError(f"{url} did not return a valid zip archive") from err
    log.info("Extracted %d files to %s", count, dest)
    return count


# ------------------------------------------------------------------- 4. virtualenv


def venv_python(venv_dir: Path) -> Path:
    return Path(venv_dir) / "bin" / "python"


def ensure_venv(venv_dir: Path) -> Path:
    python = venv_python(venv_dir)
    if python.exists():
        log.info("Reusing virtualenv %s", venv_dir)
        return python
    log.info("Creating virtualenv %s", venv_dir)
    try:
        venv.EnvBuilder(with_pip=True).create(str(venv_dir))
    except (subprocess.CalledProcessError, OSError) as err:
        shutil.rmtree(venv_dir, ignore_errors=True)
        raise SetupError(
            "could not create a virtualenv. On Debian/Ubuntu run: "
            "sudo apt-get install -y python3-venv"
        ) from err
    return python


# ---------------------------------------------------------------------- 5. protoc


def install_protoc_apt() -> None:
    if not shutil.which("apt-get"):
        raise SetupError("apt-get not found; this does not look like a Debian-based system")
    prefix: List[str] = []
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise SetupError("need root or sudo to apt-get install protobuf-compiler")
        prefix = ["sudo"]
    run(prefix + ["apt-get", "update"])
    run(prefix + ["apt-get", "install", "-y", "protobuf-compiler"])


def resolve_protoc(
    backend: str, python: PathLike, insecure: bool = False, install_system: bool = False
) -> List[str]:
    """Return the command prefix that invokes protoc.

    backend: "system" (protoc on PATH), "grpcio-tools" (pip-installed into the
    target interpreter), or "auto" (system if available, else grpcio-tools).
    """
    if backend in ("auto", "system"):
        exe = shutil.which("protoc")
        if exe is None and install_system:
            install_protoc_apt()
            exe = shutil.which("protoc")
        if exe:
            version = run([exe, "--version"], capture_output=True, text=True).stdout.strip()
            log.info("Using system protoc: %s (%s)", exe, version)
            return [exe]
        if backend == "system":
            raise SetupError(
                "protoc not found. Install it with `sudo apt-get install -y protobuf-compiler`, "
                "or re-run with --install-protoc or --protoc grpcio-tools"
            )
        log.info("protoc not on PATH, falling back to the one bundled with grpcio-tools")
    pip_install(python, [GRPCIO_TOOLS_REQUIREMENT, PROTOBUF_REQUIREMENT], trusted_hosts=insecure)
    return [str(python), "-m", "grpc_tools.protoc"]


def build_protoc_command(
    protoc: Sequence[str], proto_dir: Path, out_dir: Path, proto_files: Sequence[Path]
) -> List[str]:
    return (
        list(protoc)
        + [f"--proto_path={proto_dir}", f"--python_out={out_dir}"]
        + [str(f) for f in proto_files]
    )


def compile_protos(paths: CasePaths, protoc: Sequence[str]) -> List[Path]:
    proto_files = sorted(paths.proto_dir.glob("*.proto"))
    if not proto_files:
        raise SetupError(f"no .proto files found in {paths.proto_dir}")
    paths.protobuf_out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Compiling %d .proto files into %s", len(proto_files), paths.protobuf_out_dir)
    run(build_protoc_command(protoc, paths.proto_dir, paths.protobuf_out_dir, proto_files))
    return sorted(paths.protobuf_out_dir.glob("*_pb2.py"))


# ---------------------------------------------------------------------- 6. verify


def verify_install(python: PathLike) -> str:
    result = run(
        [str(python), "-c", "import ibapi, ibapi.client; print(ibapi.__file__)"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# -------------------------------------------------------------------- commands


def cmd_setup(args: argparse.Namespace) -> int:
    paths = CasePaths.for_case(args.case, args.base_dir)
    releases = fetch_available_releases(insecure=args.insecure)
    release = select_release(args.api_version, releases)
    url = release.url
    log.info("Selected API version %s", release)
    ensure_base_dir(args.base_dir)
    if paths.case_dir.is_dir():
        log.info("Case directory already exists: %s", paths.case_dir)
    else:
        paths.case_dir.mkdir()
        log.info("Created case directory: %s", paths.case_dir)

    if paths.source_dir.is_dir() and not args.force_download:
        log.info("Source already present, skipping download (use --force-download to refresh)")
    else:
        download_and_extract(url, paths.case_dir, insecure=args.insecure)
    if not paths.pythonclient_dir.is_dir():
        raise SetupError(f"expected {paths.pythonclient_dir} after extraction; archive layout changed?")

    python = Path(args.python) if args.python else ensure_venv(paths.venv_dir)

    if paths.proto_dir.is_dir():
        protoc = resolve_protoc(args.protoc, python, args.insecure, args.install_protoc)
        compile_protos(paths, protoc)
        update_pb2_imports(paths.protobuf_out_dir)
    else:
        log.warning("No proto directory in this API version; skipping protobuf generation")

    pip_install(python, [PROTOBUF_REQUIREMENT, paths.pythonclient_dir], trusted_hosts=args.insecure)
    try:
        location = verify_install(python)
    except SetupError as err:
        raise SetupError(
            f"ibapi installed but failed to import:\n{err}\n"
            "If this is a protobuf version mismatch, re-run with --protoc grpcio-tools"
        ) from err

    log.info("Done. ibapi importable from %s", location)
    if not args.python:
        log.info("Activate with: source %s", paths.venv_dir / "bin" / "activate")
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    for release in fetch_available_releases(insecure=args.insecure):
        print(f"{str(release):<20} {release.url}")
    return 0


def cmd_fix_imports(args: argparse.Namespace) -> int:
    update_pb2_imports(args.directory)
    return 0


def cmd_pip_install(args: argparse.Namespace) -> int:
    pip_install(args.python or sys.executable, args.packages, trusted_hosts=args.insecure)
    return 0


HANDLERS = {
    "setup": cmd_setup,
    "versions": cmd_versions,
    "fix-imports": cmd_fix_imports,
    "pip-install": cmd_pip_install,
}


# ------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    common.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification for the download and pass --trusted-host to pip "
        "(only for intercepting corporate proxies)",
    )

    parser = argparse.ArgumentParser(
        prog="ibapi-case-setup",
        description="Download, build and install the IBKR TWS API (ibapi) for a support case.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s versions\n"
            "  %(prog)s setup -c my_case -a stable\n"
            "  %(prog)s setup -c my_case -a 1050\n"
            "  %(prog)s my_case 1051.01\n"
            "  %(prog)s fix-imports ./IBJts/source/pythonclient/ibapi/protobuf\n"
            "  %(prog)s pip-install requests --python ~/docs/api/cases/my_case/.venv/bin/python\n"
            "\n"
            "Only API versions currently listed on " + DOWNLOAD_INDEX_URL + " are accepted."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("setup", parents=[common], help="full case setup (default command)")
    s.add_argument("case_pos", nargs="?", metavar="CASE", help="case name (positional form)")
    s.add_argument("version_pos", nargs="?", metavar="API_VERSION", help="API version (positional form)")
    s.add_argument("-c", "--case", help="case name, e.g. test_case")
    s.add_argument(
        "-a",
        "--api-version",
        help="stable, latest, a version like 1050, or a full version like 1050.02; "
        "must be listed by the `versions` command",
    )
    s.add_argument("--base-dir", default=DEFAULT_BASE_DIR, help=f"default: {DEFAULT_BASE_DIR}")
    s.add_argument(
        "--python",
        help="install into this interpreter instead of creating <case>/.venv "
        "(system python on Debian 12+ will be refused by pip, PEP 668)",
    )
    s.add_argument(
        "--protoc",
        choices=("auto", "system", "grpcio-tools"),
        default="auto",
        help="protoc source (default: auto = system if found, else grpcio-tools)",
    )
    s.add_argument("--install-protoc", action="store_true", help="apt-get install protobuf-compiler if missing")
    s.add_argument("--force-download", action="store_true", help="re-download even if source exists")

    sub.add_parser("versions", parents=[common], help="list API versions available for download")

    f = sub.add_parser("fix-imports", parents=[common], help="qualify pb2 imports under a directory")
    f.add_argument("directory", nargs="?", default=".", help="directory to scan (default: .)")

    p = sub.add_parser("pip-install", parents=[common], help="pip install packages")
    p.add_argument("packages", nargs="+")
    p.add_argument("--python", help="target interpreter (default: the one running this script)")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Legacy form `script.py CASE VERSION` (and flags without a command) -> `setup`.
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "setup")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup":
        args.case = args.case or args.case_pos
        args.api_version = args.api_version or args.version_pos
        if not args.case:
            parser.error("setup needs a case name, e.g. `setup -c my_case -a stable`")
        if not args.api_version:
            parser.error("setup needs an API version (-a stable, -a 1050); see the `versions` command")
        if not VERSION_INPUT_RE.fullmatch(args.api_version):
            parser.error(
                f"invalid API version {args.api_version!r}: use stable, latest, "
                "a version like 1050 or a full version like 1050.02"
            )
        try:
            CasePaths.for_case(args.case, args.base_dir)
        except ValueError as err:
            parser.error(str(err))
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    try:
        return HANDLERS[args.command](args)
    except SetupError as err:
        log.error("%s", err)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
