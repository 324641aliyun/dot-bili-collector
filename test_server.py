import hashlib
import hmac
import unittest
from server import allowed_url, request_headers, valid_signature


class BoundaryTests(unittest.TestCase):
    def test_only_public_read_sources(self):
        for url in ['https://api.bilibili.com/x/web-interface/nav', 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate', 'https://i0.hdslb.com/a.jpg', 'https://upos.bilivideo.com/video.m4s']:
            allowed_url(url, resolve=False)
        for url in ['http://api.bilibili.com/x/web-interface/nav', 'https://api.bilibili.com/x/web-interface/archive/like', 'https://127.0.0.1/', 'https://api.bilibili.com.evil.test/', 'https://name:pass@api.bilibili.com/x/web-interface/nav', 'https://i0.hdslb.com:8080/a', 'https://i0.hdslb.com/a#secret']:
            with self.assertRaises(ValueError):
                allowed_url(url, resolve=False)

    def test_cookies_never_reach_media_or_passport(self):
        headers = {'Cookie': 'SESSDATA=fixture; DedeUserID=12; other=reject', 'Authorization': 'reject', 'Range': 'bytes=0-31'}
        self.assertEqual(request_headers(headers, 'api.bilibili.com')['Cookie'], 'SESSDATA=fixture; DedeUserID=12')
        for host in ['i0.hdslb.com', 'upos.bilivideo.com', 'passport.bilibili.com']:
            result = request_headers(headers, host)
            self.assertNotIn('Cookie', result)
            self.assertNotIn('Authorization', result)
            self.assertEqual(result['Range'], 'bytes=0-31')
        with self.assertRaises(ValueError):
            request_headers({'Range': 'bytes=0-1\r\nCookie: bad'}, 'api.bilibili.com')

    def test_signed_requests_reject_changes_and_expiry(self):
        secret = 'test-fixture-key-' * 3
        body = b'{"url":"https://api.bilibili.com/x/web-interface/nav"}'
        signature = hmac.new(secret.encode(), b'100000.' + body, hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature(secret, '100000', signature, body, now=100010))
        for args in [(secret, '100000', signature, body+b' ', 100010), (secret+'x', '100000', signature, body, 100010), (secret, '100000', signature, body, 100100), ('', '100000', signature, body, 100010)]:
            self.assertFalse(valid_signature(*args))


if __name__ == '__main__':
    unittest.main()
