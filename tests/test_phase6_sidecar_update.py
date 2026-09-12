import asyncio
import json
from pathlib import Path

import pytest

from ai_provider_gateway.sidecars.cliproxy.updater import (
    SidecarManifest,
    SidecarManifestError,
    SidecarUpdateError,
    SidecarUpdateManager,
)


SHA = "a" * 64


class FakeLifecycle:
    def __init__(self, fail_start=False):
        self.state = "RUNNING"
        self.fail_start = fail_start
        self.calls = []

    async def stop(self):
        self.calls.append("stop")
        self.state = "STOPPED"

    async def start(self):
        self.calls.append("start")
        if self.fail_start:
            self.fail_start = False
            raise RuntimeError("boom")
        self.state = "RUNNING"


def manifest(version="1.0.0"):
    return {"name": "cliproxy", "version": version, "commit": "abc123", "sha256": SHA,
            "download_source": "https://example.test/release-asset", "installed_at": "2026-01-01T00:00:00Z", "tested": True}


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def make_manager(tmp_path, *, confirm=lambda _: True, verifier=lambda *_: True, fail_start=False):
    install = tmp_path / "install"
    install.mkdir()
    executable = install / "cliproxy.exe"
    executable.write_bytes(b"old")
    write_json(install / "manifest.json", manifest("1.0.0"))
    stage = tmp_path / "stage"
    stage.mkdir()
    backups = tmp_path / "backups"
    lifecycle = FakeLifecycle(fail_start)
    manager = SidecarUpdateManager(executable, install / "manifest.json", stage, backups,
                                   lifecycle, confirm, verifier)
    return manager, executable, stage, backups, lifecycle


def stage_candidate(stage, executable, candidate_id="v2", version="2.0.0"):
    candidate = stage / candidate_id
    candidate.mkdir()
    (candidate / executable.name).write_bytes(b"new")
    write_json(candidate / "manifest.json", manifest(version))


def test_manifest_rejects_unpinned_paths_and_bad_checksum():
    for value in ("latest", "LATEST", "../2.0", "", "2/0"):
        data = manifest(value)
        with pytest.raises(SidecarManifestError):
            SidecarManifest.from_mapping(data)
    data = manifest()
    data["sha256"] = "not-a-hash"
    with pytest.raises(SidecarManifestError):
        SidecarManifest.from_mapping(data)


def test_untested_candidate_is_neither_listed_nor_installable(tmp_path):
    manager, executable, stage, _, _ = make_manager(tmp_path)
    stage_candidate(stage, executable)
    data = manifest("2.0.0")
    data["tested"] = False
    write_json(stage / "v2" / "manifest.json", data)
    assert manager.list_candidates() == []
    with pytest.raises(SidecarUpdateError):
        asyncio.run(manager.install("v2"))


def test_candidate_rejects_file_resolving_outside_candidate_directory(tmp_path, monkeypatch):
    manager, executable, stage, _, _ = make_manager(tmp_path)
    stage_candidate(stage, executable)
    original = manager._regular_file_in

    def reject_artifact(path, parent):
        if path.name == executable.name:
            return False
        return original(path, parent)

    monkeypatch.setattr(manager, "_regular_file_in", reject_artifact)
    assert manager.list_candidates() == []
    with pytest.raises(SidecarUpdateError):
        asyncio.run(manager.install("v2"))


def test_install_refuses_unverified_current_pair_before_mutation(tmp_path):
    calls = []

    def verifier(path, _):
        calls.append(path.read_bytes())
        return path.read_bytes() != b"old"

    manager, executable, stage, backups, lifecycle = make_manager(tmp_path, verifier=verifier)
    stage_candidate(stage, executable)
    with pytest.raises(SidecarUpdateError):
        asyncio.run(manager.install("v2"))
    assert calls and executable.read_bytes() == b"old"
    assert lifecycle.calls == []
    assert not backups.exists()


def test_path_traversal_and_cancel_do_not_change_install(tmp_path):
    manager, executable, stage, _, lifecycle = make_manager(tmp_path, confirm=lambda _: False)
    stage_candidate(stage, executable)
    with pytest.raises(Exception):
        asyncio.run(manager.install("../v2"))
    result = asyncio.run(manager.install("v2"))
    assert result.status == "cancelled"
    assert executable.read_bytes() == b"old"
    assert lifecycle.calls == []


def test_verification_failure_does_not_change_install(tmp_path):
    manager, executable, stage, backups, lifecycle = make_manager(tmp_path, verifier=lambda *_: False)
    stage_candidate(stage, executable)
    with pytest.raises(Exception):
        asyncio.run(manager.install("v2"))
    assert executable.read_bytes() == b"old"
    assert not backups.exists()
    assert lifecycle.calls == []


