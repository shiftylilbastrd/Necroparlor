#!/usr/bin/env python3
"""Generates gpio-pinout.svg (in this same docs/ folder): a full 40-pin
Raspberry Pi GPIO header diagram, color-coded by category, with this
project's actual wiring (from climate.py's PIN_* constants) labeled on
the pins that are used. Not part of the running app - a one-off doc
generator. If PIN_* ever changes in climate.py again (like the GPIO4->5
move did), update the ROWS table below to match and rerun:

    python3 docs/gen_gpio_pinout.py

then commit the regenerated docs/gpio-pinout.svg alongside the code
change - README.md references it by path, nothing else to update."""
import os

# (physical_pin, bcm_label, category, function_text)
# categories: power, ground, used, i2c, reserved, unused
ROWS = [
    (1, "3.3V", "power", "3.3V"),
    (2, "5V", "power", "5V"),
    (3, "GPIO2 (SDA)", "i2c", "SHT31 SDA (optional)"),
    (4, "5V", "power", "5V"),
    (5, "GPIO3 (SCL)", "i2c", "SHT31 SCL (optional)"),
    (6, "GND", "ground", "GND"),
    (7, "GPIO4", "deadpin", "unused - dead on this board, see README"),
    (8, "GPIO14 (TXD)", "unused", "unused"),
    (9, "GND", "ground", "GND"),
    (10, "GPIO15 (RXD)", "unused", "unused (was Light - moved, shares UART)"),
    (11, "GPIO17", "used", "Fan relay"),
    (12, "GPIO18 (PWM)", "used", "Door servo"),
    (13, "GPIO27", "used", "Internal DHT22 - DATA"),
    (14, "GND", "ground", "GND"),
    (15, "GPIO22", "used", "Heater relay"),
    (16, "GPIO23", "used", "Dehumidifier relay"),
    (17, "3.3V", "power", "3.3V"),
    (18, "GPIO24", "used", "Door reed switch"),
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
    (29, "GPIO5", "used", "External DHT22 fallback - DATA"),
    (30, "GND", "ground", "GND"),
    (31, "GPIO6", "unused", "unused"),
    (32, "GPIO12", "unused", "unused"),
    (33, "GPIO13", "unused", "unused"),
    (34, "GND", "ground", "GND"),
    (35, "GPIO19", "unused", "unused"),
    (36, "GPIO16", "unused", "unused"),
    (37, "GPIO26", "used", "Light relay"),
    (38, "GPIO20", "unused", "unused"),
    (39, "GND", "ground", "GND"),
    (40, "GPIO21", "unused", "unused"),
]

COLORS = {
    "power":   ("#d9534f", "#ffffff"),
    "ground":  ("#3d4750", "#e7ecef"),
    "used":    ("#4fb286", "#0d1712"),
    "i2c":     ("#4f8fd9", "#ffffff"),
    "reserved":("#93a0ab", "#14181c"),
    "unused":  ("#2e353c", "#8a97a2"),
    "deadpin": ("#7a3030", "#f5c6c6"),
}
STROKE = "#5a6570"

ROW_H = 34
TOP = 150
LEFT_LABEL_X = 318
LEFT_CIRC_X = 340
RIGHT_CIRC_X = 460
RIGHT_LABEL_X = 482
R = 13
WIDTH = 1040
HEIGHT = TOP + ROW_H * 20 + 60

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
    # Fixed x per item (not computed from text length - a proportional
    # font makes character-count-based spacing unreliable) laid out
    # across two rows so nothing runs off the right edge of the canvas.
    row1 = [
        ("used", "Used by Necroparlor", 40),
        ("i2c", "I²C - optional SHT31", 300),
        ("power", "Power (3.3V / 5V)", 560),
        ("ground", "Ground", 780),
    ]
    row2 = [
        ("reserved", "Reserved (EEPROM ID)", 40),
        ("unused", "Unused", 300),
        ("deadpin", "Dead pin - see README", 460),
    ]
    parts = []
    for row_items, y in ((row1, 82), (row2, 110)):
        for cat, label, x in row_items:
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
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#93a0ab">Pin 1 is the corner nearest the SD card slot (square pad on the board silkscreen). BCM numbering.</text>
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
    with open(out_path, "w") as f:
        f.write(out)
    print("wrote", len(out), "bytes")
