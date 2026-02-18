import sys
# Force unbuffered output so logs show immediately on Render/Vercel
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

"""
main_web.py — Flask-based scoreboard server for online hosting (Vercel).

This is a hosted version of main.py that:
  1. Runs a Flask web server to serve the scoreboard HTML and generated image.
  2. On Vercel (serverless), generates the image fresh on each request.
  3. For local dev, uses a background thread for periodic regeneration.

Usage:
  pip install flask Pillow requests
  python main_web.py

The scoreboard will be available at http://localhost:5000/
"""

import os
import time
import threading
import requests
from io import BytesIO
from flask import Flask, send_file, Response
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

# --- Flask App ---
app = Flask(__name__)

# Get the directory where this script lives (for resolving relative paths)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Font Setup (Chakra Petch) ---
FONT_PATH = os.path.join(BASE_DIR, "ChakraPetch-Medium2.ttf")
FONT_BOLD_PATH = os.path.join(BASE_DIR, "ChakraPetch-Bold.ttf")
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

# Shared sizing for all 12 teams (change these to adjust all rows at once)
ROW_LOGO_SIZE = 35    # logo width & height in pixels
ROW_NAME_FS = 28      # font size for team name
ROW_NUM_FS = 28       # font size for stat numbers (booyah, elims, place, total)

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
        # Case-insensitive logo lookup (Linux is case-sensitive unlike Windows)
        logos_dir = os.path.join(BASE_DIR, LOGO_DIR)
        logo_path = None
        if os.path.isdir(logos_dir):
            target = f"{tag}.png".lower()
            for f in os.listdir(logos_dir):
                if f.lower() == target:
                    logo_path = os.path.join(logos_dir, f)
                    break
        if logo_path and os.path.exists(logo_path):
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
                img.paste(logo, pos["logo"], logo)
        else:
            print(f"  [WARN] Logo not found: {tag}")

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


# Store the last error for the /debug route
_last_error = None

def generate_scoreboard_image():
    """Generate the scoreboard image and return it as a PIL Image object."""
    global _last_error
    try:
        print("Fetching data from Google Sheets...")
        teams = fetch_sheet_data()
        print(f"Loaded {len(teams)} teams.")

        template_path = os.path.join(BASE_DIR, IMAGE_PATH)
        if os.path.exists(template_path):
            img = Image.open(template_path).convert("RGBA")
        else:
            print(f"[WARNING] Template not found: {template_path} — using transparent canvas.")
            img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Draw each team at its designated coordinates
        for i, team in enumerate(teams):
            if i >= len(team_positions):
                break
            draw_team(img, draw, team_positions[i], i + 1, team)

        # Draw block images for the first 6 teams (column L tags)
        logos_dir = os.path.join(BASE_DIR, LOGO_DIR)
        for i, team in enumerate(teams[:6]):
            tag = team.get("block_tag", "").strip()
            if not tag:
                continue
            bx1, by1, bx2, by2 = BLOCK_POSITIONS[i]
            box_w, box_h = bx2 - bx1, by2 - by1
            # Draw a semi-transparent black overlay on the block
            overlay = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 150))
            img.paste(overlay, (bx1, by1), overlay)
            # Case-insensitive block image lookup
            block_path = None
            if os.path.isdir(logos_dir):
                target = f"{tag}.png".lower()
                for f in os.listdir(logos_dir):
                    if f.lower() == target:
                        block_path = os.path.join(logos_dir, f)
                        break
            if not block_path or not os.path.exists(block_path):
                print(f"  [WARN] Block image not found: {tag}")
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

        _last_error = None
        return img
    except Exception as e:
        _last_error = str(e)
        print(f"[ERROR in generate_scoreboard_image] {e}")
        import traceback
        traceback.print_exc()
        raise


def generate_scoreboard():
    """Generate the scoreboard image and save it to OUTPUT_PATH (for local dev)."""
    img = generate_scoreboard_image()
    output_path = os.path.join(BASE_DIR, OUTPUT_PATH)
    temp_path = output_path + ".tmp"
    img.save(temp_path, format="PNG")
    os.replace(temp_path, output_path)
    print(f"Scoreboard saved to: {output_path}")


