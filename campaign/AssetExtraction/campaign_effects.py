"""Zone effects for the campaign editor: particles, GoAnim and animated shaders.

Read from a level's Unity objects (panorama bundles, dungeon background
prefabs) into plain JSON the editor's ``effects.js`` simulates:

* ``ParticleSystem`` + ``ParticleSystemRenderer`` -> an emitter: the main,
  shape, emission, velocity/force/colour/size/rotation-over-lifetime and
  texture-sheet modules, with Unity's MinMaxCurve / MinMaxGradient values
  kept as data (``curve`` / ``gradient`` below);
* the client's ``GoAnim`` component (``hex-private/HexClient/.../GoAnim.cs``) -> its
  material (tint colour, texture offset/scale, float) and transform (scale,
  position, rotation) animation stacks;
* material shader parameters for the animated shaders (``Hex/Animated
  Grad`` family, vertex-animated flags, scrolling flow/distort textures).

Heat-haze distortion shaders refract the image behind them and have no
colour of their own; they are reported as ``blend: null`` and not drawn.
"""

from __future__ import annotations

from typing import Any, Callable

def shader_name(material) -> str:
    """The shader's display name (``m_ParsedForm.m_Name``; ``m_Name`` is
    empty for compiled shaders)."""
    shader = getattr(material, "m_Shader", None)
    if not shader or not shader.path_id:
        return ""
    try:
        tree = shader.deref().read_typetree()
    except Exception:  # external (built-in) shader
        return ""
    return str((tree.get("m_ParsedForm") or {}).get("m_Name") or tree.get("m_Name") or "")


def blend_of(shader: str) -> str | None:
    """``add``, ``premultiply``, ``alpha``, or ``None`` (not drawable:
    screen distortion and hidden image-effect shaders)."""
    name = shader.lower()
    if "distortion" in name or name.startswith("hidden/"):
        return None
    if "premultiply" in name:
        return "premultiply"
    if "additive" in name:
        return "add"
    return "alpha"


def _props(material) -> dict[str, Any]:
    tree = material.read_typetree() if hasattr(material, "read_typetree") else \
        material.object_reader.read_typetree()
    saved = tree.get("m_SavedProperties") or {}
    return {
        "tex": {k: v for k, v in saved.get("m_TexEnvs") or ()},
        "floats": {k: float(v) for k, v in saved.get("m_Floats") or ()},
        "colors": {k: [v["r"], v["g"], v["b"], v["a"]] for k, v in saved.get("m_Colors") or ()},
    }


def _pid(env_tex: dict[str, Any]) -> int:
    return int(((env_tex or {}).get("m_Texture") or {}).get("m_PathID") or 0)


