"""Local face locations for an adjustable avatar crop; no identity inference."""

from PIL import Image


def find_faces(image: Image.Image) -> list[dict]:
    import cv2
    import numpy as np

    # OpenCV ships this detector. No remote model or photo transfer is needed.
    detector = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    thumbnail = image.copy()
    thumbnail.thumbnail((1024, 1024))
    grey = cv2.cvtColor(np.asarray(thumbnail), cv2.COLOR_RGB2GRAY)
    boxes = detector.detectMultiScale(
        grey, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
    )
    sx, sy = image.width / thumbnail.width, image.height / thumbnail.height
    return [
        {
            "x": float(x * sx),
            "y": float(y * sy),
            "width": float(w * sx),
            "height": float(h * sy),
        }
        for x, y, w, h in boxes
    ]
