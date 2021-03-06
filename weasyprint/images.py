# coding: utf8
"""
    weasyprint.images
    -----------------

    Fetch and decode images in various formats.

    :copyright: Copyright 2011-2013 Simon Sapin and contributors, see AUTHORS.
    :license: BSD, see LICENSE for details.

"""

from __future__ import division, unicode_literals

from io import BytesIO
import math

import cairocffi
cairocffi.install_as_pycairo()  # for CairoSVG

import cairosvg.parser
import cairosvg.surface
assert cairosvg.surface.cairo is cairocffi, (
    'CairoSVG is using pycairo instead of cairocffi. '
    'Make sure it is not imported before WeasyPrint.')

try:
    from cairocffi import pixbuf
except OSError:
    pixbuf = None

from .logger import LOGGER
from .compat import xrange


# Map values of the image-rendering property to cairo FILTER values:
# Values are normalized to lower case.
IMAGE_RENDERING_TO_FILTER = dict(
    optimizespeed=cairocffi.FILTER_FAST,
    auto=cairocffi.FILTER_GOOD,
    optimizequality=cairocffi.FILTER_BEST,
)


class RasterImage(object):
    def __init__(self, image_surface):
        self.image_surface = image_surface
        self.intrinsic_width = image_surface.get_width()
        self.intrinsic_height = image_surface.get_height()
        self.intrinsic_ratio = (
            self.intrinsic_width / self.intrinsic_height
            if self.intrinsic_height != 0 else float('inf'))

    def draw(self, context, concrete_width, concrete_height, image_rendering):
        if self.intrinsic_width > 0 and self.intrinsic_height > 0:
            context.scale(concrete_width / self.intrinsic_width,
                          concrete_height / self.intrinsic_height)
            context.set_source_surface(self.image_surface)
            context.get_source().set_filter(
                IMAGE_RENDERING_TO_FILTER[image_rendering])
            context.paint()


class ScaledSVGSurface(cairosvg.surface.SVGSurface):
    """
    Have the cairo Surface object have intrinsic dimension
    in pixels instead of points.
    """
    @property
    def device_units_per_user_units(self):
        scale = super(ScaledSVGSurface, self).device_units_per_user_units
        return scale / 0.75


class SVGImage(object):
    def __init__(self, svg_data, base_url):
        # Don’t pass data URIs to CairoSVG.
        # They are useless for relative URIs anyway.
        self._base_url = (
            base_url if not base_url.lower().startswith('data:') else None)
        self._svg_data = svg_data

        # TODO: find a way of not doing twice the whole rendering.
        svg = self._render()
        # TODO: support SVG images with none or only one of intrinsic
        #       width, height and ratio.
        if not (svg.width > 0 and svg.height > 0):
            raise ValueError(
                'SVG images without an intrinsic size are not supported.')
        self.intrinsic_width = svg.width
        self.intrinsic_height = svg.height
        self.intrinsic_ratio = self.intrinsic_width / self.intrinsic_height

    def _render(self):
        # Draw to a cairo surface but do not write to a file.
        # This is a CairoSVG surface, not a cairo surface.
        return ScaledSVGSurface(
            cairosvg.parser.Tree(
                bytestring=self._svg_data, url=self._base_url),
            output=None, dpi=96)

    def draw(self, context, concrete_width, concrete_height, _image_rendering):
        # Do not re-use the rendered Surface object,
        # but regenerate it as needed.
        # If a surface for a SVG image is still alive by the time we call
        # show_page(), cairo will rasterize the image instead writing vectors.
        svg = self._render()
        context.scale(concrete_width / svg.width, concrete_height / svg.height)
        context.set_source_surface(svg.cairo)
        context.paint()


