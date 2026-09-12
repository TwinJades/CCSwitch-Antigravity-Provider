from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH).parent
hiddenimports = (
    collect_submodules("ai_provider_gateway")
    + collect_submodules(
        "alembic", filter=lambda name: not name.startswith("alembic.testing")
    )
    + collect_submodules("uvicorn")
)

analysis = Analysis(
    [str(root / "packaging" / "start.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "alembic.ini"), "."),
        (str(root / "migrations"), "migrations"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
python_module = PYZ(analysis.pure)
executable = EXE(
    python_module,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="start",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="AIProviderGateway",
)
