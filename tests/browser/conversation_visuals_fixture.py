"""Local, credential-free presentation fixture; never connects to Von's database.

Exercises the actual chatTab append/render pipeline and server Markdown renderer.
This is deterministic browser evidence, not authenticated/live-provider acceptance.
"""

import io
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.backend.services.conversation_image_service import inspect_image
from src.backend.services.display_elements_service import build_turn_display_elements
from src.backend.services.markdown_render_service import render_markdown_to_safe_html

app = Flask(__name__, static_folder=str(ROOT / "src/frontend/web/von_interface/static"))
stream = io.BytesIO()
Image.new("RGB", (320, 192), "#246f8c").save(stream, "PNG")
PIXELS = stream.getvalue()
ASSET = {
    "concept_id": "#V#browser-image",
    "filename": "fixture.png",
    **inspect_image(PIXELS),
    "provenance": {"kind": "generated"},
}
DIAGRAM = """flowchart BT
    L[Local Vons and working databases] <-->|Selected knowledge| O[Organisation home — initially DGX for SAIL]
    O <-->|Authorised contributions and subscriptions| R[Durable reference service]
    X[Another organisation — local or hosted] <-->|Selected shared knowledge| R
    P[Independent personal Von] <-->|Optional subscription| R
    R --- D[Transactional catalogue and revisions]
    R --- B[Object storage: packages and evidence]"""
SOURCE = (
    r"""A source-based architecture and scientific notation.

```mermaid
"""
    + DIAGRAM
    + r"""
```

Inline \(p(x\mid y)\) and display:

\[p(x\mid y)=\frac{p(y\mid x)p(x)}{p(y)} + \alpha_i^2\]

Currency $25 and $10 stays text. Escaped \\(not maths\\).

```python
cost = '$25'; source = '\(x_i\)'
```

| Term | Value |
| --- | --- |
| Example | 3 |

Malformed \(\notASupportedCommand{x}\) leaves this prose usable.

```mermaid
this is not a valid diagram <script>window.visualAttack=true</script>
```

Final ordinary text and [citation](https://example.org/paper).
"""
)


@app.get("/health")
def health():
    return jsonify(
        fixture="conversation_visuals",
        revision=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        dirty=bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
    )


@app.get("/")
def page():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="/static/styles.css">
    <link rel="stylesheet" href="/static/vendor/conversation-visuals/katex.min.css">
    <style>body{margin:0;padding:12px;font:16px system-ui}#scrollableField{max-width:1050px;margin:auto;overflow:auto}
    [data-theme=dark] body{background:#15202c;color:#eee}</style>
    </head><body><div id="scrollableField"></div>
    <script type="module">
    import {__testOnly_appendMessage as append, setLlmDebugDataForTurn as debug} from '/static/js/chatTab.js';
    const fixtures = await (await fetch('/fixture/messages')).json();
    window.replayFixture = () => {
      for (const m of fixtures) {debug(m.id, {display_elements: m.display_elements}); append('Von',m.content,m.id,true,true);}
    };
    window.reviseDiagramFixture = () => append('Von', fixtures[0].content.replace('Organisation home — initially DGX for SAIL', 'Organisation home — research hub'), 'a-revised-source', true, true);
    window.replayFixture(); window.fixtureReady = true;
    </script></body></html>"""


@app.get("/fixture/messages")
def messages():
    parts = [
        {
            "kind": "text",
            "part_id": "before",
            "version": 1,
            "text": "Text before the image.",
        },
        {"kind": "image", "part_id": "image", "version": 1, "asset": ASSET},
        {
            "kind": "text",
            "part_id": "after",
            "version": 1,
            "text": "Text after the image.",
        },
    ]
    return jsonify(
        [
            {"id": "a-source", "content": SOURCE, "display_elements": None},
            {
                "id": "a-mixed",
                "content": "Text before the image.\n\nText after the image.",
                "display_elements": build_turn_display_elements(
                    response_text="", presenter_channels=None, content_parts=parts
                ),
            },
            {
                "id": "a-image-only",
                "content": "",
                "display_elements": build_turn_display_elements(
                    response_text="", presenter_channels=None, content_parts=[parts[1]]
                ),
            },
        ]
    )


@app.post("/von/api/render_markdown")
def markdown():
    return jsonify(html=render_markdown_to_safe_html(request.get_json()["text"]))


@app.get("/von/api/images/<path:concept_id>/original")
def image(concept_id):
    if concept_id != ASSET["concept_id"]:
        return jsonify(error="unavailable"), 404
    return send_file(io.BytesIO(PIXELS), mimetype="image/png")


@app.route("/von/<path:unused>", methods=["GET", "POST"])
def unused_route(unused):
    # Optional app hydration reads have no authority/data in this local fixture.
    return jsonify(success=True, concepts=[], authenticated=False, sessions=[])


if __name__ == "__main__":
    import threading

    from werkzeug.serving import make_server

    server = make_server(
        "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 5037, app, threaded=True
    )

    @app.post("/fixture/shutdown")
    def shutdown():
        threading.Thread(target=server.shutdown, daemon=True).start()
        return jsonify(stopping=True)

    # Fixture resource cleanup remains bounded if a browser driver crashes.
    expiry = threading.Timer(1800, server.shutdown)
    expiry.daemon = True
    expiry.start()
    server.serve_forever()