def get_image_from_uri(cache, url_fetcher, uri, forced_mime_type=None):
    """Get a cairo Pattern from an image URI."""
    try:
        missing = object()
        image = cache.get(uri, missing)
        if image is not missing:
            return image
        result = url_fetcher(uri)
        mime_type = forced_mime_type or result['mime_type']
        try:
            if mime_type == 'image/svg+xml':
                image = SVGImage(
                    result.get('string') or result['file_obj'].read(), uri)
            elif mime_type == 'image/png':
                image = RasterImage(cairocffi.ImageSurface.create_from_png(
                    result.get('file_obj') or BytesIO(result.get('string'))))
            else:
                if pixbuf is None:
                    raise OSError(
                        'Could not load GDK-Pixbuf. '
                        'PNG and SVG are the only image formats available.')
                string = result.get('string') or result['file_obj'].read()
                surface, format_name = pixbuf.decode_to_image_surface(string)
                if format_name == 'jpeg':
                    surface.set_mime_data('image/jpeg', string)
                image = RasterImage(surface)
        finally:
            if 'file_obj' in result:
                try:
                    result['file_obj'].close()
                except Exception:  # pragma: no cover
                    # May already be closed or something.
                    # This is just cleanup anyway.
                    pass
    except Exception as exc:
        LOGGER.warn('Error for image at %s : %r', uri, exc)
        image = None
    cache[uri] = image
    return image


def percentage(value, refer_to):
    """Return the evaluated percentage value, or the value unchanged."""
    if value is None:
        return value
    elif value.unit == 'px':
        return value.value
    else:
        assert value.unit == '%'
        return refer_to * value.value / 100


def process_color_stops(gradient_line_size, positions):
    """
    Gradient line size: distance between the starting point and ending point.
    Positions: list of None, or Dimension in px or %.
               0 is the starting point, 1 the ending point.

    http://dev.w3.org/csswg/css-images-3/#color-stop-syntax

    Return processed color stops, as a list of floats in px.

    """
    positions = [percentage(position, gradient_line_size)
                 for position in positions]
    # First and last default to 100%
    if positions[0] is None:
        positions[0] = 0
    if positions[-1] is None:
        positions[-1] = gradient_line_size

    # Make sure positions are increasing.
    previous_pos = positions[0]
    for i, position in enumerate(positions):
        if position is not None:
            if position < previous_pos:
                positions[i] = previous_pos
            else:
                previous_pos = position

    first = positions[0]
    last = positions[-1]
    if first == last:
        return 0, 0, [0 for _ in positions]

    # Assign missing values
    previous_i = -1
    for i, position in enumerate(positions):
        if position is not None:
            base = positions[previous_i]
            increment = (position - base) / (i - previous_i)
            for j in xrange(previous_i + 1, i):
                positions[j] = base + j * increment
            previous_i = i

    # Normalize to [0..1]
    total_length = last - first
    return first, last, [
        (pos - first) / total_length for pos in positions]


def gradient_average_color(colors, positions):
    """
    http://dev.w3.org/csswg/css-images-3/#find-the-average-color-of-a-gradient
    """
    assert positions
    nb_stops = len(positions)
    assert nb_stops == len(colors)
    if nb_stops == 1:
        return colors[0]
    total_length = positions[-1] - positions[0]
    if total_length == 0:
        positions = range(nb_stops)
        total_length = nb_stops - 1
    premul_r = [r * a for r, g, b, a in colors]
    premul_g = [g * a for r, g, b, a in colors]
    premul_b = [b * a for r, g, b, a in colors]
    alpha = [a for r, g, b, a in colors]
    result_r = result_g = result_b = result_a = 0
    total_weight = 2 * total_length
    for i, position in enumerate(positions[1:], 1):
        weight = (position - positions[i - 1]) / total_weight
        for j in (i - 1, i):
            result_r += premul_r[j] * weight
            result_g += premul_g[j] * weight
            result_b += premul_b[j] * weight
            result_a += alpha[j] * weight
    # Un-premultiply:
    return (result_r / result_a, result_g / result_a,
            result_b / result_a, result_a)


PATTERN_TYPES = dict(
    linear=cairocffi.LinearGradient,
    radial=cairocffi.RadialGradient,
    solid=cairocffi.SolidPattern)


