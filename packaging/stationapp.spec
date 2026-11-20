# PyInstaller spec for the installed Station App (one-folder, windowed).
# Built by packaging/build_installer.ps1; run from the repository root:
#   .venv\Scripts\pyinstaller packaging\stationapp.spec --noconfirm

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is set by PyInstaller

# alembic/env.py and the migration scripts are loaded from files at run
# time, so PyInstaller cannot see their imports: list them explicitly.
hiddenimports = (
    collect_submodules("stationapp", filter=lambda name: ".bench" not in name)
    + collect_submodules("alembic", filter=lambda name: not name.startswith("alembic.testing"))
    + collect_submodules("unisemble_client")
    + collect_submodules("winrt")
    + collect_submodules("serial.tools")
    + [
        "logging.config",
        "sqlalchemy.dialects.sqlite",
        "argon2",
        "_cffi_backend",
        "openpyxl",
    ]
)

# Migration scripts, without __pycache__.
datas = [
    (str(path), str(Path("alembic") / path.relative_to(ROOT / "alembic").parent))
    for path in (ROOT / "alembic").rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]
for dist in ("unisemble-client", "alembic", "SQLAlchemy", "pydantic", "pydantic-settings"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # noqa: BLE001 - metadata is optional
        pass

binaries = collect_dynamic_libs("winrt")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "pytestqt", "_pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StationApp",
    icon=str(ROOT / "packaging" / "stationapp.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
    console=False,
    upx=False,
)

# Golden Rig certificate tool: console program in the same folder.
cert_a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "stationapp" / "tools" / "golden_rig_certs.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=["cryptography.hazmat.primitives.asymmetric.rsa"],
    excludes=["tkinter", "pytest", "_pytest", "PyInstaller", "PyQt6", "sqlalchemy", "alembic"],
    noarchive=False,
)
cert_pyz = PYZ(cert_a.pure)  # noqa: F821
cert_exe = EXE(  # noqa: F821
    cert_pyz,
    cert_a.scripts,
    [],
    exclude_binaries=True,
    name="GoldenRigCertTool",
    icon=str(ROOT / "packaging" / "stationapp.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
    console=True,
    upx=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    cert_exe,
    cert_a.binaries,
    cert_a.datas,
    name="StationApp",
    upx=False,
)
