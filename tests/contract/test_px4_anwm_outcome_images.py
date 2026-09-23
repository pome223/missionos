"""The saved model PNG already has ANWM's 224-square output geometry."""

import numpy as np
from PIL import Image

from scripts.compare_px4_anwm_outcomes import image_pixels


def test_model_png_is_not_center_cropped_twice(tmp_path):
    image = np.zeros((224, 224, 3), dtype=np.uint8)
    image[:30, :, 0] = 255
    image[-30:, :, 2] = 255
    path = tmp_path / "already-transformed.png"
    Image.fromarray(image).save(path)
    assert np.array_equal(image_pixels(path), image.astype(np.float32) / 255)
