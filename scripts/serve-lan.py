import functools
import http.server
import socket
from pathlib import Path

root = Path(__file__).resolve().parents[1]
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))

port = 8765
server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)

hostname = socket.gethostname()
try:
    lan_ip = socket.gethostbyname(hostname)
except socket.error:
    lan_ip = "<本机IP>"

print("智慧武大数学课程平台 · 局域网共享模式已启动", flush=True)
print(f"  本机访问：http://127.0.0.1:{port}", flush=True)
print(f"  其他电脑访问：http://{lan_ip}:{port}", flush=True)
print(f"  （其他电脑需与本机在同一网络，且 Windows 防火墙已放行 {port} 端口）", flush=True)

try:
    server.serve_forever()
except KeyboardInterrupt:
    server.server_close()