def test_successful_install_keeps_old_version_in_backup(tmp_path):
    manager, executable, stage, backups, lifecycle = make_manager(tmp_path)
    stage_candidate(stage, executable)
    result = asyncio.run(manager.install("v2"))
    assert result.status == "installed"
    assert executable.read_bytes() == b"new"
    old = next(backups.iterdir())
    assert (old / executable.name).read_bytes() == b"old"
    assert lifecycle.calls == ["stop", "start"]


def test_start_failure_rolls_back_and_retains_failed_files(tmp_path):
    manager, executable, stage, backups, lifecycle = make_manager(tmp_path, fail_start=True)
    stage_candidate(stage, executable)
    result = asyncio.run(manager.install("v2"))
    assert result.status == "rolled_back"
    assert executable.read_bytes() == b"old"
    backup = next(backups.iterdir())
    assert (backup / executable.name).read_bytes() == b"old"
    assert (backup / "failed" / executable.name).read_bytes() == b"new"
    assert lifecycle.calls.count("stop") == 2


def test_manifest_replace_failure_uses_transaction_backup(tmp_path, monkeypatch):
    import ai_provider_gateway.sidecars.cliproxy.updater as updater

    manager, executable, stage, backups, lifecycle = make_manager(tmp_path)
    stage_candidate(stage, executable)
    original_replace = updater.os.replace
    failed = [False]

    def fail_candidate_manifest(source, destination):
        if Path(destination) == manager.manifest_path and str(source).endswith(".update") and not failed[0]:
            failed[0] = True
            raise OSError("simulated manifest replace interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(updater.os, "replace", fail_candidate_manifest)
    result = asyncio.run(manager.install("v2"))
    assert result.status == "rolled_back"
    assert executable.read_bytes() == b"old"
    assert SidecarManifest.load(manager.manifest_path).version == "1.0.0"
    assert any((entry / "failed" / executable.name).is_file() for entry in backups.iterdir())
    assert lifecycle.state == "RUNNING"


def test_interrupted_mismatched_pair_is_recovered_from_recorded_backup(tmp_path):
    manager, executable, _, _, lifecycle = make_manager(tmp_path)
    current = manager._current_manifest()
    backup = manager._backup_current(current)
    manager._write_transaction(backup, "binary_replaced")
    executable.write_bytes(b"interrupted-new-binary")
    result = asyncio.run(manager.recover_interrupted_update())
    assert result.status == "rolled_back"
    assert executable.read_bytes() == b"old"
    assert SidecarManifest.load(manager.manifest_path).version == "1.0.0"
    assert lifecycle.state == "RUNNING"


def test_stop_exception_still_restores_verified_backup(tmp_path):
    manager, executable, stage, _, lifecycle = make_manager(tmp_path)
    stage_candidate(stage, executable)
    original_stop = lifecycle.stop
    first = [True]

    async def fail_once():
        if first[0]:
            first[0] = False
            raise RuntimeError("simulated stop failure")
        await original_stop()

    lifecycle.stop = fail_once
    result = asyncio.run(manager.install("v2"))
    assert result.status == "rolled_back"
    assert executable.read_bytes() == b"old"
    assert lifecycle.state == "RUNNING"


def test_explicit_rollback_saves_current_version(tmp_path):
    manager, executable, stage, backups, _ = make_manager(tmp_path)
    stage_candidate(stage, executable)
    assert asyncio.run(manager.install("v2")).status == "installed"
    result = asyncio.run(manager.rollback())
    assert result.status == "rolled_back"
    assert executable.read_bytes() == b"old"
    assert any((item / executable.name).exists() and (item / executable.name).read_bytes() == b"new"
               for item in backups.iterdir())


def test_mutations_are_serialized(tmp_path):
    manager, executable, stage, _, lifecycle = make_manager(tmp_path)
    stage_candidate(stage, executable, "v2", "2.0.0")
    stage_candidate(stage, executable, "v3", "3.0.0")
    async def install_both():
        return await asyncio.gather(manager.install("v2"), manager.install("v3"))

    results = asyncio.run(install_both())
    assert [result.status for result in results] == ["installed", "installed"]
    assert executable.read_bytes() == b"new"
    assert lifecycle.calls == ["stop", "start", "stop", "start"]


def test_latest_backup_uses_recorded_creation_time_not_version_prefix(tmp_path):
    manager, executable, _, _, _ = make_manager(tmp_path)
    current = manager._current_manifest()
    newer = manager._backup_current(current)
    older = manager._backup_dir(SidecarManifest.from_mapping(manifest("99.0.0")))
    (older / executable.name).write_bytes(b"old-version")
    write_json(older / "manifest.json", manifest("99.0.0"))
    write_json(older / "backup-record.json", {"created_at": "2025-01-01T00:00:00+00:00"})
    write_json(newer / "backup-record.json", {"created_at": "2026-01-01T00:00:00+00:00"})
    assert manager._latest_backup() == newer
