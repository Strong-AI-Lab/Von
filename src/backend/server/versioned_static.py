"""Release-scoped static paths keep relative ES-module graphs coherent."""
from hashlib import sha256

from flask import abort


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
