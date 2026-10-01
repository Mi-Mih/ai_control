# Запускать из корня репозитория: python -m PyInstaller packaging/ai-control.spec --noconfirm
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("aiogram")
pydantic_datas, pydantic_binaries, pydantic_hidden = collect_all("pydantic")

a = Analysis(
    ["ai_control/main.py"],
    pathex=["."],
    binaries=binaries + pydantic_binaries,
    datas=datas + pydantic_datas,
    hiddenimports=hiddenimports + pydantic_hidden + ["keyring.backends.Windows"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai-control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
