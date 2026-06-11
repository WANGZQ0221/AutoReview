import hmac
import hashlib
import unittest

from autoreview.oppo.client import build_api_sign, extract_upload_url, normalize_params


class OppoClientHelpersTest(unittest.TestCase):
    def test_normalize_params_serializes_nested_values(self):
        params = normalize_params(
            {
                "pkg_name": "com.example.app",
                "apk_url": [{"url": "https://example.com/app.apk", "cpu_code": 0}],
                "empty": None,
            }
        )

        self.assertNotIn("empty", params)
        self.assertEqual(
            params["apk_url"],
            '[{"url":"https://example.com/app.apk","cpu_code":0}]',
        )

    def test_build_api_sign_uses_sorted_keys_and_ignores_existing_sign(self):
        params = {"b": "2", "a": "1", "api_sign": "old"}
        expected = hmac.new(b"secret", b"a=1&b=2", hashlib.sha256).hexdigest()

        self.assertEqual(build_api_sign(params, "secret"), expected)

    def test_extract_upload_url_accepts_common_response_keys(self):
        self.assertEqual(extract_upload_url({"uri_path": "https://example.com/a.png"}), "https://example.com/a.png")
        self.assertEqual(extract_upload_url({"file_url": "https://example.com/b.png"}), "https://example.com/b.png")
        self.assertIsNone(extract_upload_url({"md5": "abc"}))


if __name__ == "__main__":
    unittest.main()
