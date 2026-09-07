"""Generate the CC0 synthetic research-image acceptance set (no meeting content)."""

from pathlib import Path
import json
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "tests/fixtures/research_images"
FONT = next(
    p
    for p in [
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    if p.exists()
)
MONO = next(
    (
        p
        for p in [
            Path("/System/Library/Fonts/Menlo.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        ]
        if p.exists()
    ),
    FONT,
)
OUT.mkdir(parents=True, exist_ok=True)
refs = []


def slide(title):
    im = Image.new("RGB", (1600, 1000), "white")
    d = ImageDraw.Draw(im)
    d.text((50, 35), title, font=ImageFont.truetype(str(FONT), 38), fill="black")
    d.line((50, 100, 1550, 100), fill="#334455", width=3)
    return im, d


def text(d, xy, s, size=25, mono=False):
    d.multiline_text(
        xy,
        s,
        font=ImageFont.truetype(str(MONO if mono else FONT), size),
        fill="black",
        spacing=12,
    )


def save(im, name, question, required, review):
    im.save(OUT / (name + ".png"))
    refs.append(dict(id=name, question=question, required=required, review=review))


im, d = slide("Signal estimation: two experimental regimes")
text(
    d,
    (60, 135),
    "REGIME A — controlled observations\nTemperature: 293 K\nSample interval Δt = 2 ms\nNoise σ = 0.04 mV\nDecay α = 0.15 s⁻¹\nReplicates n = 12\nCalibration uses channel C₂\nEstimator: mean after baseline subtraction\nExclusion: saturated samples only\nReport uncertainty as standard deviation",
    25,
)
text(
    d,
    (850, 135),
    "REGIME B — perturbation observations\nTemperature: 310 K\nSample interval Δt = 5 ms\nNoise σ = 0.09 mV\nDecay α = 0.21 s⁻¹\nReplicates n = 8\nCalibration uses channel C₃\nEstimator: median after baseline subtraction\nExclusion: missing timestamps only\nReport uncertainty as interquartile range",
    25,
)
text(
    d,
    (60, 710),
    "Shared assumptions: β ≥ 0; μ is a fitted offset, not a measured voltage.\nThe two columns describe different experiments. Do not pool their sample counts.",
    25,
)
save(
    im,
    "dense_columns",
    "Compare sampling interval, noise and estimator in regimes A and B. Transcribe the shared β constraint.",
    ["2", "5", "0.04", "0.09", "mean", "median", "β"],
    "Preserve column associations, units and β ≥ 0; no pooled n or invented causal claim.",
)
im, d = slide("Discrete relaxation update")
# Position subscripts explicitly: installed fonts need not contain Unicode t-subscript.
x = 110
for body, sub in [("x", "t+1"), (" = x", "t"), (" − η ∇L(x", "t"), (") + β² x", "t−1")]:
    font = ImageFont.truetype(str(FONT), 57)
    d.text((x, 220), body, font=font, fill="black")
    x += d.textlength(body, font=font)
    small = ImageFont.truetype(str(FONT), 32)
    d.text((x, 267), sub, font=small, fill="black")
    x += d.textlength(sub, font=small) + 3
text(
    d,
    (110, 440),
    "η = 0.05       β = 0.2\nThe gradient term is subtracted.\nThe delayed state is multiplied by the square of β.",
    30,
)
save(
    im,
    "equation",
    "Transcribe the update equation, including signs, indices and exponent. What coefficient multiplies the delayed state when β = 0.2?",
    ["0.04", "0.05"],
    "Exact x_(t+1)=x_t−η∇L(x_t)+β²x_(t−1); delayed coefficient 0.04, no sign/index error.",
)
im, d = slide("Synthetic assay response — triplicate means ± SD")
x0, y0 = 180, 780
w, h = 1000, 560
d.line((x0, 180, x0, y0, x0 + w, y0), fill="black", width=3)
for v in [1, 10, 100, 1000]:
    y = y0 - math.log10(v) / 3 * h
    d.line((x0 - 8, y, x0 + w, y), fill="#cccccc", width=1)
    text(d, (95, y - 15), str(v), 23)
for n, v in enumerate([0, 10, 20, 30]):
    x = x0 + n * w / 3
    text(d, (x - 10, 800), str(v), 23)
text(d, (500, 860), "Time (min) — linear axis", 27)
text(d, (180, 145), "Fluorescence (a.u.) — log₁₀ scale", 27)
for colour, label, vals in [
    ("blue", "Control", [10, 20, 40, 80]),
    ("red", "Drug A", [10, 30, 100, 300]),
    ("green", "Drug B", [10, 15, 25, 50]),
]:
    points = []
    for n, v in enumerate(vals):
        x = x0 + n * w / 3
        y = y0 - math.log10(v) / 3 * h
        points.append((x, y))
        d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=colour)
        top = y0 - math.log10(v * 1.15) / 3 * h
        bottom = y0 - math.log10(v * 0.85) / 3 * h
        d.line((x, top, x, bottom), fill=colour, width=3)
        d.line((x - 8, top, x + 8, top), fill=colour, width=3)
        d.line((x - 8, bottom, x + 8, bottom), fill=colour, width=3)
    d.line(points, fill=colour, width=3)
    j = ["Control", "Drug A", "Drug B"].index(label)
    d.line((1250, 240 + j * 60, 1320, 240 + j * 60), fill=colour, width=4)
    text(d, (1330, 220 + j * 60), label, 23)
save(
    im,
    "log_chart",
    "Identify both axes, units and scales, series and error-bar meaning. Which series is highest at 30 minutes? Can statistical significance be concluded?",
    ["min", "log", "Control", "Drug A", "Drug B", "SD"],
    "Y fluorescence a.u. log10, X time min linear; Drug A highest; SD of triplicates, no statistical significance claim.",
)
im, d = slide("Ablation results and filtering code")
text(
    d,
    (80, 170),
    "Variant       Accuracy (%)   Latency (ms)\nBaseline      81.2           24\nNo memory     76.5           18\nNo reranking  78.9           20",
    30,
    True,
)
text(
    d,
    (80, 450),
    "for sample in samples:\n    if sample.valid:\n        accepted.append(sample)\nreport(len(accepted))",
    29,
    True,
)
save(
    im,
    "table_code",
    "Preserve the table rows/columns and code indentation. What are No memory accuracy and latency, and is report inside the loop?",
    ["76.5", "18", "outside"],
    "All rows correctly associated; append inside if, if inside loop, report outside loop.",
)
im, d = slide("Closed-loop laboratory controller")
boxes = {
    "Sensor": (80, 320, 350, 410),
    "Filter": (500, 320, 770, 410),
    "Controller": (930, 320, 1220, 410),
    "Actuator": (930, 650, 1220, 740),
    "Archive": (500, 650, 770, 740),
}
for label, box in boxes.items():
    d.rectangle(box, outline="black", width=3)
    text(d, (box[0] + 30, box[1] + 25), label, 28)


def arrow(points):
    d.line(points, fill="#164779", width=4)
    a, b = points[-2:]
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    d.polygon(
        [
            b,
            (b[0] - 16 * math.cos(angle - 0.45), b[1] - 16 * math.sin(angle - 0.45)),
            (b[0] - 16 * math.cos(angle + 0.45), b[1] - 16 * math.sin(angle + 0.45)),
        ],
        fill="#164779",
    )


arrow([(350, 365), (500, 365)])
arrow([(770, 365), (930, 365)])
arrow([(635, 410), (635, 650)])
arrow([(1075, 410), (1075, 650)])
arrow([(930, 695), (200, 695), (200, 410)])
text(d, (230, 725), "physical feedback", 23)
text(d, (655, 505), "record", 23)
text(d, (1100, 505), "command", 23)
save(
    im,
    "feedback_diagram",
    "List the directed connections and identify the logging branch and feedback path. Does Archive control Actuator?",
    ["Sensor", "Filter", "Controller", "Actuator", "Archive"],
    "Sensor→Filter→Controller→Actuator→Sensor; Filter→Archive branch; no Archive→Actuator edge.",
)
im, d = slide("Recovery and illegibility check")
text(
    d,
    (70, 160),
    "A small calibration note appears at lower right.\nThe deliberately destroyed label below cannot be recovered.",
    28,
)
text(d, (1100, 840), "κ = 0.037 s⁻¹", 19)
patch = Image.new("RGB", (700, 120), "white")
p = ImageDraw.Draw(patch)
text(p, (20, 15), "unrecoverable synthetic label", 36)
patch = patch.resize((12, 2)).resize((700, 120)).filter(ImageFilter.GaussianBlur(12))
im.paste(patch, (150, 420))
save(
    im,
    "degraded",
    "Read the lower-right calibration note, using a crop if needed, and report whether the destroyed central label is readable. Do not guess the central text.",
    ["0.037"],
    "κ = 0.037 s⁻¹ recovered with original-region citation; central label explicitly unreadable. Crop [1050,780,500,170] is available.",
)
(OUT / "references.json").write_text(
    json.dumps(
        {
            "licence": "CC0 synthetic fixtures generated for Von; no private source content",
            "criteria": "Each fixture must pass its task-specific review. Exact values/labels checked mechanically where applicable; independently inspect visual relationships and unsupported claims. Record first failures and any bounded retry. One successful crop recovery and one honest unresolved region required. No claim beyond this set and bounded private slides.",
            "cases": refs,
        },
        indent=2,
        ensure_ascii=False,
    )
)
print("Generated", len(refs), "synthetic research fixtures")
