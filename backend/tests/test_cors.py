import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cors import configure_cors, parse_cors_origins


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


    def test_cors_middleware_allows_only_the_configured_origin(self):
        test_app = FastAPI()
        configure_cors(test_app, "https://dashboard.example.com")

        @test_app.get("/probe")
        def probe():
            return {"ok": True}

        with TestClient(test_app) as client:
            allowed = client.options(
                "/probe",
                headers={
                    "Origin": "https://dashboard.example.com",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            rejected = client.options(
                "/probe",
                headers={
                    "Origin": "https://untrusted.example.net",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers.get("access-control-allow-origin"),
            "https://dashboard.example.com"
        )
        self.assertNotIn("access-control-allow-credentials", allowed.headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("access-control-allow-origin", rejected.headers)


if __name__ == "__main__":
    unittest.main()
