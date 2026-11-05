"""Bilibili read-only collector. Updated 2026-11-05 (Asia/Tokyo, UTC+09:00)."""
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from curl_cffi import requests

API_PATHS = {
    '/x/web-interface/nav', '/x/web-interface/view', '/x/web-interface/wbi/view',
    '/x/tag/archive/tags', '/x/v2/reply', '/x/v2/reply/reply', '/x/v2/reply/wbi/main',
    '/x/player/v2', '/x/player/wbi/v2', '/x/player/playurl', '/x/player/wbi/playurl',
    '/x/player/videoshot',
}
PASSPORT_PATHS = {'/x/passport-login/web/qrcode/generate', '/x/passport-login/web/qrcode/poll'}
COOKIE_HOSTS = {'api.bilibili.com', 'www.bilibili.com'}
COOKIE_NAMES = {'SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'buvid3', 'buvid4', 'sid'}
SLOTS = threading.BoundedSemaphore(6)


def allowed_url(value, resolve=True):
    u = urlsplit(value)
    if u.scheme != 'https' or u.port not in (None, 443) or u.username or u.password or u.fragment or len(value) > 8192:
        raise ValueError('unsupported_source')
    host = u.hostname or ''
    allowed = (host == 'api.bilibili.com' and u.path in API_PATHS
               or host == 'passport.bilibili.com' and u.path in PASSPORT_PATHS
               or host == 'www.bilibili.com' and u.path.startswith('/video/BV')
               or host == 'b23.tv' and len(u.path) < 64
               or host == 'comment.bilibili.com' and u.path.endswith('.xml')
               or host.endswith(('.hdslb.com', '.bilivideo.com', '.bilivideo.cn', '.biliapi.net')))
    if not allowed:
        raise ValueError('unsupported_source')
    if resolve:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError('non_public_source')
    return u


def request_headers(value, host):
    allowed = {'accept', 'accept-language', 'accept-encoding', 'range'}
    result = {'Referer': 'https://www.bilibili.com/', 'Origin': 'https://www.bilibili.com', 'Accept-Encoding': 'identity'}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or len(v) > 16384 or '\r' in v or '\n' in v:
            raise ValueError('invalid_header')
        if k.lower() in allowed:
            result[k] = v
        elif k.lower() == 'cookie' and host in COOKIE_HOSTS:
            cookies = [pair.strip() for pair in v.split(';') if pair.strip().split('=', 1)[0] in COOKIE_NAMES]
            if cookies:
                result['Cookie'] = '; '.join(cookies)
    return result


def valid_signature(secret, timestamp, signature, data, now=None):
    if not secret or len(secret) < 32 or not timestamp.isdigit() or abs((now or time.time()) - int(timestamp)) > 90:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + b'.' + data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        # Never log URLs, QR keys, Cookie or signatures.
        pass

    def reply(self, status, data):
        encoded = json.dumps(data, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'ok': bool(os.environ.get('BILI_RELAY_SECRET')), 'service': 'dot-bili-collector', 'version': 3, 'frame_interval_seconds': 1, 'thumbnail_width': 960, 'thumbnail_height': 540, 'original_frame': {'width': 1920, 'height': 1080, 'format': 'png', 'upscale': False}, 'local_machine_dependency': False, 'paid_model_api': False})
        self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path not in ['/fetch', '/frames', '/frame']:
            return self.reply(404, {'error': 'not_found'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 24576:
                return self.reply(413, {'error': 'request_too_large'})
            data = self.rfile.read(length)
            if not valid_signature(os.environ.get('BILI_RELAY_SECRET', ''), self.headers.get('X-Bili-Timestamp', ''), self.headers.get('X-Bili-Signature', ''), data):
                return self.reply(401, {'error': 'unauthorized'})
            value = json.loads(data)
            if self.path in ['/frames', '/frame']:
                from frames import validate_request
                validate_request(value, allowed_url, single=self.path == '/frame')
                return self.single_frame(value) if self.path == '/frame' else self.stream_frames(value)
            u = allowed_url(value['url'])
            headers = request_headers(value.get('headers', {}), u.hostname)
            if not SLOTS.acquire(blocking=False):
                return self.reply(503, {'error': 'collector_busy'})
        except (ValueError, TypeError, KeyError, OSError):
            return self.reply(400, {'error': 'invalid_read_only_source'})
        upstream = None
        started = False
        try:
            # Request-scoped sessions avoid leaking one visitor's cookies to another.
            with requests.Session(impersonate='chrome', trust_env=False) as session:
                upstream = session.get(value['url'], headers=headers, allow_redirects=False, timeout=180, stream=True)
                limit = 768 * 1024 * 1024 if u.hostname.endswith(('.bilivideo.com', '.bilivideo.cn', '.biliapi.net')) else 16 * 1024 * 1024
                if int(upstream.headers.get('content-length', '0')) > limit:
                    return self.reply(413, {'error': 'source_too_large'})
                self.send_response(upstream.status_code)
                for name in ['Content-Type', 'Content-Range', 'Accept-Ranges', 'Location']:
                    if upstream.headers.get(name):
                        self.send_header(name, upstream.headers[name])
                if not upstream.headers.get('content-encoding') and upstream.headers.get('content-length'):
                    self.send_header('Content-Length', upstream.headers['content-length'])
                if u.hostname == 'passport.bilibili.com':
                    for cookie in upstream.headers.get_list('set-cookie'):
                        self.send_header('Set-Cookie', cookie)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                started = True
                total = 0
                for chunk in upstream.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > limit:
                        break
                    self.wfile.write(chunk)
        except Exception:
            if not started:
                self.reply(502, {'error': 'bilibili_source_request_failed'})
            self.close_connection = True
        finally:
            if upstream is not None:
                upstream.close()
            SLOTS.release()

    def single_frame(self, value):
        from frames import FRAME_SLOT, extract_frame, SourceResolutionError
        if not FRAME_SLOT.acquire(blocking=False):
            return self.reply(503, {'error': 'frame_collector_busy', 'retry_after_seconds': 15})
        try:
            with requests.Session(impersonate='chrome', trust_env=False) as session:
                return self.reply(200, extract_frame(value, session))
        except SourceResolutionError as error:
            return self.reply(422, {'error': str(error), 'source_width': error.width, 'source_height': error.height})
        except Exception:
            return self.reply(502, {'error': 'full_hd_frame_extraction_failed'})
        finally:
            FRAME_SLOT.release()

    def stream_frames(self, value):
        from frames import FRAME_SLOT, extract_sheets, encoded_sheet
        if not FRAME_SLOT.acquire(blocking=False):
            return self.reply(503, {'error': 'frame_collector_busy'})
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            def send(value):
                self.wfile.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
                self.wfile.flush()
            try:
                send({'type': 'progress', 'stage': 'starting', 'interval_seconds': 1})
                with requests.Session(impersonate='chrome', trust_env=False) as session:
                    with extract_sheets(value, session, send) as (manifest, sheets):
                        send(manifest)
                        for index, path in enumerate(sheets):
                            send(encoded_sheet(index, path))
                        send({'type': 'done'})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                send({'type': 'error', 'message': '云端每秒截帧失败或超过处理预算；没有用稀疏缩略图冒充每秒截图。'})
        finally:
            FRAME_SLOT.release()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '7860'))), Handler).serve_forever()
