# Build a Windows one-folder application. The OCR model files are bundled so
# the installed application works without Tesseract, Docker, or a network.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project = Path(SPECPATH)
model_dir = project / "models" / "indra"
hiddenimports = collect_submodules("indra_ocr")
pymupdf_datas = collect_data_files(
    "pymupdf",
    includes=["layout/resources/onnx/*", "layout/onnx/*.yaml"],
)

a = Analysis(
    [str(project / "desktop_ui.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(model_dir), "models/indra"), *pymupdf_datas],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "tesseract"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SinLens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project / "assets" / "sinlens.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SinLens",
)
