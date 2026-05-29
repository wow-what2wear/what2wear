"""WhatToWear local static server.

Only needed for local viewing — browsers block fetch() of local files over
file://, so we need an actual HTTP server. For deployment, just upload
index.html + data/ to any static host (GitHub Pages, Netlify, S3, etc.)
and skip this script entirely.
"""

import http.server
import socketserver
import sys

PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write(f'{self.log_date_time_string()}  {fmt % args}\n')
        sys.stdout.flush()


class ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    with ReusableServer(('127.0.0.1', PORT), Handler) as srv:
        print(f'WhatToWear: http://127.0.0.1:{PORT}/')
        print('Ctrl+C to stop.')
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print('\nstopped.')
