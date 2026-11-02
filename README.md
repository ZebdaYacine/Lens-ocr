# SinLens

SinLens is a Windows desktop OCR application for images and PDF documents. It
uses the local `indra-ocr` engine, lets the user rotate and zoom a document,
select readable zones, and export OCR text while preserving page positions.

The application does not use Tesseract, Docker, a web API, or a browser. OCR is
performed locally. A remote document is downloaded only when the user imports
an HTTP(S) URL.

## Features

- Local image/PDF import.
- Import from a long HTTP(S) URL.
- Image preview with zoom and rotation controls.
- Draw one or more readable zones over the document.
- Full-document OCR when no zone is selected.
- Selected-zone OCR when one or more zones are selected.
- Fixed-width TXT output that keeps line positions and multi-column spacing.
- Output text area with horizontal scrolling for wide documents.
- Clear result, clear zones, remove document, and action log controls.
- Automatic conversion output with no save-location dialog.
- **Save result** button that opens a save-location dialog when a copy is needed.
- Light interface and a multicolor Lens-inspired application icon.
- Windows 10/11 x64 portable build and installer.

## Project structure

```text
indra-project/
├─ sinlens/
│  ├─ __init__.py       Package exports
│  └─ ui.py             OCR pipeline and desktop UI implementation
├─ desktop_ui.py        Main desktop launcher
├─ ui.py                Backward-compatible launcher for sinlens.ui
├─ main.py              Command-line OCR launcher
├─ requirements.txt     Runtime Python dependencies
├─ requirements-build.txt
│                         PyInstaller dependency
├─ build_tools/
│  └─ generate_icon.py  Creates assets/sinlens.ico
├─ assets/
│  └─ sinlens.ico       Application and installer icon
├─ models/indra/        Bundled model files used by the Windows build
├─ sinlens.spec         PyInstaller build definition
├─ installer.iss        Inno Setup installer definition
├─ build_windows.ps1    Complete Windows build script
├─ dist/SinLens/        Generated portable application
└─ installer/           Generated Setup executable
```

`sinlens/ui.py` intentionally contains the OCR pipeline and UI integration in
one package module so the CLI and desktop launcher use exactly the same OCR
implementation. The root `ui.py` file exists only for compatibility with old
commands.

## Development setup

Open PowerShell in the project directory and create or activate the virtual
environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If the environment already exists, only the final install command is needed.
The current project has been tested with Python 3.14 on Windows x64.

## Start the desktop UI

The recommended command is:

```powershell
python desktop_ui.py
```

The compatibility command also works:

```powershell
python ui.py
```

## Use the desktop UI

1. Click **Import document** and choose an image or PDF from the computer.
2. Click **Import URL** to paste a long HTTP(S) document link instead.
3. Use `↺` and `↻` to rotate the page. Use `-`, `100%`, and `+` to zoom.
4. Drag over a readable area on the input document to create a zone.
5. Select multiple zones if required. The selected-zone list has its own
   vertical and horizontal scrollbars.
6. Click **Convert to TXT**. With no selected zone, the full page is converted;
   with selected zones, only those areas are converted.
7. Wait for the progress window and completion message.
8. Read the result in the output panel. Wide fixed-width text can be viewed
   with the horizontal scrollbar.
9. Click **Save result** to choose a different output filename and folder.
10. Use the `X` result control to clear only the displayed OCR result. Use
    **Clear zones** to remove drawn zones, or **Remove document** to reset the
    workspace without deleting the original source file.

### Automatic output locations

The Convert button never asks for a save location:

- Local source: the TXT is saved beside the original file.
- Remote URL: the TXT is saved in the current user's `Downloads` folder.
- If the filename already exists, SinLens adds a timestamp and counter.
- Save result opens a normal Windows Save dialog so the user can choose a copy
  location.

## Command-line OCR

Use `main.py` for a direct OCR conversion without opening the desktop UI:

```powershell
python main.py "C:\Docs\invoice.jpg"
python main.py "C:\Docs\document.pdf" --output "C:\Docs\ocr-output"
python main.py "http://server.example/document.jpg"
```

Available options:

```text
--output PATH       Destination directory for the TXT and ocr.log files
--dpi NUMBER        Rendering resolution, default 250
--min-score NUMBER  Minimum OCR score before trying rotations, default 80
--timeout NUMBER    OCR timeout per candidate, default 300 seconds
--columns NUMBER    Fixed-width output columns, default 120
--keep-images      Keep selected preprocessing images
```

Example with all useful options:

```powershell
python main.py "C:\Docs\invoice.pdf" `
  --output "C:\Docs\invoice-result" `
  --dpi 250 `
  --min-score 80 `
  --timeout 300 `
  --columns 120 `
  --keep-images
```

The CLI writes one TXT file and `ocr.log` in the selected output directory.
The OCR pipeline evaluates the original and rotated/preprocessed candidates,
chooses the strongest result, reads line coordinates, and reconstructs the
document in fixed-width text. Table borders are intentionally not added; table
columns are represented by spacing so the result remains plain TXT.

## OCR model files

The `indra-ocr` package needs `det.onnx`, `rec.onnx`, `order.onnx`, and
`meta.json`, plus any optional recognizer/model files available in the model
folder. During development, `indra-ocr` can use its normal local Hugging Face
cache. The build script automatically searches that cache.

For an explicit model folder:

```powershell
$env:INDRA_OCR_MODEL_DIR = "C:\path\to\indra-models"
python desktop_ui.py
```

The Windows build copies model files into `models/indra`, which makes the
installed application work offline.

## Build the Windows portable app and installer

The build creates a Windows x64 application suitable for Windows 10 and 11.
Install Inno Setup 6 once if you want the Setup executable. Then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_windows.ps1
```

The script performs these operations:

1. Generates the multicolor Lens icon.
2. Finds the local Indra model cache or uses `-ModelDir`.
3. Copies the OCR models into the build input.
4. Installs/updates PyInstaller from `requirements-build.txt`.
5. Builds the portable application using `sinlens.spec`.
6. Builds the Inno Setup installer using `installer.iss`.

Outputs:

```text
dist\SinLens\SinLens.exe
installer\SinLens-Setup-1.0.0.exe
```

If the model is stored elsewhere, pass it explicitly:

```powershell
.\build_windows.ps1 -ModelDir "C:\path\to\indra-models"
```

To build only the portable application without Inno Setup:

```powershell
.\build_windows.ps1 -SkipInstaller
```

## Troubleshooting

### `No module named ...`

Activate the virtual environment and reinstall runtime requirements:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### OCR model not found

Pass a model directory explicitly to the application or build script:

```powershell
$env:INDRA_OCR_MODEL_DIR = "C:\path\to\indra-models"
.\build_windows.ps1 -ModelDir "C:\path\to\indra-models"
```

### PowerShell blocks the build script

Use a process-only execution-policy override; it does not change the system
policy permanently:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### URL import fails

Confirm that the URL begins with `http://` or `https://`, is reachable from the
computer, and points to an image or PDF. The downloaded remote source is held
in a temporary folder and removed when it is no longer needed.

### A wide document looks cut off

Use the horizontal scrollbar in the output panel, or increase the CLI layout
width:

```powershell
python main.py "C:\Docs\document.jpg" --columns 180
```

## License and data handling

SinLens writes OCR output and logs to the selected local destination. It does
not send document images to a web API. Remote URLs are downloaded only when
the user explicitly imports them. The installer bundles the OCR model files so
the installed application can process documents without internet access.
