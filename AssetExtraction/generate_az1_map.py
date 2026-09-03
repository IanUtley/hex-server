#!/usr/bin/env python3
"""Generate a top-down AZ1 node map from the original Unity prefab.

The map uses the exact local transforms from the ``campaign/az01/nodes``
GameObject in ``resources.assets``.  It is a 2-D x/z projection: Unity y
coordinates are retained in the SVG tooltips but are not used for placement.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import UnityPy
from PIL import Image, ImageDraw, ImageFont


DEFAULT_CLIENT_ROOT = Path(
    "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE"
)
PREFAB_PATH_ID = 3731  # campaign/az01/nodes


@dataclass(frozen=True)
class Transform:
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]


@dataclass(frozen=True)
class Node:
    node_id: str
    title: str
    terrain: str
    position: tuple[float, float, float]


def vec(value) -> tuple[float, float, float]:
    return (float(value.x), float(value.y), float(value.z))


def quat(value) -> tuple[float, float, float, float]:
    return (float(value.x), float(value.y), float(value.z), float(value.w))


def component(go, type_name: str):
    for pair in go.m_Component:
        obj = pair.component.deref()
        if obj is not None and obj.type.name == type_name:
            return obj
    return None


def transform_of(go) -> object:
    obj = component(go, "Transform")
    if obj is None:
        raise ValueError(f"GameObject {go.m_Name!r} has no Transform")
    return obj.read()


def local_transform(transform) -> Transform:
    return Transform(
        vec(transform.m_LocalPosition),
        quat(transform.m_LocalRotation),
        vec(transform.m_LocalScale),
    )


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def qrotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    t = (
        2 * (y * vz - z * vy),
        2 * (z * vx - x * vz),
        2 * (x * vy - y * vx),
    )
    return (
        vx + w * t[0] + (y * t[2] - z * t[1]),
        vy + w * t[1] + (z * t[0] - x * t[2]),
        vz + w * t[2] + (x * t[1] - y * t[0]),
    )


def vmul(a, b):
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def vadd(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def combine(parent: Transform, child: Transform) -> Transform:
    scaled = vmul(child.position, parent.scale)
    return Transform(
        vadd(parent.position, qrotate(parent.rotation, scaled)),
        qmul(parent.rotation, child.rotation),
        vmul(parent.scale, child.scale),
    )


def world_transform(parent: Transform, transform) -> Transform:
    return combine(parent, local_transform(transform))


def game_object(transform):
    return transform.m_GameObject.deref().read()


def scene_labels(records_path: Path) -> dict[str, tuple[str, str]]:
    labels = {}
    for line_number, line in enumerate(records_path.open(encoding="utf-8"), 1):
        if line_number == 1:
            continue  # section marker
        outer = json.loads(line)
        record = json.loads(outer) if isinstance(outer, str) else outer
        if record.get("m_Name") != "Howling Plains":
            continue
        for item in record.get("m_ItemData", []):
            node_id = item.get("m_MapNodeId")
            if node_id:
                labels[str(node_id)] = (
                    str(item.get("m_Title") or item.get("m_Name") or node_id),
                    str(item.get("m_TerrainType") or "Other"),
                )
        break
    return labels


def extract(resources_path: Path, records_path: Path):
    env = UnityPy.load(str(resources_path))
    prefab = next((obj for obj in env.objects if obj.path_id == PREFAB_PATH_ID), None)
    if prefab is None or prefab.type.name != "GameObject":
        raise RuntimeError(f"Could not find AZ1 nodes prefab path ID {PREFAB_PATH_ID}")

    root_go = prefab.read()
    root_transform = transform_of(root_go)
    root_world = local_transform(root_transform)
    labels = scene_labels(records_path)
    node_transforms = {}
    paths_transform = None

    for child_ptr in root_transform.m_Children:
        child_transform = child_ptr.deref().read()
        child_go = game_object(child_transform)
        child_name = str(child_go.m_Name)
        child_world = world_transform(root_world, child_transform)
        if child_name == "Paths":
            paths_transform = child_transform
            continue
        if child_name.startswith("Node") or child_name.startswith("Fork"):
            node_transforms[child_name] = child_world

    nodes = {}
    for node_id, transform in node_transforms.items():
        title, terrain = labels.get(node_id, (node_id, "Other"))
        nodes[node_id] = Node(node_id, title, terrain, transform.position)

    if paths_transform is None:
        raise RuntimeError("AZ1 nodes prefab has no Paths child")
    paths_world = world_transform(root_world, paths_transform)
    paths = []
    path_pattern = re.compile(r"^Path_(.+)_(.+)$")
    for path_ptr in paths_transform.m_Children:
        path_transform = path_ptr.deref().read()
        path_go = game_object(path_transform)
        match = path_pattern.match(str(path_go.m_Name))
        if not match:
            continue
        start, end = match.groups()
        if start not in nodes and start not in node_transforms:
            continue
        if end not in nodes and end not in node_transforms:
            continue
        path_world = world_transform(paths_world, path_transform)
        points = [nodes.get(start, Node(start, start, "Other", path_world.position)).position]
        for point_ptr in path_transform.m_Children:
            point_transform = point_ptr.deref().read()
            point_go = game_object(point_transform)
            if str(point_go.m_Name).startswith("wp"):
                points.append(world_transform(path_world, point_transform).position)
        points.append(nodes.get(end, Node(end, end, "Other", path_world.position)).position)
        paths.append((str(path_go.m_Name), start, end, points))

    return nodes, paths


COLORS = {
    "Forest": "#4f8a62",
    "Shroom": "#9b65b7",
    "Camp": "#d39432",
    "Dungeon": "#b44e4e",
    "Canyon": "#bf7b3e",
    "Jungle": "#327f72",
    "River": "#3e86b9",
    "Island": "#4f72a6",
    "Bridge": "#8b6f47",
    "City": "#7667a5",
    "None": "#777777",
    "Other": "#777777",
}


def natural_key(value: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def make_projection(nodes, paths, width=2600, height=1900):
    points = [node.position for node in nodes.values()]
    for _, _, _, path_points in paths:
        points.extend(path_points)
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_z = min(point[2] for point in points)
    max_z = max(point[2] for point in points)
    plot = (80, 80, 1870, height - 150)
    scale = min((plot[2] - plot[0]) / (max_x - min_x),
                (plot[3] - plot[1]) / (max_z - min_z))
    used_w = (max_x - min_x) * scale
    used_h = (max_z - min_z) * scale
    left = plot[0] + ((plot[2] - plot[0]) - used_w) / 2
    top = plot[1] + ((plot[3] - plot[1]) - used_h) / 2

    def project(point):
        return (left + (point[0] - min_x) * scale,
                top + (max_z - point[2]) * scale)

    return project


def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def write_svg(output: Path, nodes, paths, project):
    width, height = 2600, 1900
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#101820"/>',
        '<rect x="30" y="30" width="1940" height="1810" rx="16" fill="#172630" stroke="#4d6570" stroke-width="2"/>',
        '<text x="60" y="70" fill="#f3f6f7" font-family="sans-serif" font-size="28" font-weight="bold">AZ1 — Howling Plains</text>',
        '<text x="60" y="100" fill="#aebfc6" font-family="sans-serif" font-size="15">Exact x/z layout from campaign/az01/nodes; Unity y is omitted from the projection.</text>',
    ]
    for path_name, start, end, points in paths:
        coords = " ".join(f"{project(point)[0]:.1f},{project(point)[1]:.1f}" for point in points)
        lines.append(f'<polyline points="{coords}" fill="none" stroke="#8aa1aa" stroke-opacity="0.58" stroke-width="3"/>')
    for node_id in sorted(nodes, key=natural_key):
        node = nodes[node_id]
        x, y = project(node.position)
        color = COLORS.get(node.terrain, COLORS["Other"])
        radius = 9 if node_id != "Node001" else 13
        title = html.escape(f"{node_id} — {node.title} ({node.terrain}); Unity {node.position}")
        lines.append(f'<g><title>{title}</title><circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" stroke="#ffffff" stroke-width="2"/>')
        lines.append(f'<text x="{x + 13:.1f}" y="{y + 4:.1f}" fill="#f3f6f7" font-family="sans-serif" font-size="12">{html.escape(node_id)}</text></g>')
    lines += [
        '<rect x="2010" y="30" width="560" height="1810" rx="16" fill="#172630" stroke="#4d6570" stroke-width="2"/>',
        '<text x="2040" y="75" fill="#f3f6f7" font-family="sans-serif" font-size="24" font-weight="bold">AZ1 node key</text>',
        '<text x="2040" y="103" fill="#aebfc6" font-family="sans-serif" font-size="14">Hover a node for its Unity coordinates.</text>',
    ]
    sorted_nodes = sorted(nodes.values(), key=lambda node: natural_key(node.node_id))
    col_x = [2040, 2220, 2400]
    for index, node in enumerate(sorted_nodes):
        column = index // 27
        row = index % 27
        x = col_x[column]
        y = 140 + row * 60
        color = COLORS.get(node.terrain, COLORS["Other"])
        lines.append(f'<circle cx="{x + 6}" cy="{y - 5}" r="6" fill="{color}"/>')
        lines.append(f'<text x="{x + 18}" y="{y}" fill="#f3f6f7" font-family="sans-serif" font-size="12" font-weight="bold">{html.escape(node.node_id)}</text>')
        title = html.escape(node.title[:25] + ("…" if len(node.title) > 25 else ""))
        lines.append(f'<text x="{x + 18}" y="{y + 16}" fill="#aebfc6" font-family="sans-serif" font-size="10">{title}</text>')
    lines.append('</svg>')
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_png(output: Path, nodes, paths, project):
    width, height = 2600, 1900
    image = Image.new("RGB", (width, height), "#101820")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((30, 30, 1970, 1840), radius=16, fill="#172630", outline="#4d6570", width=2)
    draw.rounded_rectangle((2010, 30, 2570, 1840), radius=16, fill="#172630", outline="#4d6570", width=2)
    draw.text((60, 45), "AZ1 — Howling Plains", fill="#f3f6f7", font=font(28, True))
    draw.text((60, 82), "Exact x/z layout from campaign/az01/nodes; Unity y omitted", fill="#aebfc6", font=font(15))
    for _, _, _, points in paths:
        draw.line([project(point) for point in points], fill="#8aa1aa", width=3, joint="curve")
    label_font = font(12)
    for node_id in sorted(nodes, key=natural_key):
        node = nodes[node_id]
        x, y = project(node.position)
        color = COLORS.get(node.terrain, COLORS["Other"])
        draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=color, outline="#ffffff", width=2)
        draw.text((x + 13, y - 7), node_id, fill="#f3f6f7", font=label_font)
    draw.text((2040, 45), "AZ1 node key", fill="#f3f6f7", font=font(24, True))
    draw.text((2040, 80), "Node IDs and authored titles", fill="#aebfc6", font=font(14))
    for index, node in enumerate(sorted(nodes.values(), key=lambda item: natural_key(item.node_id))):
        column, row = divmod(index, 27)
        x, y = [2040, 2220, 2400][column], 120 + row * 66
        color = COLORS.get(node.terrain, COLORS["Other"])
        draw.ellipse((x, y, x + 12, y + 12), fill=color)
        draw.text((x + 18, y - 3), node.node_id, fill="#f3f6f7", font=font(12, True))
        title = node.title[:25] + ("…" if len(node.title) > 25 else "")
        draw.text((x + 18, y + 17), title, fill="#aebfc6", font=font(10))
    image.save(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=DEFAULT_CLIENT_ROOT)
    parser.add_argument("--records", type=Path, default=Path("Records/SceneData.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("docs/az1-node-map"))
    args = parser.parse_args()
    resources = args.client_root / "Hex_Data" / "resources.assets"
    if not resources.exists():
        raise SystemExit(f"Missing Unity resources file: {resources}")
    nodes, paths = extract(resources, args.records)
    if len(nodes) < 70:
        raise SystemExit(f"Refusing to generate incomplete map: found only {len(nodes)} nodes")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    project = make_projection(nodes, paths)
    write_svg(args.output.with_suffix(".svg"), nodes, paths, project)
    write_png(args.output.with_suffix(".png"), nodes, paths, project)
    print(f"Generated {len(nodes)} nodes and {len(paths)} paths")
    print(args.output.with_suffix(".svg"))
    print(args.output.with_suffix(".png"))


if __name__ == "__main__":
    main()
