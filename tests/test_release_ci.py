# SPDX-License-Identifier: GPL-2.0-or-later
"""Release must reuse trusted, complete CI results without a second package build."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from release_ci import (
    ARTIFACTS,
    Artifact,
    GitHub,
    ReleaseError,
    Run,
    complete_artifacts,
    newest_trusted_run,
    validate_packages,
    version_from_tag,
    wait_for_ci,
)

REPOSITORY = "owner/network-manager-vless"
SHA = "a" * 40
TAG = "v0.6.0"


def run(run_id: int = 1, status: str = "completed", conclusion: str | None = "success") -> Run:
    return Run(
        id=run_id,
        head_sha=SHA,
        head_repository={"full_name": REPOSITORY},
        path=".github/workflows/ci.yml",
        event="push",
        status=status,
        conclusion=conclusion,
    )


def artifacts() -> list[Artifact]:
    return [Artifact(name=name, expired=False) for name in sorted(ARTIFACTS)]


class FakeGitHub(GitHub):
    def __init__(self, snapshots: list[list[Run]]) -> None:
        super().__init__(REPOSITORY)
        self.snapshots = snapshots
        self.polls = 0
        self.seconds = 0.0
        self.dispatched: list[str] = []
        self.expired_runs: set[int] = set()
        self.tag_commit = SHA

    def runs(self, sha: str) -> list[Run]:
        assert sha == SHA
        snapshot = self.snapshots[min(self.polls, len(self.snapshots) - 1)]
        self.polls += 1
        return snapshot

    def artifacts(self, run_id: int) -> list[Artifact]:
        result = artifacts()
        if run_id in self.expired_runs:
            result[0]["expired"] = True
        return result

    def tag_sha(self, tag: str) -> str:
        assert tag == TAG
        return self.tag_commit

    def dispatch(self, tag: str) -> None:
        self.dispatched.append(tag)

    def now(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    def wait(self) -> int:
        return wait_for_ci(self, TAG, SHA, timeout=90, grace=30, now=self.now, sleep=self.sleep)


def test_finished_ci_is_reused_immediately() -> None:
    api = FakeGitHub([[run()]])
    assert api.wait() == 1
    assert api.seconds == 0
    assert api.dispatched == []


def test_simultaneous_branch_and_tag_push_waits_for_existing_ci() -> None:
    api = FakeGitHub(
        [
            [],
            [run(status="queued", conclusion=None)],
            [run(status="in_progress", conclusion=None)],
            [run()],
        ]
    )
    assert api.wait() == 1
    assert api.seconds == 45
    assert api.dispatched == []


def test_tag_only_dispatches_ci_once() -> None:
    api = FakeGitHub([[], [], [], [], [run(2)]])
    assert api.wait() == 2
    assert api.dispatched == [TAG]


def test_expired_artifacts_trigger_one_replacement_build() -> None:
    api = FakeGitHub([[run()]] * 4 + [[run(), run(2)]])
    api.expired_runs.add(1)
    assert api.wait() == 2
    assert api.dispatched == [TAG]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_failed_ci_blocks_release_instead_of_using_an_older_success(conclusion: str) -> None:
    api = FakeGitHub([[run(), run(2, conclusion=conclusion)]])
    with pytest.raises(ReleaseError, match="rerun CI, then Release"):
        api.wait()
    assert api.dispatched == []


@pytest.mark.parametrize("missing", [False, True])
def test_timeout_never_dispatches_more_than_once(missing: bool) -> None:
    api = FakeGitHub([[] if missing else [run(status="in_progress", conclusion=None)]])
    with pytest.raises(ReleaseError, match="timed out"):
        api.wait()
    assert api.dispatched == ([TAG] if missing else [])


def test_moved_tag_cannot_dispatch_a_build_for_another_commit() -> None:
    api = FakeGitHub([[]])
    api.tag_commit = "b" * 40
    with pytest.raises(ReleaseError, match="tag changed"):
        api.wait()
    assert api.dispatched == []


def test_untrusted_ci_runs_are_not_release_candidates() -> None:
    pull_request, fork, other_commit, other_workflow, missing_repo = [run(i) for i in range(2, 7)]
    pull_request["event"] = "pull_request"
    fork["head_repository"] = {"full_name": "someone/fork"}
    other_commit["head_sha"] = "b" * 40
    other_workflow["path"] = ".github/workflows/other.yml"
    missing_repo["head_repository"] = None
    untrusted = [pull_request, fork, other_commit, other_workflow, missing_repo]
    assert newest_trusted_run(untrusted, SHA, REPOSITORY) is None
    assert newest_trusted_run([run(), *untrusted], SHA, REPOSITORY) == run()


def test_manually_dispatched_ci_is_a_release_candidate() -> None:
    candidate = run()
    candidate["event"] = "workflow_dispatch"
    assert newest_trusted_run([candidate], SHA, REPOSITORY) == candidate


@pytest.mark.parametrize("problem", ["missing", "duplicate", "expired", "unexpected"])
def test_incomplete_or_ambiguous_artifacts_are_rejected(problem: str) -> None:
    result = artifacts()
    if problem == "missing":
        result.pop()
    elif problem == "duplicate":
        result.append(result[0])
    elif problem == "expired":
        result[0]["expired"] = True
    else:
        result.append(Artifact(name="package-unknown-amd64", expired=False))
    assert not complete_artifacts(result)


@pytest.mark.parametrize("annotated", [False, True])
def test_both_tag_types_resolve_to_the_commit(
    annotated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = GitHub(REPOSITORY)

    def pages(endpoint: str) -> list[dict[str, Any]]:
        if endpoint == "git/ref/tags/v0.6.0" and annotated:
            return [{"object": {"type": "tag", "sha": "c" * 40}}]
        assert endpoint == (f"git/tags/{'c' * 40}" if annotated else "git/ref/tags/v0.6.0")
        return [{"object": {"type": "commit", "sha": SHA}}]

    monkeypatch.setattr(api, "pages", pages)
    assert api.tag_sha(TAG) == SHA


@pytest.mark.parametrize("tag", ["main", "v1.2", "v01.2.3", "v1.2.3/other", "v1.2.3\nsha=bad"])
def test_invalid_tag_cannot_be_used_in_api_calls_or_workflow_outputs(tag: str) -> None:
    with pytest.raises(ReleaseError, match="version tag"):
        version_from_tag(tag)


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    for name in (
        "network-manager-vless_0.6.0+ubuntu24.04_amd64.deb",
        "network-manager-vless_0.6.0+ubuntu24.04_arm64.deb",
        "network-manager-vless_0.6.0+deb13_amd64.deb",
        "network-manager-vless_0.6.0+deb13_arm64.deb",
        "NetworkManager-vless-0.6.0-1.fc44.x86_64.rpm",
        "NetworkManager-vless-0.6.0-1.fc44.aarch64.rpm",
        "NetworkManager-vless-0.6.0-1.fc45.x86_64.rpm",
        "NetworkManager-vless-0.6.0-1.fc45.aarch64.rpm",
    ):
        (tmp_path / name).write_bytes(name.encode())
    return tmp_path


def test_checksums_cover_every_validated_package(packages: Path) -> None:
    validate_packages(packages, TAG)
    lines = (packages / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 8
    for line in lines:
        digest, name = line.split("  ")
        assert hashlib.sha256((packages / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("problem", ["missing", "extra", "empty", "symlink", "wrong_version"])
def test_invalid_downloads_never_get_a_checksum_manifest(packages: Path, problem: str) -> None:
    package = packages / "network-manager-vless_0.6.0+deb13_amd64.deb"
    if problem in {"missing", "symlink"}:
        package.unlink()
        if problem == "symlink":
            package.symlink_to(packages / "network-manager-vless_0.6.0+deb13_arm64.deb")
    elif problem == "extra":
        (packages / "unexpected.sh").write_text("exit 0\n")
    elif problem == "empty":
        package.write_bytes(b"")
    with pytest.raises(ReleaseError):
        validate_packages(packages, "v0.7.0" if problem == "wrong_version" else TAG)
    assert not (packages / "SHA256SUMS").exists()
