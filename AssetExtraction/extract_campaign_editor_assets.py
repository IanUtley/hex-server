#!/usr/bin/env python3
"""Extract campaign zone maps for the campaign editor.

Writes ``tools/campaign_editor/generated/``:

* ``zones.json`` - every editable zone (overworlds, dungeons, panoramas) with
  its Records template, client prefabs and the overworld node that leads to
  it (taken from the client's ``POI_MapIcons_*`` node markers);
* ``<zone>/map.json`` - node world positions, paths and background tiles in
  the zone's world x/z plane (all campaign maps are top-down);
* ``<zone>/tiles/*.jpg`` (dungeons) and ``terrain/tiles/*.jpg`` (the shared
  ``campaign/azmap/azmap`` continent used by both overworlds).

Each background tile is placed by a least-squares UV->world fit over its own
mesh (per submesh), so the art lands where the client renders it. Panorama
scenes are 2.5D bundle levels and are extracted separately. Output is
derived from the original client assets and is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, cast

import UnityPy  # pyright: ignore[reportMissingImports] -- optional extraction dependency

import campaign_effects as effects

from generate_az1_map import (DEFAULT_CLIENT_ROOT, Transform, combine, qrotate,
                              component, game_object, local_transform,
                              natural_key, transform_of, world_transform)

DEFAULT_OUTPUT = Path("tools/campaign_editor/generated")
WATER_RGB = (52, 98, 132)
WATER_OPACITY = 0.8

# id, Records template name, server campaign template, node prefab, scene
# name prefix in encounter_scenes.
OVERWORLDS = [
    ("AZ1", "Howling Plains", "AZ1", "campaign/az01/nodes", "AZ 1 - NODE"),
    ("AZ2", "The Alachian Sea", "AZ2", "campaign/az02/nodes", "AZ 2 - NODE"),
]
# zone id, Records DungeonTemplate name, prefab folder, client POI marker.
DUNGEONS = [
    ("crayburn", "Castle Crayburn", "campaign/az01/crayburncastle", None),
    ("devonshirekeep", "Devonshire Keep", "campaign/az01/devonshirekeep", "Devonshire"),
    ("fortromor", "Fort Romor", "campaign/az01/fortromor", "FortRomor"),
    ("smolderingdead", "The Smoldering Dead", "campaign/az01/smolderingdead", "SmolderingDead"),
    ("theusurper", "The Usurper", "campaign/az01/theusurper", "Usurper"),
    ("tranquildream", "The Tranquil Dream", "campaign/az01/tranquildream", "TranquilDream"),
    ("brutecrownbluff", "Brutecrown Bluff", "campaign/az02/brutecrownbluff", "BruteCrownBluff"),
    ("greatmachinegraveyard", "Great Machine Graveyard", "campaign/az02/greatmachinegraveyard", "GreatMachineGraveyard"),
    ("kraken", "The Kraken", "campaign/az02/kraken", "Kraken"),
    ("ruinsofkukatan", "Ruins of Kukatan", "campaign/az02/ruinsofkukatan", "RuinsOfKukatan"),
]
# zone id, title, Records template (if any), bundle, client POI marker.
PANORAMAS = [
    ("cavein", "The Cave-In", "The Cave-In", "adventurezone01/p_dwrf_cavein", "CaveIn"),
    ("amblingmesa", "Ambling Mesa", "Ambling Mesa", "adventurezone01/p_cytl_amblingmesa", "AmblingMesa"),
    ("thunderfield", "Thunderfield", "Thunderfield", "adventurezone01/p_cytl_thunderfield", "Thunderfield"),
    ("necropolis", "The Necropolis", "The Necropolis", "adventurezone01/p_ncrtc_necropolis", "Necropolis"),
    ("wakuna", "Wakuna", None, "adventurezone02/p_cytl_wakuna", "Wakuna"),
    ("naagaan", "Naagaan", None, "adventurezone02/p_ncrtc_naagaan", "Naagaan"),
]
# Race starter (AZ0 training) panoramas: race -> (title, Records template).
# The bundle and NPC anchors come from campaign.py ``_AZ0_RACE_CONFIG``.
TRAINING_TITLES = {
    "Human": "Wren's Citadel", "Elf": "Satyr's Roost", "Coyotle": "Thunderfield",
    "Orc": "Xamahuac", "Dwarf": "The Quarry", "Shin'hare": "Jinguru",
    "Vennen": "The Hatchery", "Necrotic": "The Necropolis",
}
RACE_IDS = {1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc", 5: "Dwarf",
            6: "Shin'hare", 7: "Vennen", 8: "Necrotic"}
# Dungeons whose battles are per-race scenes named ``<Race> Tutorial <node>``
# (the node is the Records item name without its ``NNN - `` prefix).
RACE_SCENES = {"crayburn": {"server_template": "Crayburn Castle",
                            "scene_regex": r"^(?P<race>.+?) Tutorial (?P<node>.+)$"}}
# Fort Romor battle names identify their opponents instead of map nodes. Its
# authored UIEventBeginEncounter links provide a node fallback for those scenes.
DUNGEON_SCENE_MATCH = {"fortromor": "items_with_event_fallback"}


def training_panoramas(campaign_py: Path) -> list[tuple[str, str, Any, str, str]]:
    """``(zone id, title, template, bundle, race)`` per AZ0 race, read from the
    ``_AZ0_RACE_CONFIG`` literal in campaign.py (without importing it)."""
    import ast
    tree = ast.parse(campaign_py.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and
                getattr(node.targets[0], "id", "") == "_AZ0_RACE_CONFIG"):
            config = ast.literal_eval(node.value)
            break
    else:
        raise SystemExit("_AZ0_RACE_CONFIG not found in campaign.py")
    result = []
    for race_id, entry in sorted(config.items()):
        race = RACE_IDS[race_id]
        title = TRAINING_TITLES.get(race, race)
        zone_id = "az0-" + re.sub(r"[^a-z]", "", race.lower())
        result.append((zone_id, f"AZ0 {race}: {title}", title, entry["bundle"], race))
    return result


NON_NODE = re.compile(r"^(Paths|Nodes|Audio.*|SoundGroup.*|DynamicSound.*|.*_FOW|.*NodeMask)$")
PATH_NAME = re.compile(r"^Path_(.+)_(.+)$")


# ----------------------------------------------------------------- geometry

def _solve3(rows, values):
    """Least squares for value = a*x + b*z + c (normal equations, 3x3)."""
    sums = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0] * 3
    for row, value in zip(rows, values):
        for i in range(3):
            rhs[i] += row[i] * value
            for j in range(3):
                sums[i][j] += row[i] * row[j]
    matrix = [sums[i] + [rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        if abs(matrix[col][col]) < 1e-12:
            raise ValueError("degenerate UV fit")
        for row in range(3):
            if row != col:
                factor = matrix[row][col] / matrix[col][col]
                for k in range(col, 4):
                    matrix[row][k] -= factor * matrix[col][k]
    return [matrix[i][3] / matrix[i][i] for i in range(3)]


def _mesh_handler(mesh):
    from UnityPy.helpers.MeshHelper import MeshHandler  # pyright: ignore[reportMissingImports]
    handler = MeshHandler(mesh)
    handler.process()
    return handler


def _mesh(mesh):
    handler = _mesh_handler(mesh)
    vertices = [tuple(float(c) for c in v[:3]) for v in handler.m_Vertices or ()]
    uvs = [tuple(float(c) for c in uv[:2]) for uv in handler.m_UV0 or ()]
    return vertices, uvs, handler


def _submesh_triangles(mesh, handler, vertex_count):
    """Triangles (vertex index triples) of each submesh, in material order."""
    indices = list(getattr(handler, "m_IndexBuffer", None) or [])
    result = []
    for sub in mesh.m_SubMeshes or ():
        stride = 2 if int(getattr(mesh, "m_IndexFormat", 0) or 0) == 0 else 4
        first = int(sub.firstByte) // stride
        part = indices[first:first + int(sub.indexCount)]
        result.append([tuple(part[i:i + 3]) for i in range(0, len(part) - 2, 3)
                       if all(v < vertex_count for v in part[i:i + 3])])
    return result


def _submesh_vertex_sets(mesh, handler, vertex_count):
    """Vertex indices used by each submesh (one textured quad per material)."""
    result = [sorted({v for tri in tris for v in tri})
              for tris in _submesh_triangles(mesh, handler, vertex_count)]
    return [ids for ids in result if ids] or [list(range(vertex_count))]


def _fit_tile(world, uvs, vertex_ids):
    rows = [(world[i][0], world[i][2], 1.0) for i in vertex_ids]
    u_fit = _solve3(rows, [uvs[i][0] for i in vertex_ids])
    v_fit = _solve3(rows, [uvs[i][1] for i in vertex_ids])
    xs = [world[i][0] for i in vertex_ids]
    zs = [world[i][2] for i in vertex_ids]
    return u_fit, v_fit, {"min_x": min(xs), "max_x": max(xs),
                          "min_z": min(zs), "max_z": max(zs)}


def _texture(material, name="_MainTex"):
    for key, env_tex in material.m_SavedProperties.m_TexEnvs:
        if key == name and env_tex.m_Texture.path_id:
            return env_tex.m_Texture.deref().read()
    return None


def _save_tile(texture, path: Path, size: int, water=None):
    from PIL import Image  # pyright: ignore[reportMissingImports]
    image = texture.image.convert("RGB")
    if water is not None:
        mask = water.image.getchannel("B")
        if mask.size != image.size:
            mask = mask.resize(image.size)
        mask = mask.point(lambda value: int(value * WATER_OPACITY))
        image = Image.composite(Image.new("RGB", image.size, WATER_RGB), image, mask)
    if size and max(image.size) > size:
        scale = size / max(image.size)
        image = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=85)


def _walk(transform, world, out, depth=0):
    out.append((depth, game_object(transform), transform, world))
    for child in transform.m_Children:
        child_transform = child.deref().read()
        _walk(child_transform, world_transform(world, child_transform), out, depth + 1)


def _world_points(world, local):
    return [combine(world, Transform(v, (0, 0, 0, 1), (1, 1, 1))).position
            for v in local]


# ----------------------------------------------------------------- assets

def resource_paths(client: Path) -> dict[str, int]:
    """``campaign/...`` resource path -> resources.assets path ID."""
    env = UnityPy.load(str(client / "Hex_Data" / "globalgamemanagers"))
    manager = next(o for o in env.objects if o.type.name == "ResourceManager").read()
    return {key: value.path_id for key, value in manager.m_Container
            if key.startswith("campaign/")}


def is_path_fork(go) -> bool:
    """``UIDungeonMapNodeData.IsPathFork`` on a node GameObject: a path
    junction the token passes through, not a location. The script lives in
    another assembly, so read its one serialized field (a bool, 4-byte
    aligned) from the end of the component's raw data."""
    for pair in go.m_Component:
        try:
            obj = pair.component.deref()
        except Exception:  # components in other files
            continue
        if obj is not None and obj.type.name == "MonoBehaviour":
            raw = obj.get_raw_data()
            if len(raw) == 36:          # m_GameObject, m_Enabled, m_Script, name, flag
                return bool(raw[-4])
    return False


