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
  2. reads the IBKR download page for the Linux packages currently offered
     (Stable and Latest), accepts only one of those (stable, latest, 1051
     or 1051.01), downloads the Linux installer and unpacks it into
     <case>/tws-api/<version>/
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
  python3 ibapi_case_setup.py setup --case my_case --api-version latest
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
import tarfile
import venv
import zipfile
from dataclasses import dataclass, field, replace
from html import unescape
from pathlib import Path
from typing import List, Optional, Sequence, Union
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import urlopen

__version__ = "1.0.0"

log = logging.getLogger("ibapi_case_setup")

DEFAULT_BASE_DIR = "~/docs/api/cases"
# IBKR download page that lists the API packages currently offered.
DOWNLOAD_INDEX_URL = "https://download2.interactivebrokers.com/installers/tws-api/index.html"
# Package links are only followed to these hosts (and their subdomains), over https.
TRUSTED_DOWNLOAD_HOSTS = ("interactivebrokers.com", "interactivebrokers.github.io")
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
# Linux packages, e.g. tws-api-latest-linux-x64.sh (also the older twsapi_macunix.X.Y.zip)
LINUX_PACKAGE_NAME_RE = re.compile(
    r"(?i)(?:linux[^/]*\.(?:sh|zip|tar\.gz|tgz)|twsapi_macunix\.\d{3,5}\.\d{2}\.zip)$"
)
VERSION_IN_NAME_RE = re.compile(r"(?i)twsapi_macunix\.(\d{3,5})\.(\d{2})\.zip$")
# Section header text: "Version: <strong>1051.01</strong>"
VERSION_TEXT_RE = re.compile(r"(?i)Version:\s*(?:&nbsp;|\s|<[^>]*>)*(\d{3,5})\.(\d{2})\b")
# Section title: "<h2><span>Latest</span></h2>"
SECTION_LABEL_RE = re.compile(r"(?i)<h[1-6][^>]*>\s*(?:<[^>]*>\s*)*(Stable|Latest)\b")
LABEL_IN_URL_RE = re.compile(r"(?i)(?:^|[/_.-])(stable|latest)(?=[/_.-]|$)")
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
    """All filesystem locations for one case, derived from the case directory.

    `source_root` is the folder that contains `pythonclient/` (and `proto/`).
    It is found after the package is unpacked; the default is the classic
    <case>/IBJts/source layout.
    """

    case_dir: Path
    source_root: Optional[Path] = None

    @classmethod
    def for_case(cls, case_name: str, base_dir: PathLike = DEFAULT_BASE_DIR) -> "CasePaths":
        if not case_name or "/" in case_name or case_name in (".", ".."):
            raise ValueError(f"invalid case name: {case_name!r}")
        return cls(Path(base_dir).expanduser().resolve() / case_name)

    @property
    def source_dir(self) -> Path:
        return self.source_root or self.case_dir / "IBJts" / "source"

    @property
    def packages_dir(self) -> Path:
        """Unpacked API packages, one folder per version: <case>/tws-api/<version>."""
        return self.case_dir / "tws-api"

    @property
    def downloads_dir(self) -> Path:
        return self.case_dir / "downloads"

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


# ------------------------------------------------------- available API versions


@dataclass(frozen=True)
class ApiRelease:
    """One Linux API package offered on the IBKR download page."""

    version: str  # e.g. "1051"
    patch: str  # e.g. "01"
    label: Optional[str] = None  # "stable" / "latest"
    # Exact link from the page. Not part of equality: a release is identified
    # by its version, wherever it is hosted.
    link: str = field(default="", compare=False)

    @property
    def full_version(self) -> str:
        return f"{self.version}.{self.patch}"

    @property
    def url(self) -> str:
        return self.link

    def __str__(self) -> str:
        return f"{self.full_version} ({self.label})" if self.label else self.full_version


def _version_key(release: ApiRelease):
    return (int(release.version), int(release.patch))


