"""Identical byte-level current-obstacle screen in host and simulator processes."""


def red_exposure(data, width, height, step, *, bgr=False, crop_4_3=False):
    crop_width = min(width, int(height * 4 / 3)) if crop_4_3 else width
    crop_height = min(height, int(width * 3 / 4)) if crop_4_3 else height
    left, top = (width - crop_width) // 2, (height - crop_height) // 2
    count = 0
    for y in range(56, 190):
        sy = top + int(y * crop_height / 224)
        for x in range(78, 146):
            sx = left + int(x * crop_width / 224)
            index = sy * step + sx * 3
            r, g, b = data[index : index + 3]
            if bgr:
                r, b = b, r
            count += r > 51 and r > 1.5 * g and r > 1.5 * b
    return count / (134 * 68)