# --- Background Thread for Image Generation (local dev only) ---
POLLING_INTERVAL = 5  # seconds between each refresh

def scoreboard_loop():
    """Continuously regenerate the scoreboard image in the background."""
    print(f"[BG] Scoreboard loop started (refresh every {POLLING_INTERVAL}s)")
    while True:
        try:
            generate_scoreboard()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(POLLING_INTERVAL)


# --- Flask Routes ---

@app.route("/")
def index():
    """Serve the scoreboard HTML page."""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Live Scoreboard</title>
<style>
  * { margin: 0; padding: 0; }
  body { background: transparent; overflow: hidden; }
  img {
    position: absolute;
    top: 0; left: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
</style>
</head>
<body>
  <img id="scoreboard" src="/scoreboard.png" />
  <script>
    const img = document.getElementById('scoreboard');
    // Reload the image every 2 seconds with a cache-busting param
    setInterval(() => {
      const newImg = new Image();
      newImg.onload = () => {
        img.src = newImg.src;
      };
      newImg.src = '/scoreboard.png?t=' + Date.now();
    }, 2000);
  </script>
</body>
</html>"""


@app.route("/debug")
def debug_info():
    """Show diagnostic info to troubleshoot deployment issues."""
    files = os.listdir(BASE_DIR)
    template_path = os.path.join(BASE_DIR, IMAGE_PATH)
    logos_path = os.path.join(BASE_DIR, LOGO_DIR)
    logo_files = os.listdir(logos_path) if os.path.isdir(logos_path) else ["FOLDER NOT FOUND"]
    info = {
        "base_dir": BASE_DIR,
        "all_files": files,
        "template_exists": os.path.exists(template_path),
        "template_path": template_path,
        "font_exists": os.path.exists(FONT_PATH),
        "font_bold_exists": os.path.exists(FONT_BOLD_PATH),
        "logos_folder": logo_files,
        "last_error": _last_error,
    }
    import json
    return Response(json.dumps(info, indent=2), mimetype="application/json")


@app.route("/scoreboard.png")
def scoreboard_image():
    """Generate and serve the scoreboard image on-demand.

    On Vercel (serverless), the image is generated fresh each request
    and returned from memory — no disk writes needed.

    Set env var PAUSED=true to stop GAPI calls and return the bare template.
    """
    # Check if paused — return bare template without calling Google Sheets API
    if os.environ.get("PAUSED", "").lower() == "true":
        template_path = os.path.join(BASE_DIR, IMAGE_PATH)
        if os.path.exists(template_path):
            return send_file(template_path, mimetype="image/png")
        return Response(b"Paused", status=200, mimetype="text/plain")

    try:
        img = generate_scoreboard_image()
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")
    except Exception:
        return Response(b"Error generating scoreboard", status=500, mimetype="text/plain")


# --- Entry Point (local development only — not used on Vercel) ---
if __name__ == "__main__":
    # Print startup diagnostics
    print(f"=== STARTUP DIAGNOSTICS ===")
    print(f"BASE_DIR: {BASE_DIR}")
    print(f"Files in BASE_DIR: {os.listdir(BASE_DIR)}")
    print(f"Template exists: {os.path.exists(os.path.join(BASE_DIR, IMAGE_PATH))}")
    print(f"Font exists: {os.path.exists(FONT_PATH)}")
    print(f"Font bold exists: {os.path.exists(FONT_BOLD_PATH)}")
    print(f"===========================")

    # Generate the first scoreboard immediately before starting the server
    print("Generating initial scoreboard...")
    try:
        generate_scoreboard()
    except Exception as e:
        print(f"[WARNING] Initial generation failed: {e}")
        import traceback
        traceback.print_exc()

    # Start the background thread for periodic regeneration
    bg_thread = threading.Thread(target=scoreboard_loop, daemon=True)
    bg_thread.start()

    # Get port from environment variable (hosting platforms set this)
    port = int(os.environ.get("PORT", 5000))
    print(f"\n=== Scoreboard server running at http://localhost:{port}/ ===\n")
    app.run(host="0.0.0.0", port=port, debug=False)
