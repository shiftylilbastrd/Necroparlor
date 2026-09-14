#!/usr/bin/env python3
"""Generates gpio-pinout.svg (in this same docs/ folder): a full 40-pin
Raspberry Pi GPIO header diagram, color-coded to match the actual jumper
wire colors used in this build (not a generic category scheme), with
this project's actual wiring (from climate.py's PIN_* constants, plus
the VCC/GND pins DHT22s are physically wired to) labeled on every pin
that's in use. Not part of the running app - a one-off doc generator.
If PIN_* ever changes in climate.py again (like the GPIO4->5 move did),
or the physical wiring changes (like the DHT22s moving from 5V to 3.3V
did), update the ROWS table below to match and rerun:

    python3 docs/gen_gpio_pinout.py

then commit the regenerated docs/gpio-pinout.svg alongside the code
change - README.md references it by path, nothing else to update."""
import os

# (physical_pin, bcm_label, category, function_text)
#
# Categories map 1:1 to Ryan's actual jumper wire colors (some colors
# are deliberately reused across categories, exactly as in the real
# wiring - e.g. door switch and heater relay are both gray):
#   power_3v3   orange  - 3.3V
#   power_5v    red     - 5V
#   ground_3v3  brown   - 3.3V ground
#   ground      (dark, unassigned) - spare/unused GND, no wire present
#   doorswitch  gray    - door reed switch
#   heaterrelay gray    - heater relay (same gray as door switch)
#   sensordata  yellow  - DHT22 DATA lines
#   doorservo   yellow  - door servo signal (same yellow as sensor data)
#   lightrelay  green   - light relay
#   fanrelay    blue    - fan relay
#   dehumidrelay purple - dehumidifier relay
#   i2c         muted blue - optional SHT31 SDA/SCL, not currently wired
#   reserved    (light gray) - EEPROM ID pins
#   unused      (dark) - unused GPIO, no wire present
#   deadpin     dark red - dead on this specific board
ROWS = [
    (1, "3.3V", "power_3v3", "Internal DHT22 - VCC (3.3V)"),
    (2, "5V", "power_5v", "5V (relay board power)"),
    (3, "GPIO2 (SDA)", "i2c", "SHT31 SDA (optional, not installed)"),
    (4, "5V", "power_5v", "5V (relay board power)"),
    (5, "GPIO3 (SCL)", "i2c", "SHT31 SCL (optional, not installed)"),
    (6, "GND", "ground_3v3", "Internal DHT22 - GND"),
    (7, "GPIO4", "deadpin", "unused - dead on this board, see README"),
    (8, "GPIO14 (TXD)", "unused", "unused"),
    (9, "GND", "ground_3v3", "External DHT22 fallback - GND"),
    (10, "GPIO15 (RXD)", "unused", "unused (was Light - moved, shares UART)"),
    (11, "GPIO17", "fanrelay", "Fan relay"),
    (12, "GPIO18 (PWM)", "doorservo", "Door servo"),
    (13, "GPIO27", "sensordata", "Internal DHT22 - DATA"),
    (14, "GND", "ground", "GND"),
    (15, "GPIO22", "heaterrelay", "Heater relay"),
    (16, "GPIO23", "dehumidrelay", "Dehumidifier relay"),
    (17, "3.3V", "power_3v3", "External DHT22 fallback - VCC (3.3V)"),
    (18, "GPIO24", "doorswitch", "Door reed switch"),
    (19, "GPIO10 (MOSI)", "unused", "unused"),
    (20, "GND", "ground", "GND"),
    (21, "GPIO9 (MISO)", "unused", "unused"),
    (22, "GPIO25", "unused", "unused"),
    (23, "GPIO11 (SCLK)", "unused", "unused"),
    (24, "GPIO8 (CE0)", "unused", "unused"),
    (25, "GND", "ground", "GND"),
    (26, "GPIO7 (CE1)", "unused", "unused"),
    (27, "ID_SD", "reserved", "EEPROM ID - reserved"),
    (28, "ID_SC", "reserved", "EEPROM ID - reserved"),
    (29, "GPIO5", "sensordata", "External DHT22 fallback - DATA"),
    (30, "GND", "ground", "GND"),
    (31, "GPIO6", "unused", "unused"),
    (32, "GPIO12", "unused", "unused"),
    (33, "GPIO13", "unused", "unused"),
    (34, "GND", "ground", "GND"),
    (35, "GPIO19", "unused", "unused"),
    (36, "GPIO16", "unused", "unused"),
    (37, "GPIO26", "lightrelay", "Light relay"),
    (38, "GPIO20", "unused", "unused"),
    (39, "GND", "ground", "GND"),
    (40, "GPIO21", "unused", "unused"),
]

COLORS = {
    "power_3v3":   ("#e08a3c", "#2b1400"),
    "power_5v":    ("#d9534f", "#ffffff"),
    "ground_3v3":  ("#6b4423", "#f5e6d3"),
    "ground":      ("#3d4750", "#e7ecef"),
    "doorswitch":  ("#7d8790", "#14181c"),
    "heaterrelay": ("#7d8790", "#14181c"),
    "sensordata":  ("#d9c74f", "#241f00"),
    "doorservo":   ("#d9c74f", "#241f00"),
    "lightrelay":  ("#4fb286", "#0d1712"),
    "fanrelay":    ("#4f8fd9", "#ffffff"),
    "dehumidrelay":("#8a63c9", "#ffffff"),
    "i2c":         ("#3d5a78", "#c9d8e6"),
    "reserved":    ("#93a0ab", "#14181c"),
    "unused":      ("#2e353c", "#8a97a2"),
    "deadpin":     ("#7a3030", "#f5c6c6"),
}
STROKE = "#5a6570"