def extract_nodes(by_id, path_id, node_ids=None):
    """Nodes (with their POI markers) and paths of a client nodes prefab."""
    root = cast(Any, transform_of(by_id[path_id].read()))
    walked = []
    _walk(root, local_transform(root), walked)
    names = {id(t): str(go.m_Name) for _d, go, t, _w in walked}
    nodes, raw_paths = {}, []
    for depth, go, transform, world in walked:
        name = str(go.m_Name)
        parent_name = ""
        if depth:
            parent_name = str(game_object(transform.m_Father.deref().read()).m_Name)
        container = depth == 1 or (depth == 2 and parent_name == "Nodes")
        if container and not NON_NODE.match(name) and (
                (node_ids and name in node_ids) or
                name.startswith(("Node", "Fork"))):
            pois = [str(game_object(c.deref().read()).m_Name)
                    for c in transform.m_Children]
            pois = [p for p in pois if p.startswith("POI_")]
            nodes[name] = {"id": name, "x": world.position[0],
                           "y": world.position[1], "z": world.position[2],
                           "poi": pois[0] if pois else ""}
            if is_path_fork(go):
                nodes[name]["fork"] = True
        match = PATH_NAME.match(name)
        if match and parent_name == "Paths":
            waypoints = []
            for child in transform.m_Children:
                child_transform = child.deref().read()
                if str(game_object(child_transform).m_Name).startswith("wp"):
                    waypoints.append(world_transform(world, child_transform).position)
            raw_paths.append((name, match.group(1), match.group(2), waypoints))
    del names
    paths = []
    for name, start, end, waypoints in sorted(raw_paths):
        if start not in nodes or end not in nodes:
            continue
        points = ([[nodes[start]["x"], nodes[start]["z"]]] +
                  [[p[0], p[2]] for p in waypoints] +
                  [[nodes[end]["x"], nodes[end]["z"]]])
        paths.append({"id": name, "from": start, "to": end, "points": points})
    return sorted(nodes.values(), key=lambda n: natural_key(n["id"])), paths