def is_trusted_download_url(url: str) -> bool:
    """Only https links on IBKR-owned hosts may be downloaded."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in TRUSTED_DOWNLOAD_HOSTS
    )


def _url_basename(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def parse_available_releases(html: str, index_url: str = DOWNLOAD_INDEX_URL) -> List[ApiRelease]:
    """Find the Linux packages on the download page, with their version and label.

    The page has one section per channel (Stable, Latest). Each section starts
    with a title and "Version: 1051.01", followed by one link per platform. For
    every Linux link, the version and label are the last ones that appear
    between the previous Linux link and this one. Links are resolved against
    `index_url`; links outside TRUSTED_DOWNLOAD_HOSTS are ignored, and so are
    Linux links without a version (they could not be checked).
    """
    releases = {}
    prev_end = 0
    for m in HREF_RE.finditer(html):
        link = urljoin(index_url, unescape(m.group(1)).strip())
        name = _url_basename(link)
        if not LINUX_PACKAGE_NAME_RE.search(name):
            continue
        window = html[prev_end: m.start()]
        prev_end = m.end()
        if not is_trusted_download_url(link):
            log.warning("Ignoring package link on an untrusted host: %s", link)
            continue

        named = VERSION_IN_NAME_RE.search(name)
        versions = VERSION_TEXT_RE.findall(window)
        if named:
            version, patch = named.groups()
        elif versions:
            version, patch = versions[-1]
        else:
            log.warning("Ignoring Linux package without a version on the page: %s", link)
            continue

        labels = SECTION_LABEL_RE.findall(window)
        url_label = LABEL_IN_URL_RE.search(urlsplit(link).path)
        label = (labels[-1] if labels else url_label.group(1) if url_label else "").lower() or None

        key = (version, patch)
        if key not in releases:
            releases[key] = ApiRelease(version, patch, label, link)
        elif releases[key].label is None and label:
            releases[key] = ApiRelease(version, patch, label, releases[key].link)
    return sorted(releases.values(), key=_version_key)


def fetch_available_releases(
    index_url: str = DOWNLOAD_INDEX_URL, insecure: bool = False, timeout: int = 30
) -> List[ApiRelease]:
    """Download the IBKR API download page and return the Linux packages it offers."""
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

    releases = parse_available_releases(html, index_url)
    if not releases:
        raise SetupError(
            f"no Linux API packages found on {index_url}; "
            "the page layout or DOWNLOAD_INDEX_URL may be out of date"
        )
    return releases


def select_release(requested: str, releases: Sequence[ApiRelease]) -> ApiRelease:
    """Match the user's input against the available packages.

    Accepts "stable", "latest", a version ("1051") or a full version ("1051.01").
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
            f"invalid API version {requested!r}: use stable, latest, a version like 1051 "
            f"or a full version like 1051.01. Available: {available}"
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


