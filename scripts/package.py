from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
target = root / "智慧武大数学平台-本地展示版.zip"
excluded = {"node_modules", ".git", ".preview", "__pycache__"}
with ZipFile(target, "w", ZIP_DEFLATED, compresslevel=6) as archive:
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if not file.is_file() or any(part in excluded for part in relative.parts):
            continue
        if file.suffix == ".zip" or file.name == ".DS_Store":
            continue
        archive.write(file, Path("智慧武大数学平台") / relative)
with ZipFile(target) as archive:
    assert archive.testzip() is None, "压缩包完整性检查失败"
    assert "智慧武大数学平台/index.html" in archive.namelist()
    count = len(archive.namelist())
print(f"已打包并校验：{target.name}；{count} 个文件；{target.stat().st_size / 1024 / 1024:.1f} MB")