def extract_terrain(env, out_dir: Path, tile_size: int):
    """The shared continent (AZMap): diffuse + water mask per chunk."""
    az_map = next(o for o in env.objects if o.type.name == "GameObject"
                  and getattr(o.read(), "m_Name", "") == "AZMap")
    root = cast(Any, transform_of(az_map.read()))
    walked = []
    _walk(root, local_transform(root), walked)
    tiles = []
    for _depth, go, _transform, world in walked:
        if not str(go.m_Name).startswith("Mesh__"):
            continue
        mesh = component(go, "MeshFilter").read().m_Mesh.deref().read()
        local, uvs, _handler = _mesh(mesh)
        points = _world_points(world, local)
        u_fit, v_fit, bounds = _fit_tile(points, uvs, range(len(points)))
        material = component(go, "MeshRenderer").read().m_Materials[0].read()
        texture = _texture(material)
        file_name = f"{texture.m_Name}.jpg"
        _save_tile(texture, out_dir / "tiles" / file_name, tile_size,
                   water=_texture(material, "_WaterMask"))
        tiles.append({"id": str(go.m_Name).split(":", 1)[0],
                      "image": f"terrain/tiles/{file_name}",
                      "u_fit": u_fit, "v_fit": v_fit, "bounds": bounds})
    return sorted(tiles, key=lambda t: t["id"])


