"""Deliberate, local-only updates for the CLIProxyAPI sidecar.

This module deliberately does not download artifacts.  A caller stages a fixed
artifact and its manifest, has it verified, and then explicitly confirms the
mutation.  Credentials and the sidecar configuration are outside its scope.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
import uuid


class SidecarUpdateError(RuntimeError):
    """A safe, operator-facing update failure."""


class SidecarManifestError(SidecarUpdateError):
    """A staged manifest is malformed or not pinned."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_BAD_VALUE = re.compile(r"[\\/:<>|?*\x00-\x1f]")


@dataclass(frozen=True)
class SidecarManifest:
    name: str
    version: str
    commit: str
    sha256: str
    download_source: str
    installed_at: str
    tested: bool

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SidecarManifest":
        fields = ("name", "version", "commit", "sha256", "download_source", "installed_at", "tested")
        if not isinstance(value, dict) or any(key not in value for key in fields):
            raise SidecarManifestError("invalid sidecar manifest")
        strings = {key: value[key] for key in fields[:-1]}
        if any(not isinstance(item, str) or not item.strip() for item in strings.values()):
            raise SidecarManifestError("invalid sidecar manifest")
        # These fields become directory labels or audit records; never permit a path.
        if any(_BAD_VALUE.search(strings[key]) or ".." in strings[key] for key in ("name", "version", "commit")):
            raise SidecarManifestError("invalid sidecar manifest")
        if value["version"].strip().casefold() == "latest":
            raise SidecarManifestError("sidecar version must be pinned")
        if not _SHA256.fullmatch(value["sha256"]):
            raise SidecarManifestError("invalid sidecar checksum")
        if not isinstance(value["tested"], bool):
            raise SidecarManifestError("invalid sidecar manifest")
        source = urlsplit(value["download_source"])
        if source.scheme != "https" or not source.hostname or source.username or source.password:
            raise SidecarManifestError("invalid sidecar manifest")
        try:
            installed_at = datetime.fromisoformat(value["installed_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise SidecarManifestError("invalid sidecar manifest") from exc
        if installed_at.tzinfo is None:
            raise SidecarManifestError("invalid sidecar manifest")
        return cls(**{key: value[key].strip() if isinstance(value[key], str) else value[key] for key in fields})

    @classmethod
    def load(cls, path: Path) -> "SidecarManifest":
        try:
            return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SidecarManifestError("invalid sidecar manifest") from exc

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "commit": self.commit,
                "download_source": self.download_source, "installed_at": self.installed_at,
                "tested": self.tested}


@dataclass(frozen=True)
class SidecarUpdateResult:
    status: str  # installed, rolled_back, failed, cancelled
    message: str
    version: str | None = None


Verifier = Callable[[Path, str], bool]
Confirmer = Callable[[str], bool]


def verify_sha256(artifact: Path, expected_sha256: str) -> bool:
    digest = hashlib.sha256()
    try:
        with artifact.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return False
    return digest.hexdigest().casefold() == expected_sha256.casefold()


class SidecarUpdateManager:
    """Serializes reversible updates of one executable and its manifest."""

    def __init__(self, executable: Path, manifest_path: Path, staging_root: Path,
                 backup_root: Path, lifecycle: Any, confirm: Confirmer,
                 artifact_verifier: Verifier = verify_sha256) -> None:
        self.executable = Path(executable)
        self.manifest_path = Path(manifest_path)
        self.staging_root = Path(staging_root)
        self.backup_root = Path(backup_root)
        self.lifecycle = lifecycle
        self.confirm = confirm
        self.artifact_verifier = artifact_verifier
        self._lock = asyncio.Lock()

    def _same_volume(self) -> None:
        drives = {path.resolve().drive.casefold() for path in (self.executable.parent, self.staging_root, self.backup_root)}
        if len(drives) != 1:
            raise SidecarUpdateError("sidecar update locations must share a volume")

    def _candidate(self, candidate_id: str) -> tuple[Path, SidecarManifest]:
        if not isinstance(candidate_id, str) or not _SAFE_ID.fullmatch(candidate_id):
            raise SidecarUpdateError("invalid sidecar candidate")
        root = self.staging_root.resolve()
        candidate_path = self.staging_root / candidate_id
        directory = candidate_path.resolve()
        if directory.parent != root or candidate_path.is_symlink() or not directory.is_dir():
            raise SidecarUpdateError("invalid sidecar candidate")
        manifest_path = directory / "manifest.json"
        artifact = directory / self.executable.name
        if not self._regular_file_in(manifest_path, directory) or not self._regular_file_in(artifact, directory):
            raise SidecarUpdateError("invalid sidecar candidate")
        manifest = SidecarManifest.load(manifest_path)
        if not manifest.tested:
            raise SidecarUpdateError("sidecar candidate has not been tested")
        if not self.artifact_verifier(artifact, manifest.sha256):
            raise SidecarUpdateError("sidecar candidate verification failed")
        return directory, manifest

    @staticmethod
    def _regular_file_in(path: Path, parent: Path) -> bool:
        try:
            return (
                not path.is_symlink()
                and path.is_file()
                and path.resolve().parent == parent.resolve()
            )
        except OSError:
            return False

    def list_candidates(self) -> list[dict[str, Any]]:
        if not self.staging_root.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for entry in self.staging_root.iterdir():
            if not entry.is_dir() or not _SAFE_ID.fullmatch(entry.name):
                continue
            try:
                _, manifest = self._candidate(entry.name)
                result.append({"candidate_id": entry.name, **manifest.public()})
            except SidecarUpdateError:
                continue
        return result

    async def _call(self, name: str) -> Any:
        method = getattr(self.lifecycle, name, None)
        if not callable(method):
            raise SidecarUpdateError("sidecar lifecycle is unavailable")
        outcome = method()
        return await outcome if inspect.isawaitable(outcome) else outcome

    async def _stop(self) -> None:
        await self._call("stop")

    async def _start_running(self) -> None:
        await self._call("start")
        state = getattr(self.lifecycle, "state", getattr(self.lifecycle, "status", None))
        if callable(state):
            state = state()
            if inspect.isawaitable(state):
                state = await state
        text = getattr(state, "name", str(state)).upper()
        if "RUNNING" not in text:
            raise SidecarUpdateError("sidecar did not become ready")

    def _backup_dir(self, manifest: SidecarManifest | None) -> Path:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        label = manifest.version if manifest else "unknown"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = self.backup_root / f"{label}-{stamp}"
        directory.mkdir()
        (directory / "backup-record.json").write_text(
            json.dumps({"created_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        return directory

    def _backup_current(self, manifest: SidecarManifest) -> Path:
        if not self._installed_pair_valid(manifest):
            raise SidecarUpdateError("installed sidecar verification failed")
        backup = self._backup_dir(manifest)
        shutil.copy2(self.executable, backup / self.executable.name)
        shutil.copy2(self.manifest_path, backup / "manifest.json")
        if not self._valid_backup(backup):
            raise SidecarUpdateError("sidecar backup verification failed")
        return backup

    def _move_current_to(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        if self.executable.exists():
            os.replace(self.executable, target / self.executable.name)
        if self.manifest_path.exists():
            os.replace(self.manifest_path, target / "manifest.json")

    def _restore_from(self, source: Path) -> None:
        binary = source / self.executable.name
        manifest = source / "manifest.json"
        if not binary.is_file() or not manifest.is_file():
            raise SidecarUpdateError("sidecar backup is incomplete")
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        binary_temporary = self.executable.with_name(
            f"{self.executable.name}.{uuid.uuid4().hex}.restore"
        )
        manifest_temporary = self.manifest_path.with_name(
            f"{self.manifest_path.name}.{uuid.uuid4().hex}.restore"
        )
        shutil.copy2(binary, binary_temporary)
        shutil.copy2(manifest, manifest_temporary)
        os.replace(binary_temporary, self.executable)
        os.replace(manifest_temporary, self.manifest_path)

    def _capture_failed(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        try:
            if self.executable.is_file():
                shutil.copy2(self.executable, target / self.executable.name)
            if self.manifest_path.is_file():
                shutil.copy2(self.manifest_path, target / "manifest.json")
        except OSError:
            pass

    def _current_manifest(self) -> SidecarManifest | None:
        try:
            manifest = SidecarManifest.load(self.manifest_path)
            return manifest if manifest.tested and self._installed_pair_valid(manifest) else None
        except SidecarUpdateError:
            return None

    def _installed_pair_valid(self, manifest: SidecarManifest | None = None) -> bool:
        try:
            current = manifest or SidecarManifest.load(self.manifest_path)
            return (
                current.tested
                and self._regular_file_in(self.executable, self.executable.parent)
                and self._regular_file_in(self.manifest_path, self.manifest_path.parent)
                and self.artifact_verifier(self.executable, current.sha256)
            )
        except SidecarUpdateError:
            return False

    @property
    def _transaction_path(self) -> Path:
        return self.backup_root / "update-transaction.json"

    def _write_transaction(self, recovery_backup: Path, state: str) -> None:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        temporary = self.backup_root / f"transaction-{uuid.uuid4().hex}.json"
        temporary.write_text(
            json.dumps({"recovery_backup": recovery_backup.name, "state": state}),
            encoding="utf-8",
        )
        os.replace(temporary, self._transaction_path)

    def _transaction_backup(self) -> Path | None:
        if not self._transaction_path.is_file():
            return None
        try:
            payload = json.loads(self._transaction_path.read_text(encoding="utf-8"))
            name = payload["recovery_backup"]
            if not isinstance(name, str) or not _SAFE_ID.fullmatch(name):
                return None
            candidate = (self.backup_root.resolve() / name).resolve()
            if candidate.parent != self.backup_root.resolve() or not self._valid_backup(candidate):
                return None
            return candidate
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _archive_transaction(self, status: str) -> None:
        marker = self._transaction_path
        if not marker.exists():
            return
        history = self.backup_root / "transactions"
        history.mkdir(parents=True, exist_ok=True)
        marker.replace(history / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{status}-{uuid.uuid4().hex}.json")

    async def install(self, candidate_id: str) -> SidecarUpdateResult:
        async with self._lock:
            self._same_volume()
            directory, candidate = self._candidate(candidate_id)
            if not await asyncio.to_thread(
                self.confirm, f"Install sidecar {candidate.version}"
            ):
                return SidecarUpdateResult("cancelled", "sidecar update cancelled", candidate.version)
            previous = self._current_manifest()
            if previous is None:
                raise SidecarUpdateError("installed sidecar manifest is invalid")
            backup = self._backup_current(previous)
            self._write_transaction(backup, "prepared")
            try:
                await self._stop()
                self._write_transaction(backup, "stopped")
                temporary = self.executable.with_name(
                    f"{self.executable.name}.{uuid.uuid4().hex}.update"
                )
                shutil.copy2(directory / self.executable.name, temporary)
                self._write_transaction(backup, "replacing_binary")
                os.replace(temporary, self.executable)
                self._write_transaction(backup, "binary_replaced")
                manifest_temporary = self.manifest_path.with_name(
                    f"{self.manifest_path.name}.{uuid.uuid4().hex}.update"
                )
                shutil.copy2(directory / "manifest.json", manifest_temporary)
                self._write_transaction(backup, "replacing_manifest")
                os.replace(manifest_temporary, self.manifest_path)
                self._write_transaction(backup, "starting")
                await self._start_running()
                self._archive_transaction("installed")
                return SidecarUpdateResult("installed", "sidecar update installed", candidate.version)
            except Exception:
                try:
                    await self._stop()
                except Exception:
                    pass
                try:
                    self._capture_failed(backup / "failed")
                    self._restore_from(backup)
                    await self._start_running()
                    self._archive_transaction("rolled-back")
                    return SidecarUpdateResult("rolled_back", "sidecar update rolled back", previous.version if previous else None)
                except Exception:
                    return SidecarUpdateResult("failed", "sidecar update failed safely", previous.version if previous else None)

    def _latest_backup(self) -> Path | None:
        if not self.backup_root.is_dir():
            return None
        choices: list[tuple[datetime, Path]] = []
        root = self.backup_root.resolve()
        for raw_entry in self.backup_root.iterdir():
            entry = raw_entry.resolve()
            if raw_entry.is_symlink() or not entry.is_dir() or entry.parent != root:
                continue
            try:
                record = json.loads((entry / "backup-record.json").read_text(encoding="utf-8"))
                created_at = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
                if created_at.tzinfo is None or not self._valid_backup(entry):
                    continue
                choices.append((created_at, entry))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, SidecarUpdateError):
                continue
        return max(choices, key=lambda item: (item[0], item[1].name), default=(None, None))[1]

    def _valid_backup(self, entry: Path) -> bool:
        try:
            root = self.backup_root.resolve()
            directory = entry.resolve()
            if directory.parent != root or entry.is_symlink() or not directory.is_dir():
                return False
            binary = directory / self.executable.name
            manifest_path = directory / "manifest.json"
            if not self._regular_file_in(binary, directory) or not self._regular_file_in(manifest_path, directory):
                return False
            manifest = SidecarManifest.load(manifest_path)
            return manifest.tested and self.artifact_verifier(binary, manifest.sha256)
        except SidecarUpdateError:
            return False

    async def rollback(self) -> SidecarUpdateResult:
        async with self._lock:
            self._same_volume()
            source = self._latest_backup()
            if source is None:
                return SidecarUpdateResult("failed", "no valid sidecar backup")
            manifest = SidecarManifest.load(source / "manifest.json")
            if not await asyncio.to_thread(
                self.confirm, f"Rollback sidecar to {manifest.version}"
            ):
                return SidecarUpdateResult("cancelled", "sidecar rollback cancelled", manifest.version)
            current = self._current_manifest()
            saved = self._backup_current(current) if current is not None else None
            recovery = saved or source
            self._write_transaction(recovery, "rollback_prepared")
            try:
                await self._stop()
                self._write_transaction(recovery, "rollback_stopped")
                self._restore_from(source)
                self._write_transaction(recovery, "rollback_starting")
                await self._start_running()
                self._archive_transaction("rollback-installed")
                return SidecarUpdateResult("rolled_back", "sidecar rollback installed", manifest.version)
            except Exception:
                try:
                    await self._stop()
                except Exception:
                    pass
                try:
                    self._capture_failed(recovery / "failed")
                    self._restore_from(recovery)
                    await self._start_running()
                    self._archive_transaction("rollback-restored")
                except Exception:
                    pass
                return SidecarUpdateResult("failed", "sidecar rollback failed safely")

    async def recover_interrupted_update(self) -> SidecarUpdateResult:
        async with self._lock:
            self._same_volume()
            if not self._transaction_path.exists() and self._installed_pair_valid():
                return SidecarUpdateResult("failed", "sidecar recovery is not needed")
            source = self._transaction_backup() or self._latest_backup()
            if source is None:
                return SidecarUpdateResult("failed", "no valid sidecar backup")
            try:
                self._restore_from(source)
                await self._start_running()
                self._archive_transaction("recovered")
                return SidecarUpdateResult("rolled_back", "sidecar recovery completed")
            except Exception:
                return SidecarUpdateResult("failed", "sidecar recovery failed safely")