class Gradient(object):
    intrinsic_width = None
    intrinsic_height = None
    intrinsic_ratio = None

    def __init__(self, color_stops, repeating):
        #: List of (r, g, b, a), list of Dimension
        self.colors = [color for color, position in color_stops]
        self.stop_positions = [position for color, position in color_stops]
        #: bool
        self.repeating = repeating

    def draw(self, context, concrete_width, concrete_height, _image_rendering):
        scale_y, type_, init, stop_positions, stop_colors = self.layout(
            concrete_width, concrete_height, context.user_to_device_distance)
        context.scale(1, scale_y)
        pattern = PATTERN_TYPES[type_](*init)
        for position, color in zip(stop_positions, stop_colors):
            pattern.add_color_stop_rgba(position, *color)
        pattern.set_extend(cairocffi.EXTEND_REPEAT if self.repeating
                           else cairocffi.EXTEND_PAD)
        context.set_source(pattern)
        context.paint()

    def layout(self, width, height, user_to_device_distance):
        """width, height: Gradient box. Top-left is at coordinates (0, 0).
        user_to_device_distance: a (dx, dy) -> (ddx, ddy) function

        Returns (scale_y, type_, init, positions, colors).
        scale_y: float, used for ellipses radial gradients. 1 otherwise.
        positions: list of floats in [0..1].
                   0 at the starting point, 1 at the ending point.
        colors: list of (r, g, b, a)
        type_ is either:
            'solid': init is (r, g, b, a). positions and colors are empty.
            'linear': init is (x0, y0, x1, y1)
                      coordinates of the starting and ending points.
            'radial': init is (cx0, cy0, radius0, cx1, cy1, radius1)
                      coordinates of the starting end ending circles

        """
        raise NotImplementedError


class LinearGradient(Gradient):
    def __init__(self, color_stops, direction, repeating):
        Gradient.__init__(self, color_stops, repeating)
        #: ('corner', keyword) or ('angle', radians)
        self.direction_type, self.direction = direction

    def layout(self, width, height, user_to_device_distance):
        # (dx, dy) is the unit vector giving the direction of the gradient.
        # Positive dx: right, positive dy: down.
        if self.direction_type == 'corner':
            factor_x, factor_y = {
                'top_left': (-1, -1), 'top_right': (1, -1),
                'bottom_left': (-1, 1), 'bottom_right': (1, 1)}[self.direction]
            diagonal = math.hypot(width, height)
            dx = factor_x * height / diagonal
            dy = factor_y * width / diagonal
        else:
            angle = self.direction  # 0 upwards, then clockwise
            dx = math.sin(angle)
            dy = -math.cos(angle)
        # Distance between starting and ending point:
        distance = math.hypot(width * dx, height * dy)
        first, last, positions = process_color_stops(
            distance, self.stop_positions)
        if self.repeating and (last - first) * math.hypot(
                *user_to_device_distance(dx, dy)) < len(positions):
            color = gradient_average_color(self.colors, positions)
            return 1, 'solid', color, [], []
        points = (
            width / 2 + dx * (first - distance / 2),
            height / 2 + dy * (first - distance / 2),
            width / 2 + dx * (last - distance / 2),
            height / 2 + dy * (last - distance / 2))
        return 1, 'linear', points, positions, self.colors


