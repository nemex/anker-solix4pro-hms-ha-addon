#!/usr/bin/env python3
"""
Anker Solix 4 Pro Controller — Web UI
"""

import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Anker Solix 4 Pro Controller Web UI (Dummy)</h1>")
        elif self.path == "/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "running"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8765), UIHandler)
    print("Web UI läuft auf http://0.0.0.0:8765")
    server.serve_forever()