def _check_inside(dest: Path, name: str) -> None:
    target = (dest / name).resolve()
    if target != dest and dest not in target.parents:
        raise SetupError(f"refusing to extract outside {dest}: {name}")


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract, refusing members that would land outside `dest`. Returns file count."""
    dest = Path(dest).resolve()
    members = zf.infolist()
    for member in members:
        _check_inside(dest, member.filename)
    zf.extractall(dest)
    return sum(1 for m in members if not m.is_dir())


def safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> int:
    """Like safe_extract, for tar archives (also checks link targets)."""
    dest = Path(dest).resolve()
    members = tf.getmembers()
    for member in members:
        _check_inside(dest, member.name)
        if member.issym() or member.islnk():
            base = dest / Path(member.name).parent if member.issym() else dest
            target = (base / member.linkname).resolve()
            if target != dest and dest not in target.parents:
                raise SetupError(f"refusing link that points outside {dest}: {member.name}")
    if hasattr(tarfile, "data_filter"):
        tf.extractall(dest, filter="data")
    else:  # Python < 3.12 without the backported filter
        tf.extractall(dest)
    return sum(1 for m in members if m.isfile())


def download_file(url: str, dest_dir: PathLike, insecure: bool = False, timeout: int = 120) -> Path:
    """Stream `url` into dest_dir/<file name>. Returns the saved file."""
    if not is_trusted_download_url(url):
        raise SetupError(f"refusing to download from an untrusted location: {url}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (_url_basename(url) or "package")
    partial = target.with_name(target.name + ".part")
    log.info("Downloading %s", url)
    try:
        with urlopen(url, context=make_ssl_context(insecure), timeout=timeout) as resp, open(partial, "wb") as out:
            size = resp.headers.get("Content-Length")
            if size:
                log.info("Package size: %.1f MB", int(size) / 1e6)
            shutil.copyfileobj(resp, out, 1024 * 1024)
    except URLError as err:
        partial.unlink(missing_ok=True)
        if isinstance(getattr(err, "reason", None), ssl.SSLError):
            raise SetupError(
                f"TLS verification failed for {url}: {err.reason}. "
                "Behind an intercepting proxy? Re-run with --insecure."
            ) from err
        raise SetupError(f"download failed for {url}: {err}") from err
    except OSError as err:
        partial.unlink(missing_ok=True)
        raise SetupError(f"download failed for {url}: {err}") from err
    partial.replace(target)
    log.info("Saved %s", target)
    return target


def detect_package_kind(package: PathLike) -> str:
    """Return "zip", "tar", "makeself", "install4j" or "unknown".

    Archives are checked first so that, where possible, the package is unpacked
    without running the installer script at all.
    """
    package = Path(package)
    if zipfile.is_zipfile(package):  # also finds a zip appended to a shell script
        return "zip"
    if tarfile.is_tarfile(package):
        return "tar"
    with open(package, "rb") as fh:
        head = fh.read(64 * 1024)
    if b"makeself" in head.lower():
        return "makeself"
    if b"install4j" in head.lower():
        return "install4j"
    return "unknown"


def unpack_package(package: PathLike, dest: PathLike, timeout: int = 900) -> str:
    """Unpack a downloaded Linux API package into `dest`. Returns the kind found."""
    package, dest = Path(package), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    kind = detect_package_kind(package)
    log.info("Unpacking %s (%s) into %s", package.name, kind, dest)
    if kind == "zip":
        with zipfile.ZipFile(package) as zf:
            safe_extract(zf, dest)
    elif kind == "tar":
        with tarfile.open(package) as tf:
            safe_extract_tar(tf, dest)
    elif kind == "makeself":
        # Self-extracting archive: unpack only, never run its install step.
        run(["sh", package, "--nox11", "--noexec", "--target", dest], timeout=timeout)
    elif kind == "install4j":
        # IBKR's TWS installers are install4j; -q = unattended, -dir = target folder.
        try:
            run(["sh", package, "-q", "-overwrite", "-dir", dest], timeout=timeout)
        except SetupError as err:
            raise SetupError(
                f"the installer {package.name} failed in unattended mode:\n{err}\n"
                f"Try running it by hand: sh {package} -q -dir {dest}"
            ) from err
    else:
        raise SetupError(
            f"don't know how to unpack {package.name}: not a zip, tar, makeself or install4j package. "
            f"Check what it is with `head -c 3000 {package} | strings | head -40`."
        )
    return kind


def find_source_root(root: PathLike) -> Optional[Path]:
    """Find the folder that holds `pythonclient/ibapi` under `root` (shallowest wins)."""
    root = Path(root)
    found: List[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", "node_modules")]
        if "pythonclient" in dirnames and (Path(dirpath) / "pythonclient" / "ibapi").is_dir():
            found.append(Path(dirpath))
    return min(found, key=lambda p: (len(p.parts), str(p))) if found else None


def read_package_version(source_root: Path) -> Optional[str]:
    """Version from IBJts/API_VersionNum.txt (e.g. "API_Version=10.51.01" -> "1051.01")."""
    for folder in (source_root, *list(source_root.parents)[:3]):
        f = folder / "API_VersionNum.txt"
        if f.is_file():
            m = re.search(r"(\d+)\.(\d{2})\.(\d{2})", f.read_text(errors="replace"))
            if m:
                return f"{m.group(1)}{m.group(2)}.{m.group(3)}"
    return None


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


def prepare_source(paths: CasePaths, release: ApiRelease, insecure: bool = False, force: bool = False) -> CasePaths:
    """Make sure the release is unpacked under <case>/tws-api/<version>; return paths pointing at it."""
    package_dir = paths.packages_dir / release.full_version
    source_root = find_source_root(package_dir) if package_dir.is_dir() else None

    if source_root and not force:
        log.info("API %s already unpacked, skipping download (use --force-download to refresh)", release)
    else:
        if package_dir.exists():
            log.info("Removing previous unpack of %s: %s", release.full_version, package_dir)
            shutil.rmtree(package_dir)
        package = download_file(release.url, paths.downloads_dir / release.full_version, insecure=insecure)
        unpack_package(package, package_dir)
        source_root = find_source_root(package_dir)

    if source_root is None:
        hint = " IBKR currently ships the Python API only in Latest; try `-a latest`." if release.label != "latest" else ""
        raise SetupError(
            f"no Python API (pythonclient/ibapi) found in the {release} package under {package_dir}.{hint}"
        )

    found = read_package_version(source_root)
    if found and found != release.full_version:
        log.warning(
            "The page lists %s, but the downloaded package says %s (IBKR may have updated it since).",
            release.full_version, found,
        )
    log.info("API source: %s", source_root)
    return replace(paths, source_root=source_root)


def cmd_setup(args: argparse.Namespace) -> int:
    paths = CasePaths.for_case(args.case, args.base_dir)
    releases = fetch_available_releases(insecure=args.insecure)
    release = select_release(args.api_version, releases)
    log.info("Selected API version %s", release)
    ensure_base_dir(args.base_dir)
    if paths.case_dir.is_dir():
        log.info("Case directory already exists: %s", paths.case_dir)
    else:
        paths.case_dir.mkdir()
        log.info("Created case directory: %s", paths.case_dir)

    paths = prepare_source(paths, release, insecure=args.insecure, force=args.force_download)

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
            "  %(prog)s setup -c my_case -a latest\n"
            "  %(prog)s setup -c my_case -a 1051\n"
            "  %(prog)s my_case 1051.01\n"
            "  %(prog)s fix-imports ./IBJts/source/pythonclient/ibapi/protobuf\n"
            "  %(prog)s pip-install requests --python ~/docs/api/cases/my_case/.venv/bin/python\n"
            "\n"
            "Only the Linux API versions currently listed on " + DOWNLOAD_INDEX_URL + " are accepted.\n"
            "IBKR currently ships the Python API only in the Latest package."
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
        help="stable, latest, a version like 1051, or a full version like 1051.01; "
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
    s.add_argument("--force-download", action="store_true", help="download and unpack again even if this version is already in the case")

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
            parser.error("setup needs a case name, e.g. `setup -c my_case -a latest`")
        if not args.api_version:
            parser.error("setup needs an API version (-a latest, -a 1051); see the `versions` command")
        if not VERSION_INPUT_RE.fullmatch(args.api_version):
            parser.error(
                f"invalid API version {args.api_version!r}: use stable, latest, "
                "a version like 1051 or a full version like 1051.01"
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
