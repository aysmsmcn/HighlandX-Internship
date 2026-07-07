# highlandx.spec — PyInstaller build config for HighlandX.
#
# One-DIR build (not one-file): QtWebEngine ships ~150MB of binaries that a
# one-file build would unpack to a temp dir on every launch — slow and flaky.
# Build with:  python -m PyInstaller highlandx.spec --noconfirm
# Output:      dist/HighlandX/HighlandX.exe
#
# PySide6 (incl. QtWebEngine) is handled automatically by PyInstaller's built-in
# hooks. We only need to force-include packages that are imported dynamically and
# so escape static analysis: the Azure/MSAL/Graph SDKs and keyring's OS backend.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

for pkg in (
    "azure.identity", "msal", "msal_extensions",
    "msgraph", "msgraph_core",
    "kiota_abstractions", "kiota_http",
    "kiota_serialization_json", "kiota_authentication_azure",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # package layout differs / not present — let the build surface it

# keyring selects its backend via entry points → bundle the Windows one explicitly.
hiddenimports += collect_submodules("keyring.backends")

a = Analysis(
    ["src/main.py"],
    pathex=["src"],          # absolute imports (ui., services., …) are rooted here
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HighlandX",
    debug=False,
    strip=False,
    upx=False,               # UPX + Qt DLLs can corrupt; keep off
    console=True,            # TEMP: shows import errors; flip to False once clean
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HighlandX",
)