class RadialGradient(Gradient):
    def __init__(self, color_stops, shape, size, center, repeating):
        Gradient.__init__(self, color_stops, repeating)
        # Center of the ending shape. (origin_x, pos_x, origin_y, pos_y)
        self.center = center
        #: Type of ending shape: 'circle' or 'ellipse'
        self.shape = shape
        # size_type: 'keyword'
        #   size: 'closest-corner', 'farthest-corner',
        #         'closest-side', or 'farthest-side'
        # size_type: 'explicit'
        #   size: (radius_x, radius_y)
        self.size_type, self.size = size

    def layout(self, width, height, dx, dy, user_to_device_distance):
        origin_x, center_x, origin_y, center_y = self.center
        center_x = percentage(center_x, width)
        center_y = percentage(center_y, height)
        if origin_x == 'right':
            center_x = width - center_x
        if origin_y == 'bottom':
            center_y = height - center_y

        size_x, size_y = self._resolve_size(width, center_x, center_y)
        # http://dev.w3.org/csswg/css-images-3/#degenerate-radials
        if size_x == size_y == 0:
            size_x = size_y = 1e-10
        elif size_x == 0:
            size_x = 1e-10
            size_y = 1e10
        elif size_y == 0:
            size_x = 1e10
            size_y = 1e-10
        scale_y = size_y / size_x

        colors = self.colors
        first, last, positions = process_color_stops(
            size_x, self.stop_positions)
        gradient_line_size = last - first
        if self.repeating and any(
                gradient_line_size * unit < len(positions)
                for unit in (
                    math.hypot(*user_to_device_distance(1, 0)),
                    math.hypot(*user_to_device_distance(0, scale_y)))):
            color = gradient_average_color(colors, positions)
            return 1, 'solid', color, [], []

        if first < 0:
            # Cairo does not like negative radiuses,
            # shift into the positive realm.
            if self.repeating:
                offset = gradient_line_size * math.ceil(
                    -first / gradient_line_size)
                first += offset
                last += offset
                positions = [p + offset for p in positions]
            else:
                for i, position in enumerate(positions):
                    if position >= 0:
                        color = colors[i]
                        neg_color = colors[i - 1]
                        neg_position = positions[i - 1]
                        assert i > 0
                        assert neg_position < 0
                        intermediate_color = gradient_average_color(
                            [neg_color, neg_color, position, position],
                            [neg_position, 0, 0, position])
                        colors = [intermediate_color] + colors[i:]
                        positions = [0] + positions[i:]
                        break
                else:
                    return 1, 'solid', self.colors[-1], [], []

        circles = center_x, center_y, first, center_x, center_y, last
        return scale_y, 'radial', circles, positions, colors

    def _resolve_size(self, width, height, center_x, center_y):
        if self.size_type == 'explicit':
            return self.size
        left = abs(center_x)
        right = abs(width - center_x)
        top = abs(center_y)
        bottom = abs(height - center_y)
        if self.size == 'closest-side':
            if self.shape == 'circle':
                size_xy = min(left, right, top, bottom)
                return size_xy, size_xy
            return min(left, right), min(top, bottom)  # ellipse
        elif self.size == 'farthest-side':
            if self.shape == 'circle':
                size_xy = max(left, right, top, bottom)
                return size_xy, size_xy
            return max(left, right), max(top, bottom)  # ellipse
        top_left = math.hypot(top, left)
        top_right = math.hypot(top, right)
        bottom_left = math.hypot(bottom, left)
        bottom_right = math.hypot(bottom, right)
        if self.size == 'closest-corner':
            if self.shape == 'circle':
                size_xy = min(top_left, top_right, bottom_left, bottom_right)
                return size_xy, size_xy
            # else: ellipse
            # Coordinates of the closest corner:
            _, corner_x, corner_y = min(
                (top_left, top, left), (top_right, top, right),
                (bottom_left, bottom, left),
                (bottom_right, bottom, left))
            if 0 in (top, bottom):
                return 0, corner_y
            ratio = min(left, right) / min(top, bottom)
            size_x = math.hypot(corner_x, corner_y * ratio)
            return size_x, size_x * ratio
        if self.size == 'farthest-corner':
            if self.shape == 'circle':
                return max(top_left, top_right, bottom_left, bottom_right)
            # else: ellipse
            # Coordinates of the farthest corner:
            _, corner_x, corner_y = max(
                (top_left, top, left), (top_right, top, right),
                (bottom_left, bottom, left),
                (bottom_right, bottom, left))
            if top == bottom == 0:
                return 0, corner_y
            ratio = max(left, right) / max(top, bottom)
            size_x = math.hypot(corner_x, corner_y * ratio)
            return size_x, size_x * ratio
