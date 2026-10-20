import functools
import http.server
import threading
import urllib.request
import webbrowser
from pathlib import Path

root = Path(__file__).resolve().parents[1]
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
url = "http://127.0.0.1:8765/"
try:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
except OSError as error:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            is_platform = b'content="whu-math-platform"' in response.read(8192)
    except OSError:
        is_platform = False
    if not is_platform:
        raise RuntimeError("8765 端口被其他程序占用，请关闭占用程序后重试。") from error
    print(f"平台已经运行，正在打开 {url}", flush=True)
    webbrowser.open(url)
    raise SystemExit(0)
print(f"智慧武大数学课程平台：{url}", flush=True)
print("请保留此窗口；展示结束后按 Ctrl+C 关闭。", flush=True)
threading.Timer(0.5, lambda: webbrowser.open(url)).start()
try:
    server.serve_forever()
except KeyboardInterrupt:
    server.server_close()