def extract_dungeon_effects(by_id, path_id, zone_id, out_dir: Path,
                            tile_size: int) -> list[dict[str, Any]]:
    """Particle emitters anywhere in a dungeon background prefab (drawn
    top-down over the map art by the editor)."""
    root = cast(Any, transform_of(by_id[path_id].read()))
    walked = []
    _walk(root, local_transform(root), walked)
    save = _texture_saver(by_id, out_dir, zone_id, tile_size)
    emitters = []
    for _depth, go, _transform, world in walked:
        emitter = _emitter(go, world, save)
        if emitter is not None:
            emitters.append(emitter)
    return emitters


def extract_dungeon_background(by_id, path_id, zone_id, out_dir: Path,
                               tile_size: int):
    """The ``*Map`` art of a dungeon background, one tile per textured quad.

    Simple maps are one ``DungeonMap`` mesh with a material per half; others
    (Kraken) layer several meshes that sample a shared ``DM_*`` atlas. Every
    ``DM_*``-textured submesh under the ``*Map`` object becomes a tile, drawn
    bottom-up by height and clipped to its own quad.
    """
    root = cast(Any, transform_of(by_id[path_id].read()))
    walked = []
    _walk(root, local_transform(root), walked)
    map_depth = None
    tiles = []
    for depth, go, _transform, world in walked:
        if map_depth is not None and depth <= map_depth:
            map_depth = None
        if map_depth is None:
            if str(go.m_Name).endswith("Map"):
                map_depth = depth
            else:
                continue
        filter_ = component(go, "MeshFilter")
        renderer = component(go, "MeshRenderer")
        if filter_ is None or renderer is None:
            continue
        mesh = filter_.read().m_Mesh.deref().read()
        local, uvs, handler = _mesh(mesh)
        if not local or len(uvs) != len(local):
            continue
        points = _world_points(world, local)
        materials = [m.read() for m in renderer.read().m_Materials if m.path_id]
        for index, triangles in enumerate(
                _submesh_triangles(mesh, handler, len(points))):
            vertex_ids = sorted({v for tri in triangles for v in tri})
            if index >= len(materials) or len(vertex_ids) < 3:
                continue
            texture = _texture(materials[index])
            # Map art: a DM_ material (Kraken) or a DM_ texture (Crayburn).
            if texture is None or not (
                    str(materials[index].m_Name).startswith("DM_") or
                    str(texture.m_Name).startswith("DM_")):
                continue
            try:
                u_fit, v_fit, bounds = _fit_tile(points, uvs, vertex_ids)
            except ValueError:
                continue  # not a top-down (x/z) quad
            file_name = f"{texture.m_Name}.jpg"
            if not (out_dir / "tiles" / file_name).exists():
                _save_tile(texture, out_dir / "tiles" / file_name, tile_size)
            height = sum(points[i][1] for i in vertex_ids) / len(vertex_ids)
            tiles.append({"id": f"{go.m_Name}:{index}",
                          "image": f"{zone_id}/tiles/{file_name}",
                          "u_fit": u_fit, "v_fit": v_fit, "bounds": bounds,
                          # Per-triangle [x, z, u, v]: atlas layers map each
                          # triangle to its own texture region, so one affine
                          # fit per layer is not enough.
                          "tris": [[[round(points[v][0], 3), round(points[v][2], 3),
                                     round(uvs[v][0], 5), round(uvs[v][1], 5)]
                                    for v in tri] for tri in triangles],
                          "order": height})
    return sorted(tiles, key=lambda tile: tile["order"])


