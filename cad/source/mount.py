#!/usr/bin/env python3
"""Whisker cradle: a printed tray that clamps the Whisker board to Nybble's y1 rails and guards the
servo headers on the board's back. Builds the part, checks its fit, writes previews.

    python boards/whisker/cad/mount.py   # a Python with CadQuery and trimesh

Read, never typed:
* build/whisker/fab/whisker.step: the board as KiCad exported it, every modelled body by reference;
* build/whisker/whisker.kicad_pcb, through KiCad's own Python (pcbnew): each footprint's side,
  position, pads, courtyard, F.Fab box and the 3D model it names (and whether the file exists);
* boards/whisker/board.json: the frame the brief's coordinates are given in.

Nybble frame facts come from robots/research/cat/spec.md and are cited where used (section numbers).
Everything this script assumes rather than measures is listed in fit.json under "assumed".

Frames. KiCad's STEP puts the board at x = 100 + bx, y = -(100 + by) for a brief point (bx, by),
board bottom at z = 0. This script works in the spec's board frame (spec section 0): x along the
64.8 mm edge toward the Grove sockets (the robot's head), y toward the notch (the robot's left),
origin at the lower-left corner, z up with the board's bottom face at 0. The robot frame of the
y1 STL (spec 2.6, 5) is X lateral from the body centre line, Z fore-aft, Y up with y1's underside
at Y = -3: X = y - yc, Z = x - xc (xc, yc the hole-pattern centre), Y = z - z_top - 3.

The part. Four bosses (7 mm, the seat pad spec 2.7 and 5 ask for) stand under the board's holes on
a 2 mm floor; an M2 self-tapping screw (or Petoi's screw B) goes up through boss, board and into
the y1 pilot hole, so the board is clamped between the rail bosses and the cradle as the stock
board is held by the rails alone. The floor sits below everything the board carries on its back
(the 3-row right-angle servo headers, their mated plugs, the battery socket), is cut open under
the battery socket for its plug and cable, and stays inside the board outline, so the servo plugs
still leave sideways (spec 2.5) and the -x and +x edges stay free for USB-C, the dongle and Grove.
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import cadquery as cq
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.Bnd import Bnd_Box
from OCP.GProp import GProp_GProps
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from heypcb_kicad import env  # noqa: E402  (paths of KiCad's Python and kicad-cli only)

BUILD = ROOT / "build" / "whisker"
STEP = BUILD / "fab" / "whisker.step"
PCB = BUILD / "whisker.kicad_pcb"
OUT = BUILD / "cad"

# ---------------------------------------------------------------- design numbers of the cradle
BOSS_OD = 7.0          # spec 2.7 / 5: seat pads >= 7 mm, the boss keep-out the board keeps clear
BOSS_BORE = 2.4        # M2 self-tapping screw (spec 5) passes; the board's hole is 3.2
HEAD_BORE, HEAD_DEPTH = 4.4, 1.4   # counterbore for an M2 pan head under the floor
FLOOR_T = 2.0
CLEAR_BELOW = 2.0      # floor top this far below the lowest body over the floor
FLOOR_INSET = 1.0      # floor edge inside the board outline (the outline is photo-measured, +-0.5)
PLUG_GAP = 1.5         # around the battery plug and cable in the floor opening
LIP_H, LIP_W = 2.0, 1.6

# ---------------------------------------------------------------- estimates (listed in fit.json)
RA = {"pitch": 2.54, "insulator": 2.5, "pin_out": 6.0, "pin": 0.64, "standoff": 0.4, "bend_gap": 1.5,
      "source": "PZ254-3-03-W-2.5-G0 catalogue row (C7429379): 2.54 mm pitch, 3 rows, 2.5 mm insulator, 6 mm "
                "pins; standoff and bend gap estimated"}
SERVO_PLUG = {"length": 14.0, "width": 2.6, "source": "spec 5: plug length about 14 mm (estimated); width a "
              "standard servo plug's, estimated"}
XH_PLUG = {"below_housing": 5.0, "cable_to": -20.0, "cable_w": 4.0, "cable_d": 2.5,
           "source": "spec 2.4: XH housing 7.0 mm, about 12 mm with the plug mated (estimated); cable leaves downward"}
# bodies KiCad 10.0.6 installs no STEP for: envelopes over the part's F.Fab box, heights below
ENVELOPE_H = {
    "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12": (3.26, "heypcb-kicad models3d envelope (HRO datasheet, 3.26 mm)"),
    "Connector:NS-Tech_Grove_1x04_P2mm_Vertical": (8.0, "ZX-HY2.0-4PZZ catalogue row (C7429580) lists 8 mm"),
    "Buzzer_Beeper:Buzzer_Murata_PKLCS1212E": (3.0, "PKLCS1212E4001-R1 catalogue row (C113159) lists 3 mm"),
    "Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A": (1.5, "TS-1187A-B-A-B catalogue row (C318884) lists 1.5 mm"),
    "OptoDevice:Everlight_IRM-H6xxT": (4.0, "estimated: no height in the IRM-H638T catalogue row"),
    "Sensor_Motion:InvenSense_QFN-24_4x4mm_P0.5mm": (0.95, "estimated: 4 x 4 x 0.9 mm QFN"),
}
SERVO_REFS_FP = ("Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
                 "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")

# ---------------------------------------------------------------- Nybble frame (spec, reconstructed)
FRAME = {
    "rail_t": 3.0,                       # spec 2.6: 3 mm plywood y1 rails, underside on the board's top
    "rail_inner": 28.0,                  # spec 2.6 / 5: rail inner edge X = 28 between the bosses
    "rail_outer": 34.5,                  # spec 5: narrow middle section X = 28-34.5 between Z = +-26
    "rail_half_len": 62.2,               # spec 5: y1 outline 124.4 mm long
    "boss_reach": 22.5,                  # spec 2.6 / 5: bosses reach in to X = 22.5 around the holes
    "boss_r": 3.5,                       # spec 2.7 / 5: 7 mm seat
    "pilot_d": 1.4,                      # spec 2.3 / 5: 1.4 mm pilot holes
    "holes_Z": (-41.0, -29.0, 29.0, 41.0),  # spec 5: Z = +-29 (the board's) and +-41 (the rails' others)
    "hole_X": 24.52,                     # spec 2.3: X = +-24.52
    "belly_gap": 18.4,                   # spec 2.4: y1 underside to belly top, shared-frame assumption
    "belly_t": 3.0,
}


def log(*a):
    print(*a, flush=True)


# ================================================================ board facts through pcbnew
DUMP = r'''
import json, os, sys
import pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
mm = pcbnew.ToMM
out = []
for fp in b.GetFootprints():
    fp.BuildCourtyardCaches()
    side = "bottom" if fp.IsFlipped() else "top"
    cy = fp.GetCourtyard(pcbnew.B_CrtYd if side == "bottom" else pcbnew.F_CrtYd)
    cbox = None
    if cy.OutlineCount():
        bb = cy.BBox(); cbox = [mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetRight()), mm(bb.GetBottom())]
    fab = pcbnew.B_Fab if side == "bottom" else pcbnew.F_Fab
    xs, ys = [], []
    for g in fp.GraphicalItems():
        if g.GetLayer() == fab and g.GetClass() == "PCB_SHAPE":
            bb = g.GetBoundingBox(); xs += [mm(bb.GetX()), mm(bb.GetRight())]; ys += [mm(bb.GetY()), mm(bb.GetBottom())]
    models = []
    for m in fp.Models():
        p = m.m_Filename
        for k in ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD8_3DMODEL_DIR"):
            p = p.replace("${%s}" % k, os.environ.get("MODEL_DIR", ""))
        models.append({"file": m.m_Filename, "exists": os.path.exists(p) and p.lower().endswith((".step", ".stp"))})
    pads = []
    for p in fp.Pads():
        q = p.GetPosition(); s = p.GetSize()
        pads.append({"n": p.GetNumber(), "x": mm(q.x), "y": mm(q.y), "w": mm(s.x), "h": mm(s.y),
                     "drill": mm(p.GetDrillSize().x), "net": p.GetNetname()})
    q = fp.GetPosition()
    out.append({"ref": fp.GetReference(), "fpid": "%s:%s" % (fp.GetFPID().GetLibNickname(), fp.GetFPID().GetLibItemName()),
                "side": side, "x": mm(q.x), "y": mm(q.y), "rot": fp.GetOrientationDegrees(), "courtyard": cbox,
                "fab": [min(xs), min(ys), max(xs), max(ys)] if xs else None, "models": models, "pads": pads,
                "value": fp.GetValue()})
sys.stdout.write("DUMP" + json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def board_facts() -> list:
    e = dict(os.environ, MODEL_DIR=str(env.MODEL_DIR))
    r = subprocess.run([env.KICAD_PYTHON, "-c", DUMP, str(PCB)], capture_output=True, text=True, env=e, timeout=300)
    i = r.stdout.find("DUMP")
    if i < 0:
        raise SystemExit("pcbnew dump failed: %s" % (r.stderr or r.stdout)[-800:])
    return json.loads(r.stdout[i + 4:])


# ================================================================ the STEP, by reference
def read_step(path: Path):
    doc = TDocStd_Document(TCollection_ExtendedString("whisker"))
    rd = STEPCAFControl_Reader()
    rd.SetNameMode(True)
    if rd.ReadFile(str(path)) != 1:
        raise SystemExit("cannot read %s" % path)
    rd.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    parts = {}
    unnamed = []

    def name(lab):
        n = TDataStd_Name()
        return n.Get().ToExtString() if lab.FindAttribute(TDataStd_Name.GetID_s(), n) else ""

    for i in range(1, roots.Length() + 1):
        comps = TDF_LabelSequence()
        st.GetComponents_s(roots.Value(i), comps, False)
        for k in range(1, comps.Length() + 1):
            lab = comps.Value(k)
            nm = name(lab)
            shape = cq.Shape.cast(st.GetShape_s(lab))
            if nm and not nm.startswith("=>"):
                parts.setdefault(nm, []).append(shape)
            else:
                unnamed.append(shape)
    return parts, unnamed


def bbox(shape):
    b = Bnd_Box()
    BRepBndLib.Add_s(shape.wrapped if hasattr(shape, "wrapped") else shape, b)
    return b.Get()


def dist(a, b) -> float:
    d = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
    d.Perform()
    return d.Value() if d.IsDone() else float("nan")


def common_volume(a, b) -> float:
    c = BRepAlgoAPI_Common(a.wrapped, b.wrapped)
    c.Build()
    if not c.IsDone():
        return float("nan")
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(c.Shape(), g)
    return g.Mass()


def plan_distance_to_holes(sols, holes):
    """Plan distance from a part's bounding boxes to the nearest hole centre: more than 3.5 mm keeps
    the part off the 7 mm seat the spec reserves around each hole (spec 2.7)."""
    best = float("inf")
    for sh in sols:
        x0, y0, _, x1, y1, _ = bbox(sh)
        for hx_, hy_ in holes:
            px, py = min(max(hx_, x0), x1), min(max(hy_, y0), y1)
            best = min(best, math.hypot(px - hx_, py - hy_))
    return best


def compound(shapes):
    return cq.Compound.makeCompound([s for s in shapes])


# ================================================================ geometry
def inset_polygon(pts, d):
    """Each edge of a simple polygon moved d inward, corners re-intersected (sharp)."""
    area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
    ccw = area > 0
    lines = []
    for i in range(len(pts)):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        nx, ny = (-dy / n, dx / n) if ccw else (dy / n, -dx / n)   # inward normal
        lines.append(((x0 + nx * d, y0 + ny * d), (dx, dy)))
    out = []
    for i in range(len(lines)):
        (p, r), (q, s_) = lines[i - 1], lines[i]
        den = r[0] * s_[1] - r[1] * s_[0]
        t = ((q[0] - p[0]) * s_[1] - (q[1] - p[1]) * s_[0]) / den
        out.append((p[0] + r[0] * t, p[1] + r[1] * t))
    return out


def box(x0, y0, z0, x1, y1, z1):
    return cq.Solid.makeBox(x1 - x0, y1 - y0, z1 - z0, cq.Vector(x0, y0, z0))


def ra_header(pads, edge_dir, z_board_bottom=0.0):
    """A 3-row right-angle header on the back: insulator and mating pins toward edge_dir (-1: toward
    y = 0, +1: toward the notch edge). pads: the footprint's pads in the spec frame. The row nearest
    the edge bends lowest... closest to the board (the stock GND row, spec 1.6)."""
    xs = sorted({round(p[0], 3) for p in pads})
    ys = sorted({round(p[1], 3) for p in pads})
    edge_row = min(ys) if edge_dir < 0 else max(ys)
    x0, x1 = xs[0] - RA["pitch"] / 2, xs[-1] + RA["pitch"] / 2
    zt = z_board_bottom - RA["standoff"]
    zb = zt - 3 * RA["pitch"]
    if edge_dir < 0:
        yi1 = edge_row - RA["bend_gap"]; yi0 = yi1 - RA["insulator"]; ypin = yi0 - RA["pin_out"]
        ins = box(x0, yi0, zb, x1, yi1, zt)
        face = yi0
    else:
        yi0 = edge_row + RA["bend_gap"]; yi1 = yi0 + RA["insulator"]; ypin = yi1 + RA["pin_out"]
        ins = box(x0, yi0, zb, x1, yi1, zt)
        face = yi1
    solids = [ins]
    for x in xs:
        for k in range(3):
            zc = zt - RA["pitch"] * (k + 0.5)
            h = RA["pin"] / 2
            ya, yb = (ypin, yi0) if edge_dir < 0 else (yi1, ypin)
            solids.append(box(x - h, ya, zc - h, x + h, yb, zc + h))
    plugs = []
    for x in xs:
        w = SERVO_PLUG["width"] / 2
        if edge_dir < 0:
            plugs.append(box(x - w, face - SERVO_PLUG["length"], zb, x + w, face, zt))
        else:
            plugs.append(box(x - w, face, zb, x + w, face + SERVO_PLUG["length"], zt))
    return solids, plugs, (zb, face)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    brief = json.loads((ROOT / "boards" / "whisker" / "board.json").read_text())
    ox, oy = brief["board"]["origin"]
    fh = brief["board"]["height"]
    # brief (bx, by) -> spec (bx, fh - by); STEP (X, Y) -> spec (X - ox, Y + oy + fh)
    T_STEP = cq.Vector(-ox, oy + fh, 0)

    log("reading", STEP)
    parts, unnamed = read_step(STEP)
    pcb_candidates = sorted(unnamed, key=lambda s: -(bbox(s)[3] - bbox(s)[0]) * (bbox(s)[4] - bbox(s)[1]))
    pcb = pcb_candidates[0].translate(T_STEP)
    parts = {k: [s.translate(T_STEP) for s in v] for k, v in parts.items()}
    px0, py0, pz0, px1, py1, pz1 = bbox(pcb)
    log("board body %.2f x %.2f, z %.2f..%.2f; %d named parts in the STEP" % (px1 - px0, py1 - py0, pz0, pz1, len(parts)))
    z_bot, z_top = pz0, pz1

    log("reading footprints through pcbnew")
    fps = board_facts()

    def spec_xy(x, y):
        return (x - ox, fh - (y - oy))

    by_ref = {f["ref"]: f for f in fps}
    holes = [spec_xy(f["x"], f["y"]) for f in fps if f["fpid"].startswith("MountingHole:")]
    holes.sort()
    hx = [h[0] for h in holes]; hy = [h[1] for h in holes]
    xc, yc = (min(hx) + max(hx)) / 2, (min(hy) + max(hy)) / 2
    pattern = (max(hx) - min(hx), max(hy) - min(hy))
    log("holes", holes, "pattern %.2f x %.2f" % pattern)

    # ---------------------------------------------------------------- what the board carries
    bodies = {}     # ref -> list of solids (spec frame), what is fitted
    source = {}     # ref -> "step" | "envelope: ..." | "right-angle envelope"
    plugs = {}      # mated plugs and cables: keep-outs, not parts of the board
    servo = []
    for f in fps:
        ref = f["ref"]
        if f["fpid"].startswith("MountingHole:"):
            continue
        if f["fpid"] in SERVO_REFS_FP and f["side"] == "bottom" and ref.startswith("J"):
            pads = [spec_xy(p["x"], p["y"]) for p in f["pads"]]
            mid = sum(p[1] for p in pads) / len(pads)
            edge_dir = -1 if mid < yc else 1
            solids, pl, (zb, face) = ra_header(pads, edge_dir, z_bot)
            bodies[ref] = solids
            plugs[ref + " plugs"] = pl
            source[ref] = "right-angle envelope (replaces the land's straight-header model)"
            servo.append(ref)
            continue
        if ref in parts:
            bodies[ref] = parts[ref]
            source[ref] = "step"
            continue
        h = ENVELOPE_H.get(f["fpid"])
        bx = f["fab"] or f["courtyard"]
        if h is None or bx is None:
            source[ref] = "no body (no STEP model, no envelope)"
            continue
        (sx0, sy1), (sx1, sy0) = spec_xy(bx[0], bx[1]), spec_xy(bx[2], bx[3])
        if f["side"] == "top":
            bodies[ref] = [box(sx0, sy0, z_top, sx1, sy1, z_top + h[0])]
        else:
            bodies[ref] = [box(sx0, sy0, z_bot - h[0], sx1, sy1, z_bot)]
        source[ref] = "envelope %.2f mm: %s" % h

    # the battery socket's mated plug and cable, below its housing
    bat = [f for f in fps if f["fpid"].startswith("Connector_JST:JST_XH") and f["side"] == "bottom"][0]
    bat_pads = [spec_xy(p["x"], p["y"]) for p in bat["pads"]]
    bxs = [p[0] for p in bat_pads]; bys = [p[1] for p in bat_pads]
    bbat = bbox(compound(bodies[bat["ref"]]))
    housing_bottom = bbat[2]
    hx0, hy0, hx1, hy1 = bbat[0], bbat[1], bbat[3], bbat[4]
    plug_bottom = housing_bottom - XH_PLUG["below_housing"]
    cx, cy = (min(bxs) + max(bxs)) / 2, (min(bys) + max(bys)) / 2
    plugs[bat["ref"] + " plug+cable"] = [
        box(hx0, hy0, plug_bottom, hx1, hy1, housing_bottom),
        box(cx - XH_PLUG["cable_w"] / 2, cy - XH_PLUG["cable_d"] / 2, XH_PLUG["cable_to"],
            cx + XH_PLUG["cable_w"] / 2, cy + XH_PLUG["cable_d"] / 2, plug_bottom)]

    # ---------------------------------------------------------------- the cradle
    # the floor clears every body on the back that lies over it, and the servo plugs (mated)
    # floor outline: the brief's board polygon, every edge moved FLOOR_INSET inward with sharp
    # corners, filleted after extrusion (offsetting the filleted board edge collapses its 0.5 mm
    # notch-corner fillet into a degenerate arc and leaves the STL open)
    poly = [(x, fh - y) for x, y in brief["board"]["shape"]["points"]]
    floor_pts = inset_polygon(poly, FLOOR_INSET)
    lowest = 0.0
    lowest_ref = None
    for ref, sols in list(bodies.items()) + list(plugs.items()):
        if "plug+cable" in ref:
            continue
        for s in sols:
            b = bbox(s)
            if b[2] < lowest and b[2] < z_bot - 0.05:
                lowest, lowest_ref = b[2], ref
    floor_top = math.floor((lowest - CLEAR_BELOW) * 2) / 2.0
    floor_bot = floor_top - FLOOR_T
    log("lowest body over the floor: %s at z %.2f -> floor top %.2f" % (lowest_ref, lowest, floor_top))

    floor = cq.Workplane("XY").polyline(floor_pts).close().extrude(FLOOR_T).edges("|Z").fillet(1.0) \
        .translate((0, 0, floor_bot))
    # opening under the battery socket for its plug and cable
    ow = (hx1 - hx0) + 2 * PLUG_GAP
    oh = (hy1 - hy0) + 2 * PLUG_GAP
    opening = cq.Workplane("XY").center((hx0 + hx1) / 2, (hy0 + hy1) / 2).rect(ow, oh).extrude(FLOOR_T + 2) \
        .edges("|Z").fillet(1.0).translate((0, 0, floor_bot - 1))
    # a window under the PCA9685 / centre column, clear of the servo groups (saves 4 g of plastic)
    groups = [bbox(compound(bodies[r])) for r in servo]
    gx_left = max(g[3] for g in groups if g[0] < xc)
    gx_right = min(g[0] for g in groups if g[0] > xc)
    win = cq.Workplane("XY").center((gx_left + gx_right) / 2, yc + 3.0).rect(gx_right - gx_left - 6.0, 12.0) \
        .extrude(FLOOR_T + 2).edges("|Z").fillet(3.0).translate((0, 0, floor_bot - 1))
    cradle = floor.cut(opening).cut(win)
    # bosses under the holes, bored for the screw, counterbored under the floor
    for (x, y) in holes:
        boss = cq.Workplane("XY").center(x, y).circle(BOSS_OD / 2).extrude(z_bot - floor_bot).translate((0, 0, floor_bot))
        cradle = cradle.union(boss)
    for (x, y) in holes:
        bore = cq.Workplane("XY").center(x, y).circle(BOSS_BORE / 2).extrude(z_bot - floor_bot + 2).translate((0, 0, floor_bot - 1))
        head = cq.Workplane("XY").center(x, y).circle(HEAD_BORE / 2).extrude(HEAD_DEPTH + 1).translate((0, 0, floor_bot - 1))
        cradle = cradle.cut(bore).cut(head)
    # stiffening lips along the two short ends, where no servo plug leaves and nothing hangs low
    fx0, fy0, _, fx1, fy1, _ = bbox(floor.val())
    for xa in (fx0, fx1 - LIP_W):
        lip = cq.Workplane("XY").box(LIP_W, (fy1 - fy0) - 2 * (BOSS_OD / 2 + 3.6), LIP_H, centered=False) \
            .translate((xa, fy0 + BOSS_OD / 2 + 3.6, floor_top))
        cradle = cradle.union(lip)
    cradle_solid = cradle.val()
    log("cradle volume %.1f mm3" % cradle_solid.Volume())

    # ---------------------------------------------------------------- the Nybble frame around it
    def rail(sign):
        """One y1 rail on the board's top face: the narrow middle strip, a boss at each hole cut at
        the reach line, the 1.4 mm pilot holes."""
        z0, z1 = z_top, z_top + FRAME["rail_t"]
        ys = sorted([yc + sign * FRAME["rail_inner"], yc + sign * FRAME["rail_outer"]])
        body = box(xc - FRAME["rail_half_len"], ys[0], z0, xc + FRAME["rail_half_len"], ys[1], z1)
        r = FRAME["boss_r"]
        reach = yc + sign * FRAME["boss_reach"]
        inner = yc + sign * FRAME["rail_inner"]
        for Z in FRAME["holes_Z"]:
            hx_, hy_ = xc + Z, yc + sign * FRAME["hole_X"]
            bridge = box(hx_ - r, min(reach, inner), z0, hx_ + r, max(reach, inner), z1)
            disc = cq.Solid.makeCylinder(r, FRAME["rail_t"], cq.Vector(hx_, hy_, z0))
            clip = box(hx_ - r - 1, reach, z0 - 1, hx_ + r + 1, reach + 20, z1 + 1) if sign > 0 else \
                box(hx_ - r - 1, reach - 20, z0 - 1, hx_ + r + 1, reach, z1 + 1)
            body = body.fuse(bridge).fuse(disc.intersect(clip))
            body = body.cut(cq.Solid.makeCylinder(FRAME["pilot_d"] / 2, FRAME["rail_t"] + 2, cq.Vector(hx_, hy_, z0 - 1)))
        return body.clean()

    rails = [rail(+1), rail(-1)]
    belly_top = z_top - FRAME["belly_gap"] + 0.0
    belly = box(xc - 45, yc - 40, belly_top - FRAME["belly_t"], xc + 45, yc + 40, belly_top)

    # ---------------------------------------------------------------- fit check
    log("fit check")
    res = {"board_ref_frame": "spec board frame (spec section 0): x toward the head, y toward the notch, z up, "
                              "board bottom face z = %.2f, top z = %.2f (the STEP's board body)" % (z_bot, z_top)}
    # cradle against every body the board carries below its bottom face (back-side parts, the
    # pin tails of through-hole parts on top); a top-side part sits 1.5 mm of board above the boss
    below = {ref: [s for s in sols if bbox(s)[2] < z_bot - 1e-3] for ref, sols in bodies.items()}
    below = {r: v for r, v in below.items() if v}
    rows = sorted((min(dist(cradle_solid, s) for s in sols), ref) for ref, sols in below.items())
    inter = []
    for d, ref in rows:
        if d < 1e-6:
            v = sum(common_volume(cradle_solid, s) for s in below[ref])
            inter.append({"ref": ref, "volume_mm3": round(v, 4)})
    res["cradle_to_parts"] = {"scope": "every body reaching below the board (%d parts)" % len(below),
                              "min_mm": round(rows[0][0], 3),
                              "nearest": [{"ref": r, "mm": round(d, 3), "body": source[r]} for d, r in rows[:6]],
                              "interferences": inter}
    # cradle against the mated plugs and the battery cable
    prow = sorted((min(dist(cradle_solid, s) for s in sols), name) for name, sols in plugs.items())
    res["cradle_to_plugs"] = {"min_mm": round(prow[0][0], 3), "nearest": [{"what": n, "mm": round(d, 3)} for d, n in prow[:4]]}
    # cradle against the board body: contact only at the four boss seats
    d_pcb = dist(cradle_solid, pcb)
    v_pcb = common_volume(cradle_solid, pcb)
    seat_area = 4 * math.pi * ((BOSS_OD / 2) ** 2 - (BOSS_BORE / 2) ** 2)
    res["cradle_to_board_body"] = {"min_mm": round(d_pcb, 3), "common_volume_mm3": round(v_pcb, 4),
                                   "contact": "the four boss tops seat on the board's bottom face at the holes "
                                              "(designed contact, %.0f mm2 of seat)" % seat_area,
                                   "floor_top_below_board_mm": round(z_bot - floor_top, 3)}
    # the frame: rails against the board's parts (the rails seat on the board top at their bosses)
    rail_c = compound(rails)
    above = {ref: [s for s in sols if bbox(s)[5] > z_top + 1e-3] for ref, sols in bodies.items()}
    above = {r: v for r, v in above.items() if v}
    rrows = sorted((min(dist(rail_c, s) for s in sols), ref) for ref, sols in above.items())
    res["rails_to_parts"] = {"scope": "every body standing above the board (%d parts)" % len(above),
                             "min_mm": round(rrows[0][0], 3),
                             "nearest": [{"ref": r, "mm": round(d, 3), "body": source[r]} for d, r in rrows[:6]],
                             "seat_diameter_mm": FRAME["boss_r"] * 2,
                             "nearest_plan_distance_to_a_hole_centre_mm": {
                                 r: round(plan_distance_to_holes(above[r], holes), 2) for d, r in rrows[:6]}}
    res["rails_to_board_body"] = {"min_mm": round(dist(rail_c, pcb), 3),
                                  "contact": "rail bosses on the board's top face at the holes (designed)"}
    res["rails_to_cradle_mm"] = round(dist(rail_c, cradle_solid), 3)
    lowest_all = min(bbox(s)[2] for sols in bodies.values() for s in sols)
    res["belly"] = {"cradle_mm": round(dist(cradle_solid, belly), 3),
                    "lowest_board_part_mm": round(min(dist(belly, s) for sols in bodies.values() for s in sols), 3),
                    "battery_plug_housing_mm": round(dist(belly, plugs[bat["ref"] + " plug+cable"][0]), 3),
                    "note": "belly rebuilt from the spec's 18.4 mm gap (shared-frame assumption); the battery cable column is a keep-out drawn down through the floor opening toward the pack, not a measured part"}
    res["cradle"] = {"floor_top_z": floor_top, "floor_bottom_z": floor_bot, "boss_od": BOSS_OD, "boss_bore": BOSS_BORE,
                     "volume_mm3": round(cradle_solid.Volume(), 1),
                     "mass_g_petg": round(cradle_solid.Volume() * 1.27e-3, 1),
                     "screw": "M2 self-tapping, about %.0f mm (boss %.1f + board %.2f + rail %.1f), or Petoi screw B"
                              % (math.ceil((z_bot - floor_bot - HEAD_DEPTH) + (z_top - z_bot) + FRAME["rail_t"] - 0.5),
                                 z_bot - floor_bot - HEAD_DEPTH, z_top - z_bot, FRAME["rail_t"])}
    res["holes"] = {"spec_frame": [[round(a, 3), round(b, 3)] for a, b in holes],
                    "pattern_mm": [round(pattern[0], 3), round(pattern[1], 3)]}
    res["bodies"] = {"from_step": sorted(r for r in source if source[r] == "step"),
                     "envelopes": {r: source[r] for r in sorted(source) if source[r].startswith("envelope")},
                     "right_angle_servo_headers": sorted(servo),
                     "without_body": sorted(r for r in source if source[r].startswith("no body"))}
    res["assumed"] = {
        "right_angle_header": RA, "servo_plug": SERVO_PLUG, "battery_plug": XH_PLUG,
        "frame": dict(FRAME, note="rails and belly rebuilt from spec 2.4, 2.6 and 5 (STL measurements, not "
                                  "the STLs, which carry no licence); rail ends wider than 34.5 mm are not modelled; "
                                  "boss = 7 mm disc at the hole plus a bridge to the rail, cut at X = 22.5; the belly "
                                  "gap rests on the spec's shared-frame assumption"),
        "board_top": "the rails seat on the STEP's board body top (z %.2f); KiCad seats parts at 1.59" % z_top,
        "robot_frame": "X = y - %.2f, Z = x - %.2f, Y = z - %.2f - 3" % (yc, xc, z_top),
    }
    (OUT / "fit.json").write_text(json.dumps(res, indent=2))
    log(json.dumps({k: res[k] for k in ("cradle_to_parts", "cradle_to_plugs", "cradle_to_board_body", "rails_to_parts",
                                        "rails_to_cradle_mm", "belly")}, indent=1))

    # ---------------------------------------------------------------- exports
    cq.exporters.export(cq.Workplane().add(cradle_solid), str(OUT / "whisker_cradle.step"))
    cq.exporters.export(cq.Workplane().add(cradle_solid), str(OUT / "whisker_cradle.stl"), tolerance=0.02,
                        angularTolerance=0.1)
    import trimesh
    m = trimesh.load(str(OUT / "whisker_cradle.stl"))
    res["cradle"]["stl"] = {"watertight": bool(m.is_watertight), "faces": int(len(m.faces)),
                            "volume_mm3": round(float(m.volume), 1), "bounds_mm": [[round(v, 2) for v in r] for r in m.bounds]}
    (OUT / "fit.json").write_text(json.dumps(res, indent=2))
    log("stl watertight", m.is_watertight, "volume", round(float(m.volume), 1))

    # assembly for the previews: the board body, every fitted body, the plugs, the cradle, the rails
    asm_board = compound([pcb] + [s for sols in bodies.values() for s in sols])
    cq.exporters.export(cq.Workplane().add(compound([cradle_solid, asm_board])), str(OUT / "whisker_with_cradle.step"))
    previews(cradle_solid, asm_board, compound([s for v in plugs.values() for s in v]), rail_c)
    log("wrote", OUT)


def camera(shape, eye, up=(0.0, 0.0, 1.0)):
    """The shape turned so that looking down +Z shows it from `eye` with `up` up. CadQuery's SVG
    projector picks its own in-plane axis for an arbitrary direction, so the turn is done here and
    the projection is always along +Z (picture right = camera X, picture up = camera Y)."""
    import numpy as np
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf
    f = np.array(eye, float); f /= np.linalg.norm(f)
    r = np.cross(up, f); r /= np.linalg.norm(r)
    t = gp_Trsf()
    t.SetTransformation(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(*f), gp_Dir(*r)))
    return cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, t, True).Shape())


def svg(shape, path: Path, eye, w=900, h=640, up=(0.0, 0.0, 1.0)):
    cq.exporters.export(cq.Workplane().add(camera(shape, eye, up)), str(path), opt={
        "width": w, "height": h, "marginLeft": 20, "marginTop": 20, "projectionDir": (0, 0, 1),
        "showAxes": False, "showHidden": False, "strokeWidth": 0.25, "strokeColor": (40, 40, 48),
        "hiddenColor": (180, 180, 190)})
    png = path.with_suffix(".png")
    subprocess.run(["rsvg-convert", "-b", "white", "-w", str(w * 2), str(path), "-o", str(png)], check=True)
    return png


def previews(cradle, board, plugs, rails):
    log("previews")
    svg(cradle, OUT / "cradle_iso.svg", (1.0, -1.3, 1.0))
    svg(cradle, OUT / "cradle_top.svg", (0, 0, 1), up=(0, 1, 0))
    svg(compound([cradle, board]), OUT / "board_on_cradle_iso.svg", (1.0, -1.3, 0.8))
    svg(compound([cradle, board]), OUT / "board_on_cradle_under.svg", (0.9, -1.2, -0.9))
    svg(compound([cradle, board, plugs]), OUT / "fit_side.svg", (0, -1, 0), w=1100, h=420)
    svg(compound([cradle, board, plugs, rails]), OUT / "fit_end.svg", (1, 0, 0), w=1100, h=420)
    svg(compound([cradle, board, rails]), OUT / "fit_iso.svg", (-1.0, -1.4, 0.9))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", help="another STEP of this board (default build/whisker/fab/whisker.step)")
    ap.add_argument("--pcb", help="another .kicad_pcb (default build/whisker/whisker.kicad_pcb)")
    ap.add_argument("--out", help="output folder (default build/whisker/cad)")
    a = ap.parse_args()
    STEP = Path(a.step) if a.step else STEP
    PCB = Path(a.pcb) if a.pcb else PCB
    OUT = Path(a.out) if a.out else OUT
    main()
