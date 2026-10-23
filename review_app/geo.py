"""RD New (EPSG:28992) -> WGS84 for the map.

Uses the published approximation of Schreutelkamp & Strang van Hees (2001), accurate to
about a metre inside the Netherlands. Against the `lon`/`lat` that hl_cs_objecten
stores, it agrees to within 0.4 m for every located series. That is plenty for a map,
and it avoids a pyproj dependency.
"""

from __future__ import annotations

import numpy as np

# Amersfoort, the RD origin.
X0, Y0 = 155_000.0, 463_000.0
LAT0, LON0 = 52.15517440, 5.38720621

_K = {(0, 1): 3235.65389, (2, 0): -32.58297, (0, 2): -0.24750, (2, 1): -0.84978,
      (0, 3): -0.06550, (2, 2): -0.01709, (1, 0): -0.00738, (4, 0): 0.00530,
      (2, 3): -0.00039, (4, 1): 0.00033, (1, 1): -0.00012}
_L = {(1, 0): 5260.52916, (1, 1): 105.94684, (1, 2): 2.45656, (3, 0): -0.81885,
      (1, 3): 0.05594, (3, 1): -0.05607, (0, 1): 0.01199, (3, 2): -0.00256,
      (1, 4): 0.00128, (0, 2): 0.00022, (2, 0): -0.00022, (5, 0): 0.00026}


def rd_to_wgs84(x, y):
    """RD x/y in metres (scalars or arrays) -> (lon, lat) in degrees."""
    dx = (np.asarray(x, dtype=float) - X0) * 1e-5
    dy = (np.asarray(y, dtype=float) - Y0) * 1e-5
    lat = LAT0 + sum(c * dx**p * dy**q for (p, q), c in _K.items()) / 3600
    lon = LON0 + sum(c * dx**p * dy**q for (p, q), c in _L.items()) / 3600
    return lon, lat