# Unity's built-in Quad ("unity default resources", path ID 10210) is not in
# the bundles; many panorama cards use it. Its geometry is fixed.
BUILTIN_QUAD_ID = 10210
BUILTIN_QUAD = ([(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (-0.5, 0.5, 0.0),
                 (0.5, 0.5, 0.0)],
                [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)],
                [[(0, 3, 1), (3, 0, 2)]])


def _scripts(go) -> dict[str, Any]:
    """MonoBehaviour object readers on a GameObject, by script class name."""
    found = {}
    for pair in go.m_Component:
        try:
            obj = pair.component.deref()
            if obj is None or obj.type.name != "MonoBehaviour":
                continue
            found[obj.read().m_Script.read().m_ClassName] = obj
        except Exception:  # scripts in other files
            continue
    return found


def _texture_saver(by_id: dict[int, Any], out_dir: Path, zone_id: str,
                   tile_size: int):
    """``save(path_id) -> generated path`` for a file's Texture2D objects."""
    saved: dict[int, str | None] = {}

    def save(path_id: int) -> str | None:
        if not path_id:
            return None
        if path_id in saved:
            return saved[path_id]
        obj = by_id.get(path_id)
        saved[path_id] = None
        if obj is None or obj.type.name != "Texture2D":
            return None
        try:
            texture = obj.read()
            image = texture.image
        except Exception:
            return None
        if tile_size and max(image.size) > tile_size:
            scale = tile_size / max(image.size)
            image = image.resize((max(1, int(image.width * scale)),
                                  max(1, int(image.height * scale))))
        (out_dir / "textures").mkdir(parents=True, exist_ok=True)
        file_name = f"{texture.m_Name}_{abs(path_id) % 100000}.webp"
        image.save(out_dir / "textures" / file_name, quality=82)
        saved[path_id] = f"{zone_id}/textures/{file_name}"
        return saved[path_id]
    return save


def _emitter(go, world, save) -> dict[str, Any] | None:
    """A ParticleSystem on ``go`` as an emitter (see campaign_effects)."""
    system = component(go, "ParticleSystem")
    if system is None or not go.m_IsActive:
        return None
    renderer = component(go, "ParticleSystemRenderer")
    render_tree = renderer.read_typetree() if renderer is not None else None
    if render_tree is not None and not render_tree.get("m_Enabled", True):
        return None
    material = None
    for ref in (render_tree or {}).get("m_Materials") or ():
        if ref.get("m_FileID") == 0 and ref.get("m_PathID"):
            try:
                material = effects.material_info(
                    go.object_reader.assets_file.objects[ref["m_PathID"]].read(), save)
            except Exception:
                material = None
            break
    if material is None:
        return None    # distortion or untextured: nothing to draw
    emitter = effects.particle_system(system.read_typetree(), render_tree, material)
    emitter.update(name=str(go.m_Name), position=[round(c, 5) for c in world.position],
                   orientation=[round(c, 6) for c in world.rotation],
                   scale=[round(c, 6) for c in world.scale])
    return emitter


PANELS = ("left", "center", "right")


