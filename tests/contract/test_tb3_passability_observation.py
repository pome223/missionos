"""Regression for the observed dark-red pixels missed by the original pilot."""

from scripts.tb3_prediction.passability_observation import red_exposure


def test_real_dark_red_is_observed_without_generated_image_brightness():
    assert red_exposure(bytes([95, 44, 30]) * (224 * 224), 224, 224, 672) == 1


def test_red_outside_the_traversal_region_does_not_block_it():
    pixels = bytearray(bytes([100, 100, 100]) * (224 * 224))
    for y in range(224):
        for x in range(40):
            offset = (y * 224 + x) * 3
            pixels[offset : offset + 3] = bytes([95, 44, 30])
    assert red_exposure(pixels, 224, 224, 672) == 0


def test_bgr_camera_has_the_same_observed_screen():
    assert red_exposure(bytes([30, 44, 95]) * (224 * 224), 224, 224, 672, bgr=True) == 1
