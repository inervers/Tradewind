# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("frontend/dist", "frontend/dist"),
    ("packaging/default-data/products.json", "data"),
    ("packaging/default-data/emails.json", "data"),
]
binaries = []
hiddenimports = collect_submodules("app")

for package in ("rapidocr_onnxruntime",):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
    except Exception:
        pass

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # MarkItDown 会发现音视频和机器学习等可选插件；本项目只处理办公文档、
    # PDF 和图片 OCR，排除这些未使用的大型运行库可减少数百 MB。
    excludes=[
        "torch",
        "torchvision",
        "torchaudio",
        "tensorflow",
        "transformers",
        "tokenizers",
        "huggingface_hub",
        "pandas",
        "pyarrow",
        "scipy",
        "sklearn",
        "matplotlib",
        "speech_recognition",
        "pypdfium2",
        "pypdfium2_raw",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Tradewind",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Tradewind-Portable",
)