def _parallax(env) -> dict[str, Any] | None:
    """The level's ParallaxController settings (client ParallaxController.cs):
    the camera is fixed and three panel groups under ``PanoMeshRoot`` slide
    sideways by ``dRotateY / 360 * 2 * PanelBoundsX`` (root-local units),
    each wrapping by ``2 * PanelBoundsX`` past ``+-PanelBoundsX`` - near
    layers slide faster (parallax) and the scene loops (360 degrees)."""
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            script = obj.read().m_Script
            if not script or script.read().m_ClassName != "ParallaxController":
                continue
            tree = obj.read_typetree()
        except Exception:
            continue
        return {
            "root": tree["PanoMeshRoot"]["m_PathID"],
            "panels": {panel: {int(ref["m_PathID"] if isinstance(ref, dict) else ref)
                               for ref in tree[f"PanelObjects_{panel.title()}"]}
                       for panel in PANELS},
            "bounds_x": float(tree["PanelBoundsX"]),
            "rotate_y": float(tree["RotateY"]),
            "auto_rotate": bool(tree["AutoRotate"]),
            "auto_rotate_speed": float(tree["AutoRotateSpeed"]),
            "pan_speed": float(tree["InputPanSpeed"]),
        }
    return None


def extract_panorama(bundle_path: Path, zone_id: str, out_dir: Path,
                     tile_size: int) -> dict[str, Any]:
    """A panorama level: textured meshes baked to world space, its main
    camera, NPC anchor points (``<Npc>_Anchor`` / ``<Npc>Anchor``) and the
    parallax panel each belongs to (see ``_parallax``)."""
    env = UnityPy.load(str(bundle_path))
    meshes, npcs, camera, emitters = [], [], None, []
    lightning = None
    save = _texture_saver({o.path_id: o for o in env.objects}, out_dir, zone_id,
                          tile_size)
    parallax = _parallax(env)
    father: dict[int, int] = {}
    for obj in env.objects:
        if obj.type.name == "Transform":
            data = obj.read()
            if data.m_Father and data.m_Father.path_id:
                father[obj.path_id] = data.m_Father.path_id

    def panel_of(path_id: int) -> str:
        """The parallax panel a transform (or an ancestor) belongs to."""
        if not parallax:
            return ""
        seen = 0
        while path_id and seen < 256:
            for panel, members in parallax["panels"].items():
                if path_id in members:
                    return panel
            path_id = father.get(path_id, 0)
            seen += 1
        return ""
    root_world = None
    for obj in env.objects:
        if obj.type.name != "Transform":
            continue
        transform = obj.read()
        if transform.m_Father and transform.m_Father.path_id:
            continue
        walked = []
        _walk(transform, local_transform(transform), walked)
        for _depth, go, _t, world in walked:
            name = str(go.m_Name)
            path_id = _t.object_reader.path_id
            if parallax and path_id == parallax["root"]:
                root_world = world
            # ``<Npc>_Anchor``; The Quarry spells it ``NodeAAnchor``.
            if name.endswith("Anchor") and len(name) > len("Anchor"):
                npcs.append({"name": name[:-len("Anchor")].rstrip("_"),
                             "position": list(world.position),
                             "panel": panel_of(path_id)})
            scripts = _scripts(go)
            if "LightningController" in scripts:
                tree = scripts["LightningController"].read_typetree()
                lightning = {"rate": float(tree.get("m_FidgetRate", 10)),
                             "enabled": not tree.get("DisableFidget")}
            emitter = _emitter(go, world, save)
            if emitter is not None:
                emitter["panel"] = panel_of(path_id)
                emitters.append(emitter)
            anim = (effects.goanim(scripts["GoAnim"].read_typetree())
                    if "GoAnim" in scripts else None)
            cam = component(go, "Camera")
            if cam is not None and name == "Main Camera":
                data = cam.read()
                camera = {"position": list(world.position),
                          "rotation": list(world.rotation),
                          "fov": float(data.field_of_view),
                          "near": float(data.near_clip_plane),
                          "far": float(data.far_clip_plane)}
            filter_ = component(go, "MeshFilter")
            renderer = component(go, "MeshRenderer")
            if filter_ is None or renderer is None or not filter_.read().m_Mesh.path_id:
                continue
            if not renderer.read().m_Enabled or not go.m_IsActive:
                continue
            mesh_ref = filter_.read().m_Mesh
            if mesh_ref.file_id:
                if mesh_ref.path_id != BUILTIN_QUAD_ID:
                    continue
                local, uvs, submeshes = BUILTIN_QUAD
            else:
                mesh = mesh_ref.deref().read()
                local, uvs, handler = _mesh(mesh)
                if not local or len(uvs) != len(local):
                    continue
                submeshes = _submesh_triangles(mesh, handler, len(local))
            points = _world_points(world, local)
            materials = []
            for ref in renderer.read().m_Materials:
                try:
                    materials.append(ref.read() if ref.path_id else None)
                except FileNotFoundError:
                    materials.append(None)  # built-in material
            sorting = int(renderer.read_typetree().get("m_SortingOrder", 0))
            for index, triangles in enumerate(submeshes):
                if index >= len(materials) or not triangles:
                    continue
                material = materials[index]
                if material is None:
                    continue
                info = effects.material_info(material, save)
                if info is None:
                    continue
                used = sorted({v for tri in triangles for v in tri})
                remap = {v: i for i, v in enumerate(used)}
                entry = {
                    "name": f"{name}:{index}", **info, "panel": panel_of(path_id),
                    "order": sorting,
                    "positions": [round(c, 4) for v in used for c in points[v]],
                    "uvs": [round(c, 5) for v in used for c in uvs[v]],
                    "indices": [remap[v] for tri in triangles for v in tri]}
                if anim:
                    mine = dict(anim, materials=[a for a in anim["materials"]
                                                 if a["index"] == index])
                    if mine["materials"] or mine["transforms"]:
                        entry["anim"] = mine
                        entry["pivot"] = {"position": list(world.position),
                                          "rotation": list(world.rotation),
                                          "scale": list(world.scale),
                                          "local_scale": list(local_transform(_t).scale)}
                if info.get("wave"):
                    entry["pivot"] = {"position": list(world.position),
                                      "rotation": list(world.rotation),
                                      "scale": list(world.scale),
                                      "local_scale": list(local_transform(_t).scale)}
                meshes.append(entry)
    scene = {"camera": camera, "meshes": meshes, "npcs": npcs,
             "emitters": emitters}
    if lightning and lightning["enabled"]:
        scene["lightning"] = lightning
    if parallax and root_world is not None:
        # One root-local unit along x, in world space (the panels' slide axis).
        axis = qrotate(root_world.rotation, (root_world.scale[0], 0.0, 0.0))
        scene["parallax"] = {k: v for k, v in parallax.items()
                             if k not in ("root", "panels")}
        scene["parallax"]["axis"] = [round(c, 6) for c in axis]
    return scene


