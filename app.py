"""UIS hosting check. Does not connect to or modify the Telegram bot."""
import json
import os
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    print(f'UIS hosting check listening on port {port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
