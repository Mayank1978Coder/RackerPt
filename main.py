import os
import time
import requests
from PIL import Image, ImageDraw, ImageFont

# --- Configuration ---
IMAGE_PATH = "pointstable.png"
OUTPUT_PATH = "output_stream.png"
LOGO_DIR = "logos"       # Folder containing team logos as tag.png
LOGO_SIZE = 30           # Default logo width & height (square)

# Google Sheets API config
API_KEY = "AIzaSyCQsWb6Q1iAu4A9wcrA_oZKrJlzkGEs1NY"
SHEET_ID = "1XXQHJDiAoM0JPQGjVxrctFJNfCOrH0a-PC1wefCc9Q8"
RANGE = "RANKING!A5:L16"  # Rows 5–16 contain the 12 teams

# Column indices in the sheet row (0-based)
# A=0, B=1, C=2, D=3, E=4, F=5, G=6, H=7, I=8, J=9, K=10, L=11
COL_TEAM_NAME = 3    # Column D
COL_LOGO_TAG = 4     # Column E  (used as logos/tag.png)
COL_BOOYAH = 6       # Column G
COL_ELIMS = 7        # Column H
COL_PLACE_PTS = 8    # Column I
COL_TOTAL = 10       # Column K
COL_BLOCK_TAG = 11   # Column L  (block image tag for first 6 teams)

# --- Font Setup (Chakra Petch) ---
# Download Chakra Petch from Google Fonts and place the .ttf files in this directory
FONT_PATH = "ChakraPetch-Medium2.ttf"
FONT_BOLD_PATH = "ChakraPetch-Bold.ttf"
FONT_SIZE = 18

try:
    font = ImageFont.truetype(FONT_PATH, size=FONT_SIZE)
    font_bold = ImageFont.truetype(FONT_BOLD_PATH, size=FONT_SIZE)
except OSError:
    print(f"[WARNING] Could not load Chakra Petch fonts. Falling back to default.")
    font = ImageFont.load_default()
    font_bold = font

# Font cache for per-team sizes (populated on demand)
_font_cache = {}
_font_bold_cache = {}

def get_font(size):
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(FONT_PATH, size=size)
        except OSError:
            _font_cache[size] = font
    return _font_cache[size]

def get_font_bold(size):
    if size not in _font_bold_cache:
        try:
            _font_bold_cache[size] = ImageFont.truetype(FONT_BOLD_PATH, size=size)
        except OSError:
            _font_bold_cache[size] = font_bold
    return _font_bold_cache[size]

# --- Team Position Coordinates ---
# Template image: 1920×1080 (@1x)
# All 12 teams use identical sizing (single column layout).
#
# KEY FIELDS:
#   logo_box   = (x1, y1, x2, y2) bounding box — logo is centred inside it
#   logo_size  = pixel size to resize logo (width & height)
#   name_box   = (x1, y1, x2, y2) bounding box — name LEFT-aligned inside it
#   name_align = "left" to left-align name inside name_box
#   name_fs    = font size for team name
#   num_fs     = font size for stat numbers
#   *_box      = (x1, y1, x2, y2) bounding box for booyah/elims/place/total — text centred inside
#
# Row height = 39px, gap between rows = 24px, step = 63px

# Shared sizing for all 12 teams (change these to adjust all rows at once)
ROW_LOGO_SIZE = 35    # logo width & height in pixels
ROW_NAME_FS = 28      # font size for team name
ROW_NUM_FS = 28       # font size for stat numbers (booyah, elims, place, total)

# --- X-coordinates for each column (same for every row) ---
# logo:   1029 → 1068
# name:   1069 → 1476
# booyah: 1496 → 1538
# elims:  1565 → 1606
# place:  1630 → 1672
# total:  1698 → 1785

