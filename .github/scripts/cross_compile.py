#!/usr/bin/env python3
"""Prepare Ubuntu runners for aarch64 PyO3 cross-compilation.

This script moves the cross-compilation setup out of workflow YAML so the logic
is easier to maintain and test. It intentionally documents *why* each step
exists because PyO3 + cross-arch Python packaging on Ubuntu has several gotchas.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AARCH64_LIB_DIR = Path("/usr/lib/aarch64-linux-gnu")
AARCH64_PYTHON_DIR = AARCH64_LIB_DIR / "python3.13"


def run(
    command: list[str], *, capture_output: bool = False, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command and fail fast with useful stderr/stdout passthrough."""
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        cwd=cwd,
    )


def ensure_arm64_apt_sources() -> None:
    """Enable arm64 apt architecture and keep existing sources amd64-only.

    We add arm64 via a dedicated ubuntu-ports source below. Existing sources are
    constrained to amd64 to avoid 404s on archives that do not publish arm64.
    """
    run(["sudo", "dpkg", "--add-architecture", "arm64"])

    sources_list = Path("/etc/apt/sources.list")
    if sources_list.exists():
        run(
            [
                "sudo",
                "sed",
                "-i",
                "s/^deb \\(http\\)/deb [arch=amd64] \\1/",
                str(sources_list),
            ]
        )

    for source_file in sorted(Path("/etc/apt/sources.list.d").glob("*.sources")):
        content = source_file.read_text(encoding="utf-8")
        if "Architectures:" not in content:
            run(
                [
                    "sudo",
                    "sed",
                    "-i",
                    "/^Types: deb/a Architectures: amd64",
                    str(source_file),
                ]
            )


def configure_deadsnakes_for_arm64() -> None:
    """Add deadsnakes PPA and ensure it serves both amd64 and arm64."""
    run(["sudo", "add-apt-repository", "-y", "-n", "ppa:deadsnakes/ppa"])

    for source_file in sorted(
        Path("/etc/apt/sources.list.d").glob("*deadsnakes*.sources")
    ):
        content = source_file.read_text(encoding="utf-8")
        if "Architectures:" in content:
            run(
                [
                    "sudo",
                    "sed",
                    "-i",
                    "s/^Architectures:.*/Architectures: amd64 arm64/",
                    str(source_file),
                ]
            )
        else:
            run(
                [
                    "sudo",
                    "sed",
                    "-i",
                    "/^Types:/a Architectures: amd64 arm64",
                    str(source_file),
                ]
            )


def add_ubuntu_ports_source() -> None:
    """Add arm64 package sources from ubuntu-ports for the current codename."""
    codename = run(["lsb_release", "-cs"], capture_output=True).stdout.strip()
    ports_file = Path("/tmp/ubuntu-arm64-ports.list")
    ports_file.write_text(
        "\n".join(
            [
                f"deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports {codename} main restricted universe",
                f"deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports {codename}-updates main restricted universe",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run(
        [
            "sudo",
            "cp",
            str(ports_file),
            "/etc/apt/sources.list.d/ubuntu-arm64-ports.list",
        ]
    )


def install_cross_dependencies() -> None:
    """Install linker and target Python libs without target interpreter postinst."""
    run(["sudo", "apt-get", "update", "-q"])
    run(
        [
            "sudo",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "gcc-aarch64-linux-gnu",
            "libpython3.13-dev:arm64",
            "libpython3.13-stdlib:arm64",
        ]
    )


def stage_single_sysconfigdata() -> None:
    """Place exactly one target _sysconfigdata*.py under PYO3_CROSS_LIB_DIR.

    PyO3 scans PYO3_CROSS_LIB_DIR for _sysconfigdata files; multiple matches can
    cause an ambiguity error. We extract python3.13:arm64 (no postinst scripts
    are executed by dpkg-deb --extract) and copy one discovered file to the
    libdir root while removing duplicates from python3.13/.
    """
    tmp_dir = Path("/tmp/logmedic-py-cross")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    run(["apt-get", "download", "python3.13:arm64"], cwd=tmp_dir)
    pymin_dir = tmp_dir / "pymin"
    pymin_dir.mkdir(parents=True, exist_ok=True)
    deb_candidates = sorted(tmp_dir.glob("python3.13_*.deb"))
    if not deb_candidates:
        raise RuntimeError("Could not download python3.13:arm64 package")
    run(["dpkg-deb", "--extract", str(deb_candidates[0]), str(pymin_dir)])

    search_roots = [
        Path("/usr/lib/python3.13"),
        AARCH64_PYTHON_DIR,
        pymin_dir,
    ]
    candidates = sorted(
        candidate
        for root in search_roots
        if root.exists()
        for candidate in root.rglob("_sysconfigdata*.py")
    )

    if not candidates:
        raise RuntimeError("Could not locate _sysconfigdata*.py for arm64 Python 3.13")

    sysconfigdata_path = candidates[0]
    run(["sudo", "mkdir", "-p", str(AARCH64_PYTHON_DIR)])
    for duplicate in AARCH64_PYTHON_DIR.glob("_sysconfigdata*.py"):
        run(["sudo", "rm", "-f", str(duplicate)])
    run(["sudo", "cp", str(sysconfigdata_path), str(AARCH64_LIB_DIR)])


def write_github_env() -> None:
    """Publish environment variables consumed by the build step in GitHub Actions."""
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        raise RuntimeError("GITHUB_ENV is not set")

    with Path(github_env).open("a", encoding="utf-8") as env_file:
        env_file.write(
            "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc\n"
        )
        env_file.write("PYO3_CROSS_PYTHON_VERSION=3.13\n")
        env_file.write(f"PYO3_CROSS_LIB_DIR={AARCH64_LIB_DIR}\n")


def main() -> None:
    ensure_arm64_apt_sources()
    configure_deadsnakes_for_arm64()
    add_ubuntu_ports_source()
    install_cross_dependencies()
    stage_single_sysconfigdata()
    write_github_env()


if __name__ == "__main__":
    main()