def material_info(material, save_texture: Callable) -> dict[str, Any] | None:
    """How to draw a material: texture, blend, tint and animated-shader
    parameters. ``save_texture(path_id)`` saves a Texture2D of the same file
    and returns its generated path (``None`` for 0 / unreadable)."""
    shader = shader_name(material)
    blend = blend_of(shader)
    if blend is None:
        return None
    props = _props(material)
    main = props["tex"].get("_MainTex") or {}
    texture = save_texture(_pid(main)) if main else None
    info: dict[str, Any] = {"shader": shader, "blend": blend, "texture": texture}
    if main:
        scale, offset = main.get("m_Scale") or {}, main.get("m_Offset") or {}
        st = [scale.get("x", 1), scale.get("y", 1), offset.get("x", 0), offset.get("y", 0)]
        if st != [1, 1, 0, 0]:
            info["uv"] = [round(v, 5) for v in st]
    colors, floats = props["colors"], props["floats"]
    lower = shader.lower()
    if "_TintColor" in colors and "particles" in lower:
        # Legacy particle shaders: colour = 2 * tint * texture.
        info["tint"] = [round(c * 2, 4) for c in colors["_TintColor"]]
    elif "_Color" in colors and colors["_Color"] != [1, 1, 1, 1]:
        info["tint"] = [round(c, 4) for c in colors["_Color"]]
    if "animated" in lower and "grad" in lower:
        alpha = props["tex"].get("_AlphaTex") or {}
        info["grad"] = {
            "black": colors.get("_ReBlack", [0, 0, 0, 1]),
            "white": colors.get("_ReWhite", [1, 1, 1, 1]),
            "scroll": [floats.get("_ScrollU", 0), floats.get("_ScrollV", 0)],
            "scroll_alpha": [floats.get("_ScrollAlphaU", 0), floats.get("_ScrollAlphaV", 0)],
            "time": floats.get("_TimeMult", 1), "alpha": floats.get("_Alpha", 1),
            "alpha_texture": save_texture(_pid(alpha)) if alpha else None,
        }
        if texture is None and not info["grad"]["alpha_texture"]:
            info["texture"] = None   # a pure gradient (uses the UVs)
    if "vertexanim" in lower:
        mask = props["tex"].get("_AnimMask") or {}
        info["wave"] = {
            "speed": floats.get("_Speed", 0.05), "frequency": floats.get("_Frequency", 50),
            "intensity": floats.get("_Intensity", 0.1), "axis": floats.get("_AxisTurn", 0),
            "noise": floats.get("_Noise", 0),
            "mask": save_texture(_pid(mask)) if mask else None,
        }
    if "flow" in lower:
        info["scroll"] = [floats.get("_TexSpeed", 0.001) * 20, 0]
    if "distort" in lower and "_USpeed" in floats:
        # Hex_TransparentDiffuseDistort: the speeds scroll the *distortion*
        # texture, which wobbles the picture slightly; the picture stays put.
        distort = props["tex"].get("_DistortTex") or {}
        scale = distort.get("m_Scale") or {}
        info["distort"] = {
            "texture": save_texture(_pid(distort)) if distort else None,
            "tiling": [scale.get("x", 1), scale.get("y", 1)],
            "speed": [floats.get("_USpeed", 0), floats.get("_VSpeed", 0)],
            "intensity": floats.get("_DistortionIntensity", 0.01)}
    # Nothing to draw without a texture, unless it is a pure gradient.
    return info if texture or info.get("grad") else None


# --------------------------------------------------------------- curves

def curve(value: dict[str, Any] | None, scale: float = 1.0) -> dict[str, Any]:
    """Unity MinMaxCurve -> ``{"mode", "max", "min", "kmax", "kmin"}``.

    mode 0 constant (``max``), 1 curve (``max`` * kmax(t)), 2 random
    between two curves, 3 random between two constants (``min``..``max``).
    Keys are ``[time, value, inSlope, outSlope]``.
    """
    if not value:
        return {"mode": 0, "max": 0.0}

    def keys(c):
        return [[round(k["time"], 5), round(k["value"], 5),
                 _finite(k.get("inSlope")), _finite(k.get("outSlope"))]
                for k in (c or {}).get("m_Curve") or ()]
    result = {"mode": int(value.get("minMaxState", 0)),
              "max": round(float(value.get("scalar", 0)) * scale, 6),
              "min": round(float(value.get("minScalar", value.get("scalar", 0))) * scale, 6)}
    if result["mode"] in (1, 2):
        result["kmax"] = keys(value.get("maxCurve"))
        if result["mode"] == 2:
            result["kmin"] = keys(value.get("minCurve"))
    return result