def records_node_ids(records_dir: Path, template: str) -> set[str]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from AssetExtraction.gamedata_seed import parse_record
    path = records_dir / "SceneData.jsonl"
    for index, line in enumerate(path.open(encoding="utf-8")):
        if index == 0:
            continue
        value = json.loads(line)
        record = parse_record(value if isinstance(value, str) else line) or {}
        if record.get("m_Name") == template and record.get("m_ItemData"):
            return {item.get("m_MapNodeId") for item in record["m_ItemData"]
                    if item.get("m_MapNodeId")}
    return set()


def _write_map(out: Path, zone_id: str, nodes, paths, tiles, emitters=None):
    (out / zone_id).mkdir(parents=True, exist_ok=True)
    data = {"zone": zone_id, "nodes": nodes, "paths": paths, "tiles": tiles}
    if emitters:
        data["emitters"] = emitters
    (out / zone_id / "map.json").write_text(json.dumps(data, indent=1) + "\n",
                                            encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=DEFAULT_CLIENT_ROOT)
    parser.add_argument("--records", type=Path, default=Path("Records"),
                        help="the Records directory")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tile-size", type=int, default=1024,
                        help="maximum tile edge in pixels (0 = keep)")
    parser.add_argument("--only", nargs="*", default=None,
                        help="re-extract only these zone ids (others keep "
                        "their generated files; zones.json is rewritten)")
    args = parser.parse_args()
    wanted = (lambda zone_id: True) if args.only is None else (
        lambda zone_id: zone_id in args.only)
    resources = args.client_root / "Hex_Data" / "resources.assets"
    if not resources.exists():
        raise SystemExit(f"Missing Unity resources file: {resources}")
    paths = resource_paths(args.client_root)
    env = UnityPy.load(str(resources))
    by_id = {o.path_id: o for o in env.objects}
    out = args.output
    zones = []

    terrain = (extract_terrain(env, out / "terrain", args.tile_size)
               if any(wanted(z[0]) for z in OVERWORLDS) else None)
    poi_owner = {}
    for zone_id, template, server_template, nodes_path, prefix in OVERWORLDS:
        nodes, path_list = extract_nodes(
            by_id, paths[nodes_path], records_node_ids(args.records, template))
        for node in nodes:
            if node["poi"]:
                poi_owner[node["poi"]] = (zone_id, node["id"])
        if wanted(zone_id):
            _write_map(out, zone_id, nodes, path_list, terrain)
        zones.append({"id": zone_id, "kind": "overworld", "title": template,
                      "records": template, "server_template": server_template,
                      "scene_prefix": prefix})
        print(f"{zone_id}: {len(nodes)} nodes, {len(path_list)} paths")

    def parent_of(marker):
        found = poi_owner.get(f"POI_MapIcons_{marker}") if marker else None
        return {"parent": found[0], "parent_node": found[1]} if found else {}

    training = training_panoramas(Path(__file__).resolve().parents[1] / "campaign.py")
    for zone_id, template, folder, marker in DUNGEONS:
        # Most dungeons link content to nodes by name; Fort Romor also reads
        # its authored encounter event links (import_dungeons.py, catalog.py).
        extra = dict(RACE_SCENES.get(zone_id) or
                     {"server_template": template,
                      "scene_match": DUNGEON_SCENE_MATCH.get(zone_id, "items")})
        if zone_id == "crayburn":
            # Each race enters Crayburn from its own starter panorama.
            extra["race_parents"] = {race: tid for tid, _t, _r, _b, race in training}
        zones.append({"id": zone_id, "kind": "dungeon", "title": template,
                      "records": template, **parent_of(marker), **extra})
        if not wanted(zone_id):
            continue
        nodes, path_list = extract_nodes(
            by_id, paths[f"{folder}/prefabs/nodes"],
            records_node_ids(args.records, template))
        tiles = extract_dungeon_background(
            by_id, paths[f"{folder}/prefabs/background"], zone_id,
            out / zone_id, args.tile_size)
        emitters = extract_dungeon_effects(
            by_id, paths[f"{folder}/prefabs/background"], zone_id,
            out / zone_id, args.tile_size)
        _write_map(out, zone_id, nodes, path_list, tiles, emitters)
        print(f"{zone_id}: {len(nodes)} nodes, {len(path_list)} paths, "
              f"{len(tiles)} tiles, {len(emitters)} emitters")

    bundles = args.client_root / "AssetBundles" / "pve"
    panoramas = [(*row, {}) for row in PANORAMAS] + [
        (zone_id, title, template, bundle, None,
         {"race": race, "server_template": "AZ0", "children": ["crayburn"]})
        for zone_id, title, template, bundle, race in training]
    for zone_id, title, template, bundle, marker, extra in panoramas:
        bundle_file = bundles / f"{bundle}.ab"
        zones.append({"id": zone_id, "kind": "panorama", "title": title,
                      "records": template, "bundle": bundle,
                      **parent_of(marker), **extra})
        if not wanted(zone_id):
            continue
        if bundle_file.exists():
            scene = extract_panorama(bundle_file, zone_id, out / zone_id,
                                     args.tile_size)
            (out / zone_id).mkdir(parents=True, exist_ok=True)
            (out / zone_id / "panorama.json").write_text(
                json.dumps(scene) + "\n", encoding="utf-8")
            print(f"{zone_id}: {len(scene['meshes'])} meshes, "
                  f"{len(scene['npcs'])} NPCs, camera={'yes' if scene['camera'] else 'no'}")
    (out / "zones.json").write_text(json.dumps(
        {"zones": zones, "poi": {k: list(v) for k, v in poi_owner.items()}},
        indent=1) + "\n", encoding="utf-8")
    print(f"{len(zones)} zones -> {out / 'zones.json'}")


if __name__ == "__main__":
    main()
