"""RRFS-MPAS winter diagnostics — one-panel reference style + optional four-panel preview.

Adapted from the verified RRFS-MPAS data-access / earth-relative wind-rotation
script and the supplied Model_1Panel.ipynb style.
The actual Model_4Panel notebook was not available in this session; the optional
2x2 preview is a NEW composite of the single-panel PNGs, not a reproduction of
that notebook's exact multi-panel implementation.

Colab setup (run in a separate notebook cell before this script):
    %pip -q install eccodes cartopy scipy matplotlib numpy pillow
County outlines, the actual LBF CWA border, and NOAA/NWS logos are enabled.
The asset files must be supplied from the existing graphics repositories
before publishing maps. No substitute CWA geography is fabricated.

Products: 1-km/composite reflectivity + P-type, cumulative 0–F QPF, 2-m temperature,
925/850/700-mb shaded temperature + black heights/barbs (no isotherms),
wind speed + heights/barbs, shaded 850/700/500-mb relative vorticity
with geopotential height contours and earth-relative wind barbs; native surface gusts without barbs,
10-m sustained wind speed + MSLP with earth-relative wind barbs,
2-m RH and dewpoint, combined temperature advection/frontogenesis (no advection-only map),
optional native accumulated snowfall, a separate 10:1 SWE-based snowfall
estimate, and snow-water equivalent; LBF city labels and values.

The GRIB must contain ASNOW/WEASD 0-F accumulations to plot the corresponding
snow products. Native ASNOW is unchanged. The separate 10:1 product multiplies
WEASD liquid-equivalent depth by 10; it is a simplified SWE-based estimate,
not a snowfall forecast derived from all-precipitation QPF. Snowpack SWE
can also be affected by melting and compaction. Site outputs are staged LOCALLY only;
this test script does not publish to AWS S3 or modify your existing site.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ftplib import FTP
from urllib.request import urlretrieve
from pathlib import Path
import json
import shutil
import zipfile
import os
import re
import sys
import warnings

import numpy as np
from scipy.ndimage import gaussian_filter, minimum_filter, maximum_filter
from scipy.spatial import cKDTree
from mpl_toolkits.axes_grid1 import make_axes_locatable
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import colors, patches
import matplotlib.patheffects as pe
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from eccodes import codes_grib_new_from_file, codes_get, codes_get_array, codes_release

# ──────────────────────────────────────────────────────────────────
# SETTINGS: retain Colab defaults; GitHub Actions sets environment variables.
# ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

FTP_HOST = 'gsdftp.fsl.noaa.gov'
FTP_DIR = '/ncar/upp/det'
# Anonymous FTP requires an email-shaped password; enter your own address.
FTP_EMAIL = os.getenv('GSL_FTP_EMAIL', 'your_email@example.com')
CYCLE_ID = os.getenv('MPAS_CYCLE', 'AUTO').strip().upper()  # YYYYMMDDHH or AUTO
FORECAST_HOUR = int(os.getenv('MPAS_FHR', '6'))  # one forecast hour per job
LEVELS_HPA = (925, 850, 700)
# Vorticity maps use three levels; do not add 500 mb to the original
# temperature, wind-speed, or frontogenesis product loops.
VORTICITY_LEVELS_HPA = (850, 700, 500)
# These are the exact three geographic extents used in the existing CAMs viewer.
# Download/decode the MPAS GRIB once; produce matching products for selected domains.
DOMAINS = {
    'lbf': (-103.8, -97.0, 40.0, 43.4),
    'regional': (-107.5, -93.0, 38.5, 44.2),
    'central_plains': (-107.5, -91.0, 34.5, 45.2),
}
# One fixed 1547-pixel image width, while the height is calculated from each
# domain's projected geographic aspect ratio. This prevents the large white
# side margins caused by Cartopy shrinking an incorrectly shaped map axes.
# Plot construction copied from your supplied operational HRRR CAMs plotting code:
# plt.figure(figsize=(14,10)); plt.axes(projection=ccrs.PlateCarree());
# plt.savefig(...,dpi=140,bbox_inches='tight').
CAMS_FIGSIZE = (14, 10)
CAMS_REFERENCE_LAYOUT = True
FIGURE_WIDTH_IN = 15.47  # fallback if CAMS_REFERENCE_LAYOUT=False
# Keep axis width and height fractions fixed; the FIGURE height is adaptive.
PTYPE_MAP_BOX = [0.012, 0.135, 0.976, 0.775]
OTHER_MAP_BOX = [0.012, 0.120, 0.976, 0.795]
CAMS_DOMAIN_STYLE = {
    'lbf': {'title_size':14,'subtitle_size':11,
            'logo_ax':[.78,.70,.10,.10], 'office_text_xy':[.83,.71],
            'credit_xy':[.13,.25]},
    'regional': {'title_size':13,'subtitle_size':11,
                 'logo_ax':[.78,.63,.10,.10], 'office_text_xy':[.83,.64],
                 'credit_xy':[.13,.31]},
    'central_plains': {'title_size':13,'subtitle_size':11,
                       'logo_ax':[.78,.77,.10,.10], 'office_text_xy':[.83,.78],
                       'credit_xy':[.13,.175]},
}
# Logo/credit overlays in supplied HRRR's plotting function are positioned
# uniformly by ax.transAxes, while its domain dict above retains older
# per-domain placement values.
RUN_DOMAINS = tuple(x.strip() for x in os.getenv('MPAS_DOMAINS', 'lbf,regional,central_plains').split(',') if x.strip())
DOMAIN_NAME = RUN_DOMAINS[0]                       # set per domain inside run()
EXTENT = DOMAINS[DOMAIN_NAME]                      # set per domain inside run()
OUT_DIR = Path(os.getenv('MPAS_OUT_DIR', '/content/rrfs_mpas_winter_test' if Path('/content').exists() else './rrfs_mpas_winter_test'))
OUT_DIR.mkdir(exist_ok=True, parents=True)
# Local-only folder layout for future S3/GitHub Winter CAMs viewer integration.
# It does not connect to AWS, publish, or alter the current severe website.
STAGE_SITE_FILES = True
SITE_STAGING_ROOT = OUT_DIR / 'site_export'
SITE_RELATIVE_PREFIX = Path('runs') / 'cams' / 'rrfs_mpas' / 'winter'

# Product toggles: set False to skip.
DO_PTYPE = True
DO_PTYPE_REF1KM = True
DO_PTYPE_COMPOSITE = True
DO_QPF = True
DO_ACCUM_SNOWFALL = True  # native 0-F snowfall (ASNOW)
DO_SNOWFALL_10TO1 = True  # separate 10:1 estimate based on 0-F WEASD
DO_SNOW_WATER_EQUIV = True # 0-F snow-water equiv (WEASD)
DO_2M_TEMP = True
# Additional native RRFS-MPAS surface diagnostics.
DO_10M_GUST = True          # instantaneous GUST, converted to mph
DO_10M_WSPD_MSLP = True    # 10-m sustained U/V speed (mph), 2-mb MSLP contours and wind barbs
DO_2M_RH = True             # native 2-m RH (%)
DO_2M_DEWPOINT = True       # native 2-m dewpoint (K -> F)
DO_LEVEL_TEMP = True
DO_LEVEL_WSPD = True
DO_LEVEL_VORTICITY = True  # 850/700/500 mb relative vorticity + heights + barbs
# Publish only the combined temperature-advection shading + green
# frontogenesis contours at 925/850/700 mb; skip standalone advection maps.
DO_ADV = False
DO_FGEN = True
DO_THICKNESS_MSLP = False   # standalone thickness/MSLP map OFF by default

# Data and display settings
# Combined synoptic overlays on the precipitation-type map
OVERLAY_THICKNESS_MSLP_ON_PTYPE = True
THICKNESS_CONTOUR_INTERVAL_DAM = 6   # equivalently 60-meter intervals
MSLP_CONTOUR_INTERVAL_HPA = 2

# Match Model_1Panel.ipynb's P-type style (5–60 dBZ at 5-dBZ intervals).
# Render phase reflectivity using ALL native points rather than PLOT_STRIDE.
# This is intentionally independent of the stride used for upper-air diagnostics.
REFLECTIVITY_THRESHOLD_DBZ = 5.0
# Reference notebook does not smooth this reflectivity. Keep 0.0 for an exact
# counterpart; 0.5-0.8 is an optional very light display-only smoothing.
PTYPE_REFLECTIVITY_SMOOTH_SIGMA = 0.0
TEMP_SMOOTH_SIGMA_PIX = 5.0    # smoothing of primitive fields for gradient diagnostics
WIND_SMOOTH_SIGMA_PIX = 5.0
DISPLAY_SMOOTH_SIGMA_PIX = 2.0
PLOT_STRIDE = 3               # plot every 3rd gridpoint for faster maps
# Match the existing CAMs geography on every winter diagnostic.
COUNTIES = True               # dark charcoal county boundaries, black within LBF CWA
# Use charcoal for counties outside LBF. The CWA overlay below redraws its
# internal county boundaries in solid black, preserving that distinction.
COUNTY_OUTSIDE_COLOR = '#3f3f3f'
COUNTY_OUTSIDE_LINEWIDTH = 0.45
DRAW_CWA_OUTLINE = True       # prominent black/white NWS LBF CWA boundary
# Supply the same site assets as your CAMs viewer in /content/assets when available.
# Missing assets are skipped (never invent a CWA boundary or logo).
ASSET_DIR = Path(os.getenv('MPAS_ASSET_DIR', '/content/assets' if Path('/content').exists() else str(BASE_DIR / 'assets')))
CWA_GEOJSON_PATH = os.getenv('MPAS_CWA_GEOJSON', '')
# Both published LBF forecast-office boundary naming conventions are recognized.
# To use another source, set MPAS_CWA_SHP (or MPAS_CWA_GEOJSON).
CWA_SHP_PATH = os.getenv('MPAS_CWA_SHP', str(ASSET_DIR / 'c_18mr25.shp'))
CWA_ZIP_PATH = os.getenv('MPAS_CWA_ZIP', str(ASSET_DIR / 'c_18mr25.zip'))
LOGO_PATH = os.getenv('MPAS_LOGO_PATH', str(ASSET_DIR / 'NOAANWSLogos.png'))
COUNTY_SHP_PATH = os.getenv('MPAS_COUNTY_SHP', str(ASSET_DIR / 'cb_2018_us_county_500k.shp'))
STATE_SHP_PATH = os.getenv('MPAS_STATE_SHP', str(ASSET_DIR / 'cb_2018_us_state_500k.shp'))
# On CI publishing runs, do not silently upload plots without their office assets.
REQUIRE_MAP_ASSETS = os.getenv('MPAS_REQUIRE_MAP_ASSETS', '0').strip().lower() in ('1','true','yes')

# I-80 and I-76 on LBF maps only, using the TIGER/Line primary-roads shapefile
# from the user's MRMS QPF notebook. No generated/approximate road coordinates.
SHOW_LBF_INTERSTATE = True
INTERSTATE_SHP_PATH = os.getenv('MPAS_ROADS_SHP', '/content/drive/MyDrive/Colab Notebooks/tl_2023_us_primaryroads.shp' if Path('/content').exists() else str(ASSET_DIR / 'tl_2023_us_primaryroads.shp'))
FETCH_INTERSTATE_SHAPEFILE = os.getenv('MPAS_FETCH_ROADS', '1') == '1'  # CI only: official US Census shapefile
INTERSTATE_ROUTE_NUMBERS = ('80', '76')  # only these interstate routes, LBF domain
INTERSTATE_COLOR = 'red'          # match original MRMS/QPF reference
INTERSTATE_LINEWIDTH = 1.0
INTERSTATE_LINE_ZORDER = 38       # above state/county lines, below ALL weather overlays
AUTO_MOUNT_DRIVE_FOR_INTERSTATE = Path('/content').exists()  # Colab only
MAP_CREDIT = 'Plot created by: Matthew Labenz'
SHOW_CITY_LABELS = True     # draw cities only on the LBF domain during testing
SHOW_LBF_CITY_VALUES = True
CITY_VALUE_LAYERS = {
    'qpf': True, 't2m': True, 'asnow': True, 'snow10to1': True, 'weasd': True,
    'pressure_temp': True, 'pressure_wind': False,
    'gust10m': True, 'rh2m': True, 'd2m': True,
}
CITY_VALUE_OFFSET_DEG = 0.095     # position value a little closer to city name
CITY_MAX_SAMPLE_DISTANCE_KM = 20.0
# Each graphic uses a shared list of fixed city coordinates and no markers.

SHOW_HL_CENTERS = True      # label actual MSLP maxima/minima when detectable
MAX_HL_PER_TYPE = 2            # fewer synoptic-scale centers per domain
HL_MIN_PROMINENCE_HPA = 2.5  # increase for stricter H/L placement
HL_LOW_MAX_PRESSURE_HPA = 1009.0  # label L only if its actual MSLP <= 1009 mb
HL_MIN_SPACING_KM = 275      # reject closely clustered centers
# Reference RRFS one-panel style: red H/L letter and pressure value, one black halo.
HL_BLACK_OUTLINE_PT = 3.2    # a single solid black stroke (no white inner outline)
HL_LETTER_FONT_SIZE = 22
HL_PRESSURE_FONT_SIZE = 13
HL_PRESSURE_LAT_OFFSET = 0.18  # spacing for high centers
HL_LOW_PRESSURE_LAT_OFFSET = 0.10  # bring L pressure value closer to the letter
MSLP_LABEL_INTERVAL_HPA = 4  # ALL MSLP maps: draw every 2 mb, label every 4 mb
THICKNESS_LABEL_INTERVAL_DAM = 12  # draw every 6 dam, label every 12 dam
CONTOUR_LABEL_SPACING = 10
# Common map layering; the values intentionally exceed county/CWA/interstate
# layers (29–38), regardless of the order each plotting function calls them.
# Filled fields use zorder=2; higher values below are always in front.
WIND_BARB_ZORDER = 70
TEMP_CONTOUR_ZORDER = 80
THICKNESS_CONTOUR_ZORDER = 85
MSLP_CONTOUR_ZORDER = 95
FGEN_CONTOUR_ZORDER = 100
HEIGHT_CONTOUR_ZORDER = 110
CONTOUR_LABEL_ZORDER = 160
HEIGHT_LABEL_ZORDER = 165
PRESSURE_CENTER_ZORDER = 180
MAP_CREDIT_ZORDER = 245
MAP_TITLE_ZORDER = 255
TEMP_CONTOUR_RED = '#dc1428'  # stronger red on all nonzero isotherms
OFFICE_LABEL_ZORDER = 250     # office name ABOVE cities, contours, labels, barbs
CAM_STYLE_LOGO = True        # NOAA/NWS logos embedded in PNGs for all map domains
FIGURE_DPI = 140  # exact HRRR CAMs save DPI
CAMS_SAVE_TIGHT = True  # same bbox_inches='tight' setting as HRRR CAMs

# Reference styles / optional montage output
TADV_HOURS = 3                  # user-supplied 1-panel notebook uses deg C / 3h
TADV_COLOR_BOUNDS = np.arange(-16., 17., 1.)
FGEN_CONTOURS = [2, 4, 6, 8, 10, 14, 18, 22, 28, 34, 40]
PLOT_HEIGHT_CONTOURS = True     # when pressure-level geopotential height exists
# Map style: height contours in dam above barbs; no white contour-label
# halos; wind barb length=5.2 and linewidth=0.65.
MAKE_FOUR_PANEL_PREVIEW = False  # optional per-domain montage; keep separate plots by default

# ──────────────────────────────────────────────────────────────────
# FTP DISCOVERY / DOWNLOAD
# ──────────────────────────────────────────────────────────────────
FILENAME_RE = re.compile(
    r'^(?P<date>\d{8})(?P<cycle>\d{2})\.rrfs\.t(?P=cycle)z'
    r'\.testbed\.f(?P<fhr>\d{3})\.conus\.grib2$'
)


def find_grib_filename(names: list[str]) -> tuple[str, str]:
    candidates = []
    for name in names:
        base = Path(name).name
        m = FILENAME_RE.fullmatch(base)
        if not m or int(m['fhr']) != FORECAST_HOUR:
            continue
        cycle_id = m['date'] + m['cycle']
        if CYCLE_ID != 'AUTO' and cycle_id != CYCLE_ID:
            continue
        candidates.append((cycle_id, base))
    if not candidates:
        raise FileNotFoundError(f'No MPAS testbed F{FORECAST_HOUR:03d} available for CYCLE_ID={CYCLE_ID}')
    return max(candidates)  # lexicographically latest YYYYMMDDHH


def download_grib() -> tuple[Path, str]:
    print(f'Connecting to {FTP_HOST}{FTP_DIR}')
    with FTP(FTP_HOST, timeout=120) as ftp:
        ftp.set_pasv(True)
        ftp.login('anonymous', FTP_EMAIL)
        ftp.cwd(FTP_DIR)
        cycle_id, filename = find_grib_filename(ftp.nlst())
        destination = OUT_DIR / filename
        print('Selected:', filename)
        if not destination.exists() or destination.stat().st_size < 1_000_000:
            with destination.open('wb') as output:
                ftp.retrbinary(f'RETR {filename}', output.write, blocksize=1 << 20)
        else:
            print('Using already downloaded:', destination)
    print('GRIB2 size (MiB):', round(destination.stat().st_size / (1024**2), 1))
    return destination, cycle_id


# ──────────────────────────────────────────────────────────────────
# GRIB DECODING. Select unique fields by GRIB2 parameter + level,
# NOT message numbers. Crucial for reusing different forecast hours.
# ──────────────────────────────────────────────────────────────────

def meta(gid, name, fallback=None):
    try:
        return codes_get(gid, name)
    except Exception:
        return fallback


def field_key(gid):
    disc = int(meta(gid, 'discipline', -1))
    cat = int(meta(gid, 'parameterCategory', -1))
    num = int(meta(gid, 'parameterNumber', -1))
    lvltype = str(meta(gid, 'typeOfLevel', ''))
    level = int(meta(gid, 'level', -999))
    stype = str(meta(gid, 'stepType', 'instant'))
    srange = str(meta(gid, 'stepRange', ''))
    # Unique identifier even if ecCodes says 'unknown'.
    return disc, cat, num, lvltype, level, stype, srange


def field_definitions(fhr: int):
    # Each candidate is a metadata predicate; first matching message accepted.
    defs = {
        'ref1km': lambda k: k[:3] == (0, 16, 195) and k[3:5] == ('heightAboveGround', 1000) and k[5] == 'instant',
        # Composite reflectivity metadata can vary slightly by source.
        # Accept the common GRIB2 identifier used by UPP/refc-style output.
        'refcomp': lambda k: k[:3] == (0, 16, 196) and k[5] == 'instant',
        'rain':   lambda k: k[:3] == (0, 1, 33) and k[3] == 'surface' and k[5] == 'instant',
        'snow':   lambda k: k[:3] == (0, 1, 36) and k[3] == 'surface' and k[5] == 'instant',
        'sleet':  lambda k: k[:3] == (0, 1, 35) and k[3] == 'surface' and k[5] == 'instant',
        'fzra':   lambda k: k[:3] == (0, 1, 34) and k[3] == 'surface' and k[5] == 'instant',
        # Choose the TOTAL 0-Fxxx accumulation, NOT the one-hour F-1 -> F amount.
        # Example: F006 displays 0–6h cumulative QPF, F012 displays 0–12h.
        # The GitHub workflow initially renders ONE FHR at a time.
        'qpf':    lambda k: k[:3] == (0, 1, 8) and k[3] == 'surface' and k[5] == 'accum' and k[6] == f'0-{fhr}',
        # NCEP GRIB2 table 4.2-0-1: ASNOW (total snowfall, m) and WEASD
        # (water equivalent of accumulated snow depth, kg/m2). Require
        # total 0-F accumulations; do not use instantaneous SNOD snow depth.
        'asnow': lambda k: k[:3] == (0, 1, 29) and k[3] == 'surface'
                    and k[5] == 'accum' and k[6] == f'0-{fhr}',
        'weasd': lambda k: k[:3] == (0, 1, 13) and k[3] == 'surface'
                    and k[5] == 'accum' and k[6] == f'0-{fhr}',
        't2m':    lambda k: k[:3] == (0, 0, 0) and k[3:5] == ('heightAboveGround', 2),
        # Native RRFS-MPAS surface fields confirmed in the F001/F006/F024
        # GSL inventory (GRIB2 identifiers, not ecCodes short names).
        'gust10m': lambda k: k[:3] == (0, 2, 22) and k[3] == 'surface' and k[5] == 'instant',
        'u10m': lambda k: k[:3] == (0, 2, 2) and k[3:5] == ('heightAboveGround', 10) and k[5] == 'instant',
        'v10m': lambda k: k[:3] == (0, 2, 3) and k[3:5] == ('heightAboveGround', 10) and k[5] == 'instant',
        'rh2m': lambda k: k[:3] == (0, 1, 1) and k[3:5] == ('heightAboveGround', 2) and k[5] == 'instant',
        'd2m': lambda k: k[:3] == (0, 0, 6) and k[3:5] == ('heightAboveGround', 2) and k[5] == 'instant',
        'mslp':   lambda k: k[:3] == (0, 3, 198) and k[3] == 'meanSea' and k[5] == 'instant',
        'psfc':   lambda k: k[:3] == (0, 3, 0) and k[3] == 'surface' and k[5] == 'instant',
        'gh500':  lambda k: k[:3] == (0, 3, 5) and k[3:5] == ('isobaricInhPa', 500),
        'gh925':  lambda k: k[:3] == (0, 3, 5) and k[3:5] == ('isobaricInhPa', 925),
        'gh850':  lambda k: k[:3] == (0, 3, 5) and k[3:5] == ('isobaricInhPa', 850),
        'gh700':  lambda k: k[:3] == (0, 3, 5) and k[3:5] == ('isobaricInhPa', 700),
        'gh1000': lambda k: k[:3] == (0, 3, 5) and k[3:5] == ('isobaricInhPa', 1000),
    }
    for lev in LEVELS_HPA:
        defs.update({
            f't{lev}': lambda k, lev=lev: k[:3] == (0, 0, 0) and k[3:5] == ('isobaricInhPa', lev),
            f'u{lev}': lambda k, lev=lev: k[:3] == (0, 2, 2) and k[3:5] == ('isobaricInhPa', lev),
            f'v{lev}': lambda k, lev=lev: k[:3] == (0, 2, 3) and k[3:5] == ('isobaricInhPa', lev),
        })
    # 850/700 mb U/V already exist in LEVELS_HPA; add 500 mb only for the
    # 500-mb vorticity calculation. 500-mb heights (gh500) are defined above.
    for lev in VORTICITY_LEVELS_HPA:
        defs.setdefault(f'u{lev}', lambda k, lev=lev: k[:3] == (0, 2, 2)
                        and k[3:5] == ('isobaricInhPa', lev))
        defs.setdefault(f'v{lev}', lambda k, lev=lev: k[:3] == (0, 2, 3)
                        and k[3:5] == ('isobaricInhPa', lev))
    return defs


def read_selected_fields(grib_path: Path, fhr: int):
    requested = field_definitions(fhr)
    fields = {}
    shape = None
    lat = lon = None
    uv_flags = {}
    wind_grid_meta = None
    with grib_path.open('rb') as stream:
        index = 0
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            index += 1
            try:
                key = field_key(gid)
                if key[:3] in ((0,1,29),(0,1,13),(0,1,11)):
                    print('  SNOW INVENTORY',
                          str(meta(gid,'shortName','?')),
                          'level=',key[3:5], 'step=',key[5:7],
                          'units=',str(meta(gid,'units','?')))
                for label, selector in requested.items():
                    if label in fields or not selector(key):
                        continue
                    nx = int(meta(gid, 'Nx', meta(gid, 'Ni', 0)))
                    ny = int(meta(gid, 'Ny', meta(gid, 'Nj', 0)))
                    if nx <= 0 or ny <= 0:
                        raise ValueError(f'Invalid GRIB dimensions Nx={nx}, Ny={ny}')
                    if shape is None:
                        shape = (ny, nx)
                        lat = np.asarray(codes_get_array(gid, 'latitudes'), dtype=np.float32).reshape(shape)
                        lon = np.asarray(codes_get_array(gid, 'longitudes'), dtype=np.float32).reshape(shape)
                        lon = ((lon + 180) % 360) - 180
                    elif shape != (ny, nx):
                        raise ValueError('Different GRIB grids encountered; cannot combine fields')
                    arr = np.asarray(codes_get_array(gid, 'values'), dtype=np.float32).reshape(shape)
                    missing_value = float(meta(gid, 'missingValue', 9.999e20))
                    arr[~np.isfinite(arr) | np.isclose(arr, missing_value, rtol=0, atol=0)] = np.nan
                    fields[label] = arr
                    if (label[0] in ('u', 'v') and label[1:].isdigit()) or label in ('u10m', 'v10m'):
                        # ecCodes: 1=grid-relative, 0=earth-relative.
                        # Do not default missing metadata to 0 (unsafe).
                        uv_flags[label] = int(meta(gid, 'uvRelativeToGrid', -1))
                        current_grid = {
                            'gridType': str(meta(gid, 'gridType', 'unknown')),
                            'iScansNegatively': meta(gid, 'iScansNegatively', None),
                            'jScansPositively': meta(gid, 'jScansPositively', None),
                            'jPointsAreConsecutive': meta(gid, 'jPointsAreConsecutive', 0),
                            'alternativeRowScanning': meta(gid, 'alternativeRowScanning', 0),
                        }
                        if wind_grid_meta is None:
                            wind_grid_meta = current_grid
                        elif wind_grid_meta != current_grid:
                            raise ValueError('U/V messages have inconsistent grid/scanning metadata')
                    print(f'  GRIB message {index:3d}: {label:7} ({key[3]}, {key[4]}, {key[5]}, {key[6]})')
                    break
            finally:
                codes_release(gid)
    if not fields:
        raise ValueError('No expected winter fields were found in the selected GRIB2')
    if lat is None or lon is None:
        raise ValueError('Could not decode latitude / longitude')
    return lat, lon, fields, uv_flags, wind_grid_meta


def crop_to_domain(lat, lon, fields):
    lo, hi, south, north = EXTENT
    mask = (lon >= lo) & (lon <= hi) & (lat >= south) & (lat <= north)
    jj, ii = np.where(mask)
    if len(jj) == 0:
        raise ValueError(f'No MPAS gridpoints found inside {EXTENT}')
    # Add padding for derivative stencils and Gaussian smoothing.
    buffer = 35
    j0, j1 = max(0, jj.min()-buffer), min(lat.shape[0], jj.max()+buffer+1)
    i0, i1 = max(0, ii.min()-buffer), min(lat.shape[1], ii.max()+buffer+1)
    sl = np.s_[j0:j1, i0:i1]
    print(f'Using subset {j1-j0} rows x {i1-i0} columns')
    return lat[sl], lon[sl], {k: v[sl] for k, v in fields.items()}


# ──────────────────────────────────────────────────────────────────
# WIND ROTATION (GRID-RELATIVE -> TRUE EAST / TRUE NORTH)
# ──────────────────────────────────────────────────────────────────
def rotate_winds_to_earth(lat, lon, fields, uv_flags, wind_grid_meta):
    """Rotate GRIB Lambert-grid U/V winds into geographic east/north.

    GRIB2 uvRelativeToGrid=1 means U points along grid +x and V along
    grid +y. The GRIB scanning flags indicate whether the returned
    2-D arrays increase toward +x and +y, respectively. We derive the
    geographic directions of those axes from the decoded lat/lon grids.

    Returns a dictionary of fields with corrected winds; all contour
    plots, barbs, temperature advection and frontogenesis then use true
    earth-relative U/V. No interpolation or regridding is performed.
    """
    if not uv_flags:
        print('No pressure-level wind vectors to rotate.')
        return fields

    wind_levels = tuple(dict.fromkeys(LEVELS_HPA + VORTICITY_LEVELS_HPA))
    wind_pairs = [(f'u{lev}', f'v{lev}') for lev in wind_levels]
    if DO_10M_WSPD_MSLP:
        wind_pairs.append(('u10m', 'v10m'))
    for uk, vk in wind_pairs:
        if uk in fields and vk in fields:
            if uk not in uv_flags or vk not in uv_flags or uv_flags[uk] != uv_flags[vk]:
                raise ValueError(f'{uk}/{vk} wind orientation metadata disagree')

    flags = set(uv_flags.values())
    if not flags <= {0, 1}:
        raise ValueError(f'Unknown GRIB uvRelativeToGrid flag(s): {sorted(flags)}')
    if flags == {0}:
        print('GRIB winds are already earth-relative; no rotation necessary.')
        return fields
    # Mixed 0/1 flags across levels are valid when each U/V pair agrees.
    # Rotate ONLY pairs flagged as grid-relative.

    if not wind_grid_meta:
        raise ValueError('Missing GRIB grid definition for wind rotation')
    grid_type = wind_grid_meta['gridType'].lower()
    if grid_type != 'lambert':
        raise ValueError(f'Unsupported wind-rotation grid {grid_type!r}; ' 
                         'this implementation is designed for RRFS-MPAS Lambert GRIB2')
    if int(wind_grid_meta.get('jPointsAreConsecutive') or 0) != 0:
        raise ValueError('jPointsAreConsecutive=1 requires changing the GRIB reshape order')
    if int(wind_grid_meta.get('alternativeRowScanning') or 0) != 0:
        raise ValueError('Alternating row scanning requires an explicit row-order conversion')

    i_neg = wind_grid_meta['iScansNegatively']
    j_pos = wind_grid_meta['jScansPositively']
    if i_neg is None or j_pos is None:
        raise ValueError('GRIB scanning direction keys are missing; cannot safely rotate winds')
    i_sign = -1.0 if int(i_neg) else 1.0
    j_sign = 1.0 if int(j_pos) else -1.0

    # For a Lambert conformal grid, the native +i and +j axes are locally
    # orthogonal; compute their true-east/north components from lat/lon.
    R = 6_371_000.0
    lat_r = np.deg2rad(np.asarray(lat, dtype=np.float64))
    lon_r = np.deg2rad(np.asarray(lon, dtype=np.float64))
    lon_r = np.unwrap(np.unwrap(lon_r, axis=1), axis=0)
    di_e = i_sign * R * np.cos(lat_r) * np.gradient(lon_r, axis=1)
    di_n = i_sign * R * np.gradient(lat_r, axis=1)
    dj_e = j_sign * R * np.cos(lat_r) * np.gradient(lon_r, axis=0)
    dj_n = j_sign * R * np.gradient(lat_r, axis=0)
    mag_i = np.hypot(di_e, di_n)
    mag_j = np.hypot(dj_e, dj_n)
    if np.any(~np.isfinite(mag_i)) or np.any(~np.isfinite(mag_j)) or min(np.min(mag_i), np.min(mag_j)) < 1.0:
        raise ValueError('Invalid native grid geometry for wind rotation')
    ei_e, ei_n = di_e / mag_i, di_n / mag_i
    ej_e, ej_n = dj_e / mag_j, dj_n / mag_j
    dot = ei_e * ej_e + ei_n * ej_n
    cross = ei_e * ej_n - ei_n * ej_e
    if np.nanmedian(np.abs(dot)) > 0.10 or np.nanmedian(cross) < 0.80:
        raise ValueError('Native grid directions fail Lambert orientation checks; '
                         f'median |dot|={np.nanmedian(np.abs(dot)):.3f}, '
                         f'median cross={np.nanmedian(cross):.3f}; check GRIB scan flags')

    print('Rotating grid-relative Lambert winds to true-east/true-north U/V')
    print(f'  Scan metadata: iScansNegatively={int(i_neg)}, '
          f'jScansPositively={int(j_pos)}')
    rotated = dict(fields)
    for uk, vk in wind_pairs:
        if uk not in fields or vk not in fields:
            continue
        if uv_flags[uk] == 0:
            print(f'  {uk}/{vk} already earth-relative; kept unchanged')
            continue
        grid_u = np.asarray(fields[uk], dtype=np.float64)
        grid_v = np.asarray(fields[vk], dtype=np.float64)
        east_u = grid_u * ei_e + grid_v * ej_e
        north_v = grid_u * ei_n + grid_v * ej_n
        rotated[uk] = east_u.astype(np.float32)
        rotated[vk] = north_v.astype(np.float32)
        print(f'  {uk}/{vk} rotated successfully')
    return rotated


# ──────────────────────────────────────────────────────────────────
# METEOROLOGICAL DIAGNOSTICS
# All derivatives taken in local true-east / true-north coordinates.
# Earth-relative U/V are used directly. Handles Lambert grid rotation.
# ──────────────────────────────────────────────────────────────────
class GeographicGradient:
    """2D derivative in true east/north using local curvilinear Jacobian.

    For short i,j coordinate increments, dx ~ R cos(lat) d(lon),
    dy ~ R d(lat). Useful over LBF and avoids treating Lambert grid
    axes as true east/north. No external projection-library dependency.
    """
    def __init__(self, lat_deg, lon_deg):
        earth_r = 6_371_000.0
        lat = np.deg2rad(np.asarray(lat_deg, dtype=np.float64))
        lon = np.unwrap(np.deg2rad(np.asarray(lon_deg, dtype=np.float64)), axis=1)
        dx_i = earth_r * np.cos(lat) * np.gradient(lon, axis=1)
        dx_j = earth_r * np.cos(lat) * np.gradient(lon, axis=0)
        dy_i = earth_r * np.gradient(lat, axis=1)
        dy_j = earth_r * np.gradient(lat, axis=0)
        jac = dx_i*dy_j - dx_j*dy_i
        if np.nanmedian(np.abs(jac)) < 1.:
            raise ValueError('Degenerate grid detected while calculating spatial gradients')
        self.ai = dy_j / jac
        self.aj = -dy_i / jac
        self.bi = -dx_j / jac
        self.bj = dx_i / jac

    def gradient(self, field):
        dj, di = np.gradient(np.asarray(field, dtype=np.float64))
        return (di*self.ai + dj*self.aj, di*self.bi + dj*self.bj)


def smooth(field, sigma):
    """Gaussian smoothing that handles missing values without spreading NaNs."""
    v = np.asarray(field, dtype=np.float64)
    valid = np.isfinite(v)
    numerator = gaussian_filter(np.where(valid, v, 0), sigma=sigma, mode='nearest')
    denominator = gaussian_filter(valid.astype(float), sigma=sigma, mode='nearest')
    return np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator>0.95)


def calculate_relative_vorticity(lat, lon, fields, lev, grad):
    """Return 2-D relative vorticity on a pressure surface, in 10^-5 s^-1.

    U and V must first be rotated from RRFS-MPAS GRIB grid-relative winds
    to true east/north using rotate_winds_to_earth(). Differentiate the
    (lightly smoothed) earth-relative vectors using the local geographic
    Jacobian rather than treating native Lambert i/j as east/north.

    zeta = d(v_north)/dx_east - d(u_east)/dy_north
    Relative vorticity DOES NOT include Coriolis parameter f.
    """
    ukey, vkey = f'u{lev}', f'v{lev}'
    missing = [name for name in (ukey, vkey, f'gh{lev}') if name not in fields]
    if missing:
        print(f'Skipping {lev}-mb relative vorticity; missing {missing}')
        return None
    u = smooth(fields[ukey], VORTICITY_WIND_SMOOTH_SIGMA)
    v = smooth(fields[vkey], VORTICITY_WIND_SMOOTH_SIGMA)
    _, duy = grad.gradient(u)
    dvx, _ = grad.gradient(v)
    zeta_scaled = (dvx - duy) * 1.0e5
    zeta_scaled = smooth(zeta_scaled, VORTICITY_DISPLAY_SMOOTH_SIGMA)
    # Suppress pressure surfaces below local terrain, without altering the
    # native U/V arrays used for barbs or any other winter products.
    if 'psfc' in fields:
        visible = np.asarray(fields['psfc'], float) >= lev * 100.0 + 500.0
        zeta_scaled = np.where(visible, zeta_scaled, np.nan)
    return zeta_scaled


def diagnostics_for_level(lev, fields, grad):
    labels = [f't{lev}', f'u{lev}', f'v{lev}']
    if not all(k in fields for k in labels):
        print(f'Skipping {lev} mb; missing: {[k for k in labels if k not in fields]}')
        return None
    temp = smooth(fields[f't{lev}'], TEMP_SMOOTH_SIGMA_PIX)  # K
    u = smooth(fields[f'u{lev}'], WIND_SMOOTH_SIGMA_PIX)     # m/s east
    v = smooth(fields[f'v{lev}'], WIND_SMOOTH_SIGMA_PIX)     # m/s north
    tx, ty = grad.gradient(temp)                           # K/m
    ux, uy = grad.gradient(u)                              # 1/s
    vx, vy = grad.gradient(v)                              # 1/s
    # Advective tendency: positive = WAA (deg C per hour).
    adv = -(u*tx + v*ty) * 3600.0
    # 2D kinematic Petterssen frontogenesis (temperature gradient intensification).
    # Potential-temperature frontogenesis, using EACH field's actual level.
    # The reference notebook fixes pressure at 850 hPa even at other levels;
    # this explicitly uses lev (925, 850, or 700 hPa).
    theta = temp * (1000.0 / float(lev))**0.2854
    thx, thy = grad.gradient(theta)
    g = np.hypot(thx, thy)
    num = -(thx**2*ux + thx*thy*(uy+vx) + thy**2*vy)
    fgen = np.divide(num, g, out=np.full_like(num, np.nan), where=g>5e-6)
    # Units K/(100 km · 3 h): (K m-1 s-1)*100000m*10800s
    fgen *= 100_000.0 * (3*3600.0)
    # Mask pressure surfaces located below the terrain.
    if 'psfc' in fields:
        aloft = (fields['psfc'] >= lev*100.0 + 500)  # 5-hPa clearance
        adv = np.where(aloft, adv, np.nan)
        fgen = np.where(aloft, fgen, np.nan)
        temp = np.where(aloft, temp, np.nan)
        u = np.where(aloft, u, np.nan)
        v = np.where(aloft, v, np.nan)
    return {'temp_c': temp-273.15, 'u': u, 'v': v, 'adv': adv, 'fgen': fgen}


# ──────────────────────────────────────────────────────────────────
# PLOTTING / COMMON STYLING
# ──────────────────────────────────────────────────────────────────
MAP_CRS = ccrs.PlateCarree()  # CAMs viewer uses PlateCarree for plotting
PLATE = ccrs.PlateCarree()


# Colors for snow/rain intensity, adapted from the one-panel notebook.
PTYPE_PALETTES = {
    'rain': ['#C1FFC1','#90EE90','#32CD32','#228B22','#006400','#004d00',
             '#FFFF00','#FFD700','#FFA500','#FF0000','#8B0000'],
    'snow': ['#BDE1F3','#5AA2C7','#28749B','#225973','#1C3D4A','#B1298B',
             '#BF449D','#C967B0','#D787C3','#E7ABD7','#F3CBE1'],
    'sleet': ['#E3CBEF','#D1A2DF','#BD7FD4','#B15FCE','#A44EC2','#9B30BF',
              '#902CAD','#812894','#70247A','#60205C','#441B28'],
    'fzra': ['#F3CBE1','#F6BAC1','#F69796','#F17A64','#EF481E','#D53F30',
             '#BB3935','#A53A37','#923438','#7D313D','#60205C'],
}
# Advection color table: 32 one-degree bins for boundaries -16..+16.
# IMPORTANT: indices 15 and 16 correspond to [-1, 0) and [0, +1),
# respectively. The earlier version mistakenly made bins 11..13 white,
# turning approximately -5..-2 white and shifting warm colors toward zero.
# Use a BoundaryNorm to tie each exact 1-degree bin to its intended color.
TADV_NEGATIVE_COLORS = [  # 15 intervals: -16 to -1 (cool colors)
    '#bdb7e8', '#9785e4', '#6b56dd', '#4335d2', '#14029c',
    '#1734bc', '#0c56d5', '#087ce6', '#079cf1', '#03b6fc',
    '#51c9fa', '#83dafb', '#ace6f9', '#d4f0fa', '#edfaff',
]
TADV_NEUTRAL_COLORS = ['#ffffff', '#ffffff']  # -1 to 0 and 0 to +1
TADV_POSITIVE_COLORS = [  # 15 intervals: +1 to +16 (warm colors)
    '#fff9ed', '#ffedc7', '#ffdda0', '#ffcb71', '#ffb94b',
    '#fca503', '#f58e00', '#e97600', '#dd5800', '#d03a00',
    '#c21c04', '#b30000', '#cb2630', '#e33d5d', '#fc587f',
]
TADV_INTERVAL_COLORS = (
    TADV_NEGATIVE_COLORS + TADV_NEUTRAL_COLORS + TADV_POSITIVE_COLORS
)
assert len(TADV_COLOR_BOUNDS) == len(TADV_INTERVAL_COLORS) + 1
TADV_CMAP = colors.ListedColormap(TADV_INTERVAL_COLORS, name='tadv_centered_white')
TADV_CMAP.set_under(TADV_NEGATIVE_COLORS[0])
TADV_CMAP.set_over(TADV_POSITIVE_COLORS[-1])
TADV_NORM = colors.BoundaryNorm(TADV_COLOR_BOUNDS, TADV_CMAP.N, clip=False)


# LBF-domain city label styling used for winter products.
# No dots/markers are drawn; labels ride above all map layers.
LBF_CITY_LABELS = [
    ('Gordon', -102.2037, 42.8061),
    ('Ellsworth', -102.1754, 42.0928),
    ('Oshkosh', -102.3441, 41.4044),
    ('Ogallala', -101.7193, 41.1289),
    ('Imperial', -101.6432, 40.5161),
    ('Mullen', -101.0421, 42.0667),
    ('Valentine', -100.5504, 42.8736),
    ('Ainsworth', -99.8626, 42.5494),
    ('Butte', -98.8420, 42.9117),
    ("O'Neill", -98.6476, 42.4575),
    ('Burwell', -99.1370, 41.7817),
    ('Broken Bow', -99.6418, 41.4014),
    ('North Platte', -100.7654, 41.1239),
    ('Curtis', -100.5074, 40.6300),
]
CITY_LABEL_SIZE = 8.4
CITY_LABEL_COLOR = 'white'
CITY_LABEL_OUTLINE = 1.8
CITY_LABEL_ZORDER = 200     # city names and values above contours, wind barbs and H/L centers
CITY_LABEL_LAT_OFFSET = 0.0

# Custom 2-m temperature colormap from the SPCOA page (-100 to 130 F, 1-degree bins).
SFC_TEMP_BOUNDS = np.arange(-100, 131, 1, dtype=np.float64)
SFC_TEMP_COLORS = ['#3f0390', '#46038f', '#4e038e', '#55038e', '#5d038d', '#64038c', '#6b038b', '#73038a', '#7a0389', '#810489', '#890488', '#900487', '#980486', '#9f0485', '#a60484', '#ae0484', '#b50483', '#bc0482', '#c00984', '#c50e87', '#c91389', '#cd188b', '#d11d8e', '#d52290', '#d92792', '#de2b94', '#e23097', '#e63599', '#ea3a9b', '#ee3f9e', '#f244a0', '#f749a2', '#fa4ea5', '#f854a8', '#f65aab', '#f461ae', '#f267b1', '#f06db4', '#ee73b7', '#ec79bb', '#ea7fbe', '#e886c1', '#e68cc4', '#e392c7', '#e198ca', '#df9ece', '#dda5d1', '#dbaad3', '#d7add5', '#d4b1d6', '#d0b4d8', '#cdb8d9', '#c9bbda', '#c6bfdc', '#c2c2dd', '#bfc6de', '#bbc9e0', '#b8cde1', '#b4d0e3', '#b1d4e4', '#add7e5', '#aadae7', '#a6dee8', '#a3e1e9', '#9fe5eb', '#9ce8ec', '#98ecee', '#95efef', '#90f0ee', '#88e6e4', '#80dbd9', '#78d1cf', '#70c7c5', '#67bdbb', '#5fb2b0', '#57a8a6', '#4f9e9c', '#479492', '#3f8987', '#367f7d', '#2e7573', '#266b69', '#1e605e', '#165654', '#165452', '#235c5b', '#306563', '#3d6e6c', '#4a7775', '#577f7e', '#648887', '#719190', '#7e9999', '#8ba2a1', '#98abaa', '#a5b4b3', '#b2bcbc', '#bfc5c5', '#cccece', '#897fb9', '#371e9a', '#3e1f93', '#46208c', '#4d2185', '#55227e', '#5c2376', '#64246f', '#6b2568', '#732561', '#7a265a', '#822753', '#89284c', '#902944', '#982a3d', '#9f2b36', '#a72c2f', '#ad3333', '#b23e3e', '#b64a4a', '#bb5656', '#c06161', '#c56d6d', '#ca7979', '#ce8484', '#d39090', '#d89c9c', '#dda7a7', '#e2b3b3', '#e6bfbf', '#ebcaca', '#f0d6d6', '#f5e2e2', '#eee6e7', '#dce5e8', '#cbe3e9', '#bae1ea', '#a9e0eb', '#9ed9e7', '#97cddf', '#91c2d8', '#8bb6d0', '#84abc8', '#7e9fc1', '#7793b9', '#7188b2', '#6b7caa', '#6471a2', '#5e659b', '#585a93', '#5e5e8f', '#6e6e8c', '#7d7d8a', '#8d8d88', '#9c9c86', '#acac84', '#bbbb82', '#cbcb7f', '#dada7d', '#eaea7b', '#f9f979', '#fcf875', '#f6ee70', '#f0e36b', '#ebd866', '#e5ce61', '#dfc35c', '#d9b857', '#d4ad52', '#cea34d', '#c89848', '#c38d43', '#bd833d', '#b77838', '#b26d33', '#ac632e', '#a65829', '#a14d24', '#9b421f', '#95381a', '#902d15', '#8a2210', '#84180b', '#7f0d06', '#790201', '#740807', '#6e120f', '#691c17', '#64261f', '#6b332b', '#743f38', '#7d4c44', '#865951', '#8e655d', '#97726a', '#a07f76', '#a98b83', '#b2988f', '#bba59c', '#b9a19a', '#b59b95', '#b19490', '#ad8d8c', '#a98787', '#a58082', '#a1797d', '#9d7278', '#996c74', '#95656f', '#915e6a', '#8d5865', '#895161', '#854a5c', '#824357', '#7e3d52', '#7a364d', '#762f49', '#722944', '#693144', '#603943', '#574043', '#4f4843', '#464f42', '#3d5742', '#345e42', '#2c6642', '#236e41', '#1a7541', '#117d41', '#098440', '#008c40']
SFC_TEMP_CMAP = colors.ListedColormap(SFC_TEMP_COLORS, name='spcoa_2m_temperature')
SFC_TEMP_NORM = colors.BoundaryNorm(SFC_TEMP_BOUNDS, SFC_TEMP_CMAP.N, clip=True)
SFC_TEMP_TICKS = np.arange(-100, 131, 10)

# Pressure-level temperature colormap from your WeatherBell-style scheme.
PRESSURE_TEMPERATURE_CONTROL_POINTS = [
    (0.000, '#3f0390'), (0.074, '#bc0482'), (0.139, '#fa4da4'),
    (0.204, '#dca9d3'), (0.296, '#92f2f0'), (0.365, '#11504e'),
    (0.430, '#d2d2d2'), (0.435, '#341e9d'), (0.504, '#aa2c2c'),
    (0.574, '#f7e7e7'), (0.596, '#a1dfeb'), (0.648, '#555590'),
    (0.696, '#ffff78'), (0.800, '#780000'), (0.817, '#632720'),
    (0.861, '#bca79e'), (0.943, '#722944'), (1.000, '#008c40'),
]
PRESSURE_TEMP_MASTER_CMAP = colors.LinearSegmentedColormap.from_list(
    'WeatherBell_Master', PRESSURE_TEMPERATURE_CONTROL_POINTS, N=8192)
PRESSURE_TEMP_COLD_BOUNDS = np.arange(-50.0, 0.0 + 1.0, 1.0)
PRESSURE_TEMP_WARM_BOUNDS = np.arange(0.0, 40.0 + 0.2, 0.2)
PRESSURE_TEMP_BOUNDS = np.concatenate([PRESSURE_TEMP_COLD_BOUNDS, PRESSURE_TEMP_WARM_BOUNDS[1:]])
_cold_midpoints = (PRESSURE_TEMP_COLD_BOUNDS[:-1] + PRESSURE_TEMP_COLD_BOUNDS[1:]) / 2.0
_cold_fraction = ((_cold_midpoints - (-50.0)) / (0.0 - (-50.0)))
_cold_palette_pos = (0.000 + _cold_fraction * (0.430 - 0.000))
_cold_colors = PRESSURE_TEMP_MASTER_CMAP(_cold_palette_pos)
_warm_midpoints = (PRESSURE_TEMP_WARM_BOUNDS[:-1] + PRESSURE_TEMP_WARM_BOUNDS[1:]) / 2.0
_warm_fraction = (_warm_midpoints / 40.0)
_warm_palette_pos = (0.435 + _warm_fraction * (1.000 - 0.435))
_warm_colors = PRESSURE_TEMP_MASTER_CMAP(_warm_palette_pos)
PRESSURE_TEMP_COLORS = np.vstack([_cold_colors, _warm_colors])
PRESSURE_TEMP_CMAP = colors.ListedColormap(PRESSURE_TEMP_COLORS, name='WeatherBell_Pressure_Temperature')
PRESSURE_TEMP_NORM = colors.BoundaryNorm(PRESSURE_TEMP_BOUNDS, PRESSURE_TEMP_CMAP.N)
PRESSURE_TEMP_TICKS = np.concatenate([np.arange(-50, 1, 5), np.arange(3, 41, 3)])

# Pressure-level wind speed colormap (kt) for 925/850/700 mb products.
WSPD_BOUNDS = np.array([20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,79,80,81], dtype=np.float64)
WSPD_COLORS = ["#ffffff","#def0fd","#cae6fc","#b7defb","#a4d5fa","#92cdf8","#8ab6ef","#839fe6","#7c87dd","#7570d4","#6e59cb","#8566ce","#9c72d1","#b27fd5","#ca8bd8","#e298db","#dc8cd5","#d580cf","#cf74c9","#c969c3","#c35dbd","#bb4fb5","#b342ad","#ab35a5","#a3289d","#9b1d95","#a21c80","#a91c6a","#b11c55","#b81c41","#c01c2e","#c42032","#c72435","#cb2939","#cf2e3d","#d33441","#d73b45","#db4249","#df494c","#e35050","#e75754","#e97559","#ec935d","#efb262","#f3d167","#f7f16b","#f0e765","#eadd60","#e4d35a","#dec954","#d8bf4e","#d1b548","#cbab42","#c5a13c","#bf9737","#b98e31","#b3842b","#ad7a26","#a77021","#a1661c","#9b5c17"]
# White for the first (20-21 kt) bin; display-only clipping also makes
# all winds below 20 kt pure white on 925/850/700 mb plots.
WSPD_CMAP = colors.ListedColormap(WSPD_COLORS, name='midlevelwinds_bins')
WSPD_NORM = colors.BoundaryNorm(WSPD_BOUNDS, WSPD_CMAP.N, clip=True)
WSPD_TICKS = np.arange(20, 82, 5)
HEIGHT_CONTOUR_INTERVALS_DAM = {925: 3, 850: 3, 700: 3, 500: 3}  # 3 dam = 30 m

# RRFS-MPAS relative vorticity: exact SPCOA pressure-level color table.
# Negative vorticity: black -> gray; neutral: white; positive: yellow -> red.
# Field units after calculation are 10^-5 s^-1. 45 bins, 46 edges.
VORTICITY_BOUNDS = np.arange(-40.0, 52.0, 2.0, dtype=np.float64)
VORTICITY_COLORS = [
    '#000000', '#0f0f0f', '#1e1e1e', '#303030', '#434343', '#555555', '#636363', '#717171', '#7f7f7f', '#8f8f8f',
    '#9e9e9e', '#afafaf', '#bebebe', '#cbcbcb', '#d6d6d6', '#e1e1e1', '#eaeaea', '#f3f3f3', '#f9f9f9', '#ffffff',
    '#ffffff', '#ffffcc', '#fff9bd', '#fff3ae', '#ffec9f', '#ffe590', '#fede82', '#fed673', '#fec965', '#feba55',
    '#fead4a', '#fea044', '#fd933f', '#fd8239', '#fc6c33', '#fc572c', '#f74327', '#ed3022', '#e51e1d', '#d9131f',
    '#cb0a22', '#be0126', '#aa0026', '#950026', '#800026',
 ]
assert len(VORTICITY_COLORS) == len(VORTICITY_BOUNDS) - 1
VORTICITY_CMAP = colors.ListedColormap(VORTICITY_COLORS, name='spcoa_relative_vorticity')
# Exceeding +/- palette bounds receives saturated endpoint colors and arrowheads.
VORTICITY_CMAP.set_under(VORTICITY_COLORS[0])
VORTICITY_CMAP.set_over(VORTICITY_COLORS[-1])
VORTICITY_NORM = colors.BoundaryNorm(VORTICITY_BOUNDS, VORTICITY_CMAP.N, clip=False)
VORTICITY_TICKS = np.arange(-40, 51, 10)
VORTICITY_WIND_SMOOTH_SIGMA = 3.0  # smooth vectors BEFORE spatial derivatives
VORTICITY_DISPLAY_SMOOTH_SIGMA = 1.0  # additional display-only vorticity smoothing

# Exact user-supplied 10-m sustained-wind colors: 52 swatches.
# 9..60 inclusive supplies 51 intervals; extend the top edge to 61 mph
# so all 52 swatches retain their intended 1-mph bins. Tick labels stop at 60.
WSPD10M_BOUNDS = np.arange(9., 62., 1.)
WSPD10M_COLORS = [
    '#fefefe', '#f1f8ff', '#daedfd', '#c3e3fc', '#abd9fa', '#95cff9', '#8ab6ef', '#819ae4', '#797ed9', '#7062cf',
    '#8064cd', '#9c72d1', '#b781d6', '#d490d9', '#de91d7', '#d682d0', '#cf74c9', '#c867c2', '#c057ba', '#b648b0',
    '#ad38a7', '#a3289d', '#9d1d91', '#a51c77', '#ae1c5e', '#b61c45', '#c01c2e', '#c52133', '#c92637', '#cd2c3b',
    '#d23340', '#d73b45', '#dc4349', '#e14b4e', '#e55452', '#e96f58', '#ec935d', '#f0b863', '#f4de69', '#f2eb67',
    '#ebdf61', '#e4d35a', '#ddc753', '#d5bb4c', '#cdaf44', '#c6a33d', '#bf9737', '#b88c30', '#b18029', '#a97422',
    '#a2681c', '#9b5c17',
]
assert len(WSPD10M_COLORS) == len(WSPD10M_BOUNDS) - 1, '10-m sustained speed bins mismatch'
WSPD10M_CMAP = colors.ListedColormap(WSPD10M_COLORS, name='10mwinds_bins')
WSPD10M_NORM = colors.BoundaryNorm(WSPD10M_BOUNDS, WSPD10M_CMAP.N, clip=True)
WSPD10M_TICKS = np.arange(10, 61, 5)

# Custom gust palette: 15–75 mph, 60 one-mph bins.
# The supplied 59-color palette ended at 74 mph; repeat the last swatch
# once to create the 74–75 mph bin without introducing a blank white tail.
GUST10M_BOUNDS = np.arange(15., 76., 1.)
GUST10M_COLORS = [
    '#ffffff', '#f1f8ff', '#def0fd', '#cae6fc', '#b7defb', '#a4d5fa', '#92cdf8', '#8ab6ef', '#839fe6', '#7c87dd',
    '#7570d4', '#6e59cb', '#8566ce', '#9c72d1', '#b27fd5', '#ca8bd8', '#e298db', '#dc8cd5', '#d580cf', '#cf74c9',
    '#c969c3', '#c35dbd', '#bb4fb5', '#b342ad', '#ab35a5', '#a3289d', '#9b1d95', '#a21c80', '#a91c6a', '#b11c55',
    '#b81c41', '#c01c2e', '#c42032', '#c72435', '#d33441', '#d73b45', '#df494c', '#e35050', '#e75754', '#e97559',
    '#ec935d', '#efb262', '#f3d167', '#f7f16b', '#f0e765', '#eadd60', '#e4d35a', '#dec954', '#d8bf4e', '#d1b548',
    '#cbab42', '#c5a13c', '#bf9737', '#b98e31', '#b3842b', '#ad7a26', '#a77021', '#a1661c', '#9b5c17', '#9b5c17',
]
assert len(GUST10M_COLORS) == len(GUST10M_BOUNDS) - 1, 'Gust bins mismatch'
GUST10M_CMAP = colors.ListedColormap(GUST10M_COLORS, name='gust_bins')
GUST10M_NORM = colors.BoundaryNorm(GUST10M_BOUNDS, GUST10M_CMAP.N, clip=True)
GUST10M_TICKS = np.arange(15, 76, 5)

# 2-m RH in 1%-wide bins using user's custom palette.
RH2M_BOUNDS = np.arange(-1., 101., 1.)
RH2M_COLORS = [
    '#a1744f','#966d4b','#8b6648','#816044','#765940','#6c523c','#624b38','#574434','#4c3d30','#42362d','#372f28','#3b352a','#413a2f','#464035','#4c453a',
    '#514b3f','#575044','#5c554a','#625b4f','#676054','#6d6559','#736b5f','#787064','#7e766a','#837b6f','#898074','#8e867a','#948b7f','#999084','#9f968a',
    '#a49b8f','#aaa195','#b0a69a','#b5ab9f','#bbb1a4','#c0b6aa','#c6bbaf','#cbc1b4','#d1c6b9','#d6ccbf','#dcd1c4','#c9d7c0','#c5d4bd','#c1d1ba','#bdceb7',
    '#b9cbb4','#b5c9b1','#b1c6ae','#adc3ab','#a9c0a8','#a5bda5','#a1baa2','#9db79f','#99b49c','#95b29a','#92af97','#8eac94','#8aa991','#86a68e','#82a38b',
    '#7d9f88','#799c85','#769a82','#72977f','#6e947c','#6a9179','#668e76','#628b73','#5f8870','#5b856d','#57836a','#538067','#4f7d64','#4b7a61','#47775e',
    '#43745b','#407158','#3c6e55','#386c53','#356950','#31664d','#2e634a','#2a6047','#275d44','#235a41','#1f573e','#1c553b','#195238','#164f35','#164f35',
    '#134c32','#10492f','#0c4023','#11422e','#144538','#184743','#1b494d','#1f4c57','#234e61','#27506c','#2b5276','#2f5581','#33578b',
]
# The supplied RH table has 103 colors for 101 1%-wide bins.
# BoundaryNorm spans all 103 supplied colors across the 101 intervals (two skipped),
# preserving the exact user-supplied bounds/colors instead of shifting percentages.
assert len(RH2M_COLORS) >= len(RH2M_BOUNDS) - 1, 'RH palette too short'
RH2M_CMAP = colors.ListedColormap(RH2M_COLORS, name='rh_bins')
RH2M_NORM = colors.BoundaryNorm(RH2M_BOUNDS, RH2M_CMAP.N, clip=True)
RH2M_TICKS = np.arange(0, 101, 10)

# Exact user-supplied 2-m dewpoint color scale (-41 to +90 deg F in 1F bins).
DEW2M_BOUNDS = np.arange(-41., 91., 1.)
DEW2M_COLORS = [
    '#946e4f','#926d4e','#906c4e','#8e6b4d','#8c6a4d','#8b694c','#89674c','#87664b',
    '#85654a','#83644a','#816349','#7f6249','#7d6147','#7b6047','#795f46','#775e45',
    '#755c45','#735b44','#715a44','#705943','#6f5843','#6d5742','#6b5641','#695541',
    '#675440','#655340','#63513f','#61503e','#5f4f3e','#5d4e3d','#5b4c3d','#594b3c','#574a3c',
    '#56493b','#54483a','#52473a','#504539','#4e4439','#4c4338','#4a4237','#484136',
    '#4c4335','#504739','#554c3d','#595042','#5d5546','#61594a','#665e4e','#6a6252',
    '#6e6756','#736b5b','#77705f','#7b7463','#7f7967','#847d6b','#888270','#8c8674',
    '#918b78','#958f7c','#999480','#9d9884','#a29d89','#a6a18d','#aaa691','#aeaa95',
    '#b3af99','#b7b39d','#bbb8a2','#c0bca6','#c4c1aa','#c8c5ae','#cccab2','#d1ceb7',
    '#d5d3bb','#d9d7bf','#dedcc3','#e2e0c7','#e6e5cb','#eae9d0','#efeed4','#f3f2d8',
    '#e7f5e6','#d9f0d7','#caeac9','#bce4ba','#aedeab','#a0d99c','#92d38d','#84ce7f',
    '#76c870','#69c362','#42ad35','#3da231','#38982c','#338d27','#2d8222','#28781e',
    '#236d19','#1e6215','#195810','#144d0c','#6aa2ae','#60959f','#588891','#4e7a82',
    '#456d73','#3c6066','#325357','#294648','#20393a','#162c2b','#686699','#625e93',
    '#5b568d','#554e87','#4f4681','#483e7b','#423675','#3c2e6f','#352669','#2f1d63',
    '#704170','#754673','#7a4c75','#805176','#855778','#8b5c7a','#90627c','#96677d',
    '#9b6d7f','#a07281',
]
assert len(DEW2M_COLORS) == len(DEW2M_BOUNDS) - 1, 'Dewpoint bins mismatch'
DEW2M_CMAP = colors.ListedColormap(DEW2M_COLORS, name='dewpoint_bins')
DEW2M_NORM = colors.BoundaryNorm(DEW2M_BOUNDS, DEW2M_CMAP.N, clip=True)
DEW2M_TICKS = np.arange(-40, 91, 10)


# EXACT user-supplied QPF palette: 52 discrete colors and 53 boundaries.
# BoundaryNorm(..., clip=True) and shade_at_palette_limits(...) ensure
# rectangular colorbars without extends, while numerical fields stay intact.
# Only temperature-advection and advection+frontogenesis maps use extend='both'.
QPF_COLORS = ['#ffffff', '#dcdcdc', '#bebebe', '#a0a0a0', '#828282', '#b7f0be', '#9fdbb3', '#87c7a7', '#6fb29c', '#579d90', '#3f8885', '#277479', '#146473', '#1450b4', '#2a61bb', '#3f73c2', '#5584c9', '#6b96d0', '#80a7d6', '#96b9dd', '#accae4', '#c1dceb', '#d7edf2', '#cebce0', '#c9addb', '#c49ed5', '#bf90d0', '#ba81ca', '#b472c5', '#af63bf', '#aa55ba', '#a546b4', '#a037af', '#a53a34', '#ad4842', '#b5554f', '#bd635d', '#c6716b', '#ce7f79', '#d68c86', '#de9a94', '#f8eea2', '#eed68c', '#e5bd76', '#dba560', '#d28c4a', '#c87434', '#ac632d', '#965727', '#814a21', '#6b3e1b', '#553115']
QPF_BOUNDS = np.concatenate([
    np.arange(0.00, 0.01, 0.01),
    np.arange(0.01, 0.05, 0.02),
    np.arange(0.05, 0.10, 0.025),
    np.arange(0.10, 1.0, 0.05),
    np.arange(1.0, 2.0, 0.1),
    np.arange(2.0, 4.0, 0.25),
    np.arange(4.0, 6.0, 0.5),
    np.arange(6.0, 10.0, 1.0),
    np.arange(10.0, 17.5, 2.5),
])
if QPF_BOUNDS[-1] < 17.5:
    QPF_BOUNDS = np.append(QPF_BOUNDS, 17.5)
assert len(QPF_COLORS) == len(QPF_BOUNDS)-1, (len(QPF_COLORS),len(QPF_BOUNDS))
QPF_CMAP = colors.ListedColormap(QPF_COLORS, name='precip_bins')
QPF_NORM = colors.BoundaryNorm(QPF_BOUNDS, QPF_CMAP.N, clip=True)
# Match the MRMS LBF precipitation colorbar's displayed tick labels exactly.
# These are label positions only; the 52-bin user-supplied palette is unchanged.
QPF_TICKS = [
    0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
    1, 1.2, 1.4, 1.6, 1.8, 2, 2.5, 3, 3.5, 4, 5, 6, 8, 10, 15,
]
def _qpf_tick_label(value):
    if value < 0.1:
        return f'{value:.2f}'
    if value < 1.0:
        return f'{value:.1f}'
    if float(value).is_integer():
        return str(int(value))
    return f'{value:.1f}'
QPF_TICK_LABELS = [_qpf_tick_label(value) for value in QPF_TICKS]

# Accumulated snowfall (native ASNOW, inches) — user-specified palette.
# Native model snowfall is not computed using a hard-coded 10:1 ratio.
# Each color applies to [bound[i], bound[i+1]) in inches.
SNOWFALL_BOUNDS = np.array([
    0, 0.1, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5,
    5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 11, 12,
    13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
    25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
    37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48
], dtype=float)
SNOWFALL_COLORS = [
    '#ffffff', '#dadbdb', '#959596', '#6e6e6e', '#505050', '#a0d2fa', '#84b8f9', '#63a3f3', '#5495f3', '#4581ef',
    '#3d6ce9', '#3462d1', '#2d58c2', '#40008e', '#4d028d', '#59048c', '#66068a', '#810c88', '#9c1484', '#c21c81',
    '#ea247d', '#ec2b86', '#f0469c', '#f564af', '#f773b7', '#f488c3', '#ef90c7', '#e897cb', '#e39fcd', '#d6aed5',
    '#d0b5d8', '#c4c7e0', '#b9d6e8', '#b2e2ee', '#aaeef2', '#a2faf8', '#9ff2f0', '#8adad8', '#7cbcc4', '#84b8ca',
    '#88b6cc', '#8bb2ce', '#8fb0d0', '#92aed2', '#96acd4', '#99a8d8', '#9ca6da', '#a0a4dc', '#a5a3de', '#a99fe0',
    '#ac9de4', '#b09be6', '#b399e8', '#b795e9', '#bb93eb', '#be91ef', '#c28ff1', '#c78cf3', '#cf88f7'
 ]
assert len(SNOWFALL_COLORS) == len(SNOWFALL_BOUNDS) - 1, 'Snowfall bins mismatch'
SNOWFALL_CMAP = colors.ListedColormap(SNOWFALL_COLORS, name='snowfall_bins')
SNOWFALL_NORM = colors.BoundaryNorm(SNOWFALL_BOUNDS, SNOWFALL_CMAP.N, clip=True)
# Label key values rather than every one-inch boundary on the figure.
# Match the snowfall reference graphic: 0.1; 1 through 10 by 1; 12 through 48 by 2.
# These are display tick positions; all original 59 color bins remain unchanged.
SNOWFALL_TICKS = [0.1] + list(range(1, 11)) + list(range(12, 49, 2))
SNOWFALL_TICK_LABELS = ['0.1'] + [str(n) for n in range(1, 11)] + [str(n) for n in range(12, 49, 2)]


def _unpack_map_shapefile_archive(archive_path):
    """Extract only GIS sidecars from an owned ZIP into assets/, safely.

    Supports c_18mr25.zip, w_16ap26.zip and zipped Census counties/states,
    including ZIPs with a single containing folder. Never executes files.
    """
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        return
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as z:
        for member in z.infolist():
            filename = Path(member.filename).name
            if (member.is_dir() or not filename or
                    Path(filename).suffix.lower() not in
                    {'.shp', '.shx', '.dbf', '.prj', '.cpg'}):
                continue
            dest = ASSET_DIR / filename
            if not dest.is_file():
                dest.write_bytes(z.read(member))
    print('Unpacked map shapefile archive:', archive_path.name)


def _complete_shapefile(shp):
    shp = Path(shp)
    return all(shp.with_suffix(ext).is_file() for ext in ('.shp','.shx','.dbf'))


def prepare_assets():
    """Resolve optional Colab assets and the required branded CI map assets.

    Existing assets from the user's severe CAMs/GIS projects are used as-is;
    no approximate CWA lines or logos are generated. An automated publishing
    run fails clearly if the map boundary/logo cannot be found.
    """
    global CWA_SHP_PATH
    candidate_zips = [CWA_ZIP_PATH, ASSET_DIR / 'w_16ap26.zip',
                      ASSET_DIR / 'cb_2018_us_county_500k.zip',
                      ASSET_DIR / 'cb_2018_us_state_500k.zip']
    for archive in dict.fromkeys(map(str, candidate_zips)):
        if Path(archive).is_file():
            _unpack_map_shapefile_archive(archive)

    # Preference: user's configured office boundary; alternate NWS WFO archive.
    # Do not select unrelated shapes in the assets folder.
    candidates = [Path(CWA_SHP_PATH), ASSET_DIR / 'w_16ap26.shp',
                  ASSET_DIR / 'c_18mr25.shp']
    found = next((p for p in candidates if _complete_shapefile(p)), None)
    if found:
        CWA_SHP_PATH = str(found)
        print('CWA geometry source:', CWA_SHP_PATH)
    else:
        print('NOTICE: LBF CWA shapefile is missing (c_18mr25 or w_16ap26).')

    missing=[]
    if DRAW_CWA_OUTLINE and not (
        (found is not None) or
        (CWA_GEOJSON_PATH and Path(CWA_GEOJSON_PATH).is_file())
    ):
        missing.append('LBF CWA shapefile/GeoJSON')
    if COUNTIES and not _complete_shapefile(COUNTY_SHP_PATH):
        missing.append('cb_2018_us_county_500k.shp/.shx/.dbf')
    if not _complete_shapefile(STATE_SHP_PATH):
        missing.append('cb_2018_us_state_500k.shp/.shx/.dbf')
    if CAM_STYLE_LOGO and not Path(LOGO_PATH).is_file():
        missing.append('NOAANWSLogos.png')
    if missing:
        msg = 'Missing map assets in ' + str(ASSET_DIR) + ': ' + '; '.join(missing)
        if REQUIRE_MAP_ASSETS:
            raise FileNotFoundError(msg + '. Supply them before publishing to AWS S3.')
        print('NOTICE:', msg, '(non-publishing test may omit these overlays)')
    else:
        print('Verified: state/county/CWA shapefiles and NOAA/NWS logo are present.')

    # Existing official 2023 Census road retrieval retained for I-80/I-76.
    if SHOW_LBF_INTERSTATE and 'lbf' in RUN_DOMAINS:
        shp = Path(INTERSTATE_SHP_PATH).expanduser()
        if not shp.is_file() and not str(shp).startswith('/content/drive/'):
            target_zip = shp.with_suffix('.zip')
            target_zip.parent.mkdir(parents=True, exist_ok=True)
            if not target_zip.is_file() and FETCH_INTERSTATE_SHAPEFILE:
                url = ('https://www2.census.gov/geo/tiger/TIGER2023/'
                       'PRIMARYROADS/tl_2023_us_primaryroads.zip')
                print('Downloading official 2023 Census primary roads ...')
                try:
                    urlretrieve(url, target_zip)
                except Exception as exc:
                    print('Census interstate shapefile download failed:', exc)
                    target_zip.unlink(missing_ok=True)
            if target_zip.is_file():
                with zipfile.ZipFile(target_zip) as archive:
                    wanted = {shp.stem + suffix for suffix in
                              ('.shp','.shx','.dbf','.prj','.cpg')}
                    for info in archive.infolist():
                        if Path(info.filename).name in wanted and \
                                '/' not in info.filename.strip('/\\'):
                            (target_zip.parent / Path(info.filename).name).write_bytes(
                                archive.read(info))
                print('Census interstate shapefile available:', shp.is_file())


def add_counties_and_states(ax):
    """Draw dark-charcoal counties, black LBF interior counties and black state borders.

    The provided site shapefiles take precedence; use MetPy/Cartopy only as
    fallbacks. Return the county geometries, reused for CWA interior borders.
    """
    import cartopy.io.shapereader as shpreader
    counties = None
    if COUNTIES:
        if Path(COUNTY_SHP_PATH).is_file():
            try:
                # Restrict expensive national shapefile to this map's extent.
                bbox=(EXTENT[0]-.3,EXTENT[2]-.3,EXTENT[1]+.3,EXTENT[3]+.3)
                counties=list(shpreader.Reader(COUNTY_SHP_PATH,bbox=bbox).geometries())
                ax.add_geometries(counties,crs=PLATE,facecolor='none',
                                  edgecolor=COUNTY_OUTSIDE_COLOR,linewidth=COUNTY_OUTSIDE_LINEWIDTH,zorder=29)
            except Exception as exc:
                print('County shapefile unavailable:',str(exc)[:150])
        if counties is None:
            try:
                from metpy.plots import USCOUNTIES
                ax.add_feature(USCOUNTIES,facecolor='none',edgecolor=COUNTY_OUTSIDE_COLOR,
                               linewidth=COUNTY_OUTSIDE_LINEWIDTH,zorder=29)
            except Exception as exc:
                print('MetPy counties missing:',str(exc)[:120])
    if Path(STATE_SHP_PATH).is_file():
        try:
            ax.add_geometries(shpreader.Reader(STATE_SHP_PATH).geometries(),
                              crs=PLATE,edgecolor='black',facecolor='none',
                              linewidth=1.4,zorder=30)
        except Exception as exc:
            print('State shapefile unavailable:',str(exc)[:120])
            ax.add_feature(cfeature.STATES.with_scale('50m'),facecolor='none',
                           edgecolor='black',linewidth=1.4,zorder=30)
    else:
        ax.add_feature(cfeature.STATES.with_scale('50m'),facecolor='none',
                       edgecolor='black',linewidth=1.4,zorder=30)
    return counties


def add_cwa_outline(ax, county_geometries=None):
    """Draw LBF CWA exactly as the CAMs viewer: bold black/white double line,
    with a thicker black county network within the CWA polygon.
    """
    from cartopy.io import shapereader as shpreader
    from shapely.ops import unary_union
    geom=None
    if Path(CWA_SHP_PATH).is_file():
        try:
            records=list(shpreader.Reader(CWA_SHP_PATH).records())
            chosen=[r.geometry for r in records if any(
                str(r.attributes.get(field, '')).strip().upper() in ('LBF', 'KLBF')
                for field in ('CWA', 'WFO', 'WFO_ID', 'CWA_CODE', 'OFFICE')
            )]
            if chosen:
                geom=unary_union(chosen)
            elif len(records)==1:
                geom=records[0].geometry
            else:
                print('CWA shapefile has several features and no LBF record; '
                      'skipping to avoid drawing an incorrect office outline.')
        except Exception as exc:
            print('CWA boundary unavailable:',str(exc)[:160])
    elif CWA_GEOJSON_PATH and Path(CWA_GEOJSON_PATH).is_file():
        try:
            from shapely.geometry import shape
            raw=json.loads(Path(CWA_GEOJSON_PATH).read_text())
            items=raw.get('features',[]) if raw.get('type')=='FeatureCollection' else [raw]
            geom=unary_union([shape(o['geometry'] if o.get('type')=='Feature' else o)
                              for o in items])
        except Exception as exc:
            print('CWA GeoJSON unavailable:',str(exc)[:160])
    if geom is None:
        print('For exact CAMs CWA styling, supply the LBF WFO boundary (c_18mr25 or w_16ap26) under',ASSET_DIR)
        return
    if county_geometries:
        try:
            from shapely.prepared import prep
            prepared=prep(geom)
            internal=[]
            for county in county_geometries:
                if prepared.intersects(county):
                    segment=county.intersection(geom)
                    if not segment.is_empty:
                        internal.append(segment)
            if internal:
                ax.add_geometries(internal,crs=PLATE,facecolor='none',
                                  edgecolor='black',linewidth=1.0,zorder=32)
        except Exception as exc:
            print('CWA interior county overlay unavailable:',str(exc)[:150])
    ax.add_geometries([geom],crs=PLATE,facecolor='none',
                      edgecolor='black',linewidth=3.5,zorder=34)
    ax.add_geometries([geom],crs=PLATE,facecolor='none',
                      edgecolor='white',linewidth=1.8,zorder=35)



# ──────────────────────────────────────────────────────────────────
# LBF I-80 + I-76 OVERLAY — same shapefile/style as the MRMS QPF code.
# ──────────────────────────────────────────────────────────────────
_LBF_INTERSTATE_CACHE = None


def load_lbf_interstate_geometries():
    """Load I-80/I-76 segments from the QPF primary-roads shapefile once.

    TIGER FULLNAME typically uses values such as 'I- 80' and 'I- 76'.
    Match full route numbers to avoid accidentally selecting US highways or
    other interstate numbers. Only segments in/near the LBF domain are read.
    """
    global _LBF_INTERSTATE_CACHE
    if _LBF_INTERSTATE_CACHE is not None:
        return _LBF_INTERSTATE_CACHE
    _LBF_INTERSTATE_CACHE = {route: [] for route in INTERSTATE_ROUTE_NUMBERS}
    road_file = Path(INTERSTATE_SHP_PATH).expanduser()
    route_names = ' and '.join('I-' + route for route in INTERSTATE_ROUTE_NUMBERS)
    if not road_file.is_file() and AUTO_MOUNT_DRIVE_FOR_INTERSTATE:
        if str(road_file).startswith('/content/drive/') and Path('/content').is_dir():
            try:
                from google.colab import drive
                print(f'Mounting Google Drive to locate {route_names} shapefile ...')
                drive.mount('/content/drive', force_remount=False)
            except Exception as exc:
                print(f'Unable to mount Google Drive for {route_names}:', str(exc)[:180])
    if not road_file.is_file():
        print(f'{route_names} overlay skipped: road shapefile not found at', road_file)
        print('  If needed, change INTERSTATE_SHP_PATH to your Colab/Drive location.')
        return _LBF_INTERSTATE_CACHE

    try:
        import cartopy.io.shapereader as shpreader
        west, east, south, north = DOMAINS['lbf']
        bbox = (west - .75, south - .75, east + .75, north + .75)
        reader = shpreader.Reader(str(road_file), bbox=bbox)
        # FULLNAME examples: 'I- 80', 'I-80', 'I 76'; exact route matching.
        route_re = re.compile(
            r'\bI\s*[- ]\s*(' +
            '|'.join(re.escape(route) for route in INTERSTATE_ROUTE_NUMBERS) +
            r')\b', flags=re.IGNORECASE)
        for rec in reader.records():
            attrs = rec.attributes
            fullname = str(attrs.get('FULLNAME') or attrs.get('FullName') or '')
            match = route_re.search(fullname)
            if match:
                geom = rec.geometry
                if geom is not None and not geom.is_empty:
                    _LBF_INTERSTATE_CACHE[match.group(1)].append(geom)
        try:
            reader.close()
        except (AttributeError, NotImplementedError):
            pass
        for route, geometries in _LBF_INTERSTATE_CACHE.items():
            if geometries:
                print(f'I-{route}: loaded {len(geometries)} road segment(s) '
                      f'from {road_file.name}')
            else:
                print(f'I-{route}: no matching FULLNAME segments in/near the '
                      'LBF extent; check shapefile data coverage.')
    except Exception as exc:
        print(f'{route_names} shapefile could not be read:', str(exc)[:220])
        _LBF_INTERSTATE_CACHE = {route: [] for route in INTERSTATE_ROUTE_NUMBERS}
    return _LBF_INTERSTATE_CACHE


def add_lbf_interstate(ax):
    """Draw thin red I-80 and I-76 lines on LBF maps only."""
    if not SHOW_LBF_INTERSTATE or DOMAIN_NAME != 'lbf':
        return
    for route, geometries in load_lbf_interstate_geometries().items():
        if geometries:
            ax.add_geometries(geometries, crs=PLATE, facecolor='none',
                              edgecolor=INTERSTATE_COLOR,
                              linewidth=INTERSTATE_LINEWIDTH,
                              zorder=INTERSTATE_LINE_ZORDER)


def new_map(title, subtitle, ptype=False):
    """Match the existing RRFS-MPAS CAMs layout for every winter product.

    Geographic bounds are identical to CAMs. By default, figure construction
    and DPI follow the supplied HRRR CAMs code (14 x 10 in, PlateCarree,
    140 DPI, tight bbox). CAMS_REFERENCE_LAYOUT=False restores the
    adaptive-height option if the classic figure leaves excess margins.
    """
    box=PTYPE_MAP_BOX if ptype else OTHER_MAP_BOX
    if CAMS_REFERENCE_LAYOUT:
        # Match the actual HRRR CAMs plotting script, not a screenshot estimate.
        fig=plt.figure(figsize=CAMS_FIGSIZE,facecolor='white')
        ax=plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent(EXTENT,crs=ccrs.PlateCarree())
    else:
        # Retain the prior compact adaptive-height layout as an option.
        fig=plt.figure(figsize=(FIGURE_WIDTH_IN,10),facecolor='white')
        ax=fig.add_axes(box,projection=MAP_CRS)
        ax.set_extent(EXTENT,crs=PLATE)
        dx=float(np.ptp(ax.get_xlim()))
        dy=float(np.ptp(ax.get_ylim()))
        if dx<=0 or dy<=0:
            raise ValueError(f'Invalid map extent for domain {DOMAIN_NAME}: {EXTENT}')
        fig.set_size_inches(
            FIGURE_WIDTH_IN,
            FIGURE_WIDTH_IN*box[2]/box[3]*dy/dx,
            forward=True,
        )
        ax.set_position(box)
    ax.set_facecolor('white')
    counties=add_counties_and_states(ax)
    if DRAW_CWA_OUTLINE:
        add_cwa_outline(ax,counties)
    # QPF-reference thin red I-80/I-76 lines on LBF only; no regional routes.
    add_lbf_interstate(ax)

    if SHOW_CITY_LABELS and DOMAIN_NAME == 'lbf':
        for name, lo, la in LBF_CITY_LABELS:
            if EXTENT[0] <= lo <= EXTENT[1] and EXTENT[2] <= la <= EXTENT[3]:
                ax.text(lo, la + CITY_LABEL_LAT_OFFSET, name, transform=PLATE,
                        fontsize=CITY_LABEL_SIZE, color=CITY_LABEL_COLOR,
                        weight='bold', ha='center', va='center', clip_on=True,
                        zorder=CITY_LABEL_ZORDER,
                        path_effects=[pe.withStroke(linewidth=CITY_LABEL_OUTLINE,
                                                    foreground='black')])

    # Top-of-map typography mirrors CAMs: title top left, FHR/valid below left,
    # run initialization right-aligned on the same second line.
    parts=subtitle.split(' | ',1)
    valid_title=parts[0]
    init_title=parts[1] if len(parts)>1 else ''
    cfg_sizes={'lbf':(14,11),'regional':(13,11),'central_plains':(13,11)}
    title_size,subtitle_size=cfg_sizes.get(DOMAIN_NAME,(13,11))
    ax.text(0.0,1.042,title,transform=ax.transAxes,ha='left',va='bottom',
            fontsize=title_size,weight='bold',clip_on=False,zorder=MAP_TITLE_ZORDER)
    ax.text(0.0,1.005,valid_title,transform=ax.transAxes,ha='left',va='bottom',
            fontsize=subtitle_size,weight='bold',clip_on=False,zorder=MAP_TITLE_ZORDER)
    ax.text(1.0,1.005,init_title,transform=ax.transAxes,ha='right',va='bottom',
            fontsize=subtitle_size,weight='bold',clip_on=False,zorder=MAP_TITLE_ZORDER)

    # Inset logo and credit duplicate the existing CAMs severe-product style.
    if CAM_STYLE_LOGO and LOGO_PATH and Path(LOGO_PATH).is_file():
        try:
            from matplotlib.image import imread
            logo_ax=ax.inset_axes([.82,.84,.165,.155],transform=ax.transAxes,zorder=55)
            logo_ax.imshow(imread(LOGO_PATH))
            logo_ax.axis('off')
        except Exception as exc:
            print('Logo rendering error:',str(exc)[:150])
    # Office label is the topmost map annotation: preserve legibility even
    # where frontogenesis/temperature/height labels cross this location.
    ax.text(.902,.835,'NWS North Platte, NE',transform=ax.transAxes,
            ha='center',va='top',fontsize=10,weight='bold',color='black',
            zorder=OFFICE_LABEL_ZORDER,
            path_effects=[pe.withStroke(linewidth=2.5,foreground='white')])
    ax.text(.01,.015,MAP_CREDIT,transform=ax.transAxes,ha='left',va='bottom',
            fontsize=9,weight='bold',color='black',zorder=MAP_CREDIT_ZORDER,
            path_effects=[pe.withStroke(linewidth=2.5,foreground='white')])
    return fig,ax


def finish(fig, ax, fname, label=None, cf=None, colorbar_extend=None):
    if cf is not None:
        # CAMs reference: make_axes_locatable, bottom bar 3%, 0.25 in padding.
        divider=make_axes_locatable(ax)
        cax=divider.append_axes('bottom',size='3%',pad=.25,axes_class=plt.Axes)
        cb_kwargs = {'cax':cax, 'orientation':'horizontal', 'spacing':'uniform',
                     'drawedges':not (label and 'Temperature' in label)}
        if colorbar_extend is not None:
            cb_kwargs['extend'] = colorbar_extend
        cb=fig.colorbar(cf, **cb_kwargs)
        if label:
            cb.set_label(label,fontsize=10,weight='bold')
            cb.ax.xaxis.set_label_position('top')
        cb.ax.tick_params(axis='x',which='both',length=0,labelsize=9)
        if label == '2-m Temperature (°F)':
            apply_colorbar_ticks(cb, SFC_TEMP_TICKS, fontsize=8)
        elif label and 'Temperature (°C)' in label:
            apply_colorbar_ticks(cb, PRESSURE_TEMP_TICKS, fontsize=7)
        elif label and 'Wind Speed (kt)' in label:
            apply_colorbar_ticks(cb, WSPD_TICKS, fontsize=8)
        elif label and 'Relative Vorticity' in label:
            apply_colorbar_ticks(cb, VORTICITY_TICKS, fontsize=8)
        elif label == '10-m Wind Speed (mph)':
            apply_colorbar_ticks(cb, WSPD10M_TICKS, fontsize=8)
        elif label == '10-m Wind Gust (mph)':
            apply_colorbar_ticks(cb, GUST10M_TICKS, fontsize=8)
        elif label == '2-m Relative Humidity (%)':
            apply_colorbar_ticks(cb, RH2M_TICKS, fontsize=8)
        elif label == '2-m Dewpoint (°F)':
            apply_colorbar_ticks(cb, DEW2M_TICKS, fontsize=8)
        elif label in ('Total Precipitation (inches)', 'Snow-water equivalent (inches)'):
            cb.set_ticks(QPF_TICKS)
            cb.set_ticklabels(QPF_TICK_LABELS)
            cb.ax.xaxis.set_ticks_position('bottom')
            cb.ax.tick_params(axis='x', which='both', length=0, width=0,
                              labelsize=8, pad=3)
        elif label in ('Accumulated Snowfall (inches)',
                       '10:1 Snowfall Estimate (inches)'):
            cb.set_ticks(SNOWFALL_TICKS)
            cb.set_ticklabels(SNOWFALL_TICK_LABELS)
            # Reference image displays the numeric snowfall labels ABOVE the bar.
            # Put the product/unit label below, so it does not overlap the ticks.
            cb.ax.xaxis.set_ticks_position('top')
            cb.ax.xaxis.set_label_position('bottom')
            cb.ax.tick_params(axis='x', which='both', length=0, width=0,
                              labeltop=True, labelbottom=False, labelsize=8, pad=3)
    # Watermark INSIDE the geographic plot, not in the white outer margin.
    ax.text(.995,.012,'RRFS-MPAS GSL EXPERIMENTAL · evaluation only',
            transform=ax.transAxes,ha='right',va='bottom',fontsize=8,
            color='#333333',weight='bold',zorder=260,clip_on=True,
            path_effects=[pe.withStroke(linewidth=2.4,foreground='white')])
    output_path=OUT_DIR/fname
    save_kwargs={'dpi':FIGURE_DPI,'facecolor':'white'}
    if CAMS_SAVE_TIGHT:
        save_kwargs['bbox_inches']='tight'
    fig.savefig(output_path,**save_kwargs)
    plt.close(fig)
    print('Saved:',output_path)



def title_info(cycle_id):
    init = datetime.strptime(cycle_id,'%Y%m%d%H').replace(tzinfo=timezone.utc)
    valid = init + timedelta(hours=FORECAST_HOUR)
    return (f'F{FORECAST_HOUR:03d} Valid: {valid:%a %Y-%m-%d %Hz} | '
            f'Init: {init:%a %Y-%m-%d %Hz} RRFS-MPAS')


def mesh_values(lon, lat, arr):
    return lon[::PLOT_STRIDE,::PLOT_STRIDE], lat[::PLOT_STRIDE,::PLOT_STRIDE], arr[::PLOT_STRIDE,::PLOT_STRIDE]


def shade_at_palette_limits(values, bounds):
    """Display-only clipping to preserve rectangular (no-extend) colorbars.

    For e.g. QPF >17.5 inches, use the last swatch rather than leaving a
    hole. Never modify the source field or city-value samples. Keep NaNs.
    """
    low = float(bounds[0])
    high = np.nextafter(float(bounds[-1]), -np.inf)
    return np.clip(np.asarray(values, dtype=np.float64), low, high)


def raise_contour_labels(labels, halo_width=None, zorder=CONTOUR_LABEL_ZORDER):
    """Keep contour labels over boundaries with no white halos.

    Height labels can override zorder to sit above wind barbs and all
    other contour lines. City names and city values retain top priority.
    """
    for txt in labels:
        txt.set_zorder(zorder)
        txt.set_path_effects([])
    return labels


def contours_temp(ax, lon, lat, temp_c):
    """Plot nonzero isotherms only; no 0°C line or label.

    Keep the existing dashed colder/warmer temperature contours on the
    advection and frontogenesis plots, without adding white label halos.
    """
    x, y, f = mesh_values(lon, lat, temp_c)
    for values, color, lw, ls in [
        (np.arange(-40, 0, 2), TEMP_CONTOUR_RED, 0.95, 'dashed'),
        (np.arange(2, 42, 2), TEMP_CONTOUR_RED, 0.95, 'dashed')]:
        c = ax.contour(x, y, f, levels=values, colors=color,
                       linewidths=lw, linestyles=ls,
                       transform=PLATE, zorder=TEMP_CONTOUR_ZORDER)
        if any(len(s) for segs in c.allsegs for s in segs):
            labels = ax.clabel(c, fontsize=6.7, fmt='%d', inline=True)
            raise_contour_labels(labels)


def contours_height(ax, lon, lat, fields, level):
    """Draw geopotential-height contours in dam, above wind barbs."""
    name=f'gh{level}'
    if not PLOT_HEIGHT_CONTOURS or name not in fields:
        return
    # GRIB heights are in geopotential meters; 1 dam = 10 m.
    z_dam=smooth(np.asarray(fields[name], float), 2.0) / 10.0
    x,y,z=mesh_values(lon,lat,z_dam)
    if not np.isfinite(z).any():
        return
    interval=HEIGHT_CONTOUR_INTERVALS_DAM.get(level, 3)
    zmin, zmax = np.nanmin(z), np.nanmax(z)
    levels = np.arange(np.floor(zmin/interval)*interval,
                       np.ceil(zmax/interval)*interval+interval, interval)
    if len(levels) < 2:
        return
    contours=ax.contour(x,y,z,levels=levels,
                        colors='black',linewidths=1.1,alpha=.95,
                        transform=PLATE,zorder=HEIGHT_CONTOUR_ZORDER)
    if any(len(s) for segs in contours.allsegs for s in segs):
        labels=ax.clabel(contours, fontsize=7.8, fmt='%d')
        raise_contour_labels(labels,zorder=HEIGHT_LABEL_ZORDER)


def wind_barbs(ax, lon, lat, wind):
    skip = max(16,int(min(lon.shape)/22))
    ax.barbs(lon[::skip,::skip],lat[::skip,::skip],wind['u'][::skip,::skip]*1.943844,
             wind['v'][::skip,::skip]*1.943844,transform=PLATE,length=5.2,
             color='#242424',linewidth=.65,zorder=WIND_BARB_ZORDER)


def label_pressure_centers(ax, lat, lon, pressure_hpa):
    """Only label distinct synoptic MSLP minima/maxima.

    Find 2-D extrema after ~30-km smoothing. Require at least a 2.5-hPa
    departure from the ~250-km background, then apply 275-km separation.
    Only plot lows with smoothed MSLP <= HL_LOW_MAX_PRESSURE_HPA; high
    center criteria remain unchanged.
    """
    if not SHOW_HL_CENTERS:
        return
    skip=max(4,PLOT_STRIDE)
    z=smooth(np.asarray(pressure_hpa,dtype=np.float64),8.0)[::skip,::skip]
    xs=lon[::skip,::skip]
    ys=lat[::skip,::skip]
    good=np.isfinite(z)
    if not good.any():
        return
    # Use physical distance when estimating filter scales; do not allow
    # varying geographic domains to change synoptic center criteria.
    midlat=float(np.nanmedian(ys))
    km_i=np.nanmedian(np.abs(np.diff(xs,axis=1)))*111.1*np.cos(np.deg2rad(midlat))
    km_j=np.nanmedian(np.abs(np.diff(ys,axis=0)))*111.1
    step_km=max(5.,float(np.nanmean([km_i,km_j])))
    window=max(9,int(round(220./step_km))|1)
    large_sigma=max(3.,240./step_km)
    background=gaussian_filter(np.where(good,z,np.nanmedian(z[good])),
                               sigma=large_sigma,mode='nearest')
    low=minimum_filter(np.where(good,z,np.inf),size=window,mode='nearest')
    high=maximum_filter(np.where(good,z,-np.inf),size=window,mode='nearest')
    valid=(good & (xs>EXTENT[0]+.28) & (xs<EXTENT[1]-.28)
           & (ys>EXTENT[2]+.18) & (ys<EXTENT[3]-.18))

    for mark,is_extreme,prominence in (
        ('L',np.isclose(z,low,atol=.035),background-z),
        ('H',np.isclose(z,high,atol=.035),z-background),
    ):
        candidates = valid & is_extreme & (prominence >= HL_MIN_PROMINENCE_HPA)
        if mark == 'L':
            candidates &= z <= HL_LOW_MAX_PRESSURE_HPA
        pts = np.argwhere(candidates)
        if not len(pts):
            continue
        pts=sorted(pts,key=lambda ji:float(prominence[tuple(ji)]),reverse=True)
        chosen=[]
        for j,i in pts:
            lo=float(xs[j,i]); la=float(ys[j,i]); pres=float(z[j,i])
            def apart_from_all():
                for chosen_lo,chosen_la in chosen:
                    dx=(lo-chosen_lo)*111.1*np.cos(np.deg2rad((la+chosen_la)/2))
                    dy=(la-chosen_la)*111.1
                    if np.hypot(dx,dy)<HL_MIN_SPACING_KM:
                        return False
                return True
            if not apart_from_all():
                continue
            chosen.append((lo,la))
            # Match the reference style: blue H, red L, both with a
            # single clean black outline around the letter and pressure value.
            text_color = '#1565ff' if mark == 'H' else '#ff1a1a'
            outline=[pe.withStroke(linewidth=HL_BLACK_OUTLINE_PT,
                                   foreground='black')]
            ax.text(lo,la,mark,color=text_color,
                    fontsize=HL_LETTER_FONT_SIZE,weight='bold',
                    ha='center',va='center',transform=PLATE,zorder=PRESSURE_CENTER_ZORDER,
                    path_effects=outline)
            ax.text(lo,la-(HL_LOW_PRESSURE_LAT_OFFSET if mark == 'L' else HL_PRESSURE_LAT_OFFSET),f'{pres:.0f}',
                    color=text_color,fontsize=HL_PRESSURE_FONT_SIZE,
                    weight='bold',ha='center',va='top',transform=PLATE,zorder=PRESSURE_CENTER_ZORDER,
                    path_effects=outline)
            if len(chosen)>=MAX_HL_PER_TYPE:
                break


def overlay_thickness_mslp(ax, lat, lon, fields, label_style=False):
    """Red dashed thickness every 6 dam, black MSLP every 2 hPa.

    Contour labels are less frequent to prevent collisions on the winter P-type
    shading, without reducing the number of contour lines themselves.
    """
    missing=[k for k in ('gh500','gh1000','mslp') if k not in fields]
    if missing:
        print('Thickness/MSLP overlay skipped: missing',missing)
        return False
    thickness=(np.asarray(fields['gh500'],float)-
               np.asarray(fields['gh1000'],float))/10.0
    p=np.asarray(fields['mslp'],float)/100.0
    tx,ty,th=mesh_values(lon,lat,smooth(thickness,3.0))
    _,_,ps=mesh_values(lon,lat,smooth(p,4.0))
    if not np.isfinite(th).any() or not np.isfinite(ps).any():
        return False

    tstep=THICKNESS_CONTOUR_INTERVAL_DAM
    tlevels=np.arange(480,618,tstep)
    if tlevels.size:
        ct=ax.contour(tx,ty,th,levels=tlevels,colors='#ce1818',
                      linestyles='dashed',linewidths=1.12,
                      transform=PLATE,zorder=THICKNESS_CONTOUR_ZORDER)
        label_levels=[lev for lev in tlevels
                      if lev%THICKNESS_LABEL_INTERVAL_DAM==0]
        if label_levels and any(len(x) for segs in ct.allsegs for x in segs):
            labels=ax.clabel(ct,levels=label_levels,fontsize=8.1,fmt='%d',
                             inline=True,inline_spacing=CONTOUR_LABEL_SPACING,
                             rightside_up=True)
            raise_contour_labels(labels,halo_width=2.1)

    step=MSLP_CONTOUR_INTERVAL_HPA
    m0=int(np.floor(np.nanmin(ps)/step)*step)
    m1=int(np.ceil(np.nanmax(ps)/step)*step)
    levels=np.arange(m0,m1+step,step)
    if len(levels)>1:
        cp=ax.contour(tx,ty,ps,levels=levels,colors='#0f0f0f',
                      linewidths=1.16,transform=PLATE,zorder=MSLP_CONTOUR_ZORDER)
        label_levels=[lev for lev in levels if lev%MSLP_LABEL_INTERVAL_HPA==0]
        if label_levels and any(len(x) for segs in cp.allsegs for x in segs):
            labels=ax.clabel(cp,levels=label_levels,fontsize=8.1,fmt='%d',
                             inline=True,inline_spacing=CONTOUR_LABEL_SPACING,
                             rightside_up=True)
            raise_contour_labels(labels,halo_width=2.1)
    label_pressure_centers(ax,lat,lon,p)
    if label_style:
        ax.text(.02,.055,'MSLP: 2-hPa black contours  |  Thickness: 6-dam red dashed',
                transform=ax.transAxes,fontsize=8.2,ha='left',va='bottom',
                zorder=CONTOUR_LABEL_ZORDER,bbox=dict(facecolor='white',alpha=.85,edgecolor='none'))
    return True



def plot_ptype(lat,lon,fields,cycle_id,reflectivity_field='ref1km'):
    """Plot precipitation type using either 1-km or composite reflectivity.

    The phase masks still come from the model surface P-type flags. Only the
    reflectivity source changes. This keeps the winter precip product
    consistent while letting you compare 1-km and composite structure.
    """
    names=(reflectivity_field,'rain','snow','sleet','fzra')
    if not all(k in fields for k in names):
        print(f'Skipping P-type + Reflectivity ({reflectivity_field}); missing:',
              [k for k in names if k not in fields])
        return None

    ref=np.asarray(fields[reflectivity_field],dtype=np.float64)
    if PTYPE_REFLECTIVITY_SMOOTH_SIGMA > 0:
        ref=smooth(ref,PTYPE_REFLECTIVITY_SMOOTH_SIGMA)
    ref=np.where(np.isfinite(ref),np.maximum(ref,0.),0.)

    phase_data={}
    for key in ('rain','snow','sleet','fzra'):
        flag=np.asarray(fields[key],dtype=np.float64)
        is_phase=np.isfinite(flag)&(flag>0.5)
        phase_data[key]=np.where(is_phase,ref,0.)

    ref_label = '1-km Reflectivity' if reflectivity_field == 'ref1km' else 'Composite Reflectivity'
    title = f'RRFS-MPAS | MSLP, 1000–500 mb Thickness (dam), {ref_label} / P-Type'
    fig,ax=new_map(title, title_info(cycle_id), ptype=True)

    bounds=np.arange(5,61,5,dtype=np.float64)
    palettes=[('Rain','rain'),('Snow','snow'),('Sleet','sleet'),
              ('Freezing Rain','fzra')]

    for layer,(label,key) in enumerate(palettes):
        z=phase_data[key]
        if not np.any(z >= REFLECTIVITY_THRESHOLD_DBZ):
            continue
        cmap=colors.ListedColormap(PTYPE_PALETTES[key])
        ax.contourf(lon,lat,z,levels=bounds,cmap=cmap,
                    transform=PLATE,zorder=2+layer*.1,
                    antialiased=True)

    counts={key:int(np.count_nonzero(phase_data[key]>=REFLECTIVITY_THRESHOLD_DBZ))
            for _,key in palettes}
    print(f'  [{DOMAIN_NAME}] Native-resolution precipitation grid ({reflectivity_field}): {ref.shape}')
    print(f'  [{DOMAIN_NAME}] Phase pixels >= {REFLECTIVITY_THRESHOLD_DBZ:g} dBZ:',counts)

    if OVERLAY_THICKNESS_MSLP_ON_PTYPE:
        overlay_thickness_mslp(ax,lat,lon,fields)

    divider=make_axes_locatable(ax)
    bar_strip=divider.append_axes('bottom',size='4.0%',pad=.25,
                                  axes_class=plt.Axes)
    bar_strip.set_axis_off()
    for i,(label,key) in enumerate(palettes):
        cax=bar_strip.inset_axes([.015+.25*i,.08,.22,.86])
        cmap=colors.ListedColormap(PTYPE_PALETTES[key])
        mappable=plt.cm.ScalarMappable(
            cmap=cmap,norm=colors.BoundaryNorm(bounds,cmap.N))
        mappable.set_array([])
        cb=fig.colorbar(mappable,cax=cax,orientation='horizontal',
                        ticks=[5,15,25,35,45,55],spacing='uniform',
                        drawedges=True)
        cb.ax.tick_params(axis='x',which='both',labelsize=8,length=0)
        cb.set_label(label+' (dBZ)',fontsize=9,weight='bold')
        cb.ax.xaxis.set_label_position('top')

    suffix = 'ref1km' if reflectivity_field == 'ref1km' else 'refcomp'
    filename=f'rrfs_mpas_ptype_{suffix}_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename)
    return filename




_CITY_SAMPLE_KEY = None
_CITY_SAMPLE_LOCATIONS = {}

def city_sample_locations(lat, lon):
    """Nearest native MPAS pixel to each reference city; cache for this domain."""
    global _CITY_SAMPLE_KEY, _CITY_SAMPLE_LOCATIONS
    key = (id(lat), id(lon), lat.shape, DOMAIN_NAME)
    if _CITY_SAMPLE_KEY == key:
        return _CITY_SAMPLE_LOCATIONS
    _CITY_SAMPLE_KEY = key
    _CITY_SAMPLE_LOCATIONS = {}
    if DOMAIN_NAME != 'lbf':
        return _CITY_SAMPLE_LOCATIONS
    finite = np.isfinite(lat) & np.isfinite(lon)
    if not np.any(finite):
        return _CITY_SAMPLE_LOCATIONS
    inds = np.flatnonzero(finite)
    ref_lat = 41.9
    scale_lon = 111.1 * np.cos(np.deg2rad(ref_lat))
    xy = np.column_stack((lon.flat[inds]*scale_lon, lat.flat[inds]*111.1))
    tree = cKDTree(xy)
    for name, cl, ct in LBF_CITY_LABELS:
        dist, idx = tree.query([cl*scale_lon, ct*111.1], k=1)
        if np.isfinite(dist) and dist <= CITY_MAX_SAMPLE_DISTANCE_KM:
            j, i = np.unravel_index(int(inds[int(idx)]), lat.shape)
            _CITY_SAMPLE_LOCATIONS[name] = (j, i, float(dist))
    return _CITY_SAMPLE_LOCATIONS


def city_grid_values(lat, lon, values):
    """Return numeric city samples for metadata; missing values stay null."""
    out = {}
    samples = city_sample_locations(lat, lon)
    for name, cl, ct in LBF_CITY_LABELS:
        pos = samples.get(name)
        if pos is None:
            out[name] = None
        else:
            j, i, dist = pos
            v = float(values[j, i])
            out[name] = round(v, 4) if np.isfinite(v) else None
    return out


def add_lbf_city_values(ax, lat, lon, values, layer, fmt,
                        min_display_value=None):
    """Value ABOVE city name; suppress accumulation amounts rounding to zero.

    min_display_value is in the units of the plotted city values. The city
    names themselves are rendered elsewhere and are always retained.
    """
    if not (SHOW_LBF_CITY_VALUES and SHOW_CITY_LABELS and DOMAIN_NAME=='lbf'
            and CITY_VALUE_LAYERS.get(layer, False)):
        return
    samples = city_grid_values(lat, lon, values)
    for name, cl, ct in LBF_CITY_LABELS:
        value = samples.get(name)
        if value is None or not (EXTENT[0] <= cl <= EXTENT[1]
                                  and EXTENT[2] <= ct <= EXTENT[3]):
            continue
        # Only label measurable city precipitation/snowfall accumulations.
        # QPF/SWE print hundredths; snowfall prints tenths.
        if min_display_value is not None and value < min_display_value:
            continue
        formatted = fmt(value)
        if min_display_value is not None and formatted in ('0.00"', '0.0"'):
            continue
        ax.text(cl, ct+CITY_VALUE_OFFSET_DEG, formatted, transform=PLATE,
                ha='center', va='center', fontsize=8.4, weight='bold',
                color='white', zorder=CITY_LABEL_ZORDER+5, clip_on=True,
                path_effects=[pe.withStroke(linewidth=1.9,
                                            foreground='black')])


def inches_text(v):
    return f'{v:.2f}"' if v >= .005 else '0.00"'


def snowfall_inches_text(v):
    """Snowfall city labels: nearest tenth (native and 10:1)."""
    return f'{max(float(v), 0.0):.1f}"'


def snowfall_10to1_from_weasd(weasd_kg_m2):
    """Convert model WEASD (kg/m2 ~= mm water) to inches at a fixed 10:1.

    This is a SWE-based 10:1 snow-depth estimate, NOT all-QPF times ten.
    WEASD may represent snow water on the ground rather than total snowfall,
    so pre-existing snowpack, melting and compaction can influence it.
    """
    return np.maximum(np.asarray(weasd_kg_m2, dtype=float), 0.0) / 25.4 * 10.0

def plot_qpf(lat,lon,fields,cycle_id):
    if 'qpf' not in fields:
        print('Skipping QPF: no complete 0-F accumulation in GRIB');return None
    # GRIB2 APCP in kg/m2 = mm liquid water; convert to inches.
    qpf_in=np.maximum(np.asarray(fields['qpf'],float),0.0)/25.4
    fig,ax=new_map(f'Accumulated QPF (0–F{FORECAST_HOUR:03d})', title_info(cycle_id))
    x,y,z=mesh_values(lon,lat,qpf_in)
    # Exact custom 0–17.5-inch QPF palette: square-ended bar, no extensions.
    cf=ax.contourf(x,y,shade_at_palette_limits(z,QPF_BOUNDS),
                   levels=QPF_BOUNDS,cmap=QPF_CMAP,norm=QPF_NORM,
                   transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,qpf_in,'qpf',inches_text, min_display_value=0.005)
    filename=f'rrfs_mpas_qpf_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'Total Precipitation (inches)',cf)
    return filename


def plot_accumulated_snowfall(lat,lon,fields,cycle_id):
    """Plot native ASNOW accumulation; never calculate snow from instantaneous CSNOW."""
    if 'asnow' not in fields:
        print('Skipping native accumulated snowfall: ASNOW 0-F accumulation not found')
        return None
    snow_inches=np.maximum(np.asarray(fields['asnow'],float),0.0)*39.3700787402
    fig,ax=new_map(f'Accumulated Snowfall (0–F{FORECAST_HOUR:03d})',
                   title_info(cycle_id))
    x,y,z=mesh_values(lon,lat,snow_inches)
    cf=ax.contourf(x,y,shade_at_palette_limits(z,SNOWFALL_BOUNDS),
                   levels=SNOWFALL_BOUNDS,cmap=SNOWFALL_CMAP,
                   norm=SNOWFALL_NORM,transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,snow_inches,'asnow',snowfall_inches_text, min_display_value=0.05)
    filename=f'rrfs_mpas_asnow_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'Accumulated Snowfall (inches)',cf)
    return filename


def plot_snowfall_10to1(lat,lon,fields,cycle_id):
    """Plot optional 10:1 snow estimate based on cumulative WEASD, not QPF.

    A 10:1 conversion of snow-water equivalent does NOT account for the
    evolution of the actual snow ratio. Depending on the model's WEASD
    definition, it may describe snowpack equivalent, not total newly fallen
    snowfall. Keep this product separate from native ASNOW.
    """
    if 'weasd' not in fields:
        print('Skipping 10:1 snowfall estimate: cumulative WEASD not found')
        return None
    snow_inches = snowfall_10to1_from_weasd(fields['weasd'])
    fig,ax = new_map(f'10:1 Snowfall Estimate (0–F{FORECAST_HOUR:03d})',
                     title_info(cycle_id))
    x,y,z = mesh_values(lon,lat,snow_inches)
    cf = ax.contourf(x,y,shade_at_palette_limits(z,SNOWFALL_BOUNDS),
                     levels=SNOWFALL_BOUNDS,cmap=SNOWFALL_CMAP,
                     norm=SNOWFALL_NORM,transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,snow_inches,'snow10to1',snowfall_inches_text, min_display_value=0.05)
    filename = f'rrfs_mpas_snow10to1_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'10:1 Snowfall Estimate (inches)',cf)
    return filename


def plot_snow_water_equivalent(lat,lon,fields,cycle_id):
    """Plot native WEASD water equivalent; NOT total fallen snow or snow depth."""
    if 'weasd' not in fields:
        print('Skipping snow-water equivalent: WEASD 0-F accumulation not found')
        return None
    swe_inches=np.maximum(np.asarray(fields['weasd'],float),0.0)/25.4
    fig,ax=new_map(f'Accumulated Snow Water Equivalent (0–F{FORECAST_HOUR:03d})',
                   title_info(cycle_id))
    x,y,z=mesh_values(lon,lat,swe_inches)
    cf=ax.contourf(x,y,shade_at_palette_limits(z,QPF_BOUNDS),
                   levels=QPF_BOUNDS,cmap=QPF_CMAP,norm=QPF_NORM,
                   transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,swe_inches,'weasd',inches_text, min_display_value=0.005)
    filename=f'rrfs_mpas_weasd_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'Snow-water equivalent (inches)',cf)
    return filename


def plot_adv(lat,lon,diag,level,cycle_id,fields):
    fig,ax=new_map(f'{level} mb Temperature Advection',title_info(cycle_id))
    adv=smooth(diag['adv']*TADV_HOURS,DISPLAY_SMOOTH_SIGMA_PIX)
    x,y,z=mesh_values(lon,lat,adv)
    cf=ax.contourf(x,y,z,
                   levels=TADV_COLOR_BOUNDS,cmap=TADV_CMAP,norm=TADV_NORM,
                   extend='both',transform=PLATE,zorder=2)
    contours_temp(ax,lon,lat,diag['temp_c'])
    contours_height(ax,lon,lat,fields,level)
    wind_barbs(ax,lon,lat,diag)
    filename=f'rrfs_mpas_tadv_{level}mb_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,f'Temperature advection (°C/{TADV_HOURS} h)',cf)
    return filename


def plot_fgen(lat,lon,diag,level,cycle_id,fields):
    """One-panel style: temperature-advection fill with GREEN FGen contours.

    FGen uses the correct pressure level; the reference script hardcoded 850
    hPa in potential-temperature calculation, which we do not replicate.
    """
    fig,ax=new_map(f'{level} mb Temp Advection + Frontogenesis',title_info(cycle_id))
    adv=smooth(diag['adv']*TADV_HOURS,DISPLAY_SMOOTH_SIGMA_PIX)
    fgen=smooth(diag['fgen'],DISPLAY_SMOOTH_SIGMA_PIX)
    x,y,z=mesh_values(lon,lat,adv)
    cf=ax.contourf(x,y,z,
                   levels=TADV_COLOR_BOUNDS,cmap=TADV_CMAP,norm=TADV_NORM,
                   extend='both',transform=PLATE,zorder=2)
    # Frontogenesis = positive signed intensification; plot only positive.
    _,_,fg=mesh_values(lon,lat,fgen)
    c=ax.contour(x,y,fg,levels=FGEN_CONTOURS,
                 colors='#07631e',linewidths=1.7,transform=PLATE,zorder=FGEN_CONTOUR_ZORDER)
    if any(len(s) for segs in c.allsegs for s in segs):
        labels=ax.clabel(c,fontsize=8,fmt='%g',inline=True)
        raise_contour_labels(labels,halo_width=2.0)
    contours_temp(ax,lon,lat,diag['temp_c'])
    contours_height(ax,lon,lat,fields,level)
    wind_barbs(ax,lon,lat,diag)
    filename=f'rrfs_mpas_fgen_{level}mb_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,f'Advection °C/{TADV_HOURS} h (shading); frontogenesis K/(100 km·3 h) (green)',cf)
    return filename


def plot_thickness(lat,lon,fields,cycle_id):
    """Optional standalone MSLP/thickness map, using same contour styling as P-type."""
    if not all(k in fields for k in ('gh500', 'gh1000', 'mslp')):
        print('Skipping standalone thickness + MSLP; required fields unavailable')
        return None
    fig,ax=new_map('1000–500 mb Thickness (dam) + MSLP (hPa)',title_info(cycle_id))
    overlay_thickness_mslp(ax,lat,lon,fields)
    filename=f'rrfs_mpas_thickness_mslp_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename)
    return filename


def four_panel_preview(output_names,cycle_id):
    """Create an optional 2x2 composite from already rendered single panels.

    This is a preview, NOT an implementation of the unavailable 4-panel
    reference notebook. Tiles preserve common time, domain, and color scales.
    """
    if not MAKE_FOUR_PANEL_PREVIEW:
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print('Skipping four-panel preview: pip install pillow')
        return None
    wanted=[f'rrfs_mpas_ptype_ref1km_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png',
            f'rrfs_mpas_qpf_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png',
            f'rrfs_mpas_t2m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png',
            f'rrfs_mpas_fgen_850mb_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png']
    if any(fn not in output_names for fn in wanted):
        print('Four-panel preview skipped: one or more required products disabled/missing.')
        return None
    cell=(1250,760); gap=16
    grid=Image.new('RGB',(2*cell[0]+3*gap,2*cell[1]+3*gap),'white')
    for i,fn in enumerate(wanted):
        with Image.open(OUT_DIR/fn) as image:
            tile=ImageOps.contain(image.convert('RGB'),cell,method=Image.Resampling.LANCZOS)
        left=gap+(i%2)*(cell[0]+gap)+(cell[0]-tile.width)//2
        top=gap+(i//2)*(cell[1]+gap)+(cell[1]-tile.height)//2
        grid.paste(tile,(left,top))
    name=f'rrfs_mpas_winter_4panel_preview_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    grid.save(OUT_DIR/name,optimize=True)
    print('Saved:',OUT_DIR/name)
    return name


# ──────────────────────────────────────────────────────────────────
# MAIN / EXPORT
# ──────────────────────────────────────────────────────────────────


def apply_colorbar_ticks(cb, ticks, fontsize=8):
    cb.set_ticks(ticks)
    cb.ax.tick_params(labelsize=fontsize, length=0)


def plot_level_heights(ax, lon, lat, fields, lev):
    """Black height lines and labels in dam, drawn above wind barbs."""
    hkey = f'gh{lev}'
    if not PLOT_HEIGHT_CONTOURS or hkey not in fields:
        return False
    interval = HEIGHT_CONTOUR_INTERVALS_DAM.get(lev, 3)
    h_dam = np.asarray(fields[hkey], float) / 10.0
    x, y, z = mesh_values(lon, lat, smooth(h_dam, 2.0))
    if not np.isfinite(z).any():
        return False
    start = int(np.floor(np.nanmin(z) / interval) * interval)
    end = int(np.ceil(np.nanmax(z) / interval) * interval)
    levels = np.arange(start, end + interval, interval)
    cs = ax.contour(x, y, z, levels=levels, colors='black', linewidths=1.1,
                    transform=PLATE, zorder=HEIGHT_CONTOUR_ZORDER)
    if len(levels) > 0 and any(len(seg) for segs in cs.allsegs for seg in segs):
        labels = ax.clabel(cs, fontsize=8, fmt='%d', inline=True,
                           inline_spacing=CONTOUR_LABEL_SPACING)
        raise_contour_labels(labels, zorder=HEIGHT_LABEL_ZORDER)
    return True


def plot_2m_temperature(lat, lon, fields, cycle_id):
    if 't2m' not in fields:
        print('Skipping 2-m temperature; missing t2m')
        return None
    temp_f = (np.asarray(fields['t2m'], float) - 273.15) * 9.0 / 5.0 + 32.0
    x, y, z = mesh_values(lon, lat, smooth(temp_f, 1.2))
    title = 'RRFS-MPAS | 2-m Temperature'
    fig, ax = new_map(title, title_info(cycle_id))
    cf = ax.contourf(x, y, shade_at_palette_limits(z,SFC_TEMP_BOUNDS),
                     levels=SFC_TEMP_BOUNDS, cmap=SFC_TEMP_CMAP,
                     norm=SFC_TEMP_NORM,  transform=PLATE, zorder=2)
    add_lbf_city_values(ax,lat,lon,temp_f,'t2m',lambda v:f'{v:.0f}°F')
    filename = f'rrfs_mpas_t2m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig, ax, filename, label='2-m Temperature (°F)', cf=cf)
    return filename



def plot_10m_gust(lat, lon, fields, cycle_id):
    """Native instantaneous GUST at surface in mph; NOT hourly max 10-m wind."""
    if 'gust10m' not in fields:
        print('Skipping surface gusts: GRIB 0/2/22 GUST is missing')
        return None
    gust_mph = np.maximum(np.asarray(fields['gust10m'],float), 0.) * 2.2369362921
    fig,ax = new_map('RRFS-MPAS | 10-m Wind Gusts', title_info(cycle_id))
    x,y,z = mesh_values(lon,lat,smooth(gust_mph,1.0))
    cf = ax.contourf(x,y,shade_at_palette_limits(z,GUST10M_BOUNDS),
                     levels=GUST10M_BOUNDS,cmap=GUST10M_CMAP,
                     norm=GUST10M_NORM,transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,gust_mph,'gust10m',lambda v:f'{v:.0f} mph')
    filename = f'rrfs_mpas_gust10m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'10-m Wind Gust (mph)',cf)
    return filename



def plot_10m_sustained_wind_mslp(lat, lon, fields, cycle_id):
    """Native sustained 10-m wind speed, MSLP and earth-relative wind barbs.

    Use the 10-m U/V vector magnitude (not GUST) converted to mph. Pressure
    contours are every 2 mb and preserve the existing 1009-mb L criterion.
    No numerical mph city values are drawn, to avoid contour/barb clutter.
    """
    required = ('u10m', 'v10m', 'mslp')
    missing = [key for key in required if key not in fields]
    if missing:
        print('Skipping 10-m sustained winds + MSLP; missing', missing)
        return None
    u = np.asarray(fields['u10m'], float)
    v = np.asarray(fields['v10m'], float)
    speed_mph = np.hypot(u, v) * 2.2369362921
    p_hpa = np.asarray(fields['mslp'], float) / 100.0
    if not (np.isfinite(speed_mph).any() and np.isfinite(p_hpa).any()):
        print('Skipping 10-m sustained winds + MSLP: no finite data')
        return None

    fig, ax = new_map('RRFS-MPAS | 10-m Wind Speed + MSLP', title_info(cycle_id))
    x, y, z = mesh_values(lon, lat, smooth(speed_mph, 1.0))
    cf = ax.contourf(x, y, shade_at_palette_limits(z, WSPD10M_BOUNDS),
                     levels=WSPD10M_BOUNDS, cmap=WSPD10M_CMAP,
                     norm=WSPD10M_NORM, transform=PLATE, zorder=2)

    # Black MSLP contours every 2 hPa; label every 4 hPa for legibility.
    # Share the same contour/label spacing settings as the P-type MSLP overlays.
    _, _, ps = mesh_values(lon, lat, smooth(p_hpa, 4.0))
    if np.isfinite(ps).any():
        step = MSLP_CONTOUR_INTERVAL_HPA
        pmin = int(np.floor(np.nanmin(ps) / step) * step)
        pmax = int(np.ceil(np.nanmax(ps) / step) * step)
        levels = np.arange(pmin, pmax + step, step)
        if len(levels) > 1:
            cp = ax.contour(x, y, ps, levels=levels, colors='#141414',
                            linewidths=1.05, transform=PLATE, zorder=MSLP_CONTOUR_ZORDER)
            label_levels = [lev for lev in levels if lev % MSLP_LABEL_INTERVAL_HPA == 0]
            if label_levels and any(len(segment) for group in cp.allsegs for segment in group):
                labels = ax.clabel(cp, levels=label_levels, fontsize=8, fmt='%d',
                                   inline=True, inline_spacing=CONTOUR_LABEL_SPACING)
                raise_contour_labels(labels, zorder=HEIGHT_LABEL_ZORDER)
    wind_barbs(ax, lon, lat, {'u': u, 'v': v})  # barbs in knots; gust map has none
    label_pressure_centers(ax, lat, lon, p_hpa)

    filename = f'rrfs_mpas_wspd10m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig, ax, filename, label='10-m Wind Speed (mph)', cf=cf)
    return filename

def plot_2m_rh(lat, lon, fields, cycle_id):
    """Native instantaneous 2-m RH; no humidity reconstruction needed."""
    if 'rh2m' not in fields:
        print('Skipping 2-m relative humidity: GRIB 0/1/1 field missing')
        return None
    rh = np.clip(np.asarray(fields['rh2m'],float),0.,100.)
    fig,ax = new_map('RRFS-MPAS | 2-m Relative Humidity',title_info(cycle_id))
    x,y,z = mesh_values(lon,lat,smooth(rh,1.0))
    cf = ax.contourf(x,y,shade_at_palette_limits(z,RH2M_BOUNDS),
                     levels=RH2M_BOUNDS,cmap=RH2M_CMAP,norm=RH2M_NORM,
                     transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,rh,'rh2m',lambda v:f'{v:.0f}%')
    filename = f'rrfs_mpas_rh2m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'2-m Relative Humidity (%)',cf)
    return filename

def plot_2m_dewpoint(lat, lon, fields, cycle_id):
    """Native 2-m dewpoint Kelvin to degrees Fahrenheit; SPCOA T2m palette."""
    if 'd2m' not in fields:
        print('Skipping 2-m dewpoint: GRIB 0/0/6 field missing')
        return None
    dew_f = (np.asarray(fields['d2m'],float) - 273.15) * 1.8 + 32.
    fig,ax = new_map('RRFS-MPAS | 2-m Dewpoint',title_info(cycle_id))
    x,y,z = mesh_values(lon,lat,smooth(dew_f,1.0))
    cf = ax.contourf(x,y,shade_at_palette_limits(z,DEW2M_BOUNDS),
                     levels=DEW2M_BOUNDS,cmap=DEW2M_CMAP,norm=DEW2M_NORM,
                     transform=PLATE,zorder=2)
    add_lbf_city_values(ax,lat,lon,dew_f,'d2m',lambda v:f'{v:.0f}°F')
    filename = f'rrfs_mpas_d2m_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig,ax,filename,'2-m Dewpoint (°F)',cf)
    return filename

def plot_level_temperature(lat, lon, fields, lev, cycle_id):
    tkey, ukey, vkey = f't{lev}', f'u{lev}', f'v{lev}'
    missing = [k for k in (tkey, ukey, vkey) if k not in fields]
    if missing:
        print(f'Skipping {lev}-mb temperature; missing', missing)
        return None
    temp_c = np.asarray(fields[tkey], float) - 273.15
    x, y, z = mesh_values(lon, lat, smooth(temp_c, 1.0))
    title = f'RRFS-MPAS | {lev} mb Temperature, Heights, and Winds'
    fig, ax = new_map(title, title_info(cycle_id))
    cf = ax.contourf(x, y, shade_at_palette_limits(z,PRESSURE_TEMP_BOUNDS),
                     levels=PRESSURE_TEMP_BOUNDS,
                     cmap=PRESSURE_TEMP_CMAP, norm=PRESSURE_TEMP_NORM,
                      transform=PLATE, zorder=2)
    # The 925/850/700-mb temperature maps show shaded temperature only;
    # no red/other temperature isotherms. Retain black heights and wind barbs.
    plot_level_heights(ax, lon, lat, fields, lev)
    wind_barbs(ax, lon, lat, {'u': np.asarray(fields[ukey], float),
                              'v': np.asarray(fields[vkey], float)})
    add_lbf_city_values(ax,lat,lon,temp_c,'pressure_temp',lambda v:f'{v:.0f}°C')
    filename = f'rrfs_mpas_t{lev}_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig, ax, filename, label=f'{lev}-mb Temperature (°C)', cf=cf)
    return filename


def plot_level_windspeed(lat, lon, fields, lev, cycle_id):
    ukey, vkey = f'u{lev}', f'v{lev}'
    missing = [k for k in (ukey, vkey) if k not in fields]
    if missing:
        print(f'Skipping {lev}-mb wind speed; missing', missing)
        return None
    wspd = np.hypot(np.asarray(fields[ukey], float), np.asarray(fields[vkey], float)) * 1.943844
    x, y, z = mesh_values(lon, lat, smooth(wspd, 1.0))
    title = f'RRFS-MPAS | {lev} mb Wind Speed, Heights, and Winds'
    fig, ax = new_map(title, title_info(cycle_id))
    cf = ax.contourf(x, y, shade_at_palette_limits(z,WSPD_BOUNDS),
                     levels=WSPD_BOUNDS,
                     cmap=WSPD_CMAP, norm=WSPD_NORM,
                      transform=PLATE, zorder=2)
    plot_level_heights(ax, lon, lat, fields, lev)
    wind_barbs(ax, lon, lat, {'u': np.asarray(fields[ukey], float),
                              'v': np.asarray(fields[vkey], float)})
    add_lbf_city_values(ax,lat,lon,wspd,'pressure_wind',lambda v:f'{v:.0f} kt')
    filename = f'rrfs_mpas_wspd{lev}_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig, ax, filename, label=f'{lev}-mb Wind Speed (kt)', cf=cf)
    return filename



def plot_level_relative_vorticity(lat, lon, fields, lev, cycle_id, grad):
    """Plot 850/700/500-mb relative vorticity, black heights and wind barbs.

    Shading: +/- relative vorticity in 10^-5 s^-1 using the exact SPCOA
    discrete -40..+50 palette. Above/below limits are colored, and the
    horizontal colorbar has triangles at BOTH ends (extend='both').
    """
    if lev not in VORTICITY_LEVELS_HPA:
        raise ValueError(f'Unsupported vorticity level {lev}')
    vort = calculate_relative_vorticity(lat, lon, fields, lev, grad)
    if vort is None or not np.isfinite(vort).any():
        print(f'Skipping {lev}-mb relative vorticity: no valid calculated values')
        return None
    title = f'RRFS-MPAS | {lev} mb Relative Vorticity, Heights, and Winds'
    fig, ax = new_map(title, title_info(cycle_id))
    x, y, z = mesh_values(lon, lat, vort)
    cf = ax.contourf(x, y, z, levels=VORTICITY_BOUNDS,
                     cmap=VORTICITY_CMAP, norm=VORTICITY_NORM,
                     extend='both', transform=PLATE, zorder=2)
    # Both pressure-height labels and 850/700/500-mb wind barbs share the
    # exact level of the shaded vorticity (not 10-m or adjacent-level winds).
    plot_level_heights(ax, lon, lat, fields, lev)
    wind_barbs(ax, lon, lat, {
        'u': np.asarray(fields[f'u{lev}'], float),
        'v': np.asarray(fields[f'v{lev}'], float)
    })
    # No numeric city values for pressure-level vorticity maps.
    filename = f'rrfs_mpas_rvort{lev}_{DOMAIN_NAME}_{cycle_id}_f{FORECAST_HOUR:03d}.png'
    finish(fig, ax, filename,
           label=f'{lev}-mb Relative Vorticity (10⁻⁵ s⁻¹)',
           cf=cf, colorbar_extend='both')
    return filename


def stage_site_run(payload):
    """Prepare a LOCAL site directory; no S3 credentials or uploads required.

    Compatible future path convention:
      runs/cams/rrfs_mpas/winter/{cycle}/{domain}/{product}_f006.png
      runs/cams/rrfs_mpas/winter/{cycle}/manifest_f006.json
      runs/cams/rrfs_mpas/winter/runs.json
    """
    if not STAGE_SITE_FILES:
        return None
    site = SITE_STAGING_ROOT / SITE_RELATIVE_PREFIX
    cycle = payload['cycle']
    fcode = f"f{payload['forecast_hour']:03d}"
    product_index = {}
    for domain, info in payload['domains'].items():
        product_index[domain] = {}
        for filename in info['outputs']:
            source = OUT_DIR / filename
            if not source.is_file():
                continue
            # Parse EXACT filename from plot functions (never infer time from forecast data).
            match = re.fullmatch(
                rf'rrfs_mpas_(.+)_{re.escape(domain)}_{re.escape(cycle)}_{fcode}\.png',
                filename)
            product_id = match.group(1) if match else filename[:-4]
            destination = site / cycle / domain / f'{product_id}_{fcode}.png'
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            product_index[domain][product_id] = (
                f'{cycle}/{domain}/{product_id}_{fcode}.png')
    if not any(product_index.values()):
        print('Site stage: no PNG outputs to index')
        return None
    m = {
        'schema_version': 1,
        'model': payload['model'],
        'cycle': cycle,
        'forecast_hour': payload['forecast_hour'],
        'valid_time_utc': payload['valid_time_utc'],
        'experimental': True,
        'disclaimer': payload['disclaimer'],
        'domains': product_index,
        'available_fields': payload['available_fields'],
    }
    manifest_name = f'manifest_{fcode}.json'
    (site / cycle).mkdir(parents=True, exist_ok=True)
    (site / cycle / manifest_name).write_text(json.dumps(m, indent=2))
    # Merge with earlier test times, never mark a product available without a PNG.
    index_path = site / 'runs.json'
    try:
        old = json.loads(index_path.read_text()) if index_path.exists() else {}
    except (ValueError, OSError):
        old = {}
    entries = [x for x in old.get('runs', []) if isinstance(x, dict)]
    entries = [x for x in entries if not (x.get('cycle')==cycle and
                   x.get('forecast_hour')==payload['forecast_hour'])]
    entries.append({
        'cycle': cycle,
        'forecast_hour': payload['forecast_hour'],
        'valid_time_utc':payload['valid_time_utc'],
        'manifest': f'{cycle}/{manifest_name}',
        'domains': list(product_index),
    })
    entries.sort(key=lambda x: (x['cycle'], x['forecast_hour']), reverse=True)
    index_path.write_text(json.dumps({'schema_version':1,'model':'rrfs_mpas',
                                     'experimental':True,'runs':entries},indent=2))
    print('Site-ready local files:', site)
    print('Site index:', index_path)
    return str(index_path)

def run():
    global DOMAIN_NAME,EXTENT
    prepare_assets()
    if not RUN_DOMAINS or any(d not in DOMAINS for d in RUN_DOMAINS):
        raise ValueError(f'MPAS_DOMAINS must choose from {tuple(DOMAINS)}')
    if not (0 <= FORECAST_HOUR <= 120):
        raise ValueError('MPAS_FHR must be an integer from 0 to 120')
    print(f'CI config: MPAS_CYCLE={CYCLE_ID}, MPAS_FHR={FORECAST_HOUR}, MPAS_DOMAINS={RUN_DOMAINS}')
    path,cycle_id=download_grib()   # ONE FTP retrieval for all selected domains
    lat_all,lon_all,fields_all,uv_flags,wind_grid_meta=read_selected_fields(path,FORECAST_HOUR)
    results={}
    for domain in RUN_DOMAINS:
        if domain not in DOMAINS:
            raise KeyError(f'Unrecognized domain {domain!r}; choose from {list(DOMAINS)}')
        DOMAIN_NAME=domain
        EXTENT=DOMAINS[domain]
        print(f'\\n========== DOMAIN {domain.upper()} | {EXTENT} ==========')
        lat,lon,fields=crop_to_domain(lat_all,lon_all,fields_all)
        fields=rotate_winds_to_earth(lat,lon,fields,uv_flags,wind_grid_meta)
        outputs=[]
        def add(fname):
            if fname: outputs.append(fname)
        if DO_PTYPE:
            if DO_PTYPE_REF1KM:
                add(plot_ptype(lat,lon,fields,cycle_id,reflectivity_field='ref1km'))
            if DO_PTYPE_COMPOSITE:
                add(plot_ptype(lat,lon,fields,cycle_id,reflectivity_field='refcomp'))
        if DO_QPF:
            add(plot_qpf(lat,lon,fields,cycle_id))
        if DO_ACCUM_SNOWFALL:
            add(plot_accumulated_snowfall(lat,lon,fields,cycle_id))
        if DO_SNOWFALL_10TO1:
            add(plot_snowfall_10to1(lat,lon,fields,cycle_id))
        if DO_SNOW_WATER_EQUIV:
            add(plot_snow_water_equivalent(lat,lon,fields,cycle_id))
        if DO_2M_TEMP:
            add(plot_2m_temperature(lat,lon,fields,cycle_id))
        if DO_10M_GUST:
            add(plot_10m_gust(lat,lon,fields,cycle_id))
        if DO_10M_WSPD_MSLP:
            add(plot_10m_sustained_wind_mslp(lat,lon,fields,cycle_id))
        if DO_2M_RH:
            add(plot_2m_rh(lat,lon,fields,cycle_id))
        if DO_2M_DEWPOINT:
            add(plot_2m_dewpoint(lat,lon,fields,cycle_id))
        if DO_LEVEL_TEMP:
            for lev in LEVELS_HPA:
                add(plot_level_temperature(lat,lon,fields,lev,cycle_id))
        if DO_LEVEL_WSPD:
            for lev in LEVELS_HPA:
                add(plot_level_windspeed(lat,lon,fields,lev,cycle_id))
        if DO_LEVEL_VORTICITY or DO_ADV or DO_FGEN:
            grad=GeographicGradient(lat,lon)
        if DO_LEVEL_VORTICITY:
            for lev in VORTICITY_LEVELS_HPA:
                add(plot_level_relative_vorticity(lat,lon,fields,lev,cycle_id,grad))
        if DO_ADV or DO_FGEN:
            for lev in LEVELS_HPA:
                diag=diagnostics_for_level(lev,fields,grad)
                if diag is None:continue
                if DO_ADV:add(plot_adv(lat,lon,diag,lev,cycle_id,fields))
                if DO_FGEN:add(plot_fgen(lat,lon,diag,lev,cycle_id,fields))
        if DO_THICKNESS_MSLP:
            add(plot_thickness(lat,lon,fields,cycle_id))
        add(four_panel_preview(outputs,cycle_id))
        results[domain]={'extent':EXTENT,'outputs':outputs}
        print(f'Domain {domain}: {len(outputs)} graphic(s) written')

    payload={'model':'rrfs_mpas','cycle':cycle_id,'forecast_hour':FORECAST_HOUR,
             'valid_time_utc':(datetime.strptime(cycle_id,'%Y%m%d%H')+
                              timedelta(hours=FORECAST_HOUR)).isoformat()+'Z',
             'domains':results,'available_fields':list(fields_all),
             'ptype_overlays':{'thickness_1000_500mb':OVERLAY_THICKNESS_MSLP_ON_PTYPE,
                               'mslp':OVERLAY_THICKNESS_MSLP_ON_PTYPE,
                               'pressure_centers':SHOW_HL_CENTERS,
                               'center_prominence_hpa':HL_MIN_PROMINENCE_HPA,
                               'low_center_max_mslp_hpa':HL_LOW_MAX_PRESSURE_HPA,
                               'mslp_contour_interval_hpa':MSLP_CONTOUR_INTERVAL_HPA,
                               'thickness_contour_interval_dam':THICKNESS_CONTOUR_INTERVAL_DAM},
             'disclaimer':'Experimental forecast for evaluation; physical QA before publication.'}
    meta_path=OUT_DIR/f'rrfs_mpas_winter_{cycle_id}_f{FORECAST_HOUR:03d}_manifest.json'
    meta_path.write_text(json.dumps(payload,indent=2))
    print('\\nComplete:',sum(len(x['outputs']) for x in results.values()),'maps across',
          len(results),'domain(s).')
    print('Manifest:',meta_path)
    payload['site_index_path'] = stage_site_run(payload)
    return payload


if __name__ == '__main__':
    run()
