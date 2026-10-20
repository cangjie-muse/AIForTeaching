import functools
import http.server
from pathlib import Path

root = Path(__file__).resolve().parents[1]
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
print("智慧武大数学课程平台：http://127.0.0.1:8765", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    server.server_close()
