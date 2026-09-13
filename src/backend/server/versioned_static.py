"""Release-scoped static paths keep relative ES-module graphs coherent."""
from hashlib import sha256
from pathlib import Path

from flask import abort, make_response


def install_versioned_static(app, version: str) -> None:
    build = sha256(version.encode()).hexdigest()[:20]

    @app.url_defaults
    def version_static_urls(endpoint, values):
        if endpoint == "static" and "filename" in values:
            filename = values["filename"]
            if not filename.startswith("build/"):
                values["filename"] = f"build/{build}/{filename}"

    @app.get(f"{app.static_url_path}/build/<build_id>/<path:filename>")
    def versioned_static(build_id, filename):
        if build_id != build:
            # Never return new bytes under an old release's URL.
            abort(404)
        return app.send_static_file(filename)

    @app.get("/von/outage-worker.js")
    def outage_worker():
        # A release-specific cache name prevents mixed fallback assets. This endpoint
        # contains only public static code; it never embeds session data.
        source = (Path(app.static_folder) / "outage" / "service-worker.js").read_text()
        source = source.replace(
            "const CACHE = 'von-outage-v1';",
            f"const CACHE = 'von-outage-v1-{build}';",
        )
        source = source.replace(
            "const STATIC_ROOT = '/static/';",
            f"const STATIC_ROOT = '/static/build/{build}/';",
        )
        response = make_response(source)
        response.headers["Content-Type"] = "application/javascript"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/von/"
        return response
