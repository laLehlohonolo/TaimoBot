import unittest

from app.cors import parse_cors_origins


class CorsOriginTests(unittest.TestCase):
    def test_empty_allowlist_disables_cross_origin_access(self):
        self.assertEqual(parse_cors_origins(""), [])

    def test_explicit_origins_are_normalized_and_deduplicated(self):
        self.assertEqual(
            parse_cors_origins(
                " https://dashboard.example.com/,https://dashboard.example.com,"
                "http://localhost:5173 "
            ),
            [
                "https://dashboard.example.com",
                "http://localhost:5173"
            ]
        )

    def test_wildcard_origin_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_cors_origins("*")

    def test_path_query_and_fragment_are_rejected(self):
        for origin in (
            "https://example.com/app",
            "https://example.com/?next=home",
            "https://example.com/#section",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    parse_cors_origins(origin)

    def test_non_http_schemes_and_credentials_are_rejected(self):
        for origin in (
            "ftp://example.com",
            "https://user:password@example.com",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    parse_cors_origins(origin)


if __name__ == "__main__":
    unittest.main()
