"""UIS hosting check. Does not connect to or modify the Telegram bot."""
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/health', '/healthz'):
            self.send_error(404)
            return
        body = json.dumps({
            'service': 'UIS Telegram Transfer',
            'status': 'ok',
            'stage': 'hosting-check',
            'transfer_enabled': False,
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def self_check(port):
    # Bypass HTTP proxy variables: this request must stay inside the container.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/healthz', timeout=5) as response:
            result = json.load(response)
            if response.status != 200 or result.get('status') != 'ok':
                raise ValueError('Unexpected health response')
        print(f'UIS SELF-CHECK OK: local HTTP on port {port}', flush=True)
    except Exception as error:
        print(f'UIS SELF-CHECK FAILED: {type(error).__name__}', flush=True)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'UIS hosting check listening on port {port}', flush=True)
    threading.Thread(target=self_check, args=(port,), daemon=True).start()
    server.serve_forever()
