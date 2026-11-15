"""Strict parsing for explicitly trusted browser origins."""

from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware


def parse_cors_origins(value):
    """Return unique HTTP(S) origins; reject wildcards and URL paths."""
    origins = []
    for item in str(value or "").split(","):
        candidate = item.strip()
        if not candidate:
            continue

        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "TAIMOBOT_CORS_ORIGINS must contain only explicit HTTP(S) origins."
            )

        origin = parsed.scheme.lower() + "://" + parsed.netloc.lower()
        if origin not in origins:
            origins.append(origin)

    return origins


def configure_cors(app, value):
    """Attach restrictive CORS middleware when trusted origins are configured."""
    origins = parse_cors_origins(value)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Accept", "Authorization", "Content-Type"],
        )
    return origins
