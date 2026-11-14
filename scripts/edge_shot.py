"""Screenshot an HTML or SVG file with Windows Edge headless (no Linux browser needed).
Usage: edge_shot.py in.(html|svg) out.png WIDTH HEIGHT [--trim]"""
import subprocess, sys, pathlib, shutil, tempfile
from PIL import Image, ImageChops
src, out = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]).resolve()
w, h = int(sys.argv[3]), int(sys.argv[4]); trim = "--trim" in sys.argv; transparent = "--transparent" in sys.argv
EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
# Edge runs on Windows: stage files on the Windows side so paths are plain C:\ paths.
win_tmp = pathlib.Path(subprocess.check_output(["wslpath", subprocess.check_output(
    ["/mnt/c/Windows/System32/cmd.exe", "/c", "echo %TEMP%"], stderr=subprocess.DEVNULL, cwd="/mnt/c").decode().strip().splitlines()[-1]]).decode().strip()) / "sentinel-shots"
win_tmp.mkdir(exist_ok=True)
staged = win_tmp / src.name; shutil.copy(src, staged); shot = win_tmp / (src.stem + ".png")
shot.unlink(missing_ok=True)
wp = lambda p: subprocess.check_output(["wslpath", "-w", str(p)]).decode().strip()
subprocess.run([EDGE, "--headless=new", f"--user-data-dir={wp(win_tmp / f'profile-{__import__("os").getpid()}')}", "--no-first-run", "--disable-gpu", "--hide-scrollbars", *(["--default-background-color=00000000"] if transparent else []), "--force-device-scale-factor=2",
                f"--window-size={w},{h}", f"--screenshot={wp(shot)}", "file:///" + wp(staged).replace("\\", "/")],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
import time
for _ in range(60):                       # Edge can return before the PNG is on disk
    if shot.exists() and shot.stat().st_size > 0: time.sleep(0.3); break
    time.sleep(0.5)
img = Image.open(shot).convert("RGBA" if transparent else "RGB")
if trim:
    bg = Image.new("RGB", img.size, img.getpixel((img.width - 1, img.height - 1)))
    box = ImageChops.difference(img, bg).getbbox()
    if box: img = img.crop((0, 0, img.width, min(img.height, box[3] + 40)))
img.save(out, optimize=True)
prof = win_tmp / f"profile-{__import__('os').getpid()}"
shutil.rmtree(prof, ignore_errors=True); staged.unlink(missing_ok=True); shot.unlink(missing_ok=True)   # leave nothing behind on Windows; print(out.name, img.size)