# --- Y-coordinates for each team row (edit these to adjust individual rows) ---
ROW_Y = [
    307,   # Team 1
    372,   # Team 2
    430,   # Team 3
    494,   # Team 4
    555,   # Team 5
    616,   # Team 6
    676,   # Team 7
    738,   # Team 8
    798,   # Team 9
    860,   # Team 10
    921,   # Team 11
    982,   # Team 12
]

_ROW_H = 39  # row height (346 - 307)

def _make_row(y1):
    """Generate one team-row dict given the top y-coordinate."""
    y2 = y1 + _ROW_H
    return {
        "logo": (1029, y1), "logo_size": ROW_LOGO_SIZE,
        "logo_box": (1029, y1, 1068, y2),
        "name_fs": ROW_NAME_FS, "num_fs": ROW_NUM_FS,
        "name": (1090, y1), "name_box": (1090, y1, 1476, y2), "name_align": "left",
        "booyah": (1496, y1), "booyah_box": (1496, y1, 1538, y2),
        "elims": (1565, y1),  "elims_box": (1565, y1, 1606, y2),
        "place": (1630, y1),  "place_box": (1630, y1, 1672, y2),
        "total": (1698, y1),  "total_box": (1698, y1, 1785, y2),
    }

# All 12 teams — same x-coords, y from ROW_Y list above
team_positions = [_make_row(y) for y in ROW_Y]

# --- Block image positions (6 blocks, 3 rows × 2 columns) ---
# Each entry is (x1, y1, x2, y2) bounding box. Image is centred & fitted inside.
# Only the first 6 teams use these (column L tag).
BLOCK_POSITIONS = [
    (106, 337, 440, 512),   # Block 1 (top-left)
    (484, 342, 816, 510),   # Block 2 (top-right)
    (106, 535, 440, 722),   # Block 3 (mid-left)
    (484, 549, 816, 724),   # Block 4 (mid-right)
    (106, 764, 441, 939),   # Block 5 (bottom-left)
    (484, 761, 816, 937),   # Block 6 (bottom-right)
]


def fetch_sheet_data():
    """Fetch team data from Google Sheets via GAPI REST API."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{RANGE}?key={API_KEY}"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    rows = data.get("values", [])

    teams = []
    for row in rows:
        # Pad row to at least 12 columns in case of trailing empty cells
        padded = row + [""] * (12 - len(row))
        teams.append({
            "name":      padded[COL_TEAM_NAME],
            "logo_tag":  padded[COL_LOGO_TAG],    # e.g. "UGxGRP" -> logos/UGxGRP.png
            "booyah":    padded[COL_BOOYAH],
            "elims":     padded[COL_ELIMS],
            "place":     padded[COL_PLACE_PTS],
            "total":     padded[COL_TOTAL],
            "block_tag": padded[COL_BLOCK_TAG],    # block image tag (first 6 teams)
        })
    return teams


def draw_team(img, draw, pos, rank, team, text_color="white"):
    """Draw one team's logo and data at the specified coordinate positions."""
    # --- Paste the team logo ---
    tag = team.get("logo_tag", "").strip()
    if tag:
        logo_path = os.path.join(LOGO_DIR, f"{tag}.png")
        if os.path.exists(logo_path):
            logo = Image.open(logo_path).convert("RGBA")
            if "logo_box" in pos:
                # Resize logo using logo_size, then centre inside the bounding box
                s = pos.get("logo_size", LOGO_SIZE)
                logo = logo.resize((s, s), Image.LANCZOS)
                bx1, by1, bx2, by2 = pos["logo_box"]
                box_w, box_h = bx2 - bx1, by2 - by1
                # Centre inside the box
                paste_x = bx1 + (box_w - s) // 2
                paste_y = by1 + (box_h - s) // 2
                img.paste(logo, (paste_x, paste_y), logo)
            else:
                s = pos.get("logo_size", LOGO_SIZE)
                logo = logo.resize((s, s), Image.LANCZOS)
                # Paste with alpha mask so transparency is preserved
                img.paste(logo, pos["logo"], logo)
        else:
            print(f"  [WARN] Logo not found: {logo_path}")

    # --- Draw text fields ---
    nf = get_font_bold(pos.get("name_fs", FONT_SIZE))       # team name font (always bold)
    nf_num = get_font(pos.get("num_fs", FONT_SIZE))          # numbers font (regular)
    nf_num_b = get_font_bold(pos.get("num_fs", FONT_SIZE))   # numbers font (bold — used for total)
    # Centre team name inside bounding box when one is defined
    if "name_box" in pos:
        x1, y1, x2, y2 = pos["name_box"]
        cy = (y1 + y2) / 2
        if pos.get("name_align") == "left":
            # Left-aligned, vertically centred
            draw.text((x1, cy), team["name"], fill=text_color, font=nf, anchor="lm")
        else:
            # Centre-aligned
            cx = (x1 + x2) / 2
            draw.text((cx, cy), team["name"], fill=text_color, font=nf, anchor="mm")
    else:
        draw.text(pos["name"], team["name"], fill=text_color, font=nf)
    # Draw stat fields, centred in bounding box when defined
    for key, fnt in [("booyah", nf_num), ("elims", nf_num), ("place", nf_num), ("total", nf_num_b)]:
        box_key = f"{key}_box"
        if box_key in pos:
            x1, y1, x2, y2 = pos[box_key]
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            draw.text((cx, cy), team[key], fill=text_color, font=fnt, anchor="mm")
        else:
            draw.text(pos[key], team[key], fill=text_color, font=fnt)