def _finite(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(value, 5) if abs(value) < 1e6 else 0.0


def _rgba(c) -> list[float]:
    return [round(c.get("r", 1), 4), round(c.get("g", 1), 4),
            round(c.get("b", 1), 4), round(c.get("a", 1), 4)]


def _grad(g: dict[str, Any] | None) -> dict[str, Any] | None:
    if not g:
        return None
    colors = [[round(g.get(f"ctime{i}", 0) / 65535, 4), *_rgba(g.get(f"key{i}", {}))[:3]]
              for i in range(int(g.get("m_NumColorKeys", 2)))]
    alphas = [[round(g.get(f"atime{i}", 0) / 65535, 4), round(g.get(f"key{i}", {}).get("a", 1), 4)]
              for i in range(int(g.get("m_NumAlphaKeys", 2)))]
    return {"colors": colors, "alphas": alphas}


def gradient(value: dict[str, Any] | None) -> dict[str, Any]:
    """Unity MinMaxGradient -> mode 0 colour, 1 gradient, 2 random between
    two colours, 3 random between two gradients."""
    if not value:
        return {"mode": 0, "max": [1, 1, 1, 1]}
    result = {"mode": int(value.get("minMaxState", 0)),
              "max": _rgba(value.get("maxColor") or {}),
              "min": _rgba(value.get("minColor") or {})}
    if result["mode"] in (1, 3):
        result["gmax"] = _grad(value.get("maxGradient"))
        if result["mode"] == 3:
            result["gmin"] = _grad(value.get("minGradient"))
    return result


def _radius(shape: dict[str, Any]) -> float:
    radius = shape.get("radius")
    if isinstance(radius, dict):
        return float(radius.get("value", 1))
    return float(radius or 1)


def _arc(shape: dict[str, Any]) -> float:
    arc = shape.get("arc")
    if isinstance(arc, dict):
        return float(arc.get("value", 360))
    return float(arc or 360)


def particle_system(tree: dict[str, Any], renderer: dict[str, Any] | None,
                    material: dict[str, Any] | None) -> dict[str, Any]:
    """One ParticleSystem (typetree) as an emitter definition."""
    initial = tree.get("InitialModule") or {}
    shape = tree.get("ShapeModule") or {}
    emission = tree.get("EmissionModule") or {}
    result: dict[str, Any] = {
        "duration": float(tree.get("lengthInSec", 5)),
        "looping": bool(tree.get("looping", True)),
        "prewarm": bool(tree.get("prewarm", False)),
        "delay": curve(tree.get("startDelay")),
        "speed_scale": float(tree.get("simulationSpeed", 1) or 1),
        "world": int(tree.get("moveWithTransform", 0)) == 1,
        "scaling": int(tree.get("scalingMode", 0)),
        "max_particles": int(initial.get("maxNumParticles", 1000)),
        "lifetime": curve(initial.get("startLifetime")),
        "speed": curve(initial.get("startSpeed")),
        "size": curve(initial.get("startSize")),
        "rotation": curve(initial.get("startRotation")),
        "color": gradient(initial.get("startColor")),
        "gravity": float((initial.get("gravityModifier") or {}).get("scalar", 0)
                         if isinstance(initial.get("gravityModifier"), dict)
                         else initial.get("gravityModifier", 0) or 0),
        "random_rotation_direction": float(initial.get("randomizeRotationDirection", 0) or 0),
        "shape": {
            "enabled": bool(shape.get("enabled", True)),
            "type": int(shape.get("type", 4)),
            "radius": _radius(shape), "angle": float(shape.get("angle", 25)),
            "length": float(shape.get("length", 5)), "arc": _arc(shape),
            "box": [float(shape.get("boxX", 1)), float(shape.get("boxY", 1)),
                    float(shape.get("boxZ", 1))],
            "random_direction": float(shape.get("randomDirectionAmount",
                                                shape.get("randomDirection", 0)) or 0),
        },
        "rate": curve(emission.get("rateOverTime") or emission.get("rate")),
        "bursts": [{"time": float(b.get("time", 0)),
                    "min": int(b.get("minCount", (b.get("countCurve") or {}).get("scalar", 0))),
                    "max": int(b.get("maxCount", (b.get("countCurve") or {}).get("scalar", 0)))}
                   for b in (emission.get("m_Bursts") or ())] if emission.get("enabled", True) else [],
    }
    if not emission.get("enabled", True):
        result["rate"] = {"mode": 0, "max": 0.0}
    for module, key in (("VelocityModule", "velocity"), ("ForceModule", "force")):
        m = tree.get(module) or {}
        if m.get("enabled"):
            result[key] = {"x": curve(m.get("x")), "y": curve(m.get("y")),
                           "z": curve(m.get("z")), "world": bool(m.get("inWorldSpace"))}
    m = tree.get("ColorModule") or {}
    if m.get("enabled"):
        result["color_over_life"] = gradient(m.get("gradient"))
    m = tree.get("SizeModule") or {}
    if m.get("enabled"):
        result["size_over_life"] = curve(m.get("curve"))
    m = tree.get("RotationModule") or {}
    if m.get("enabled"):
        result["spin"] = curve(m.get("curve"))
    m = tree.get("UVModule") or {}
    if m.get("enabled"):
        result["sheet"] = {
            "tiles": [int(m.get("tilesX", 1)), int(m.get("tilesY", 1))],
            "frame": curve(m.get("frameOverTime")), "start": curve(m.get("startFrame")),
            "cycles": float(m.get("cycles", 1)), "row_mode": int(m.get("animationType", 0)),
            "row": int(m.get("rowIndex", 0)), "random_row": bool(m.get("randomRow", True)),
        }
    if renderer:
        result["render"] = {
            "mode": int(renderer.get("m_RenderMode", 0)),
            "length_scale": float(renderer.get("m_LengthScale", 2)),
            "velocity_scale": float(renderer.get("m_VelocityScale", 0)),
            "max_size": float(renderer.get("m_MaxParticleSize", 0.5)),
            "order": int(renderer.get("m_SortingOrder", 0)),
        }
    if material:
        result["material"] = material
    return result


# --------------------------------------------------------------- GoAnim

def _v(value, n=3) -> list[float]:
    keys = "xyzw"[:n]
    return [round(float((value or {}).get(k, 0)), 5) for k in keys]


def goanim(tree: dict[str, Any]) -> dict[str, Any] | None:
    """The client's GoAnim component (GoAnim.cs) as data."""
    def timing(a):
        return {"delay": float(a.get("Delay", 0)),
                "interval": _v(a.get("Interval"), 2), "duration": _v(a.get("Duration"), 2),
                "loop": bool(a.get("Loop")), "max_plays": int(a.get("MaxPlayCount", 0)),
                "time": float(a.get("CurTime", 0)),
                "curve": [[round(k["time"], 5), round(k["value"], 5),
                           _finite(k.get("inSlope")), _finite(k.get("outSlope"))]
                          for k in (a.get("Interpolation") or {}).get("m_Curve") or ()]}
    materials = []
    for a in tree.get("AnimStack_Material") or ():
        entry = {**timing(a), "id": a.get("ValueId", ""), "index": int(a.get("MaterialIndex", 0)),
                 "type": ("float", "vector", "color", "texture")[int(a.get("ValueType", 0)) % 4]}
        if entry["type"] == "float":
            entry["values"] = [float(a.get("ValueFloat0", 0)), float(a.get("ValueFloat1", 0))]
        elif entry["type"] == "color":
            entry["values"] = [_rgba(a.get("ValueColor0") or {}), _rgba(a.get("ValueColor1") or {})]
        elif entry["type"] == "vector":
            entry["values"] = [_v(a.get("ValueVector0"), 4), _v(a.get("ValueVector1"), 4)]
        else:
            entry["offset"] = [_v(a.get("ValueTexOffset0"), 2), _v(a.get("ValueTexOffset1"), 2)]
            entry["scale"] = [_v(a.get("ValueTexScale0"), 2), _v(a.get("ValueTexScale1"), 2)]
        materials.append(entry)
    transforms = []
    for a in tree.get("AnimStack_Transform") or ():
        transforms.append({**timing(a), "local": bool(a.get("IsLocal", True)),
                           "random": bool(a.get("IsRandomRange")),
                           "random_type": int(a.get("RandomType", 0)),
                           "position": [_v(a.get("Position0")), _v(a.get("Position1"))],
                           "rotation": [_v(a.get("Rotation0")), _v(a.get("Rotation1"))],
                           "scale": [_v(a.get("Scale0")), _v(a.get("Scale1"))]})
    if not materials and not transforms:
        return None
    return {"time_scale": float(tree.get("TimeScale", 1)),
            "time_offset": float(tree.get("TimeOffset", 0)),
            "overrides": ({"interval": _v(tree.get("OverrideRandomInterval"), 2),
                           "duration": _v(tree.get("OverrideRandomDuration"), 2)}
                          if tree.get("UseOverrides") else None),
            "play": bool(tree.get("PlayOnStart", True) or tree.get("PlayOnEnable")),
            "materials": materials, "transforms": transforms}
