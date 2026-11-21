"""Small deterministic geometry used both for validation and zone classification."""

EPS = 1e-9


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(p, a, b):
    return abs(cross(a, b, p)) < EPS and all(
        min(a[i], b[i]) - EPS <= p[i] <= max(a[i], b[i]) + EPS for i in (0, 1)
    )


def contains(p, poly, boundary=True):
    inside = False
    for a, b in zip(poly, poly[1:] + poly[:1], strict=False):
        if on_segment(p, a, b):
            return boundary
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


def proper_intersection(a, b, c, d):
    return cross(a, b, c) * cross(a, b, d) < -EPS and cross(c, d, a) * cross(c, d, b) < -EPS


def polygon_valid(poly):
    edges = list(zip(poly, poly[1:] + poly[:1], strict=False))
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges))
    if area < EPS:
        return False
    for i, (a, b) in enumerate(edges):
        for j, (c, d) in enumerate(edges):
            if j <= i or j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if proper_intersection(a, b, c, d) or any(
                on_segment(p, c, d) for p in (a, b)
            ) or any(on_segment(p, a, b) for p in (c, d)):
                return False
    return True


def polygons_overlap(first, second):
    edges1 = list(zip(first, first[1:] + first[:1], strict=False))
    edges2 = list(zip(second, second[1:] + second[:1], strict=False))
    if any(proper_intersection(a, b, c, d) for a, b in edges1 for c, d in edges2):
        return True
    for source, target in ((first, second), (second, first)):
        points = source + [((a[0]+b[0])/2, (a[1]+b[1])/2) for a, b in zip(source, source[1:]+source[:1], strict=False)]
        if any(contains(p, target, boundary=False) for p in points):
            return True
    return set(first) == set(second)
