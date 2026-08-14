# PyInstaller spec for the surveyor's Windows build.
#
# Built on Windows only. PyInstaller does not cross-compile: it freezes the interpreter
# and binaries of the machine it runs on, so a spec run on macOS produces a macOS app no
# matter what is asked of it. The GitHub Actions workflow in .github/workflows exists for
# exactly this reason.
#
# --onedir, not --onefile. Onefile unpacks ~2GB of torch to a temp directory on every
# launch, which adds 20-40 seconds to startup and trips antivirus scanners. A folder with
# an .exe in it starts immediately and can be zipped for distribution.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent          # noqa: F821 - injected by PyInstaller

datas = [
    (str(ROOT / "survey" / "static"), "survey/static"),
    (str(ROOT / "shared"), "shared"),
    # engine.py loads the ByteTrack config from disk at extraction time. It is 505 bytes
    # and without it extraction dies on the first clip -- the cheapest possible way to
    # ship a build that installs perfectly and cannot count anything.
    (str(ROOT / "benchmark" / "bytetrack_low.yaml"), "benchmark"),
    # agent.py is the code that runs on a rented GPU. It is READ, not imported -- its
    # text is base64'd into the pod's start command -- so PyInstaller's import scan never
    # sees it and would leave it out. Without this the .exe raises FileNotFoundError on
    # the first cloud clip and nowhere else.
    (str(ROOT / "survey" / "agent.py"), "."),
]

# ffprobe AND ffmpeg. Windows has neither, and each absence breaks a different thing:
# without ffprobe no recording can be read at all, and without ffmpeg the annotated video
# comes out in a format no browser plays -- OpenCV's Windows build has no H.264 encoder
# and its mp4v fallback is not web video. The CI drops both here before freezing.
for _name in ("ffprobe.exe", "ffmpeg.exe"):
    _ff = ROOT / "vendor" / _name
    if _ff.is_file():
        datas.append((str(_ff), "."))

# The detector and the universal heads ship inside the app. A surveyor cannot be asked to
# fetch weights, and an app that downloads them on first run fails on exactly the
# site-office connection that is the reason this runs locally.
#
# But NOT the whole models folder: it is 760MB, and 528MB of that is superseded work --
# nine axle checkpoints the promotion gate rejected, plus an 80MB experimental detector.
# Shipping a rejected checkpoint is worse than wasteful; it is weights the Lab disowned
# sitting next to the ones it blessed, distinguishable only by a hash in the filename.
KEEP_DETECTORS = ["yolo26s_morth15_v5", "yolo26s_morth15_v4", "yolo26s_morth15_v3",
                  "yolo26s_morth15_v2", "yolo26s_morth15_v1"]
KEEP_HEADS = ["axles_resnet18_4d598166.pt"]      # the promoted axle head

for m in KEEP_DETECTORS:
    p = ROOT / "models" / f"{m}.pt"
    if p.is_file():
        datas.append((str(p), "models"))
for h in KEEP_HEADS:
    p = ROOT / "models" / "attrs" / h
    if p.is_file():
        datas.append((str(p), "models/attrs"))
# Ultralytics reads its own yaml configs at runtime and they are not importable modules,
# so they have to be carried as data or the first detection dies on a missing file.
datas += collect_data_files("ultralytics")

hiddenimports = [
    # Imported by string inside the app modules, so the dependency graph cannot see them.
    "db", "engine", "counting", "sites", "axle_pass", "models_registry",
    "aprdc_workbook", "attrspec", "dedup", "quality", "render",
    "verify", "report_card",
    "work", "api",
    # Cloud detection. `remote` is imported inside a function in work.py so the app still
    # runs with no key configured; that also puts it out of reach of a scan that only
    # looks at module-level imports.
    "cloud", "remote",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
hiddenimports += collect_submodules("ultralytics")

a = Analysis(                                   # noqa: F821
    [str(ROOT / "survey" / "run.py")],
    pathex=[str(ROOT / "survey"), str(ROOT / "app"), str(ROOT / "lab")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is a survey feature, and each drags in large dependency trees.
    #
    # matplotlib is NOT in this list, though it looks like it belongs: ultralytics
    # imports it from utils/plotting.py, which loads on any inference path. Excluding it
    # produced a build that installed, started, registered its models, accepted footage
    # -- and then failed every extraction with "No module named 'matplotlib'". The only
    # visible symptom was an hour that would not detect.
    #
    # tkinter stays excluded: matplotlib only wants it for an interactive backend, and
    # MPLBACKEND=Agg (set in work.py) means one is never asked for.
    excludes=["tkinter", "pytest", "IPython", "notebook",
              "PyQt5", "PySide2", "wandb", "tensorboard"],
    noarchive=False,
)
pyz = PYZ(a.pure)                               # noqa: F821

exe = EXE(                                      # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TrafficLens",
    debug=False,
    strip=False,
    upx=False,          # UPX-packed torch DLLs fail to load and are an antivirus magnet
    console=True,       # the window is the progress log; hiding it hides every error
    icon=None,
)
coll = COLLECT(                                 # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="TrafficLens",
)
