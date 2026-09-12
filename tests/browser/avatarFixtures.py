"""Build photo/crop fixtures with the real local detector, without persistence."""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.backend.services import participant_profile_service as profiles

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
portrait = Image.open(
    Path(__file__).parents[1] / "fixtures/avatar/astronaut.png"
).convert("RGB")
multiple = Image.new("RGB", (1024, 512))
multiple.paste(portrait, (0, 0))
multiple.paste(portrait, (512, 0))
# Encoded rotated, with EXIF restoring the upright portrait.
rotated = portrait.transpose(Image.Transpose.ROTATE_90)
records = {}
for name, image in [
    ("single", portrait),
    ("multiple", multiple),
    ("none", Image.new("RGB", (800, 400), "green")),
    ("oriented", rotated),
]:
    data = io.BytesIO()
    exif = Image.Exif()
    if name == "oriented":
        exif[274] = 6
    image.save(data, "PNG", exif=exif)
    raw = data.getvalue()
    (out / f"{name}.png").write_bytes(raw)
    descriptor = {"concept_id": f"#V#{name}", "url": f"/fixture/{name}.png"}
    with (
        patch.object(
            profiles, "_participant", return_value=("#V#alice", "#V#alice", {})
        ),
        patch.object(profiles, "store_image", return_value=descriptor),
    ):
        records[name] = profiles.prepare_avatar(data=raw, filename=f"{name}.png")
(out / "prepared.json").write_text(json.dumps(records, indent=2))
