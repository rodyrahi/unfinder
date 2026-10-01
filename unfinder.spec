# PyInstaller build recipe for unfinder.app — run ./build.sh rather than this directly.

APP = "unfinder"
VERSION = "1.0.0"

a = Analysis(
    ["unfinder.py"],
    datas=[("assets/unfinder.png", "assets")],
    # Qt modules the app never uses; leaving them out keeps the app much smaller.
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
              "PySide6.QtPdf", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtBluetooth"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP, console=False,
          target_arch=None, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name=APP)

app = BUNDLE(
    coll,
    name=f"{APP}.app",
    icon="assets/unfinder.icns",
    bundle_identifier="local.unfinder",
    version=VERSION,
    info_plist={
        "CFBundleName": APP,
        "CFBundleDisplayName": APP,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # follow Light/Dark mode
        # Shown by macOS the first time the app opens these locations
        "NSDesktopFolderUsageDescription": "unfinder shows the files on your Desktop.",
        "NSDocumentsFolderUsageDescription": "unfinder shows the files in your Documents folder.",
        "NSDownloadsFolderUsageDescription": "unfinder shows the files in your Downloads folder.",
        "NSRemovableVolumesUsageDescription": "unfinder shows the files on external drives.",
        "NSNetworkVolumesUsageDescription": "unfinder shows the files on network drives.",
        "NSAppleEventsUsageDescription":
            "unfinder opens Terminal to run your custom commands when you choose “Run in Terminal”.",
        # Lets you right-click a folder in Finder → Open With → unfinder
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Folder",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["public.folder"],
        }],
    },
)
