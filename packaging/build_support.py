"""Build steps used by packaging/build_installer.ps1.

    python packaging/build_support.py version
    python packaging/build_support.py version-info OUT_FILE
    python packaging/build_support.py fresh-db OUT_FILE
    python packaging/build_support.py docs-html OUT_DIR
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DOCS = [
    ("docs/operator_supervisor_guide.md", "Operator and Supervisor Guide"),
    ("docs/golden_rig_certificates.md", "Golden Rig Certificates"),
    ("docs/site_test_plan.md", "Site Test Plan"),
    ("docs/box_integration.md", "Box Integration"),
    ("docs/golden_rig_application.md", "Golden Rig Application"),
    ("README.md", "Station App README"),
]

_HTML_STYLE = """
body { font-family: "Segoe UI", Arial, sans-serif; max-width: 960px; margin: 24px auto;
       padding: 0 16px; line-height: 1.5; color: #1b1b1b; }
table { border-collapse: collapse; margin: 12px 0; }
th, td { border: 1px solid #c8c8c8; padding: 4px 8px; vertical-align: top; }
th { background: #eef2f7; }
pre, code { font-family: Consolas, monospace; background: #f4f4f4; }
pre { padding: 8px; overflow-x: auto; }
h1, h2, h3 { color: #1f5fa8; }
"""


def version() -> str:
    from stationapp import __version__

    return __version__


def write_version_info(out_file: Path) -> None:
    parts = [int(p) for p in version().split(".")[:3]]
    parts += [0] * (4 - len(parts))
    numbers = tuple(parts)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Unisem India'),
      StringStruct('FileDescription', 'Station App - BLE module programming and traceability'),
      StringStruct('FileVersion', '{version()}'),
      StringStruct('InternalName', 'StationApp'),
      StringStruct('OriginalFilename', 'StationApp.exe'),
      StringStruct('ProductName', 'Station App'),
      StringStruct('ProductVersion', '{version()}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text, encoding="utf-8")


def fresh_db(out_file: Path) -> None:
    """An empty station database at the current schema (no station data)."""
    out_file = out_file.resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(out_file) + suffix).unlink(missing_ok=True)

    # env.py reads Settings; run away from the developer .env with
    # placeholder identity values (nothing station-specific is stored).
    os.environ["STATION_ID"] = "BUILD"
    os.environ["JIG_ID"] = "BUILD"
    with tempfile.TemporaryDirectory() as empty:
        os.chdir(empty)
        from stationapp.data.schema import upgrade_to_head

        upgrade_to_head(f"sqlite:///{out_file.as_posix()}")
        os.chdir(ROOT)

    import sqlite3

    with sqlite3.connect(out_file) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        head = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    print(f"fresh database: {len(tables)} tables, schema {head}: {out_file}")


def docs_html(out_dir: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication, QTextDocument

    _app = QGuiApplication(sys.argv)
    out_dir.mkdir(parents=True, exist_ok=True)
    for source, title in DOCS:
        path = ROOT / source
        document = QTextDocument()
        document.setMarkdown(
            path.read_text(encoding="utf-8"),
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub,
        )
        body = document.toHtml()
        # Keep Qt's body, use our page head and style.
        start = body.find("<body")
        start = body.find(">", start) + 1
        end = body.rfind("</body>")
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title><style>{_HTML_STYLE}</style></head><body>"
            f"{body[start:end]}</body></html>"
        )
        # Links between the guides point at the HTML copies.
        for other, _ in DOCS:
            stem = Path(other).stem
            html = html.replace(f'href="{stem}.md', f'href="{stem}.html')
        target = out_dir / (Path(source).stem + ".html")
        target.write_text(html, encoding="utf-8")
        print("docs:", target.name)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, *rest = argv
    if command == "version":
        print(version())
    elif command == "version-info":
        write_version_info(Path(rest[0]))
    elif command == "fresh-db":
        fresh_db(Path(rest[0]))
    elif command == "docs-html":
        docs_html(Path(rest[0]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
