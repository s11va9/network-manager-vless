# SPDX-License-Identifier: GPL-2.0-or-later
"""Publish packages from CI for the exact tagged commit; never build in Release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import quote

TARGETS = ("ubuntu24.04", "debian13", "fedora44", "fedora45")
ARCHES = ("amd64", "arm64")
ARTIFACTS = {f"package-{target}-{arch}" for target in TARGETS for arch in ARCHES}
WORKFLOW = ".github/workflows/ci.yml"


class ReleaseError(Exception):
    """A release prerequisite failed; do not publish any assets."""


class Repository(TypedDict):
    full_name: str


class Run(TypedDict):
    id: int
    head_sha: str
    head_repository: Repository | None
    path: str
    event: str
    status: str
    conclusion: str | None


class Artifact(TypedDict):
    name: str
    expired: bool


def version_from_tag(tag: str) -> str:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ReleaseError("expected a version tag vX.Y.Z")
    return tag[1:]


class GitHub:
    def __init__(self, repository: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ReleaseError("invalid or missing GITHUB_REPOSITORY")
        self.repository = repository

    @staticmethod
    def command(*arguments: str) -> str:
        executable = shutil.which("gh")
        if executable is None:
            raise ReleaseError("GitHub CLI (gh) is required")
        result = subprocess.run(
            [executable, *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def pages(self, endpoint: str) -> list[dict[str, Any]]:
        result = self.command(
            "api", "--method", "GET", f"repos/{self.repository}/{endpoint}", "--paginate", "--slurp"
        )
        return cast("list[dict[str, Any]]", json.loads(result))

    def tag_sha(self, tag: str) -> str:
        version_from_tag(tag)
        obj = self.pages(f"git/ref/tags/{quote(tag, safe='')}")[0]["object"]
        # Both lightweight and annotated version tags are supported.
        for _ in range(10):
            if obj["type"] == "commit":
                sha = str(obj["sha"])
                if re.fullmatch(r"[0-9a-f]{40}", sha):
                    return sha
                break
            if obj["type"] != "tag":
                break
            obj = self.pages(f"git/tags/{obj['sha']}")[0]["object"]
        raise ReleaseError("version tag does not resolve to a commit")

    def runs(self, sha: str) -> list[Run]:
        return [
            cast("Run", run)
            for page in self.pages(f"actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100")
            for run in page["workflow_runs"]
        ]

    def artifacts(self, run_id: int) -> list[Artifact]:
        return [
            cast("Artifact", artifact)
            for page in self.pages(f"actions/runs/{run_id}/artifacts?per_page=100")
            for artifact in page["artifacts"]
        ]

    def dispatch(self, tag: str) -> None:
        self.command(
            "api",
            "--method",
            "POST",
            f"repos/{self.repository}/actions/workflows/ci.yml/dispatches",
            "-f",
            f"ref={tag}",
        )


def newest_trusted_run(runs: list[Run], sha: str, repository: str) -> Run | None:
    trusted = [
        run
        for run in runs
        if run["head_sha"] == sha
        and run["path"] == WORKFLOW
        and run["event"] in {"push", "workflow_dispatch"}
        and run["head_repository"] is not None
        and run["head_repository"]["full_name"] == repository
    ]
    return max(trusted, key=lambda run: run["id"], default=None)


def complete_artifacts(artifacts: list[Artifact]) -> bool:
    packages = [artifact for artifact in artifacts if artifact["name"].startswith("package-")]
    return (
        len(packages) == len(ARTIFACTS)
        and {artifact["name"] for artifact in packages} == ARTIFACTS
        and not any(artifact["expired"] for artifact in packages)
    )


def wait_for_ci(
    api: GitHub,
    tag: str,
    sha: str,
    *,
    timeout: float = 3600,
    grace: float = 60,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    version_from_tag(tag)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("invalid commit SHA")
    started = now()
    dispatched = False
    while now() - started < timeout:
        run = newest_trusted_run(api.runs(sha), sha, api.repository)
        if run is not None and run["status"] == "completed":
            if run["conclusion"] != "success":
                raise ReleaseError(f"CI run {run['id']} did not succeed; rerun CI, then Release")
            if complete_artifacts(api.artifacts(run["id"])):
                return run["id"]
        missing = run is None or run["status"] == "completed"
        if missing and not dispatched and now() - started >= grace:
            if api.tag_sha(tag) != sha:
                raise ReleaseError("version tag changed while waiting for CI")
            print("No reusable CI artifacts; dispatching CI once for the tag", file=sys.stderr)
            api.dispatch(tag)
            dispatched = True
        sleep(15)
    raise ReleaseError("timed out waiting for CI; rerun Release after CI completes")


def validate_packages(directory: Path, tag: str) -> None:
    version = version_from_tag(tag)
    expected = {
        f"network-manager-vless_{version}+{distro}_{arch}.deb"
        for distro in ("ubuntu24.04", "deb13")
        for arch in ARCHES
    }
    expected |= {
        f"NetworkManager-vless-{version}-1.fc{fedora}.{arch}.rpm"
        for fedora in (44, 45)
        for arch in ("x86_64", "aarch64")
    }
    files = sorted(directory.iterdir())
    if {path.name for path in files} != expected:
        raise ReleaseError("CI artifacts must contain exactly the eight expected DEB/RPM files")
    checksums = []
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ReleaseError(f"invalid package file: {path.name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("tag", "wait", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        if name in {"tag", "wait"}:
            command.add_argument("--sha", required=name == "wait")
        else:
            command.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_packages(args.directory, args.tag)
        else:
            api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""))
            if args.command == "tag":
                sha = api.tag_sha(args.tag)
                if args.sha and sha != args.sha:
                    raise ReleaseError("version tag changed after CI selection")
                print(f"sha={sha}")
            else:
                print(f"run_id={wait_for_ci(api, args.tag, args.sha)}")
    except (ReleaseError, subprocess.CalledProcessError, OSError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