# Legend entries: (category, label). Two categories intentionally share
# a color (doorswitch/heaterrelay, sensordata/doorservo) so only one of
# each pair gets a legend row, with a combined label.
LEGEND_ITEMS = [
    ("power_3v3", "3.3V — DHT22 power"),
    ("power_5v", "5V (in use elsewhere)"),
    ("ground_3v3", "3.3V ground (DHT22)"),
    ("ground", "Spare / unused GND"),
    ("doorswitch", "Door switch & heater relay"),
    ("sensordata", "Sensor data & door servo"),
    ("lightrelay", "Light relay"),
    ("fanrelay", "Fan relay"),
    ("dehumidrelay", "Dehumidifier relay"),
    ("i2c", "I²C — optional SHT31"),
    ("reserved", "Reserved (EEPROM ID)"),
    ("unused", "Unused GPIO"),
    ("deadpin", "Dead pin — see README"),
]

ROW_H = 34
TOP = 245
LEFT_LABEL_X = 398
LEFT_CIRC_X = 420
RIGHT_CIRC_X = 540
RIGHT_LABEL_X = 562
R = 13
WIDTH = 1200
HEIGHT = TOP + ROW_H * 20 + 60

LEGEND_COLS = [40, 440, 840]
LEGEND_ROW_Y = 82
LEGEND_ROW_STEP = 28

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def pin_circle(cx, cy, pin_num, category):
    fill, textcolor = COLORS[category]
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="{fill}" stroke="{STROKE}" stroke-width="1.5"/>'
        f'<text x="{cx}" y="{cy+4}" text-anchor="middle" font-size="10.5" '
        f'font-family="monospace" font-weight="700" fill="{textcolor}">{pin_num}</text>'
    )

def row_svg(row):
    pin, bcm_label, category, func = row
    cy = TOP + (ROW_H * ((pin - 1) // 2))
    is_left = (pin % 2 == 1)
    parts = []
    if is_left:
        cx = LEFT_CIRC_X
        # label to the left, right-aligned: function on top, bcm name dim below
        parts.append(
            f'<text x="{LEFT_LABEL_X}" y="{cy-3}" text-anchor="end" font-size="12.5" '
            f'font-family="-apple-system,Segoe UI,Roboto,sans-serif" font-weight="600" '
            f'fill="#e7ecef">{esc(func)}</text>'
        )
        parts.append(
            f'<text x="{LEFT_LABEL_X}" y="{cy+11}" text-anchor="end" font-size="10.5" '
            f'font-family="monospace" fill="#93a0ab">{esc(bcm_label)}</text>'
        )
        parts.append(pin_circle(cx, cy, pin, category))
        # connecting bar to the right pin's circle
        parts.append(f'<line x1="{LEFT_CIRC_X+R}" y1="{cy}" x2="{RIGHT_CIRC_X-R}" y2="{cy}" '
                      f'stroke="#2a323a" stroke-width="3"/>')
    else:
        cx = RIGHT_CIRC_X
        parts.append(pin_circle(cx, cy, pin, category))
        parts.append(
            f'<text x="{RIGHT_LABEL_X}" y="{cy-3}" text-anchor="start" font-size="12.5" '
            f'font-family="-apple-system,Segoe UI,Roboto,sans-serif" font-weight="600" '
            f'fill="#e7ecef">{esc(func)}</text>'
        )
        parts.append(
            f'<text x="{RIGHT_LABEL_X}" y="{cy+11}" text-anchor="start" font-size="10.5" '
            f'font-family="monospace" fill="#93a0ab">{esc(bcm_label)}</text>'
        )
    return "\n".join(parts)

def legend():
    # Fixed x per column (not computed from text length - a proportional
    # font makes character-count-based spacing unreliable), row-major
    # across a 3-column grid so nothing runs off the right edge.
    parts = []
    for i, (cat, label) in enumerate(LEGEND_ITEMS):
        col = i % len(LEGEND_COLS)
        row = i // len(LEGEND_COLS)
        x = LEGEND_COLS[col]
        y = LEGEND_ROW_Y + row * LEGEND_ROW_STEP
        fill, _ = COLORS[cat]
        parts.append(f'<rect x="{x}" y="{y-11}" width="16" height="16" rx="3" fill="{fill}" stroke="{STROKE}"/>')
        parts.append(f'<text x="{x+22}" y="{y+1}" font-size="12.5" font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#e7ecef">{esc(label)}</text>')
    return "\n".join(parts)

def build():
    body = []
    for row in ROWS:
        body.append(row_svg(row))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}">
  <rect width="{WIDTH}" height="{HEIGHT}" fill="#14181c"/>
  <text x="{WIDTH/2}" y="36" text-anchor="middle" font-size="22" font-weight="700"
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#e7ecef">Necroparlor - Raspberry Pi 40-pin GPIO header</text>
  <text x="{WIDTH/2}" y="58" text-anchor="middle" font-size="12.5"
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#93a0ab">Pin 1 is the corner nearest the SD card slot (square pad on the board silkscreen). BCM numbering. Colors match this build's actual jumper wires.</text>
  {legend()}
  <text x="{LEFT_LABEL_X-10}" y="{TOP-20}" text-anchor="end" font-size="11" font-weight="700" letter-spacing="1"
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#93a0ab">PIN 1 SIDE</text>
  <text x="{RIGHT_LABEL_X+10}" y="{TOP-20}" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1"
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#93a0ab">PIN 2 SIDE</text>
  {chr(10).join(body)}
</svg>'''
    return svg

if __name__ == "__main__":
    out = build()
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpio-pinout.svg")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print("wrote", len(out), "bytes")
