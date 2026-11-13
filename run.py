"""Local document library and ranked search API."""
import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from search import Library, Conflict, SAMPLES

ROOT = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'), '/app.js': ('app.js', 'text/javascript')}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def reply(self, status, data, mime='application/json'):
        body = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False).encode('utf-8')
        self.send_response(status)
        for key, value in {'Content-Type': mime+'; charset=utf-8', 'Content-Length': str(len(body)),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Referrer-Policy': 'no-referrer',
                           'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def trusted(self):
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or hosts[0].lower() not in {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}:
            self.reply(400, {'error': 'Use this server’s loopback address.'})
            return False
        origin = self.headers.get('Origin')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' or origin is not None and origin != 'http://' + hosts[0].lower():
            self.reply(403, {'error': 'Cross-site requests are not allowed.'})
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        if path in ASSETS:
            name, mime = ASSETS[path]
            return self.reply(200, (ROOT/name).read_bytes(), mime)
        if path == '/api/library':
            return self.reply(200, self.server.library.listing())
        if path == '/api/samples':
            return self.reply(200, {'documents': SAMPLES})
        if path == '/api/export':
            return self.reply(200, self.server.library.export())
        if path == '/api/document':
            try:
                query = parse_qs(urlsplit(self.path).query)
                if set(query) != {'id'} or len(query['id']) != 1:
                    raise ValueError('Provide one document ID.')
                return self.reply(200, self.server.library.get(int(query['id'][0])))
            except ValueError as error:
                return self.reply(400, {'error': str(error)})
        self.reply(404, {'error': 'Route not found.'})

    do_HEAD = do_GET

    def do_POST(self):
        if not self.trusted():
            return
        try:
            sizes = self.headers.get_all('Content-Length', [])
            if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding') or len(sizes) != 1 or not re.fullmatch(r'[0-9]{1,8}', sizes[0]):
                raise ValueError('Send JSON with one bounded Content-Length and no transfer encoding.')
            size = int(sizes[0])
            if size > 8500000:
                raise ValueError('Request exceeds 8.5 MB.')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request body.')
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('Duplicate JSON field.')
                    result[key] = value
                return result
            def nonfinite(_):
                raise ValueError('JSON numbers must be finite.')
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            path = urlsplit(self.path).path
            library = self.server.library
            if path == '/api/search':
                if set(data) != {'query', 'mode', 'k1', 'b', 'limit'}:
                    raise ValueError('Provide query, mode, k1, b, and limit.')
                return self.reply(200, library.search(**data))
            if path == '/api/import':
                if set(data) != {'documents'}:
                    raise ValueError('Provide documents only.')
                return self.reply(201, {'ids': library.add_many(data['documents'])})
            if path == '/api/save':
                if set(data) != {'id', 'version', 'document'}:
                    raise ValueError('Provide id, version, and document.')
                version = library.update(data['id'], data['version'], data['document'])
                return self.reply(200, {'version': version})
            if path == '/api/archive':
                if set(data) != {'id', 'version', 'archived'}:
                    raise ValueError('Provide id, version, and archived.')
                version = library.archive(data['id'], data['version'], data['archived'])
                return self.reply(200, {'version': version})
            self.reply(404, {'error': 'Route not found.'})
        except Conflict as error:
            self.reply(409, {'error': str(error)})
        except sqlite3.OperationalError:
            self.reply(503, {'error': 'Library is busy or unavailable. Retry after checking the local database.'})
        except (ValueError, UnicodeError, RecursionError) as error:
            self.reply(400, {'error': 'JSON nesting is too deep.' if isinstance(error, RecursionError) else str(error)})
        except TimeoutError:
            self.reply(408, {'error': 'Request body timed out.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--database', type=Path, default=ROOT/'data'/'library.sqlite3')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.library = Library(args.database)
    print(f'Index Press: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
