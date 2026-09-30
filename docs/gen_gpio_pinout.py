#!/usr/bin/env python3
"""Generates gpio-pinout.svg (in this same docs/ folder): a full 40-pin
Raspberry Pi GPIO header diagram, color-coded to match the actual jumper
wire colors used in this build (not a generic category scheme), with
this project's actual wiring (from climate.py's PIN_* constants /
EXTERNAL_SHT31_I2C_BUS, plus the power/ground pins the sensors are
physically wired to) labeled on every pin that's in use. Not part of the running app - a one-off doc generator.
If PIN_* ever changes in climate.py again (like the GPIO4->5 move did),
or the physical wiring changes (like the DHT22s moving from 5V to 3.3V
did), update the ROWS table below to match and rerun:

    python3 docs/gen_gpio_pinout.py

then commit the regenerated docs/gpio-pinout.svg alongside the code
change - README.md references it by path, nothing else to update."""
import os

# (physical_pin, bcm_label, category, function_text)
#
# Categories map 1:1 to Ryan's actual wire colors (some colors are
# deliberately reused across categories, exactly as in the real wiring -
# e.g. door switch and heater relay are both gray). The four sensor
# categories are the SHT31 probes' own factory lead colors - both
# sensors are prewired SHT31 probes (~5 ft leads) wired straight to the
# header:
#   sensorpower orange - SHT31 VIN, 3.3V. The probes' own power lead is
#                        red, but 3.3V is drawn orange on purpose so it
#                        can't be confused with the red 5V pins - never
#                        move a sensor lead onto pin 2/4: an SHT31
#                        survives 5V, but pull-ups to VIN would then put
#                        5V on the Pi's 3.3V-only SDA/SCL pins
#   power_5v    red    - 5V: pin 2 relay board, pin 4 door servo
#   sensorground black - SHT31 GND (internal pin 9, external pin 34)
#   relayground black  - relay board GND (pin 6) - black assumed, not
#                        confirmed; change its fill below if the jumper
#                        is another color
#   ground      (dark, unassigned) - spare/unused GND, no wire present
#   i2c_scl     yellow - SHT31 SCL (same yellow as the door servo)
#   doorservo   yellow - door servo signal
#   i2c_sda     green  - SHT31 SDA (same green as the light relay)
#   lightrelay  green  - light relay
#   doorswitch  gray   - door reed switch
#   heaterrelay gray   - heater relay (same gray as door switch)
#   fanrelay    blue   - fan relay
#   dehumidrelay purple - dehumidifier relay
#   reserved    (light gray) - EEPROM ID pins
#   unused      (dark) - unused GPIO, no wire present
#   deadpin     dark red - dead on this specific board
#
# SCL=yellow / SDA=green is the common convention for these 4-wire
# probes, but some vendors swap the two - if Ryan's turn out reversed,
# swap the i2c_scl/i2c_sda fills in COLORS below (the pin LABELS are
# right either way; only the colors would be).
ROWS = [
    (1, "3.3V", "sensorpower", "Internal power"),
    (2, "5V", "power_5v", "Relay board power (5V)"),
    (3, "GPIO2 (SDA)", "i2c_sda", "Internal SHT31 - SDA"),
    (4, "5V", "power_5v", "Servo power (5V)"),
    (5, "GPIO3 (SCL)", "i2c_scl", "Internal SHT31 - SCL"),
    (6, "GND", "relayground", "Relay board ground"),
    (7, "GPIO4", "deadpin", "unused - dead on this board, see README"),
    (8, "GPIO14 (TXD)", "unused", "unused"),
    (9, "GND", "sensorground", "Internal ground"),
    (10, "GPIO15 (RXD)", "unused", "unused (was Light - moved, shares UART)"),
    (11, "GPIO17", "fanrelay", "Fan relay"),
    (12, "GPIO18 (PWM)", "doorservo", "Door servo"),
    (13, "GPIO27", "unused", "unused (internal DHT22 DATA, if used instead)"),
    (14, "GND", "ground", "GND"),
    (15, "GPIO22", "heaterrelay", "Heater relay"),
    (16, "GPIO23", "dehumidrelay", "Dehumidifier relay"),
    (17, "3.3V", "sensorpower", "External power"),
    (18, "GPIO24", "lightrelay", "Light relay"),
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
    (29, "GPIO5", "unused", "unused (external DHT22 DATA, if used instead)"),
    (30, "GND", "ground", "GND"),
    (31, "GPIO6", "unused", "unused"),
    (32, "GPIO12 (SDA5)", "i2c_sda", "External SHT31 - SDA (i2c5)"),
    (33, "GPIO13 (SCL5)", "i2c_scl", "External SHT31 - SCL (i2c5)"),
    (34, "GND", "sensorground", "External ground"),
    (35, "GPIO19", "unused", "unused"),
    (36, "GPIO16", "unused", "unused"),
    (37, "GPIO26", "doorswitch", "Door reed switch"),
    (38, "GPIO20", "unused", "unused"),
    (39, "GND", "ground", "GND"),
    (40, "GPIO21", "unused", "unused"),
]

COLORS = {
    "sensorpower": ("#e08a3c", "#2b1400"),
    "power_5v":    ("#d9534f", "#ffffff"),
    "sensorground":("#050607", "#e7ecef"),
    "relayground": ("#050607", "#e7ecef"),
    "ground":      ("#3d4750", "#e7ecef"),
    "doorswitch":  ("#7d8790", "#14181c"),
    "heaterrelay": ("#7d8790", "#14181c"),
    "i2c_scl":     ("#d9c74f", "#241f00"),
    "doorservo":   ("#d9c74f", "#241f00"),
    "i2c_sda":     ("#4fb286", "#0d1712"),
    "lightrelay":  ("#4fb286", "#0d1712"),
    "fanrelay":    ("#4f8fd9", "#ffffff"),
    "dehumidrelay":("#8a63c9", "#ffffff"),
    "reserved":    ("#93a0ab", "#14181c"),
    "unused":      ("#2e353c", "#8a97a2"),
    "deadpin":     ("#7a3030", "#f5c6c6"),
}
STROKE = "#5a6570"

# Legend entries: (category, label). Categories that share a wire color
# (doorswitch/heaterrelay, i2c_scl/doorservo, i2c_sda/lightrelay) get one
# combined row.
LEGEND_ITEMS = [
    ("sensorpower", "Sensor power — 3.3V"),
    ("power_5v", "5V — relay board & servo power"),
    ("sensorground", "Ground — sensors & relay board"),
    ("i2c_sda", "SHT31 SDA & light relay"),
    ("i2c_scl", "SHT31 SCL & door servo"),
    ("doorswitch", "Door switch & heater relay"),
    ("fanrelay", "Fan relay"),
    ("dehumidrelay", "Dehumidifier relay"),
    ("ground", "Spare / unused GND"),
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
    # A black wire's pin would vanish into the dark background with the
    # normal outline - give it a lighter ring so it still reads as a pin.
    stroke = "#aab4bd" if category in ("sensorground", "relayground") else STROKE
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
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
        stroke = "#aab4bd" if cat in ("sensorground", "relayground") else STROKE
        parts.append(f'<rect x="{x}" y="{y-11}" width="16" height="16" rx="3" fill="{fill}" stroke="{stroke}"/>')
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
        font-family="-apple-system,Segoe UI,Roboto,sans-serif" fill="#93a0ab">Pin 1 is the corner nearest the SD card slot (square pad on the board silkscreen). BCM numbering. Colors match this build's wires (3.3V shown orange).</text>
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