def main():
    # Fetch live data from Google Sheets
    print("Fetching data from Google Sheets...")
    teams = fetch_sheet_data()
    print(f"Loaded {len(teams)} teams.")

    # Open the template image, or create a transparent canvas if not found
    if os.path.exists(IMAGE_PATH):
        img = Image.open(IMAGE_PATH).convert("RGBA")
    else:
        print(f"[WARNING] Template not found: {IMAGE_PATH} — using transparent 876x492 canvas.")
        img = Image.new("RGBA", (876, 492), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw each team at its designated coordinates
    for i, team in enumerate(teams):
        if i >= len(team_positions):
            break
        draw_team(img, draw, team_positions[i], i + 1, team)

    # Draw block images for the first 6 teams (column L tags)
    for i, team in enumerate(teams[:6]):
        tag = team.get("block_tag", "").strip()
        if not tag:
            continue
        bx1, by1, bx2, by2 = BLOCK_POSITIONS[i]
        box_w, box_h = bx2 - bx1, by2 - by1
        # Draw a semi-transparent black overlay on the block
        overlay = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 150))  # light black (alpha=150)
        img.paste(overlay, (bx1, by1), overlay)
        block_path = os.path.join(LOGO_DIR, f"{tag}.png")
        if not os.path.exists(block_path):
            print(f"  [WARN] Block image not found: {block_path}")
            continue
        block_img = Image.open(block_path).convert("RGBA")
        # Fit image inside box, preserving aspect ratio
        orig_w, orig_h = block_img.size
        scale = min(box_w / orig_w, box_h / orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        block_img = block_img.resize((new_w, new_h), Image.LANCZOS)
        # Centre inside the box
        paste_x = bx1 + (box_w - new_w) // 2
        paste_y = by1 + (box_h - new_h) // 2
        img.paste(block_img, (paste_x, paste_y), block_img)

    # Save to a temp file first, then atomically replace the output.
    # This prevents OBS from reading a half-written image (flicker fix).
    temp_path = OUTPUT_PATH + ".tmp"
    img.save(temp_path, format="PNG")
    os.replace(temp_path, OUTPUT_PATH)
    print(f"Scoreboard saved to: {OUTPUT_PATH}")


POLLING_INTERVAL = 30  # seconds between each refresh

if __name__ == "__main__":
    print(f"Starting scoreboard loop (refresh every {POLLING_INTERVAL}s). Press Ctrl+C to stop.")
    while True:
        try:
            main()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(POLLING_INTERVAL)
