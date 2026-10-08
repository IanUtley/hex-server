"use strict";

// World space is Unity x/z from the client prefabs; north (+z) is up.
const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const tooltip = document.getElementById("tooltip");
const els = {
  campaign: document.getElementById("campaign"),
  search: document.getElementById("search"),
  bg: document.getElementById("show-bg"),
  labels: document.getElementById("show-labels"),
  playerView: document.getElementById("player-view"),
  status: document.getElementById("status"),
  legend: document.getElementById("legend"),
  zone: document.getElementById("zone"),
  back: document.getElementById("back"),
  race: document.getElementById("race"),
  fx: document.getElementById("show-fx"),
};
const effectsOn = () => !els.fx || els.fx.checked;
let zoneId = "AZ1";
// Glyphs for nodes the client marks with a POI (dungeon/panorama entrances,
// Shroomkin Haus, Gaal camps). The client renders these as 3D props on the
// terrain; there is no 2D icon art to reuse.
const POI_ICONS = { dungeon: "🏰", panorama: "👥", shroom: "🍄", camp: "⛺" };

let area = null;          // /api/area payload
let state = null;         // /api/state payload for the selected champion
let rules = null;         // /api/rules payload {rules, problems}
// Simulator: {state, layout, history: [{state, layout, action, log}], from}
let sim = null;
let nodesById = new Map();
let tiles = [];           // {tile, img, toWorld: [a,b,c,d,e,f]}
let selected = null;
let hovered = null;
const view = { scale: 2, ox: 0, oz: 0 };
let dpr = window.devicePixelRatio || 1;

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const response = await fetch(url);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

async function postJSON(url, body) {
  const response = await fetch(url, { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" } });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result;
}

// Human-readable rules condition (campaign_rules.evaluate grammar).
function fmtCond(c) {
  if (c === undefined || c === null || c === true || (typeof c === "object" && !Object.keys(c).length)) return "always";
  if (c === false) return "never — needs a condition";
  if (isGroups(c)) return c.map((g) => `(${g.join(" and ")})`).join(" or ");
  if (Array.isArray(c)) return c.length ? c.join(" and ") : "always";
  if (c.all) return c.all.map((x) => (x.any ? `(${fmtCond(x)})` : fmtCond(x))).join(" and ");
  if (c.any) return c.any.map(fmtCond).join(" or ");
  if (c.not) return `not (${fmtCond(c.not)})`;
  if (c.flag) return `flag ${c.flag}`;
  if (c.quest) {
    let text = `${c.quest} ${(c.is || "started").replace("_", " ")}`;
    if (c.step_at_least !== undefined) text += `, step ≥ ${Number(c.step_at_least) + 1}`;
    if (c.step_below !== undefined) text += `, step < ${Number(c.step_below) + 1}`;
    return text;
  }
  if (c.faction) return `faction ${c.faction}`;
  if (c.race) return `race ${c.race}`;
  if (c.visited) return `visited ${c.visited}`;
  if (c.completed) return `completed ${c.completed}`;
  if (c.outcome) return `last battle ${c.outcome}${c.node ? ` at ${c.node}` : ""}`;
  if (c.counter) return `counter(${c.counter}) ≥ ${Number(c.at_least ?? 1)}`;
  if (c.keyword) return `has keyword ${c.keyword}`;
  if (c.visits_at_least) return `${c.visits_at_least[0]} visited ≥ ${c.visits_at_least[1]}×`;
  return JSON.stringify(c);
}

// Every flag the rules read or write, for the simulator's flag list.
function ruleFlags() {
  const found = new Set();
  const walk = (v) => {
    if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") {
      for (const key of ["flag", "set_flag", "clear_flag"]) if (typeof v[key] === "string") found.add(v[key]);
      Object.values(v).forEach(walk);
    }
  };
  if (rules) walk(rules.rules);
  for (const flag of (sim ? sim.state.flags : [])) found.add(flag);
  return [...found].sort();
}

// ---------------------------------------------------------------- geometry
const toScreen = (x, z) => [(x - view.ox) * view.scale, (view.oz - z) * view.scale];
const toWorld = (sx, sy) => [sx / view.scale + view.ox, view.oz - sy / view.scale];

function tileTransform(tile, width, height) {
  // u = a0*x + a1*z + a2, v = c0*x + c1*z + c2; pixel px = u*W, py = (1-v)*H.
  const [a0, a1, a2] = tile.u_fit;
  const [c0, c1, c2] = tile.v_fit;
  const det = a0 * c1 - a1 * c0;
  // (x, z) = inv([[a0,a1],[c0,c1]]) * (u - a2, v - c2)
  const i00 = c1 / det, i01 = -a1 / det, i10 = -c0 / det, i11 = a0 / det;
  // u = px/W, v = 1 - py/H  ->  world as an affine function of (px, py)
  const x0 = i00 * (0 - a2) + i01 * (1 - c2);
  const z0 = i10 * (0 - a2) + i11 * (1 - c2);
  return { x0, z0, dxdpx: i00 / width, dzdpx: i10 / width,
           dxdpy: -i01 / height, dzdpy: -i11 / height };
}

// Texture each triangle with its own image->screen affine map (atlas art).
function drawTriangles(img, tris) {
  const W = img.naturalWidth, H = img.naturalHeight;
  for (const tri of tris) {
    const src = tri.map(([, , u, v]) => [u * W, (1 - v) * H]);
    const dst = tri.map(([x, z]) => toScreen(x, z));
    const [[u0, v0], [u1, v1], [u2, v2]] = src;
    const det = (u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0);
    if (Math.abs(det) < 1e-9) continue;
    const [[x0, y0], [x1, y1], [x2, y2]] = dst;
    const a = ((x1 - x0) * (v2 - v0) - (x2 - x0) * (v1 - v0)) / det;
    const c = ((x2 - x0) * (u1 - u0) - (x1 - x0) * (u2 - u0)) / det;
    const b = ((y1 - y0) * (v2 - v0) - (y2 - y0) * (v1 - v0)) / det;
    const d = ((y2 - y0) * (u1 - u0) - (y1 - y0) * (u2 - u0)) / det;
    const e = x0 - a * u0 - c * v0, f = y0 - b * u0 - d * v0;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Grow the clip a hair around the centroid to hide hairline seams.
    const cx = (x0 + x1 + x2) / 3, cy = (y0 + y1 + y2) / 3;
    ctx.beginPath();
    dst.forEach(([x, y], i) => {
      const gx = x + Math.sign(x - cx) * 0.5, gy = y + Math.sign(y - cy) * 0.5;
      if (i) ctx.lineTo(gx, gy); else ctx.moveTo(gx, gy);
    });
    ctx.closePath();
    ctx.clip();
    ctx.setTransform(dpr * a, dpr * b, dpr * c, dpr * d, dpr * e, dpr * f);
    ctx.drawImage(img, 0, 0);
    ctx.restore();
  }
}

// Frame the zone: a dungeon's own background art (all of it) plus its nodes;
// an overworld's nodes (its terrain is the shared continent, so framing the
// art would show both overworlds).
function fit() {
  if (!area || !area.nodes.length && !area.tiles.length) return;
  const pad = 25;
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (const n of area.nodes) {
    minX = Math.min(minX, n.x - pad); maxX = Math.max(maxX, n.x + pad);
    minZ = Math.min(minZ, n.z - pad); maxZ = Math.max(maxZ, n.z + pad);
  }
  for (const tile of area.tiles) {
    if (!Array.isArray(tile.tris) || !tile.bounds) continue;   // dungeon art only
    minX = Math.min(minX, tile.bounds.min_x); maxX = Math.max(maxX, tile.bounds.max_x);
    minZ = Math.min(minZ, tile.bounds.min_z); maxZ = Math.max(maxZ, tile.bounds.max_z);
  }
  if (!Number.isFinite(minX)) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;      // hidden (panorama mode)
  // A margin so edge labels stay on screen.
  const mx = (maxX - minX) * 0.04, mz = (maxZ - minZ) * 0.04;
  minX -= mx; maxX += mx; minZ -= mz; maxZ += mz;
  view.scale = Math.min(w / (maxX - minX), h / (maxZ - minZ));
  view.ox = (minX + maxX) / 2 - w / 2 / view.scale;
  view.oz = (minZ + maxZ) / 2 + h / 2 / view.scale;
  draw();
}

// ---------------------------------------------------------------- styling
function contentKind(node) {
  if (node.fork) return "empty";
  if (node.encounters.some((e) => e.battle)) return "encounter";
  // A visit event whose scene is not in Records is a stale reference, not content.
  const visits = ((node.authored && node.authored.visit_events) || []).filter((e) => e.name);
  if (visits.some((e) => e.type.includes("Encounter"))) return "encounter";
  if (visits.length) return "conversation";
  // Lore/conversation nodes: authored conversations or a scene with no opponent.
  if (node.conversations.length || node.quest_links.length || node.encounters.length) return "conversation";
  return "empty";
}

function nodeState(node) {
  if (sim) {
    const n = sim.layout.nodes[node.id];
    if (!n) return { known: false, visible: false, data: null };
    return {
      known: true, visible: n.visible, enabled: n.enabled,
      visited: Boolean(n.visited), completed: n.completed, repeatable: n.retryable, blocked: n.blocked,
      impassable: Boolean(n.impassable),
      quest: n.marker, failed: n.failed, current: sim.state.current === node.id,
      data: null, sim: n,
    };
  }
  if (!state) return null;
  const data = state.nodes[node.id];
  const pub = state.public || {};
  const has = (key) => (pub[key] || []).includes(node.id);
  return {
    known: Boolean(data),
    visible: Boolean(data && data.visible),
    enabled: Boolean(data && data.enabled !== false),
    visited: has("visited_nodes") || Boolean(data && (data.completed || data.visits > 0)),
    completed: Boolean(data && data.completed),
    repeatable: Boolean(data && data.repeatable),
    blocked: has("blocked_nodes") || Boolean(data && data.enabled === false),
    impassable: Boolean(data && data.impassable),
    quest: has("quest_nodes"),
    failed: has("failed_nodes"),
    current: state.current_node === node.id ||
      (data && state.current_node && data.title === state.current_node),
    data,
  };
}

// {shown, locked, visited} for a path in the live or simulated state.
function pathState(path) {
  if (sim) {
    return sim.layout.paths.find((p) => p.id === path.id) ||
      { shown: false, locked: false, visited: false };
  }
  if (!state) return null;
  const pub = state.public || {};
  const a = nodesById.get(path.from), b = nodesById.get(path.to);
  const sa = a && nodeState(a), sb = b && nodeState(b);
  const locked = (pub.locked_paths || []).includes(path.id);
  return { shown: Boolean(sa && sb && sa.visible && sb.visible && !locked),
           locked, visited: (pub.visited_paths || []).includes(path.id) };
}

// Player view mirrors the client's fog of war: only visible nodes, and a path
// only when both endpoints are visible and it is not locked.
const hasState = () => Boolean(state || sim);
const playerView = () => Boolean(hasState() && els.playerView.checked);
function nodeShown(node) {
  if (!playerView()) return true;
  const s = nodeState(node);
  return Boolean(s && s.visible);
}
function pathShown(path) {
  if (!playerView()) return true;
  const ps = pathState(path);
  return Boolean(ps && ps.shown);
}

function nodeColors(node) {
  const s = nodeState(node);
  if (!s) {
    // Design view: a red ring marks a node the rules can block or make
    // impassable; otherwise a gold ring marks a quest giver.
    const ring = designGate(node) ? css("--blocked")
      : node.quest_links.some((q) => q.role === "start") ? css("--quest") : "#0b0f13";
    return { fill: css(`--${contentKind(node)}`), ring, alpha: 1 };
  }
  // The client's order (UIDungeonMapNode.DoStatusTransition): Green
  // completed, Red impassable, Blue repeatable, Yellow visited, else Gray.
  const ring = s.quest ? css("--quest") : "#0b0f13";
  if (!s.known || !s.visible) return { fill: css("--hidden"), ring: "#0b0f13", alpha: 0.6, dash: true };
  if (s.completed) return { fill: css("--done"), ring, alpha: 1 };
  if (s.impassable) return { fill: css("--blocked"), ring, alpha: 1 };
  if (s.repeatable) return { fill: css("--repeatable"), ring, alpha: 1 };
  if (s.visited) return { fill: css("--incomplete"), ring, alpha: 1 };
  return { fill: css("--hidden"), ring, alpha: 1 };   // new
}

// Design view: which rule states can stop travel at / to this node.
function designGate(node) {
  const spec = nodeRules(node) || {};
  return [spec.impassable !== undefined ? "⛔" : "", spec.blocked !== undefined ? "🔒" : ""].join("");
}

// Glyphs drawn beside a node / NPC for its state.
function stateGlyphs(s, nodeId) {
  if (!s) {
    const node = nodesById.get(nodeId);
    return node ? designGate(node) : "";
  }
  const n = s.sim || s;
  let text = "";
  if (s.completed && !s.repeatable) text += "✓";
  if (s.repeatable && s.visible) text += "↻";
  if (s.blocked) text += "🔒";
  if (s.impassable) text += "⛔";
  if (s.failed) text += "✗";
  if (s.quest || n.givequest) text += "!";
  if (n.turninquest) text += "?";
  if (sim && sim.state.encounter && sim.state.encounter.node === nodeId) text += "⚔️";
  return text;
}

// Simulator quest log: started quests (not finished) with their steps -
// green when done, yellow when still to do.
function renderQuestLog() {
  const box = document.getElementById("quest-log");
  const quests = (sim && sim.state.quests) || {};
  const defined = (rules && rules.rules && rules.rules.quests) || {};
  let html = "";
  for (const [script, progress] of Object.entries(quests)) {
    if (!progress || progress.status !== "active") continue;
    const known = area.quests.find((q) => q.script === script);
    const steps = (defined[script] && defined[script].steps) || (known && known.objectives) || [];
    const title = (defined[script] && defined[script].title) || (known && known.title) || script;
    const done = Number(progress.step) || 0;
    // Steps can finish out of order (taming): a step is also done when the
    // hook for its trigger has already set its flag.
    const flags = new Set(sim.state.flags || []);
    const hooks = (rules && rules.rules && rules.rules.hooks) || [];
    const isDone = (step, i) => {
      if (i < done) return true;
      const when = step.done_when || {};
      const hook = hooks.find((h) => h.on && h.on.event === when.event &&
        ["scene", "conversation", "node"].every((k) => (h.on[k] || "") === (when[k] || "")) &&
        (h.on.events || "") === (when.events || ""));
      const set = hook ? (hook.do || []).map((e) => e.set_flag).filter(Boolean) : [];
      return set.length > 0 && set.every((f) => flags.has(f));
    };
    const marks = steps.map(isDone);
    const firstTodo = marks.indexOf(false);
    const shown = steps.map((step, i) => [step, i]).filter(([, i]) => marks[i] || i === firstTodo);
    html += `<h4>${esc(title)}</h4><ul>${shown.map(([step, i]) =>
      `<li class="${marks[i] ? "done" : "todo"}">${esc(step.title || step.id || `Step ${i + 1}`)}</li>`).join("")}</ul>`;
  }
  box.innerHTML = html;
  box.hidden = !html;
}

function renderLegend() {
  renderQuestLog();
  // Play view: the client's colours (UIDungeonMapNode), in its order.
  const items = hasState() ? [
    ["Not visited yet", "--hidden"], ["Visited, unfinished", "--incomplete"],
    ["Repeatable ↻", "--repeatable"], ["Impassable ⛔ (token stops)", "--blocked"],
    ["Complete ✓", "--done"], ["Blocked 🔒 (hidden until it opens)", "--panel-2"],
    ["Failed ✗ · battle ⚔️", "--panel-2"],
    ["Quest marker", "--panel-2", "--quest"],
  ] : [
    ["Battle", "--encounter"], ["Conversation / lore", "--conversation"],
    ["No content", "--empty"], ["Quest giver", "--panel-2", "--quest"],
    ["Can be blocked 🔒 / impassable ⛔", "--panel-2", "--blocked"],
  ];
  els.legend.innerHTML = items.map(([label, fill, ring]) =>
    `<span style="--c:var(${fill});${ring ? `--b:var(${ring})` : ""}">${label}</span>`).join("");
}

// ---------------------------------------------------------------- drawing
function resize() {
  dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(canvas.clientWidth * dpr);
  canvas.height = Math.round(canvas.clientHeight * dpr);
  draw();
}

function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#0d1216";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!area) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (els.bg.checked) {
    ctx.imageSmoothingEnabled = true;
    for (const { img, t, tile } of tiles) {
      if (!img.complete || !img.naturalWidth) continue;
      if (Array.isArray(tile.tris)) { drawTriangles(img, tile.tris); continue; }
      // screen = S(world(px, py)); compose the two affine maps.
      const s = view.scale;
      ctx.setTransform(
        dpr * s * t.dxdpx, dpr * -s * t.dzdpx,
        dpr * s * t.dxdpy, dpr * -s * t.dzdpy,
        dpr * s * (t.x0 - view.ox), dpr * s * (view.oz - t.z0));
      // A one-pixel overlap hides seams between neighbouring tiles.
      ctx.drawImage(img, -0.5, -0.5, img.naturalWidth + 1, img.naturalHeight + 1);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawMapEffects();
  }

  for (const path of area.paths) {
    if (!pathShown(path)) continue;
    let alpha = 0.9, color = "rgba(255,255,255,0.85)", dash = [];
    const ps = pathState(path);
    if (ps) {
      const a = nodesById.get(path.from), b = nodesById.get(path.to);
      const sa = a && nodeState(a), sb = b && nodeState(b);
      alpha = (sa ? sa.visible : true) && (sb ? sb.visible : true) ? 0.95 : 0.25;
      if (ps.locked) { color = css("--blocked"); dash = [6, 5]; }
      else if (ps.visited) { color = css("--accent"); }
    }
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1.5, view.scale * 0.35);
    ctx.setLineDash(dash);
    ctx.beginPath();
    path.points.forEach(([x, z], i) => {
      const [sx, sy] = toScreen(x, z);
      if (i) ctx.lineTo(sx, sy); else ctx.moveTo(sx, sy);
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  const radius = Math.max(5, Math.min(11, view.scale * 2.2));
  ctx.font = "600 12px system-ui, sans-serif";
  for (const node of area.nodes) {
    if (!nodeShown(node)) continue;
    const [sx, sy] = toScreen(node.x, node.z);
    if (node.fork) {
      // A path fork (prefab IsPathFork): a junction, not a location.
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      ctx.arc(sx, sy, Math.max(2.5, radius * 0.35), 0, Math.PI * 2);
      ctx.fillStyle = "#c9d1d9";
      ctx.fill();
      ctx.globalAlpha = 1;
      continue;
    }
    const colors = nodeColors(node);
    const s = nodeState(node);
    ctx.globalAlpha = colors.alpha;
    ctx.beginPath();
    ctx.arc(sx, sy, radius, 0, Math.PI * 2);
    ctx.fillStyle = colors.fill;
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = colors.dash ? "#9aa4ad" : colors.ring;
    if (colors.dash) ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
    const glyphs = stateGlyphs(s, node.id);
    if (glyphs) {
      ctx.font = "700 12px system-ui, sans-serif";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,0.8)";
      ctx.strokeText(glyphs, sx - radius - 2 - ctx.measureText(glyphs).width, sy - radius);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(glyphs, sx - radius - 2 - ctx.measureText(glyphs).width, sy - radius);
      ctx.font = "600 12px system-ui, sans-serif";
    }
    if (s && s.current) {
      ctx.beginPath();
      ctx.arc(sx, sy, radius + 6, 0, Math.PI * 2);
      ctx.strokeStyle = css("--accent");
      ctx.lineWidth = 2.5;
      ctx.stroke();
    }
    if (node === selected || node === hovered) {
      ctx.beginPath();
      ctx.arc(sx, sy, radius + 4, 0, Math.PI * 2);
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    const icon = POI_ICONS[node.poi_kind];
    if (icon) {
      ctx.font = `${Math.round(radius * 1.9)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText(icon, sx, sy - radius - 3);
      ctx.textAlign = "start";
      ctx.font = "600 12px system-ui, sans-serif";
    }
    if (els.labels.checked && (view.scale > 1.6 || area.nodes.length < 40 || node === selected)) {
      const label = node.authored ? node.authored.title : node.id;
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,0.75)";
      ctx.strokeText(label, sx + radius + 4, sy + 4);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(label, sx + radius + 4, sy + 4);
    }
  }
  ctx.globalAlpha = 1;
}

// ---------------------------------------------------------------- interaction
function nodeAt(sx, sy) {
  const radius = Math.max(8, view.scale * 2.4);
  let best = null, bestDistance = Infinity;
  for (const node of area.nodes) {
    if (!nodeShown(node)) continue;
    const [nx, ny] = toScreen(node.x, node.z);
    const distance = Math.hypot(nx - sx, ny - sy);
    if (distance < radius && distance < bestDistance) { best = node; bestDistance = distance; }
  }
  return best;
}

let drag = null;
canvas.addEventListener("pointerdown", (event) => {
  drag = { x: event.clientX, y: event.clientY, ox: view.ox, oz: view.oz, moved: false };
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointermove", (event) => {
  const rect = canvas.getBoundingClientRect();
  const sx = event.clientX - rect.left, sy = event.clientY - rect.top;
  if (drag) {
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) {
      canvas.classList.add("dragging");
      view.ox = drag.ox - dx / view.scale;
      view.oz = drag.oz + dy / view.scale;
      draw();
      return;
    }
  }
  if (!area) return;
  const node = nodeAt(sx, sy);
  if (node !== hovered) { hovered = node; draw(); }
  if (node) {
    const title = node.authored ? node.authored.title : "(no SceneData entry)";
    tooltip.innerHTML = `<b>${esc(title)}</b><br><code>${esc(node.id)}</code>`;
    tooltip.style.left = `${sx + 14}px`;
    tooltip.style.top = `${sy + 14}px`;
    tooltip.hidden = false;
  } else {
    tooltip.hidden = true;
  }
});
canvas.addEventListener("pointerup", (event) => {
  const wasDrag = drag && drag.moved;
  drag = null;
  canvas.classList.remove("dragging");
  if (wasDrag || !area) return;
  const rect = canvas.getBoundingClientRect();
  const node = nodeAt(event.clientX - rect.left, event.clientY - rect.top);
  if (node) select(node);
});
canvas.addEventListener("dblclick", (event) => {
  if (!area) return;
  const rect = canvas.getBoundingClientRect();
  const node = nodeAt(event.clientX - rect.left, event.clientY - rect.top);
  if (node && node.link) loadZone(node.link.zone, { from: { zone: zoneId, node: node.id } });
});
canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const sx = event.clientX - rect.left, sy = event.clientY - rect.top;
  const [wx, wz] = toWorld(sx, sy);
  view.scale = Math.min(40, Math.max(0.3, view.scale * Math.exp(-event.deltaY * 0.0015)));
  view.ox = wx - sx / view.scale;
  view.oz = wz + sy / view.scale;
  draw();
}, { passive: false });

function centerOn(node) {
  view.ox = node.x - canvas.clientWidth / 2 / view.scale;
  view.oz = node.z + canvas.clientHeight / 2 / view.scale;
}

els.search.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !area) return;
  const query = els.search.value.trim().toLowerCase();
  if (!query) return;
  const node = area.nodes.find((n) => n.id.toLowerCase() === query) ||
    area.nodes.find((n) => n.id.toLowerCase().includes(query) ||
      (n.authored && n.authored.title.toLowerCase().includes(query)));
  if (node) { centerOn(node); select(node); }
});
document.getElementById("fit").addEventListener("click", () => {
  if (area && area.zone.kind === "panorama" && pano.home) {
    pano.camera.position.copy(pano.home.position);
    pano.camera.fov = pano.home.fov;
    if (pano.home.rotate !== undefined) pano.rotate = pano.home.rotate;
    renderPanoramaFrame();
  } else fit();
});
// ←/→ scroll a panorama like the client's pan triggers.
document.addEventListener("keydown", (event) => {
  if (!area || area.zone.kind !== "panorama" || !pano.parallax) return;
  if (event.target.closest("input, select, textarea, dialog")) return;
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  pano.rotate += event.key === "ArrowLeft" ? -10 : 10;
  renderPanoramaFrame();
  event.preventDefault();
});
els.bg.addEventListener("change", draw);
if (els.fx) els.fx.addEventListener("change", () => { startEffectsLoop(); draw(); renderPanoramaFrame(); });
els.labels.addEventListener("change", draw);
els.playerView.addEventListener("change", () => {
  updateHash();
  if (area && area.zone.kind === "panorama") placeNpcs(); else draw();
});
window.addEventListener("resize", () => { resize(); renderPanoramaFrame(); });

for (const button of document.querySelectorAll(".tabs button")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}
function showTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const body of document.querySelectorAll(".tab-body")) {
    body.hidden = body.id !== `tab-${name}`;
  }
}

// ---------------------------------------------------------------- panels
function chips(items, cls) {
  if (!items.length) return "";
  return `<div class="chips">${items.map((item) => `<span class="chip ${cls}">${esc(item)}</span>`).join("")}</div>`;
}

function qualifierText(q) {
  const fields = Object.entries(q.fields || {}).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  return `${q.type}${fields.length ? ` (${fields.join(", ")})` : ""}`;
}

function eventText(e) {
  if (e.EventName) return `${e.type}: ${e.EventName}`;
  const rest = Object.entries(e).filter(([k]) => k !== "type").map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  return `${e.type}${rest.length ? ` (${rest.join(", ")})` : ""}`;
}

function rewardText(rewards) {
  if (!rewards || (typeof rewards === "object" && !Object.keys(rewards).length)) return "";
  return `<pre>${esc(JSON.stringify(rewards, null, 1))}</pre>`;
}

function select(node) {
  selected = node;
  updateHash();
  draw();
  showTab("node");
  const a = node.authored;
  const s = nodeState(node);
  let html = `<h2>${esc(a ? a.title : node.id)}</h2><div class="muted"><code>${esc(node.id)}</code>${a ? ` · ${esc(a.name)} · ${esc(a.terrain)}` : " · no SceneData entry"}</div>`;

  if (node.link) {
    html += `<div class="actions"><button type="button" class="primary" data-enter="${esc(node.link.zone)}">Enter ${esc(node.link.title)} (${esc(node.link.kind)}) →</button></div>`;
  }
  html += simNodeSection(node);
  if (s && !sim) {
    const flags = s.data ? ["visible", "enabled", "completed", "repeatable", "autostart", "givequest", "turninquest", "impassable"]
      .map((key) => `<span class="chip ${s.data[key] ? "on" : "off"}">${key}</span>`).join("") : "";
    const lists = ["blocked_nodes", "quest_nodes", "quest_hidden_nodes", "quest_reveal_nodes", "unlocked_nodes", "failed_nodes", "visited_nodes"]
      .filter((key) => (state.public[key] || []).includes(node.id));
    html += `<h3>Live state · ${esc(state.champion_name)}</h3>`;
    html += s.data ? `<div class="chips">${flags}</div>${chips(lists, "event")}
      <dl class="kv"><dt>type</dt><dd>${esc(s.data.type)}</dd>
      ${s.data.encounter ? `<dt>encounter</dt><dd><code>${esc(s.data.encounter)}</code></dd>` : ""}
      ${s.data.conversationId ? `<dt>conversation</dt><dd><button class="link" data-conv="${esc(s.data.conversationId)}">${esc(s.data.conversationId)}</button></dd>` : ""}</dl>`
      : `<p class="muted">Not present in this champion's VisLocs.</p>`;
  }

  if (a) {
    html += `<h3>Descriptions (SceneData)</h3>`;
    for (const d of a.descriptions) {
      html += `<div class="card"><div class="muted">${esc(d.type)}</div>${esc(d.text)}${chips(d.qualifiers.map(qualifierText), "prereq")}</div>`;
    }
    if (a.visit_events.length) {
      html += `<h3>On visit (SceneData)</h3>`;
      for (const ev of a.visit_events) {
        const isConv = ev.type.includes("Conversation");
        const label = ev.name || `${ev.guid} (stale: not in Records)`;
        html += `<div class="card"><div class="muted">${esc(ev.type.replace("UIEvent", ""))}</div>
          ${isConv && ev.name ? `<button class="link" data-conv="${esc(ev.guid)}">${esc(label)}</button>` : esc(label)}
          ${chips(ev.qualifiers.map(qualifierText), "prereq")}</div>`;
      }
    }
  }

  const encounters = node.encounters.filter((e) => rowAllowsRace(e));
  if (encounters.length) {
    html += `<h3>Scenes</h3>`;
    for (const e of encounters) {
      const mods = (e.mods || []).map((m) => m.title).filter(Boolean);
      html += `<div class="card"><div class="title">${esc(e.title)} <span class="muted">· ${esc(e.kind)}</span></div>
        <div class="muted">${esc(e.name)}</div>
        <dl class="kv"><dt>opponent</dt><dd>${esc(e.champion_name || e.champion || "—")}</dd>
        <dt>scene</dt><dd><code>${esc(e.guid)}</code></dd></dl>
        ${rules && rules.rules ? `<div class="actions">${simButton("▶ Win", { type: "encounter_result", node: node.id, scene: e.guid, victory: true, events: [] }, true)}${sceneChecksTamed(e.guid) ? simButton("▶ Win (tamed)", { type: "encounter_result", node: node.id, scene: e.guid, victory: true, events: ["tamed_troop"] }, true) : ""}${simButton("▶ Lose", { type: "encounter_result", node: node.id, scene: e.guid, victory: false })}</div>` : ""}
        ${sceneRewardSummary(e.rewards)}
        <div class="actions"><button type="button" class="small" data-edit-scene-rewards="${esc(e.guid)}">Rewards…</button>
          <button type="button" class="small" data-edit-scene-actions="${esc(e.guid)}">Actions…</button>
          <button type="button" class="small" data-edit-scene-mods="${esc(e.guid)}">Mods…</button></div>${sceneActionSummary(e.actions)}${sceneModsSummary(e.mods)}${scenePersonalityRow(e)}</div>`;
    }
  }

  html += hiddenNote(node.encounters.length, encounters.length);
  const conversations = node.conversations.filter((c) => rowAllowsRace(c));
  if (conversations.length) {
    html += `<h3>Conversations</h3>`;
    for (const c of bySimMatch(conversations, node.id)) html += conversationCard(c, node.id);
  }
  html += hiddenNote(node.conversations.length, conversations.length);

  const questLinks = node.quest_links.filter((q) => rowAllowsRace(q));
  if (questLinks.length) {
    html += `<h3>Quest conversations</h3>`;
    for (const q of bySimMatch(questLinks, node.id)) html += questLinkCard(q, node.id);
  }

  if (!node.encounters.length && !node.conversations.length && !node.quest_links.length) {
    html += `<h3>Content</h3><p class="muted">No encounter or conversation is mapped to this node.</p>`;
  }
  html += rulesNodeSection(node);
  const issues = area.issues.filter((i) => i.node === node.id);
  if (issues.length) {
    html += `<h3>Issues</h3>${issues.map((i) => `<div class="issue"><span class="lvl ${i.level}">${i.level}</span>${esc(i.text)}</div>`).join("")}`;
  }
  document.getElementById("tab-node").innerHTML = html;
}

function nodeRules(node) {
  return (rules && rules.rules && rules.rules.nodes[node.id]) || null;
}

function nodeStateSection(node) {
  const spec = nodeRules(node) || {};
  const tagView = (rules && rules.rules && rules.rules.state_tags && rules.rules.state_tags[node.id]) || {};
  const evaluated = sim && sim.layout.nodes[node.id];
  let html = `<h3>${isPanorama() ? "NPC" : "Node"} states · campaign_rules/${esc(zoneId)}.json</h3>
    <p class="muted">The ${isPanorama() ? "NPC" : "node"} is in a state when <b>all</b> of that state's tags hold (or any "or" group).${isPanorama() ? " The client shows an NPC that is shown and not completed." : ""}${rules && rules.rules ? "" : " This zone has no rules file yet; saving a state creates one from the map's paths."}</p>`;
  for (const key of stateKeys()) {
    const has = spec[key] !== undefined;
    const tags = tagView[key];
    const value = evaluated ? evaluated[key] : null;
    const shown = !has ? `<span class="muted">default: ${esc(stateDefault(key))}</span>`
      : Array.isArray(tags) ? (tags.length ? tagsHtml(tags) : `<span class="muted">always</span>`)
        : `<span class="muted">${esc(fmtCond(spec[key]))}</span>`;
    html += `<div class="state-row"><b>${esc(stateLabel(key))}</b><div class="state-tags">${shown}</div>
      ${value === null || value === undefined ? "" : `<span class="val ${value}">${value ? "yes" : "no"}</span>`}
      <button type="button" class="small" data-edit-state="${key}">Edit</button></div>`;
  }
  return html;
}

function rulesNodeSection(node) {
  let html = area.zone.kind === "panorama" && !(rules && rules.rules) ? "" : nodeStateSection(node);
  const spec = nodeRules(node);
  if (!spec) return html;
  const evaluated = sim && sim.layout.nodes[node.id];
  if (spec.review) html += `<p class="muted">⚠ ${esc(spec.review)}</p>`;
  const active = evaluated ? evaluated.entry : null;
  if ((spec.entries || []).length) {
    html += `<div class="muted" style="margin-top:8px">Offers — first matching entry wins${sim ? "" : " (start the simulator to evaluate)"}</div>`;
    const entries = spec.entries.filter((e) => condAllowsRace(e.when));
    html += hiddenNote(spec.entries.length, entries.length);
    for (const entry of entries) {
      const convs = ["conversation", "pre", "victory", "defeat"].filter((k) => entry[k])
        .map((k) => `<button class="link" data-conv="${esc(entry[k])}">${k}</button>`).join(" · ");
      html += `<div class="card entry ${entry.id === active ? "active" : ""}">
        <div class="title">${esc(entry.label || entry.id)} <span class="muted">· ${esc(entry.kind)}${entry.npc ? ` · ${esc(entry.npc)}` : ""}</span></div>
        <div class="cond">when ${esc(fmtCond(entry.when))}</div>
        ${convs ? `<div class="cond">${convs}</div>` : ""}
        ${(entry.results || []).filter((r) => condAllowsRace(r.when)).map((r) => `<div class="cond">${esc(r.outcome)}: <button class="link" data-conv="${esc(r.conversation)}">${esc(r.label)}</button> when ${esc(fmtCond(r.when))}</div>`).join("")}
        ${entry.complete_requires_events ? chips(entry.complete_requires_events.map((e) => `completes on ${e}`), "prereq") : ""}
        ${entry.repeatable ? chips(["repeatable"], "") : ""}
        ${entry.review ? `<div class="cond">⚠ ${esc(entry.review)}</div>` : ""}</div>`;
    }
  }
  return html;
}

function simButton(label, action, primary) {
  return `<button type="button" class="${primary ? "primary" : ""}" data-sim='${esc(JSON.stringify(action))}'>${esc(label)}</button>`;
}

function simNodeSection(node) {
  if (!sim) return "";
  const n = sim.layout.nodes[node.id] || {};
  const spec = nodeRules(node) || {};
  const entry = (spec.entries || []).find((e) => e.id === n.entry);
  const progress = sim.state.nodes[node.id] || {};
  const chipKeys = isPanorama() ? [...NPC_STATES, "failed"] : ["visible", "blocked", "hidden", "marker", "completed", "retryable", "impassable", "failed"];
  let html = `<h3>Simulation</h3><div class="chips">${chipKeys
    .map((k) => `<span class="chip ${n[k] ? "on" : "off"}">${esc(isPanorama() ? NPC_STATE_LABELS[k] || k : k)}</span>`).join("")}
    <span class="chip">${Number(progress.visits || 0)} visits</span></div>`;
  html += `<div class="actions">${simButton("Visit", { type: "visit", node: node.id }, true)}`;
  const started = sim.state.encounter;
  const choices = n.mod_choices || [];
  // A card-selector battle: the player picks one offered card first.
  const choiceButtons = (base) => choices.map((guid) =>
    simButton(`Win with ${modCardLabel(guid)}`, { ...base, mod: guid, victory: true, events: [] }, true) +
    simButton(`Lose with ${modCardLabel(guid)}`, { ...base, mod: guid, victory: false })).join("");
  if (started && started.node === node.id) {
    // A conversation started this battle (encounter(...) ending action).
    const scene = { type: "encounter_result", node: node.id, scene: started.scene };
    if (choices.length) html += choiceButtons(scene);
    else {
      html += simButton(`Win ${sceneName(started.scene)}`, { ...scene, victory: true, events: [] }, true);
      html += simButton("Lose", { ...scene, victory: false });
    }
  } else if (entry && entry.kind === "encounter" && choices.length) {
    html += choiceButtons({ type: "encounter_result", node: node.id, scene: entry.scene });
  } else if (entry && entry.kind === "conversation") {
    const known = [...node.conversations, ...node.quest_links].find((c) => c.guid === entry.conversation);
    const endings = uniqueEndings(known ? known.endings : []);
    const base = { type: "conversation_end", node: node.id, conversation: entry.conversation, name: known ? known.name : entry.label };
    if (endings.length <= 1) html += simButton("Finish conversation", { ...base, ending: endings.length ? endings[0].ending : "" });
    for (const end of endings.length > 1 ? endings : []) {
      html += simButton(`End: ${end.ending}`, { ...base, ending: end.ending, events: end.event ? [end.ending] : [] });
    }
  } else if (entry && entry.kind === "encounter") {
    html += simButton("Win", { type: "encounter_result", node: node.id, scene: entry.scene, victory: true, events: [] });
    if (sceneChecksTamed(entry.scene)) {
      html += simButton("Win (tamed)", { type: "encounter_result", node: node.id, scene: entry.scene, victory: true, events: ["tamed_troop"] }, true);
    }
    for (const ev of entry.complete_requires_events || []) {
      html += simButton(`Win + ${ev}`, { type: "encounter_result", node: node.id, scene: entry.scene, victory: true, events: [ev] }, true);
    }
    html += simButton("Lose", { type: "encounter_result", node: node.id, scene: entry.scene, victory: false });
  }
  html += `</div>`;
  html += `<div class="muted">Now offers: ${entry ? `<b>${esc(entry.label || entry.id)}</b>` : "nothing (completes on arrival)"}</div>`;
  if (choices.length) html += `<div class="muted">Card selector: ${choices.map((g) => esc(modCardLabel(g))).join(", ")}</div>`;
  return html;
}

async function simApply(action) {
  try {
    const result = await postJSON("/api/sim/apply", { zone: zoneId, state: sim.state, action });
    sim.history.push({ state: sim.state, layout: sim.layout, action, log: result.log });
    sim.state = result.state;
    sim.layout = result.layout;
    sim.diff = null;  // the live comparison only describes the starting state
    refreshSim();
  } catch (error) {
    els.status.textContent = error.message;
  }
}

function refreshSim() {
  renderSim();
  renderLegend();
  if (area && area.zone.kind === "panorama") {
    const npc = area.panorama.npcs.find((n) => n.name === pano.npc);
    if (npc) selectNpc(npc); else if (pano.camera) placeNpcs();
  } else if (selected) select(selected); else draw();
}

// Continue the simulation in the teleport's zone, carrying race, flags,
// quests and champion keywords. Zones without a rules file (panoramas) just open.
async function followTeleport() {
  const target = sim && sim.state.teleport;
  if (!target) return;
  const carry = sim.state;
  // Teleporting within the zone we are already in just moves the player.
  if (String(target.zone).toLowerCase() === String(zoneId).toLowerCase()) {
    if (target.node) {
      await simApply({ type: "visit", node: target.node });
      if (nodesById.has(target.node)) select(nodesById.get(target.node));
    } else {
      els.status.textContent = `Teleport to ${target.zone}: already here`;
    }
    return;
  }
  await loadZone(target.zone, { from: { zone: zoneId, node: carry.current } });
  if (rules && rules.rules) {
    await simStart({ carry, node: target.node || "" }, `Teleported from ${carry.current || "?"}`);
    if (target.node && nodesById.has(target.node)) select(nodesById.get(target.node));
  } else {
    els.status.textContent = `Teleported to ${target.zone}${target.node ? ` · ${target.node}` : ""} (no simulator here)`;
  }
}

function simExport() {
  const file = { format: "hex-sim-state", zone: zoneId, state: sim.state };
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(file, null, 2)], { type: "application/json" }));
  link.download = `sim-${zoneId}-${sim.state.race || "state"}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function simImport(file) {
  try {
    const data = JSON.parse(await file.text());
    if (data.format !== "hex-sim-state") throw new Error("not a simulator state file");
    if (data.zone && String(data.zone).toLowerCase() !== String(zoneId).toLowerCase()) {
      await loadZone(data.zone);
    }
    if (!rules || !rules.rules) throw new Error(`no simulator for zone ${data.zone || zoneId}`);
    const result = await postJSON("/api/sim/load", { zone: zoneId, state: data.state });
    sim = { state: result.state, layout: result.layout, history: [], label: `Imported ${file.name}`, diff: null };
    state = null;
    els.campaign.value = "";
    els.playerView.disabled = false;
    refreshSim();
    showTab("sim");
  } catch (error) {
    els.status.textContent = error.message;
  }
}

async function simStart(body, label) {
  try {
    const result = await postJSON("/api/sim/start", { ...body, zone: zoneId });
    sim = { state: result.state, layout: result.layout, history: [], label, diff: null };
    if (body.campaign) {
      // Where the rules-derived layout disagrees with the server's live state.
      const live = await getJSON(`/api/state?id=${encodeURIComponent(body.campaign)}`);  // eslint-disable-line
      const liveVisible = new Set(Object.entries(live.nodes).filter(([id, d]) => d.visible && !id.startsWith("Fork")).map(([id]) => id));
      const simVisible = new Set(Object.entries(sim.layout.nodes).filter(([id, d]) => d.visible && !id.startsWith("Fork")).map(([id]) => id));
      sim.diff = {
        onlyLive: [...liveVisible].filter((id) => !simVisible.has(id) && nodesById.has(id)).sort(),
        onlyRules: [...simVisible].filter((id) => !liveVisible.has(id)).sort(),
      };
    }
    state = null;
    els.campaign.value = "";
    els.playerView.disabled = false;
    refreshSim();
    showTab("sim");
  } catch (error) {
    els.status.textContent = error.message;
  }
}

const RACES = ["Human", "Elf", "Coyotle", "Orc", "Dwarf", "Shin'hare", "Vennen", "Necrotic"];

function renderSim() {
  const body = document.getElementById("tab-sim");
  const campaigns = [...els.campaign.options].filter((o) => o.value);
  let html = `<p class="muted">Play the rules in <code>campaign_rules/${esc(zoneId)}.json</code> without the client. The map shows the derived layout; tick <b>Player view</b> for fog of war.</p>
    <div class="row"><select id="sim-race">${RACES.map((r) => `<option ${r === viewRace() ? "selected" : ""}>${esc(r)}</option>`).join("")}</select>
      <button type="button" id="sim-new" class="primary">New game</button>
      <button type="button" id="sim-import">Import state...</button>
      <input type="file" id="sim-import-file" accept=".json,application/json" hidden></div>`;
  if (campaigns.length) {
    html += `<div class="row"><select id="sim-campaign">${campaigns.map((o) => `<option value="${esc(o.value)}">${esc(o.textContent)}</option>`).join("")}</select>
      <button type="button" id="sim-live">From live state</button></div>`;
  }
  if (!rules || !rules.rules) html += `<p class="muted">No rules file for this zone yet (<code>campaign_rules/${esc(zoneId)}.json</code>).</p>`;
  if (sim && rules && rules.rules) {
    const st = sim.state;
    if (st.teleport) {
      html += `<div class="card teleport">Teleport → <b>${esc(st.teleport.zone)}</b>${st.teleport.node ? ` · ${esc(st.teleport.node)}` : ""}
        <div class="actions"><button type="button" class="primary" data-follow="1">Follow teleport</button></div></div>`;
    }
    html += `<h3>${esc(sim.label)}</h3><dl class="kv"><dt>race</dt><dd>${esc(st.race || "—")} (${esc(st.faction || "—")})</dd>
      <dt>at</dt><dd><button class="link" data-node="${esc(st.current)}">${esc(st.current || "—")}</button></dd></dl>
      <div class="actions"><button type="button" id="sim-undo" ${sim.history.length ? "" : "disabled"}>Undo</button>
      <button type="button" id="sim-export">Export state</button>
      <button type="button" id="sim-exit">Exit simulator</button></div>`;
    if (sim.diff) {
      const list = (ids) => ids.map((id) => `<button class="link" data-node="${esc(id)}">${esc(id)}</button>`).join(", ") || "none";
      html += `<h3>Rules vs live state</h3><div class="card"><div>Visible live, hidden by rules: ${list(sim.diff.onlyLive)}</div>
        <div>Visible by rules, hidden live: ${list(sim.diff.onlyRules)}</div></div>`;
    }
    html += simStateTable(st);
    html += `<h3>History</h3><ol class="log">${sim.history.slice(-20).map((h) => {
      const what = h.log.filter((l) => l.hook || l.effect || l.quest_step).map((l) =>
        l.hook ? `hook ${l.hook}` : l.quest_step ? `quest step ${l.quest_step}` : Object.entries(l.effect).map(([k, v]) => `${k} ${v}`).join("")).join("; ");
      const acts = h.log.filter((l) => l.action).map((l) => l.action).join("; ");
      return `<li>${acts ? `<span class="chip action">${esc(acts)}</span> ` : ""}<b>${esc(h.action.type)}</b> ${esc(h.action.node || h.action.flag || h.action.quest || "")}${what ? ` → ${esc(what)}` : ""}</li>`;
    }).join("")}</ol>`;
  }
  if (rules && rules.problems && rules.problems.length) {
    html += `<h3>Rules check</h3>${rules.problems.map((p) => `<div class="issue"><span class="lvl ${p.level}">${p.level}</span>${esc(p.where)}: ${esc(p.text)}</div>`).join("")}`;
  }
  body.innerHTML = html;
}

// ---------------------------------------------------------------- simulation state table
// Everything the simulation holds, editable: champion, keywords, flags (incl.
// the conversation(...) flags endings set), quests, node/NPC progress, and a
// battle a conversation started. Each row can be deleted.
function knownFlags() {
  const found = new Set(ruleFlags());
  for (const [name, guid] of knownConversations()) {
    found.add(`conversation(${name})`);
    const row = conversationRow(guid) || {};
    for (const e of row.endings || []) found.add(`conversation(${name},${e.ending})`);
  }
  return [...found].sort();
}

function simStateTable(st) {
  const x = (action, title) => `<button type="button" class="chip-x" title="${esc(title)}" data-sim='${esc(JSON.stringify(action))}'>✕</button>`;
  let html = `<h3>State</h3><table class="state-table"><tbody>
    <tr><th>champion</th><td><select class="sim-race-set">${RACES.map((r) => `<option ${r === st.race ? "selected" : ""}>${esc(r)}</option>`).join("")}</select>
      <span class="muted">${esc(st.faction || "")}</span></td><td></td></tr>
    <tr><th>at</th><td><button class="link" data-node="${esc(st.current)}">${esc(st.current || "—")}</button></td><td></td></tr>
    ${st.gold_spent ? `<tr><th>gold spent</th><td>${esc(String(st.gold_spent))}</td><td></td></tr>` : ""}`;
  if (st.encounter) {
    html += `<tr><th>battle</th><td>${esc(sceneName(st.encounter.scene))} at <b>${esc(st.encounter.node)}</b>
      ${st.encounter.conversation_id ? `<div class="muted">Opening conversation: ${esc(conversationName(st.encounter.conversation_id))}</div>` : ""}
      ${simButton("Win", { type: "encounter_result", node: st.encounter.node, scene: st.encounter.scene, victory: true, events: [] }, true)}
      ${simButton("Lose", { type: "encounter_result", node: st.encounter.node, scene: st.encounter.scene, victory: false })}</td>
      <td>${x({ type: "clear_encounter" }, "Cancel the battle")}</td></tr>`;
  }
  html += `</tbody></table>`;

  html += `<h4>Flags</h4><table class="state-table"><tbody>${st.flags.map((f) =>
    `<tr><td class="flag">${esc(f)}</td><td>${x({ type: "clear_flag", flag: f }, "Remove flag")}</td></tr>`).join("") ||
    `<tr><td class="muted">none</td></tr>`}</tbody></table>
    <div class="row"><input id="sim-flag-new" list="sim-flag-list" placeholder="add a flag, e.g. conversation(Name,choice_go)" autocomplete="off">
      <button type="button" id="sim-flag-add">Add</button></div>
    <datalist id="sim-flag-list">${knownFlags().filter((f) => !st.flags.includes(f)).map((f) => `<option value="${esc(f)}">`).join("")}</datalist>`;

  const keywords = Array.isArray(st.keywords) ? st.keywords : [];
  html += `<h4>Champion keywords</h4><table class="state-table"><tbody>${keywords.map((name) =>
    `<tr><td><code>keyword(${esc(name)})</code></td><td>${x({ type: "set_keyword", keyword: name, present: false }, "Remove keyword")}</td></tr>`).join("") ||
    `<tr><td class="muted">none</td></tr>`}</tbody></table>
    <div class="row"><input id="sim-keyword-new" list="sim-keyword-list" placeholder="add a status, e.g. Stealth or Burning" autocomplete="off">
      <button type="button" id="sim-keyword-add">Add</button></div>
    <datalist id="sim-keyword-list">${(area.keyword_names || []).filter((name) =>
      !keywords.some((value) => String(value).toLowerCase() === name.toLowerCase())).map((name) => `<option value="${esc(name)}">`).join("")}</datalist>`;

  const counters = Object.entries(st.counters || {}).sort(([a], [b]) => a.localeCompare(b));
  html += `<h4>Counters</h4><table class="state-table"><tbody>${counters.map(([name, value]) =>
    `<tr><td><code>counter(${esc(name)})</code></td><td>${Number(value) || 0}</td></tr>`).join("") ||
    `<tr><td class="muted">none</td></tr>`}</tbody></table>`;

  const quests = Object.entries(st.quests || {}).filter(([, q]) => q.status !== "not_started");
  const questInfo = (script) => (rules.rules.quests || {})[script] || area.quests.find((q) => q.script === script) || {};
  html += `<h4>Quests</h4><table class="state-table"><tbody>${quests.map(([script, q]) => {
    const steps = ((questInfo(script).steps || questInfo(script).objectives) || []).length || 1;
    return `<tr data-quest="${esc(script)}"><td title="${esc(script)}">${esc(questInfo(script).title || script)}</td>
      <td><select class="sim-quest-status">${["active", "finished"].map((v) => `<option ${v === q.status ? "selected" : ""}>${v}</option>`).join("")}</select>
      step <input class="sim-quest-step" type="number" min="1" max="${steps}" value="${Math.min(Number(q.step) + 1, steps)}"></td>
      <td>${x({ type: "set_quest", quest: script, status: "not_started", step: 0 }, "Remove quest")}</td></tr>`;
  }).join("") || `<tr><td class="muted">none</td></tr>`}</tbody></table>
    <div class="row"><select id="sim-quest-new"><option value="">add a quest…</option>${questScripts()
      .filter((q) => !quests.some(([s]) => s === q)).map((q) => `<option>${esc(q)}</option>`).join("")}</select></div>`;

  const nodes = Object.entries(st.nodes || {}).sort(([a], [b]) => a.localeCompare(b));
  const tick = (id, key, value) => `<input type="checkbox" class="sim-node-field" data-node-id="${esc(id)}" data-field="${key}" ${value ? "checked" : ""}>`;
  html += `<h4>${isPanorama() ? "NPCs" : "Nodes"}</h4><table class="state-table nodes"><thead><tr><th></th><th>visits</th><th title="Conversations finished here (talkedAt)">talked</th><th>done</th><th>retry</th><th>failed</th><th>last battle</th><th></th></tr></thead><tbody>${nodes.map(([id, n]) =>
    `<tr><td><button class="link" data-node="${esc(id)}" data-npc="${esc(id)}">${esc(id)}</button></td>
      <td>${Number(n.visits || 0)}</td>
      <td><input type="number" min="0" class="sim-node-talks" data-node-id="${esc(id)}" value="${Number(n.talks || 0)}"></td>
      <td>${tick(id, "completed", n.completed)}</td><td>${tick(id, "retryable", n.retryable)}</td><td>${tick(id, "failed", n.failed)}</td>
      <td><select class="sim-node-outcome" data-node-id="${esc(id)}">${["", "success", "fail"].map((o) => `<option value="${o}" ${o === (n.outcome || "") ? "selected" : ""}>${o || "—"}</option>`).join("")}</select></td>
      <td>${x({ type: "clear_node", node: id }, "Forget this node's progress")}</td></tr>`).join("") ||
    `<tr><td class="muted" colspan="8">none</td></tr>`}</tbody></table>`;
  return html;
}

// Start a simulation on demand (choosing an outcome without one running).
async function ensureSim() {
  if (sim) return true;
  if (!(rules && rules.rules)) {
    els.status.textContent = `No rules for ${zoneId}: the simulator needs campaign_rules/${zoneId}.json`;
    return false;
  }
  const race = viewRace() || "Human";
  await simStart({ race }, `New game · ${race}`);
  return Boolean(sim);
}

function conversationNode(guid) {
  for (const node of area.nodes) {
    if ([...node.conversations, ...node.quest_links].some((c) => c.guid === guid)) return node.id;
  }
  for (const npc of (area.panorama ? area.panorama.npcs : [])) {
    if ([...npc.conversations, ...npc.quest_links].some((c) => c.guid === guid)) return npc.name;
  }
  return null;
}

async function chooseEnding(guid, ending, isEvent) {
  const node = conversationNode(guid);
  if (!node) { els.status.textContent = "This conversation is not on a node of this zone"; return; }
  if (!(await ensureSim())) return;
  const row = conversationRow(guid) || [...area.nodes.flatMap((n) => n.quest_links)].find((q) => q.guid === guid) || {};
  await simApply({ type: "conversation_end", node, conversation: guid, name: row.name || "",
    ending, events: isEvent ? [ending] : [] });
  showTab("sim");
}

document.addEventListener("change", (event) => {
  if (event.target.id === "sim-import-file") {
    const file = event.target.files[0];
    event.target.value = "";
    if (file) simImport(file);
    return;
  }
  const target = event.target;
  if (!sim) return;
  if (target.classList.contains("sim-race-set")) {
    simApply({ type: "set_faction", race: target.value, faction: FACTION_OF_RACE[target.value] || "" });
  } else if (target.classList.contains("sim-quest-status") || target.classList.contains("sim-quest-step")) {
    const row = target.closest("[data-quest]");
    simApply({ type: "set_quest", quest: row.dataset.quest,
      status: row.querySelector(".sim-quest-status").value,
      step: Math.max(0, Number(row.querySelector(".sim-quest-step").value) - 1) });
  } else if (target.id === "sim-quest-new" && target.value) {
    simApply({ type: "set_quest", quest: target.value, status: "active", step: 0 });
  } else if (target.classList.contains("sim-node-field")) {
    simApply({ type: "set_node", node: target.dataset.nodeId, values: { [target.dataset.field]: target.checked } });
  } else if (target.classList.contains("sim-node-talks")) {
    simApply({ type: "set_node", node: target.dataset.nodeId, values: { talks: Math.max(0, Number(target.value) || 0) } });
  } else if (target.classList.contains("sim-node-outcome")) {
    simApply({ type: "set_node", node: target.dataset.nodeId, values: { outcome: target.value || null } });
  }
});

document.addEventListener("click", async (event) => {
  const npcLink = event.target.closest("[data-npc]");
  if (npcLink && area && area.panorama) {
    const npc = area.panorama.npcs.find((n) => n.name === npcLink.dataset.npc);
    if (npc) { panToNpc(npc); selectNpc(npc); }
    return;
  }
  const enter = event.target.closest("[data-enter]");
  if (enter) { loadZone(enter.dataset.enter, { from: { zone: zoneId, node: selected && selected.id } }); return; }
  const simAction = event.target.closest("[data-sim]");
  if (simAction) {
    if (await ensureSim()) await simApply(JSON.parse(simAction.dataset.sim));
    return;
  }
  const choose = event.target.closest("[data-choose]");
  if (choose) {
    document.getElementById("conversation").close();
    await chooseEnding(choose.dataset.choose, choose.dataset.ending, choose.dataset.event === "1");
    return;
  }
  const id = event.target.id;
  if (id === "sim-flag-add") {
    const input = document.getElementById("sim-flag-new");
    if (input.value.trim()) simApply({ type: "set_flag", flag: input.value.trim() });
    return;
  }
  if (id === "sim-keyword-add") {
    const input = document.getElementById("sim-keyword-new");
    if (input.value.trim()) simApply({ type: "set_keyword", keyword: input.value.trim(), present: true });
    return;
  }
  if (id === "sim-new") {
    const race = document.getElementById("sim-race").value;
    simStart({ race }, `New game · ${race}`);
    return;
  }
  if (id === "sim-import") {
    document.getElementById("sim-import-file").click();
    return;
  }
  if (id === "sim-export" && sim) {
    simExport();
    return;
  }
  if (id === "sim-live") {
    const select = document.getElementById("sim-campaign");
    simStart({ campaign: Number(select.value) }, `From ${select.selectedOptions[0].textContent}`);
    return;
  }
  if (id === "sim-undo" && sim && sim.history.length) {
    const last = sim.history.pop();
    sim.state = last.state;
    sim.layout = last.layout;
    refreshSim();
    return;
  }
  if (id === "sim-exit") {
    sim = null;
    els.playerView.disabled = true;
    refreshSim();
    return;
  }
  const conv = event.target.closest("[data-conv]");
  if (conv) { openConversation(conv.dataset.conv); return; }
  const editQuest = event.target.closest("[data-edit-quest]");
  if (editQuest) {
    const [quest, guid] = editQuest.dataset.editQuest.split("|");
    const rows = [...area.nodes.flatMap((n) => n.quest_links),
      ...(area.panorama ? area.panorama.npcs.flatMap((n) => n.quest_links) : [])];
    const row = rows.find((q) => q.quest === quest && q.guid === guid);
    if (row) {
      openTagEditor({
        title: `${row.name}`,
        subtitle: `Quest <code>${esc(row.quest)}</code> · ${esc(row.role)} — offered while all tags hold (the server still ranks by role).`,
        tags: (row.conditions && row.conditions.tags) || [],
        emptyText: "No tags: offered by its role as before.",
        onSave: (tags) => postJSON("/api/edit/quest-tags", { template: row.template, quest: row.quest, conversation: row.guid, tags }),
      });
    }
    return;
  }
  const editActions = event.target.closest("[data-edit-actions]");
  if (editActions) {
    document.getElementById("conversation").close();
    editConversationActions(editActions.dataset.editActions, editActions.dataset.ending);
    return;
  }
  const follow = event.target.closest("[data-follow]");
  if (follow) { followTeleport(); return; }
  const editConv = event.target.closest("[data-edit-conv]");
  if (editConv) { document.getElementById("conversation").close(); editConversationTags(editConv.dataset.editConv); return; }
  const editState = event.target.closest("[data-edit-state]");
  const stateTarget = area && area.zone.kind === "panorama" ? pano.npcNode : selected;
  if (editState && stateTarget) { editNodeState(stateTarget, editState.dataset.editState); return; }
  const nodeLink = event.target.closest("[data-node]");
  if (nodeLink && nodesById.has(nodeLink.dataset.node)) {
    const node = nodesById.get(nodeLink.dataset.node);
    centerOn(node);
    select(node);
  }
});

// ---------------------------------------------------------------- race filter
// The toolbar Race hides content a champion of that race never sees: rows
// whose tags (or authored name) require another race/faction, or exclude
// this one. Unknown tags never hide anything.
const FACTION_OF_RACE = { Human: "Ardent", Elf: "Ardent", Coyotle: "Ardent", Orc: "Ardent",
  Dwarf: "Underworld", "Shin'hare": "Underworld", Vennen: "Underworld", Necrotic: "Underworld" };
const sameText = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
const viewRace = () => (els.race ? els.race.value : "");

function audienceHolds(kind, value, race) {
  return kind === "race" ? sameText(value, race) : sameText(value, FACTION_OF_RACE[race]);
}

function tagAllowsRace(tag, race) {
  const m = /^\s*(!|not\s+)?\s*(ChampionIsRace|faction)\s*\((.+)\)\s*$/i.exec(String(tag));
  if (!m) return true;
  const holds = audienceHolds(/^faction$/i.test(m[2]) ? "faction" : "race", m[3].trim(), race);
  return m[1] ? !holds : holds;
}

function condAllowsRace(cond, race = viewRace()) {
  if (!race || cond === undefined || cond === null || cond === true || cond === false) return true;
  if (isGroups(cond)) return cond.some((g) => condAllowsRace(g, race));
  if (Array.isArray(cond)) return cond.every((t) => tagAllowsRace(t, race));
  if (typeof cond !== "object") return true;
  if (cond.all) return cond.all.every((c) => condAllowsRace(c, race));
  if (cond.any) return cond.any.some((c) => condAllowsRace(c, race));
  if (cond.not) {
    const inner = cond.not;
    if (inner && (inner.race || inner.faction) && Object.keys(inner).length === 1) {
      return !audienceHolds(inner.race ? "race" : "faction", inner.race || inner.faction, race);
    }
    return true;
  }
  if (cond.race) return audienceHolds("race", cond.race, race);
  if (cond.faction) return audienceHolds("faction", cond.faction, race);
  return true;
}

// A table row: its tags, or its authored-name audience when untagged.
function rowAllowsRace(row, race = viewRace()) {
  if (!race) return true;
  const tags = row.trigger && row.trigger.tags;
  if (tags && tags.length) return condAllowsRace(tags, race);
  const a = row.audience || {};
  if (a.race && !sameText(a.race, race)) return false;
  if (a.faction && !audienceHolds("faction", a.faction, race)) return false;
  if (row.race && !sameText(row.race, race)) return false;   // per-race scenes
  return true;
}

function hiddenNote(total, shown) {
  return total > shown ? `<p class="muted small-text">${total - shown} hidden for ${esc(viewRace())} (Race filter).</p>` : "";
}

// ---------------------------------------------------------------- tags
// Tag grammar: campaign_rules.parse_tag. Saved by /api/edit/* to
// campaign_data/<template>.json (conversations) and campaign_rules/<zone>.json
// (node states).
const NODE_STATES = ["visible", "hidden", "blocked", "completed", "retryable", "marker", "impassable"];
// Panorama NPCs: what the client reads per NPC location
// (UIPanoramaZoneViewModel.ProcessStateChange). A completed NPC is hidden.
const NPC_STATES = ["visible", "hidden", "completed", "givequest", "turninquest", "autostart", "retryable"];
const NPC_STATE_LABELS = { visible: "shown", hidden: "hidden", completed: "completed (hides it)",
  givequest: "quest giver !", turninquest: "turn-in ?", autostart: "auto-start", retryable: "repeatable" };
const NPC_DEFAULTS = { visible: "never — an NPC has no paths; add tags", hidden: "never",
  completed: "on finishing its conversation (unless repeatable)", givequest: "no", turninquest: "no",
  autostart: "no", retryable: "from play" };
const isPanorama = () => Boolean(area && area.zone.kind === "panorama");
const stateKeys = () => (isPanorama() ? NPC_STATES : NODE_STATES);
const stateLabel = (key) => (isPanorama() ? NPC_STATE_LABELS[key] : key);
const stateDefault = (key) => (isPanorama() ? NPC_DEFAULTS : STATE_DEFAULTS)[key];
const STATE_DEFAULTS = {
  visible: "revealed by a visited neighbour", hidden: "never", blocked: "never",
  completed: "on finishing the node's content", retryable: "from play (lost battle, repeatable content)",
  marker: "no quest marker",
  impassable: "never (red on the client map; the token stops there until completed)",
};
function encounterSceneOptions() {
  const scenes = new Map();
  const context = tagEdit && tagEdit.actionContext;
  const node = (area.nodes || []).find((item) => item.id === (context || {}).nodeId);
  for (const scene of (node && node.encounters) || []) {
    const guid = String(scene.guid || "").toLowerCase();
    if (!guid || scene.battle === false) continue;
    scenes.set(guid, scene.name || scene.title || "");
  }
  // Retain scenes already assigned to this conversation, even when the
  // scene is not otherwise mapped to the node.
  const actions = ((context || {}).row || {}).trigger?.actions || {};
  for (const action of Object.values(actions).flat()) {
    const match = String(action).match(/^\s*encounter\s*\((.+)\)\s*$/i);
    if (!match) continue;
    const guid = match[1].split(",", 1)[0].trim().toLowerCase();
    if (guid && !scenes.has(guid)) scenes.set(guid, "");
  }
  return [...scenes].map(([value, label]) => ({
    value, label: label || (area.scene_names || {})[value] || value,
  }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

function encounterConversationOptions() {
  const context = tagEdit && tagEdit.actionContext;
  const node = (area.nodes || []).find((item) => item.id === (context || {}).nodeId);
  const conversations = [...((node && node.conversations) || []),
    ...((node && node.quest_links) || [])];
  const found = new Map();
  for (const conversation of conversations) {
    const guid = String(conversation.guid || "").toLowerCase();
    if (guid) found.set(guid, conversation.name || conversation.label || guid);
  }
  return [...found].map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

const TAG_KINDS = {
  faction: { hint: "faction(X)", values: () => ["Ardent", "Underworld"] },
  keyword: { hint: "keyword(Name) — true when the champion has this status; positive matching counters also count", values: () => area.keyword_names || [] },
  ChampionIsRace: { hint: "ChampionIsRace(X)", values: () => RACES },
  outcome: { hint: "outcome:success|fail — the node's last battle", values: () => ["success", "fail"] },
  completedQuest: { hint: "completedQuest(Script[,Step]) — finished, or step N (1-based) done", values: questScripts, extra: questSteps },
  conversation: { hint: "conversation(Name[,Ending]) — set when that conversation ends", values: () => [...knownConversations().keys()], extra: conversationEndings },
  flag: { hint: "flag(X)", values: () => ruleFlags() },
  questActive: { hint: "questActive(Script)", values: questScripts },
  questStarted: { hint: "questStarted(Script)", values: questScripts },
  visited: { hint: "visited(Node)", values: () => area.nodes.map((n) => n.id) },
  completed: { hint: "completed(Node) — the node is completed", values: () => area.nodes.map((n) => n.id) },
  talkedAt: { hint: "talkedAt(Node[,N]) — N (default 1) conversations finished at the node; !talkedAt(Node) = first visit", values: () => area.nodes.map((n) => n.id), extraLabel: "times (default 1)", extra: async () => [1, 2, 3].map((v) => ({ value: String(v), label: "" })) },
  counter: { hint: "counter(Name[,N]) — true once the counter reaches N (default 1); State shows the exact value", values: ruleCounters, extraLabel: "minimum value (default 1)", extra: async () => [1, 2, 3, 5, 10].map((v) => ({ value: String(v), label: "" })) },
};

// Ending actions (campaign_rules.parse_action), run when that ending is chosen.
const ACTION_KINDS = {
  encounter: {
    hint: "Choose a scene and optionally an opening conversation from this node. outcome:success|fail tags read its result.",
    values: encounterSceneOptions, select: true,
    extraLabel: "opening conversation (optional)",
    extraValues: encounterConversationOptions, extraSelect: true,
  },
  start_quest: {
    hint: "start_quest(script) — mark the quest started (questStarted(script) holds afterwards)",
    values: questScripts,
  },
  set_flag: {
    hint: "set_flag(X) — flag(X) holds afterwards (map gates and tags read it)",
    values: () => ruleFlags(),
  },
  clear_flag: {
    hint: "clear_flag(X) — remove flag X (also unflag(X)); clear_flag(prefix*) removes every flag starting with prefix, e.g. reset a dungeon",
    values: () => ruleFlags(),
  },
  spend_gold: {
    hint: "spend_gold(N) — the server takes N gold when this ending is chosen (a bribe, a fortune)",
    values: () => ["100", "500", "1000"],
  },
  complete_quest: {
    hint: "complete_quest(script) — finish the quest; its rewards (Quests tab) are granted when it completes",
    values: questScripts,
  },
  teleport: {
    hint: "teleport(Zone[,Node]) — send the player to another zone, arriving at Node (or its start)",
    values: () => (area.zones || []).map((z) => z.id),
    extraLabel: "arrival node (optional)",
    extra: async (zone) => {
      const target = await getJSON(`/api/area?zone=${encodeURIComponent(zone)}`);
      return target.nodes.map((n) => ({ value: n.id, label: (n.authored && n.authored.title) || "" }));
    },
  },
};

function actionsFor(row, ending) {
  const table = (row && row.trigger && row.trigger.actions) || {};
  return table[ending] || [];
}

function sceneName(guid) {
  const name = (area.scene_names || {})[String(guid).trim().toLowerCase()];
  return name ? `“${name}”` : guid;
}

function conversationName(guid) {
  const key = String(guid || "").toLowerCase();
  for (const node of area.nodes || []) {
    const row = [...(node.conversations || []), ...(node.quest_links || [])]
      .find((item) => String(item.guid || "").toLowerCase() === key);
    if (row) return row.name || row.label || guid;
  }
  for (const npc of (area.panorama && area.panorama.npcs) || []) {
    const row = [...(npc.conversations || []), ...(npc.quest_links || [])]
      .find((item) => String(item.guid || "").toLowerCase() === key);
    if (row) return row.name || row.label || guid;
  }
  return guid;
}

// encounter(<scene guid>[,<opening conversation guid>]) reads by name.
function actionText(action) {
  const match = String(action).match(/^encounter\((.+)\)$/i);
  if (!match) return String(action);
  const [scene, conversation] = match[1].split(",", 2).map((part) => part.trim());
  const opening = conversation ? ` · opening: ${conversationName(conversation)}` : "";
  return `encounter ${sceneName(scene)}${opening}`;
}

function actionChips(row) {
  const table = (row.trigger && row.trigger.actions) || {};
  const items = Object.entries(table).flatMap(([ending, list]) =>
    list.map((a) => `${ending === "*" ? "on end" : `on ${ending}`}: ${actionText(a)}`));
  return chips(items, "action");
}

function editConversationActions(guid, ending) {
  const row = conversationRow(guid);
  if (!row) { els.status.textContent = "Only table conversations can carry actions"; return; }
  openTagEditor({
    title: `${row.name}`,
    subtitle: `${ending === "*" ? "Actions ON <b>any ending</b>" : `Actions ON <b>${esc(ending)}</b>`}.
      Saved to <code>campaign_data/</code> as <code>trigger_json.actions</code>.`,
    tags: actionsFor(row, ending), kinds: ACTION_KINDS, allowGroups: false, allowNot: false,
    actionContext: { nodeId: conversationNode(guid), row },
    formatTag: actionText,
    rawPlaceholder: "or type an action, e.g. teleport(AZ1,Node001)",
    emptyText: "No actions.",
    onSave: (actions) => (row.quest
      ? postJSON("/api/edit/quest-actions", { template: row.template, quest: row.quest, conversation: row.guid, ending, actions })
      : postJSON("/api/edit/conversation-actions", { template: row.template, conversation: row.guid, ending, actions })),
  });
}

function questScripts() {
  const names = new Set(area.quests.map((q) => q.script));
  for (const script of Object.keys((rules && rules.rules && rules.rules.quests) || {})) names.add(script);
  return [...names].sort();
}

function ruleCounters() {
  const definition = (rules && rules.rules) || {};
  const names = new Set(Object.keys((definition.start && definition.start.counters) || {}));
  for (const hook of definition.hooks || []) {
    for (const effect of hook.do || []) {
      const name = effect.increment_counter && effect.increment_counter.name;
      if (name) names.add(String(name));
    }
  }
  for (const name of Object.keys((sim && sim.state && sim.state.counters) || {})) names.add(name);
  return [...names].sort();
}

async function questSteps(script) {
  const quest = area.quests.find((q) => q.script === script);
  return ((quest && quest.objectives) || []).map((o, i) => ({ value: String(i + 1), label: o.title || o.id || "" }));
}

// Conversation name -> GUID for every conversation this zone's tables name.
function knownConversations() {
  const found = new Map();
  const add = (c) => { if (c && c.name && c.guid) found.set(c.name, c.guid); };
  for (const node of area.nodes) { node.conversations.forEach(add); node.quest_links.forEach(add); }
  if (area.panorama) {
    for (const npc of area.panorama.npcs) { npc.conversations.forEach(add); npc.quest_links.forEach(add); }
    area.panorama.unassigned.forEach(add);
  }
  return found;
}

async function conversationEndings(name) {
  const guid = knownConversations().get(name);
  if (!guid) return [];
  const c = await getJSON(`/api/conversation/${encodeURIComponent(guid)}`);
  return c.endings.map((e) => ({ value: e.ending, label: `${e.text}${e.server_visible ? "" : " (simulator only)"}` }));
}

function conversationRow(guid) {
  for (const node of area.nodes) {
    const row = node.conversations.find((c) => c.guid === guid);
    if (row) return row;
  }
  for (const npc of (area.panorama ? area.panorama.npcs : [])) {
    const row = npc.conversations.find((c) => c.guid === guid);
    if (row) return row;
  }
  // Quest conversations carry their actions in conditions_json.actions.
  for (const node of area.nodes) {
    const row = node.quest_links.find((q) => q.guid === guid);
    if (row) return row;
  }
  for (const npc of (area.panorama ? area.panorama.npcs : [])) {
    const row = npc.quest_links.find((q) => q.guid === guid);
    if (row) return row;
  }
  return area.panorama ? area.panorama.unassigned.find((c) => c.guid === guid && c.template) : null;
}

// Several answers can end a conversation with the same script event.
function uniqueEndings(endings) {
  const seen = new Set();
  return (endings || []).filter((e) => !seen.has(e.ending) && seen.add(e.ending));
}

// Against the simulation: "now" (the node's current offer plays it), "yes"
// (its offer condition holds), "no", or "" without a simulation.
function simMatch(guid, nodeId) {
  const n = sim && sim.layout.nodes[nodeId];
  if (!n) return "";
  if ((n.offered_now || []).includes(guid)) return "now";
  return (n.offered || []).includes(guid) ? "yes" : "no";
}

// Matching conversations first (current offer, then holding), others after.
function bySimMatch(rows, nodeId) {
  const rank = { now: 0, yes: 1, "": 1, no: 2 };
  return rows.map((row, i) => [row, i]).sort(([a, i], [b, j]) =>
    rank[simMatch(a.guid, nodeId)] - rank[simMatch(b.guid, nodeId)] || i - j).map(([row]) => row);
}

function matchBadge(match) {
  return match === "now" ? `<span class="chip match-now" title="The node offers this conversation now">▶ plays now</span>`
    : match === "yes" ? `<span class="chip match-yes" title="Its condition holds in the simulation">matches state</span>`
      : match === "no" ? `<span class="chip match-no" title="Its condition does not hold in the simulation">not offered</span>` : "";
}

function questLinkCard(q, nodeId) {
  const match = simMatch(q.guid, nodeId);
  const tags = (q.conditions && q.conditions.tags) || [];
  const cond = Object.entries(q.conditions || {}).filter(([k]) => k !== "tags").map(([k, v]) => `${k}: ${v}`);
  return `<div class="card ${match === "no" ? "dim" : ""} ${match === "now" ? "now" : ""}"><div class="card-head"><div class="title">${esc(q.quest)} <span class="muted">· ${esc(q.role)}</span> ${matchBadge(match)}</div>
    <button type="button" class="small" data-edit-quest="${esc(q.quest)}|${esc(q.guid)}">Tags…</button></div>
    ${tags.length ? tagsHtml(tags) : `<div class="muted small-text">untagged — offered by its role</div>`}
    <button class="link" data-conv="${esc(q.guid)}">${esc(q.name)}</button>
    ${chips([q.npc, q.faction].filter(Boolean), "")}${chips(cond.filter((c) => !c.startsWith("actions:")), "prereq")}
    ${actionChips(q)}${q.actions_default ? `<div class="muted small-text">default by role; edit via the conversation's Actions…</div>` : ""}
    ${q.start_hook ? chips([`start hook: ${q.start_hook}`], "event") : ""}</div>`;
}

function conversationCard(c, nodeId) {
  const match = simMatch(c.guid, nodeId);
  const tags = (c.trigger && c.trigger.tags) || [];
  const trigger = Object.entries(c.trigger || {}).filter(([k]) => !["source", "label", "tags", "actions", "edited_tags", "edited_actions"].includes(k)).map(([k, v]) => `${k}: ${v}`);
  const endings = (c.endings || []).length > 1 ? `${c.endings.length} endings` : "";
  const ends = uniqueEndings(c.endings);
  const playable = rules && rules.rules && ends.length;
  const play = playable ? `<div class="actions">${ends.slice(0, 4).map((e) =>
    `<button type="button" class="small" data-choose="${esc(c.guid)}" data-ending="${esc(e.ending)}" data-event="${e.event ? 1 : 0}" title="${esc(e.text)}">▶ ${esc(ends.length > 1 ? e.ending : "Finish")}</button>`).join("")}</div>` : "";
  return `<div class="card ${match === "no" ? "dim" : ""} ${match === "now" ? "now" : ""}"><div class="card-head"><button class="link title" data-conv="${esc(c.guid)}">${esc(c.name)}</button>
      <button type="button" class="small" data-edit-conv="${esc(c.guid)}">Tags…</button></div>
    ${matchBadge(match)}
    ${tags.length ? tagsHtml(tags) : `<div class="muted small-text">untagged — selected by name/visit metadata</div>`}
    ${c.edited ? `<div class="muted small-text">saved to campaign_data/${esc(c.template)}.json · applied at server start</div>` : ""}
    ${play}${actionChips(c)}${chips(trigger, "")}${chips(endings ? [endings] : [], "")}${chips(c.hooks.events.map(eventText), "event")}${chips(c.hooks.prerequisites.map(qualifierText), "prereq")}</div>`;
}

let tagEdit = null;   // {tags, title, onSave, onClear}

function tagDialog() { return document.getElementById("tags"); }

// A tag list ["a", "b"] (all hold) or "or" groups [["a"], ["b", "c"]].
const isGroups = (tags) => Array.isArray(tags) && tags.length > 0 && tags.every(Array.isArray);
const toGroups = (tags) => (isGroups(tags) ? tags.map((g) => [...g]) : [[...(tags || [])]]);

function fromGroups(groups) {
  const kept = groups.filter((g) => g.length);
  return kept.length > 1 ? kept : kept[0] || [];
}

// Chips for a tag list or "or" groups.
function tagsHtml(tags) {
  if (!isGroups(tags)) return chips(tags || [], "tag");
  return `<div class="tag-groups">${tags.map((g) => chips(g, "tag")).join(`<span class="or">or</span>`)}</div>`;
}

function openTagEditor(options) {
  tagEdit = { kinds: TAG_KINDS, allowGroups: true, allowNot: true, ...options,
    groups: toGroups(options.tags), active: 0 };
  const kinds = Object.keys(tagEdit.kinds).map((k) => `<option value="${k}">${k}</option>`).join("");
  document.getElementById("tags-body").innerHTML = `
    <h2>${esc(options.title)}</h2><div class="muted">${options.subtitle || ""}</div>
    <div id="tag-list" class="tag-list"></div>
    ${tagEdit.allowGroups ? `<div class="actions"><button type="button" id="tag-add-group" class="small" title="Holds when any group holds">+ "or" group</button></div>` : ""}
    <div class="tag-builder">
      <label class="toggle" ${tagEdit.allowNot ? "" : "hidden"}><input type="checkbox" id="tag-not"> not</label>
      <select id="tag-kind">${kinds}</select>
      <input id="tag-value" list="tag-values" placeholder="value" autocomplete="off">
      <input id="tag-extra" list="tag-extras" placeholder="step / ending (optional)" autocomplete="off">
      <button type="button" id="tag-add" class="primary">Add</button>
      <datalist id="tag-values"></datalist><datalist id="tag-extras"></datalist>
    </div>
    <div class="tag-builder"><input id="tag-raw" placeholder="${esc(tagEdit.rawPlaceholder || "or type a tag, e.g. !faction(Ardent)")}" autocomplete="off">
      <button type="button" id="tag-raw-add">Add</button></div>
    <div id="tag-hint" class="muted small-text"></div>
    <div id="tag-error" class="error"></div>
    <div class="actions"><button type="button" id="tag-save" class="primary">Save</button>
      ${options.onClear ? `<button type="button" id="tag-clear">${esc(options.clearLabel || "Remove")}</button>` : ""}
      <button type="button" id="tag-cancel">Cancel</button></div>`;
  renderTagList();
  updateTagKind();
  tagDialog().showModal();
}

function renderTagList() {
  const groups = tagEdit.groups;
  const many = groups.length > 1;
  document.getElementById("tag-list").innerHTML = groups.map((group, g) => {
    const tags = group.map((t, i) => `<span class="chip tag">${esc(tagEdit.formatTag ? tagEdit.formatTag(t) : t)} <button type="button" class="chip-x" data-tag-remove="${g}:${i}" aria-label="Remove">×</button></span>`).join("");
    const empty = `<span class="muted">${esc(many ? "empty group — add tags or remove it" : (tagEdit.emptyText || "No tags: always holds."))}</span>`;
    return `<div class="tag-group ${many && g === tagEdit.active ? "active" : ""}" data-group="${g}">
      ${many ? `<span class="muted small-text">all of</span>` : ""}${tags || empty}
      ${many ? `<button type="button" class="chip-x" data-group-remove="${g}" title="Remove this group">✕ group</button>` : ""}</div>`;
  }).join(`<div class="or">or</div>`);
}

async function updateTagKind() {
  const kind = document.getElementById("tag-kind").value;
  const spec = tagEdit.kinds[kind];
  document.getElementById("tag-hint").textContent = spec.hint;
  const values = spec.values();
  let value = document.getElementById("tag-value");
  if (spec.select) {
    if (value.tagName !== "SELECT") {
      const select = document.createElement("select");
      select.id = "tag-value";
      value.replaceWith(select);
      value = select;
    }
    value.innerHTML = `<option value="">Choose an encounter scene…</option>${values.map((option) =>
      `<option value="${esc(option.value)}">${esc(option.label)}</option>`).join("")}`;
    document.getElementById("tag-values").innerHTML = "";
  } else {
    if (value.tagName !== "INPUT") {
      const input = document.createElement("input");
      input.id = "tag-value";
      input.setAttribute("list", "tag-values");
      input.autocomplete = "off";
      value.replaceWith(input);
      value = input;
    }
    value.placeholder = "value";
    document.getElementById("tag-values").innerHTML = values.map((option) => {
      const item = typeof option === "string" ? { value: option, label: "" } : option;
      return `<option value="${esc(item.value)}"${item.label ? ` label="${esc(item.label)}"` : ""}>`;
    }).join("");
  }
  let extra = document.getElementById("tag-extra");
  if (spec.extraSelect) {
    if (extra.tagName !== "SELECT") {
      const select = document.createElement("select");
      select.id = "tag-extra";
      extra.replaceWith(select);
      extra = select;
    }
    const options = (spec.extraValues && spec.extraValues()) || [];
    extra.innerHTML = `<option value="">No opening conversation</option>${options.map((option) =>
      `<option value="${esc(option.value)}">${esc(option.label)}</option>`).join("")}`;
    extra.setAttribute("aria-label", spec.extraLabel || "optional value");
    extra.hidden = false;
    document.getElementById("tag-extras").innerHTML = "";
  } else if (spec.extra) {
    if (extra.tagName !== "INPUT") {
      const input = document.createElement("input");
      input.id = "tag-extra";
      input.setAttribute("list", "tag-extras");
      input.autocomplete = "off";
      extra.replaceWith(input);
      extra = input;
    }
    extra.placeholder = spec.extraLabel || "step / ending (optional)";
    extra.hidden = false;
    document.getElementById("tag-extras").innerHTML = "";
  } else {
    extra.hidden = true;
    extra.value = "";
  }
  extra.value = "";
}

async function updateTagExtras() {
  const spec = tagEdit.kinds[document.getElementById("tag-kind").value];
  if (!spec.extra) return;
  const value = document.getElementById("tag-value").value.trim();
  let options = [];
  try { options = value ? await spec.extra(value) : []; } catch (error) { options = []; }
  document.getElementById("tag-extras").innerHTML = options.map((o) => `<option value="${esc(o.value)}" label="${esc(o.label)}">`).join("");
}

function builtTag() {
  const kind = document.getElementById("tag-kind").value;
  const value = document.getElementById("tag-value").value.trim();
  const spec = tagEdit.kinds[kind];
  const extra = (spec.extra || spec.extraSelect) ? document.getElementById("tag-extra").value.trim() : "";
  if (!value) return "";
  const not = document.getElementById("tag-not").checked ? "!" : "";
  return kind === "outcome" ? `${not}outcome:${value}` : `${not}${kind}(${value}${extra ? `,${extra}` : ""})`;
}

function addTag(tag) {
  if (!tag) return;
  const group = tagEdit.groups[tagEdit.active];
  if (!group.includes(tag)) group.push(tag);
  renderTagList();
  document.getElementById("tag-error").textContent = "";
}

document.getElementById("tags").addEventListener("change", (event) => {
  if (event.target.id === "tag-kind") updateTagKind();
  if (event.target.id === "tag-value") updateTagExtras();
});

document.getElementById("tags").addEventListener("click", async (event) => {
  const remove = event.target.closest("[data-tag-remove]");
  if (remove) {
    const [g, i] = remove.dataset.tagRemove.split(":").map(Number);
    tagEdit.groups[g].splice(i, 1);
    renderTagList();
    return;
  }
  const dropGroup = event.target.closest("[data-group-remove]");
  if (dropGroup) {
    tagEdit.groups.splice(Number(dropGroup.dataset.groupRemove), 1);
    tagEdit.active = Math.min(tagEdit.active, tagEdit.groups.length - 1);
    renderTagList();
    return;
  }
  const pick = event.target.closest("[data-group]");
  if (pick) { tagEdit.active = Number(pick.dataset.group); renderTagList(); return; }
  const id = event.target.id;
  if (id === "tag-add-group") { tagEdit.groups.push([]); tagEdit.active = tagEdit.groups.length - 1; renderTagList(); return; }
  if (id === "tag-add") { addTag(builtTag()); document.getElementById("tag-value").value = ""; document.getElementById("tag-extra").value = ""; }
  if (id === "tag-raw-add") { const raw = document.getElementById("tag-raw"); addTag(raw.value.trim()); raw.value = ""; }
  if (id === "tag-cancel") tagDialog().close();
  if (id === "tag-save" || id === "tag-clear") {
    try {
      await (id === "tag-save" ? tagEdit.onSave(fromGroups(tagEdit.groups)) : tagEdit.onClear());
      tagDialog().close();
      await refreshData();
    } catch (error) {
      document.getElementById("tag-error").textContent = error.message;
    }
  }
});

function editConversationTags(guid) {
  const row = conversationRow(guid);
  if (!row) { els.status.textContent = "Only node conversations can be tagged"; return; }
  openTagEditor({
    title: row.name,
    subtitle: `Offered while <b>all</b> tags hold; <code>outcome:success|fail</code> makes it the node's battle-result conversation.
      Saved to <code>campaign_data/${esc(row.template)}.json</code> and applied to the database when the server starts.`,
    tags: (row.trigger && row.trigger.tags) || [],
    emptyText: "No tags: selected by its name/visit metadata as before.",
    onSave: (tags) => (row.quest
      ? postJSON("/api/edit/quest-tags", { template: row.template, quest: row.quest, conversation: row.guid, tags })
      : postJSON("/api/edit/conversation-tags", { template: row.template, conversation: row.guid, tags })),
  });
}

function editNodeState(node, key) {
  const spec = nodeRules(node) || {};
  const tagView = (rules && rules.rules && rules.rules.state_tags && rules.rules.state_tags[node.id]) || {};
  const complex = spec[key] !== undefined && !Array.isArray(tagView[key]);
  openTagEditor({
    title: `${node.id} · ${stateLabel(key)}`,
    subtitle: `${esc((node.authored && node.authored.title) || "")} — the ${isPanorama() ? "NPC" : "node"} is <b>${esc(stateLabel(key))}</b> when all tags hold.
      ${complex ? `<br>⚠ The current rule cannot be shown as tags (too many "or" groups or an unsupported condition): <code>${esc(fmtCond(spec[key]))}</code>. Saving replaces it.` : ""}`,
    tags: Array.isArray(tagView[key]) ? tagView[key] : [],
    emptyText: "No tags: always holds once saved.",
    clearLabel: `Use default (${stateDefault(key)})`,
    onSave: (tags) => postJSON("/api/edit/node-state", { zone: zoneId, node: node.id, state: key, tags }),
    onClear: () => postJSON("/api/edit/node-state", { zone: zoneId, node: node.id, state: key, tags: null }),
  });
}

// Reload tables and rules after a save, keeping the map, selection and
// simulation (re-evaluated against the new rules).
async function refreshData() {
  area = await getJSON(`/api/area?zone=${encodeURIComponent(zoneId)}`);
  nodesById = new Map(area.nodes.map((n) => [n.id, n]));
  try { rules = await getJSON(`/api/rules?zone=${encodeURIComponent(zoneId)}`); } catch (error) { rules = null; }
  if (sim && rules && rules.rules) {
    try {
      const result = await postJSON("/api/sim/apply", { zone: zoneId, state: sim.state,
        action: { type: "set_faction", faction: sim.state.faction, race: sim.state.race } });
      sim.layout = result.layout;
    } catch (error) { els.status.textContent = error.message; }
  }
  renderQuests();
  renderIssues();
  renderSim();
  renderLegend();
  if (area.zone.kind === "panorama") {
    const npc = area.panorama.npcs.find((n) => n.name === pano.npc);
    if (npc) selectNpc(npc); else renderPanorama();
  } else if (selected && nodesById.has(selected.id)) select(nodesById.get(selected.id));
  else draw();
}

// ---------------------------------------------------------------- conversation tree
async function openConversation(guid) {
  const body = document.getElementById("conversation-body");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  document.getElementById("conversation").showModal();
  let c;
  try { c = await getJSON(`/api/conversation/${encodeURIComponent(guid)}`); }
  catch (error) { body.innerHTML = `<p class="muted">${esc(error.message)}</p>`; return; }
  const endingOf = new Map(c.endings.map((e) => [e.answer, e]));
  const row = conversationRow(c.guid);
  const shown = new Set();
  const flag = (ending) => `conversation(${c.name}${ending ? `,${ending}` : ""})`;

  function actionLine(ending) {
    if (!row) return "";
    const list = actionsFor(row, ending);
    return `<div class="ending-actions">${list.length ? chips(list.map(actionText), "action") : `<span class="muted">no actions</span>`}
      <button type="button" class="small" data-edit-actions="${esc(c.guid)}" data-ending="${esc(ending)}">Actions…</button></div>`;
  }

  const playable = Boolean(conversationNode(c.guid) && rules && rules.rules);
  const chooseBtn = (e) => (playable ? `<button type="button" class="small primary" data-choose="${esc(c.guid)}" data-ending="${esc(e.ending)}" data-event="${e.event ? 1 : 0}" title="Apply this ending in the simulator">▶ Choose</button> ` : "");

  function endingHtml(e) {
    return `<div class="ending">${chooseBtn(e)}<span class="ending-mark">■ ends</span> <b>${esc(e.ending)}</b>
      · sets <code>${esc(flag(e.ending))}</code>${actionLine(e.ending)}
      ${e.server_visible ? "" : `<span class="warn" title="The client reports a choice only through a ServerScriptEvent">simulator only: the server cannot tell this ending apart</span>`}
      <button type="button" class="small" data-copy="${esc(flag(e.ending))}">Copy tag</button></div>`;
  }

  function questionHtml(qid) {
    if (shown.has(qid)) return `<div class="tree-ref">↪ <button type="button" class="link" data-jump="cq-${esc(qid)}">Q${esc(qid)}</button> (shown above)</div>`;
    const q = c.questions[qid];
    if (!q) return `<div class="muted">missing question Q${esc(qid)}</div>`;
    shown.add(qid);
    const continuations = new Set();
    let html = `<div class="tq" id="cq-${esc(qid)}"><div class="qid">Q${esc(qid)}${qid === c.start ? " · start" : ""}${q.speaker_override ? ` · ${esc(q.speaker_override)}` : ""}</div>
      <div class="text">${esc(q.text)}</div><ul class="tans">`;
    for (const aid of q.answers) {
      const a = c.answers[aid];
      if (!a) continue;
      html += `<li><div class="ans">▸ ${esc(a.text)} ${chips(a.prerequisites.map(qualifierText), "prereq")}${chips(a.events.map(eventText), "event")}`;
      const ending = endingOf.get(aid);
      if (!ending) {
        const next = String(a.next);
        continuations.add(next);
        html += ` <span class="tq-next-link">→ <button type="button" class="link" data-jump="cq-${esc(next)}">Q${esc(next)}</button></span>`;
      }
      html += `</div>`;
      if (ending) html += endingHtml(ending);
      html += `</li>`;
    }
    html += `</ul>`;
    for (const next of continuations) {
      html += `<div class="tq-next">${questionHtml(next)}</div>`;
    }
    return html + `</div>`;
  }

  let html = `<h2>${esc(c.name)}</h2><div class="muted">${esc(c.champion_name || c.owner || "")} · <code>${esc(c.guid)}</code></div>`;
  if (row) {
    const tags = (row.trigger && row.trigger.tags) || [];
    html += `<div class="row">${tags.length ? tagsHtml(tags) : `<span class="muted">untagged</span>`}
      <button type="button" class="small" data-edit-conv="${esc(c.guid)}">Edit tags…</button></div>`;
  }
  html += `<h3>Endings</h3><p class="muted small-text">Finishing sets <code>${esc(flag(""))}</code>, plus the ending's flag below.</p>`;
  html += c.endings.map(endingHtml).join("") || `<p class="muted">No answer ends this conversation.</p>`;
  if (row) html += `<div class="ending"><span class="ending-mark">■</span> <b>any ending</b>${actionLine("*")}</div>`;
  html += `<h3>Tree</h3>${questionHtml(c.start)}`;
  const orphans = Object.keys(c.questions).filter((qid) => !shown.has(qid));
  if (orphans.length) html += `<h3>Not reachable from the start</h3>${orphans.map(questionHtml).join("")}`;
  body.innerHTML = html;
}

document.getElementById("conversation-body").addEventListener("click", (event) => {
  const jump = event.target.closest("[data-jump]");
  if (jump) { document.getElementById(jump.dataset.jump)?.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy).then(() => { copy.textContent = "Copied"; }, () => {});
  }
});

// One line per reward: gold, xp, chests (per race), cards, items.
// Small pictures of everything a reward object grants (chests, cards, equipment, stardust).
function rewardIconStrip(r) {
  if (!r || !rewardCatalog) return "";
  const picks = new Map();                       // url -> title and accessible text
  const item = (guid, label, alt = label) => { const u = iconUrl(guid); if (u) picks.set(u, { title: label, alt }); };
  const card = (guid) => { const u = cardIconUrl(guid); if (u) picks.set(u, { title: cardTitle(guid), alt: cardText(guid) || cardName(guid) }); };
  const chestName = (g) => (rewardCatalog.chests.find((c) => c.guid === g) || {}).name || "chest";
  if (r.chest_guid) item(r.chest_guid, chestName(r.chest_guid));
  Object.values(r.chest_guid_by_race || {}).forEach((g) => item(g, chestName(g)));
  if (r.card_guid && GUID_RE.test(r.card_guid)) card(r.card_guid);
  (r.cards || []).forEach((c) => card(c.guid));
  Object.values(r.card_guid_by_race || {}).forEach(card);
  (r.items || []).forEach((i) => item(i.guid, equipmentTitle(i.guid), equipmentText(i.guid) || equipmentName(i.guid)));
  if (r.item_guid) item(r.item_guid, equipmentTitle(r.item_guid), equipmentText(r.item_guid) || equipmentName(r.item_guid));
  Object.values(r.item_guid_by_race || {}).forEach((g) => item(g, equipmentTitle(g), equipmentText(g) || equipmentName(g)));
  for (const sd of r.stardust || []) item(rewardCatalog.stardust[sd.rarity], `${sd.rarity} stardust`);
  const shown = [...picks].slice(0, 14);
  return shown.length ? `<div class="icon-strip">${shown.map(([u, text]) => `<img class="item-icon ${u.includes("/cards/") ? "card-icon" : ""}" src="${u}" title="${esc(text.title)}" alt="${esc(text.alt)}">`).join("")}${picks.size > 14 ? `<span class="muted">+${picks.size - 14}</span>` : ""}</div>` : "";
}

function rewardLines(rewards) {
  const r = rewards || {};
  const lines = [];
  if (r.gold) lines.push(`${r.gold} gold`);
  if (r.xp) lines.push(`${r.xp} xp`);
  if (r.chest_guid) lines.push(`chest ${(rewardCatalog && (rewardCatalog.chests.find((c) => c.guid === r.chest_guid) || {}).name) || r.chest_guid}`);
  for (const key of ["chest_guid_by_race", "card_guid_by_race", "item_guid_by_race"]) {
    const map = r[key];
    if (map && Object.keys(map).length) lines.push(`${key.replace("_guid_by_race", "")} by race: ${Object.keys(map).join(", ")}`);
  }
  for (const sd of r.stardust || []) lines.push(`${sd.amount} ${sd.rarity} stardust${sd.probability < 100 ? ` (${sd.probability}%)` : ""}`);
  for (const key of ["cards", "items"]) if ((r[key] || []).length) lines.push(`${r[key].length} ${key === "items" ? "equipment" : key}`);
  return lines;
}

// Conversations (in this zone) whose ending runs complete_quest(script).
// Conversations whose ending actions run ``kind`` ("start" | "complete") on
// the quest; quest rows without saved actions carry their role's default.
function questActionRows(script, kind) {
  // The server's index covers every zone (a Crayburn quest started in AZ0).
  const indexed = area.quest_actions && area.quest_actions[script];
  if (area.quest_actions) {
    return ((indexed && indexed[kind]) || []).map((r) => ({
      guid: r.guid,
      name: `${r.name || r.guid}${r.template && r.template !== area.campaign ? ` (${r.template}${r.node ? ` · ${r.node}` : ""})` : ""}`,
    }));
  }
  const found = [];
  const pattern = new RegExp(`^${kind}_?quest\\((.+)\\)$`, "i");
  for (const node of area.nodes || []) {
    for (const row of [...(node.conversations || []), ...(node.quest_links || [])]) {
      const actions = (row.trigger && row.trigger.actions) || {};
      const hit = Object.values(actions).some((list) => (list || []).some((a) => {
        const match = String(a).replace(/\s/g, "").match(pattern);
        return match && match[1] === script;
      }));
      if (hit && !found.some((f) => f.guid === row.guid)) found.push({ guid: row.guid, name: row.name });
    }
  }
  return found;
}

function renderQuests() {
  const active = new Map((state ? state.quests : []).map((q) => [q.script, q]));
  let html = state ? `<p class="muted">Quest progress for ${esc(state.champion_name)}.</p>` : "";
  for (const q of area.quests) {
    const progress = active.get(q.script);
    const status = progress ? (progress.finished ? ["finished", "on"] : [`active · step ${Number(progress.objective_index) + 1}`, "event"]) : null;
    const rewards = rewardLines(q.rewards);
    const starters = questActionRows(q.script, "start");
    const completers = questActionRows(q.script, "complete");
    html += `<div class="card"><div class="card-head"><div class="title">${esc(q.title || q.script)}</div>
      <button type="button" class="small" data-edit-rewards="${esc(q.script)}">Rewards…</button></div>
      <div class="muted"><code>${esc(q.script)}</code> · ${esc(q.group || "")}</div>
      ${status ? chips([status[0]], status[1]) : ""}
      ${q.start_hook ? chips([`start hook: ${q.start_hook}`], "prereq") : ""}
      <ol>${(q.objectives || []).map((o) => {
        const guid = o.conversation || (o.conversation_ids || [])[0];
        const link = guid ? ` <button type="button" class="link" data-conv="${esc(guid)}">conversation</button>` : "";
        return `<li>${esc(o.title || o.id)} <span class="muted">(${esc(o.type || "")})</span>${link}</li>`;
      }).join("")}</ol>
      <div class="muted small-text">started by: ${starters.length ? starters.map((c) => `<button type="button" class="link" data-conv="${esc(c.guid)}">${esc(c.name)}</button>`).join(", ") : "<b>nothing</b> (no start_quest action anywhere)"}</div>
      ${completers.length ? `<div class="muted small-text">completed by: ${completers.map((c) => `<button type="button" class="link" data-conv="${esc(c.guid)}">${esc(c.name)}</button>`).join(", ")}</div>` : ""}
      <div class="small-text">${rewards.length ? `<b>On completion:</b> ${rewards.map(esc).join(" · ")}` : `<span class="muted">No completion reward.</span>`}</div>${rewardIconStrip(q.rewards)}</div>`;
  }
  document.getElementById("tab-quests").innerHTML = html;
}

// ---- Stardust rows (rarity, amount, probability %) shared by both reward editors ----
const STARDUST_RARITIES = ["common", "uncommon", "rare", "legendary", "promo"];

function stardustRowHtml(entry, cls, delAttrs) {
  const sel = STARDUST_RARITIES.map((r) => `<option value="${r}" ${r === entry.rarity ? "selected" : ""}>${r[0].toUpperCase() + r.slice(1)}</option>`).join("");
  return `<div class="reward-row"><select class="${cls}-rarity" data-icon="stardust">${sel}</select>${iconImg(rewardCatalog.stardust[entry.rarity || "common"])}
    <label>amount <input type="number" min="1" class="${cls}-amount" value="${entry.amount || 1}"></label>
    <label>chance <input type="number" min="1" max="100" step="any" class="${cls}-prob" value="${entry.probability === undefined ? 100 : entry.probability}">%</label>
    <button type="button" class="small" ${delAttrs}>✕</button></div>`;
}

function collectStardust(root, cls) {
  return [...root.querySelectorAll(`.${cls}-rarity`)].map((select, i) => ({
    rarity: select.value,
    amount: parseInt(root.querySelectorAll(`.${cls}-amount`)[i].value, 10) || 1,
    probability: parseFloat(root.querySelectorAll(`.${cls}-prob`)[i].value) || 100,
  }));
}

// ---- Quest reward editor: gold / xp / chests / cards (same or per race) ----
let rewardCatalog = null;     // {chests, cards, races}, fetched on first use
let rewardDraft = null;       // the reward object being edited
const REWARD_FORM_KEYS = ["gold", "xp", "chest_guid", "chest_guid_by_race", "cards", "card_guid_by_race", "items", "item_guid_by_race", "stardust"];
const GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function loadRewardCatalog() {
  if (!rewardCatalog) rewardCatalog = await getJSON("/api/rewards-catalog");
  return rewardCatalog;
}

function cardGuid(text) {
  const value = String(text || "").trim();
  if (!value) return "";
  if (GUID_RE.test(value)) return value.toLowerCase();
  const lower = value.toLowerCase();
  // The label ("Name (alt-art)") is exact; a bare name means the base printing.
  const match = rewardCatalog.cards.find((c) => c.label.toLowerCase() === lower) ||
    rewardCatalog.cards.find((c) => !c.alt && c.name.toLowerCase() === lower);   // PvE printings sort first
  return match ? match.guid : "";
}
function equipmentGuid(text) {
  const value = String(text || "").trim();
  if (!value) return "";
  if (GUID_RE.test(value)) return value.toLowerCase();
  const match = rewardCatalog.equipment.find((e) => e.name.toLowerCase() === value.toLowerCase());
  return match ? match.guid : "";
}
const equipmentName = (guid) => (rewardCatalog.equipment.find((e) => e.guid === String(guid).toLowerCase()) || {}).name || guid || "";
const cardName = (guid) => (rewardCatalog.cards.find((c) => c.guid === String(guid).toLowerCase()) || {}).label || guid || "";
const equipmentText = (guid) => {
  const equipment = rewardCatalog.equipment.find((e) => e.guid === String(guid || "").toLowerCase());
  return equipment && equipment.text || "";
};
const equipmentTitle = (guid) => {
  const equipment = rewardCatalog.equipment.find((e) => e.guid === String(guid || "").toLowerCase());
  return equipment && ([equipment.text, equipment.set_name].filter(Boolean).join(" · ") || equipment.name) || equipmentName(guid);
};
const cardText = (guid) => {
  const card = rewardCatalog.cards.find((c) => c.guid === String(guid || "").toLowerCase());
  return card && card.text || "";
};
const cardTitle = (guid) => {
  const card = rewardCatalog.cards.find((c) => c.guid === String(guid || "").toLowerCase());
  return card && ([card.text, card.set_name].filter(Boolean).join(" · ") || card.label) || cardName(guid);
};

// Item art (generated/items/<art number>.png, from AssetExtraction/extract_item_icons.py).
function iconUrl(guid) {
  const art = rewardCatalog && rewardCatalog.art && rewardCatalog.art[String(guid || "").toLowerCase()];
  return art ? `/generated/items/${art}.png` : "";
}
function cardIconUrl(guid) {
  const art = rewardCatalog && rewardCatalog.card_art && rewardCatalog.card_art[String(guid || "").toLowerCase()];
  return art ? `/generated/cards/${art}.jpg` : "";
}
const cardImg = (guid) => `<img class="item-icon card-icon" src="${cardIconUrl(guid)}" ${cardIconUrl(guid) ? "" : "hidden"} title="${esc(cardTitle(guid))}" alt="${esc(cardText(guid) || cardName(guid))}">`;
const iconImg = (guid) => {
  const equipment = rewardCatalog.equipment.find((e) => e.guid === String(guid || "").toLowerCase());
  const alt = equipment ? equipment.text || equipment.name : "";
  const title = equipment ? [equipment.text, equipment.set_name].filter(Boolean).join(" · ") || equipment.name : "";
  return `<img class="item-icon" src="${iconUrl(guid)}" ${iconUrl(guid) ? "" : "hidden"} ${equipment ? `title="${esc(title)}"` : ""} alt="${esc(alt)}">`;
};

// Keep the icon beside a picker in step with its value.
function refreshIcon(target) {
  const kind = target.dataset && target.dataset.icon;
  const img = kind && target.nextElementSibling;
  if (!img || !img.classList.contains("item-icon")) return;
  const guid = kind === "chest" ? target.value : kind === "equip" ? equipmentGuid(target.value)
    : kind === "card" ? cardGuid(target.value)
    : kind === "stardust" ? rewardCatalog.stardust[target.value] : "";
  const url = kind === "card" ? cardIconUrl(guid) : iconUrl(guid);
  img.src = url;
  img.hidden = !url;
  img.alt = kind === "card" ? cardText(guid) || cardName(guid)
    : kind === "equip" ? equipmentText(guid) || equipmentName(guid) : "";
  img.title = kind === "card" ? cardTitle(guid)
    : kind === "equip" ? equipmentTitle(guid) : "";
}
for (const id of ["rewards", "scene-rewards"]) {
  for (const type of ["change", "input"]) {
    document.getElementById(id).addEventListener(type, (event) => refreshIcon(event.target));
  }
}

function chestSelect(id, value) {
  return `<select id="${id}" data-icon="chest"><option value="">— none —</option>${rewardCatalog.chests.map((c) =>
    `<option value="${esc(c.guid)}" ${c.guid === value ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>${iconImg(value)}`;
}

// A race's default chest: "Crayburn Castle (<Race>) Pack" when one exists.
function defaultChest(race) {
  const key = race.replace("'", "").toLowerCase();
  const found = rewardCatalog.chests.find((c) => c.name.toLowerCase() === `crayburn castle (${key}) pack`);
  return found ? found.guid : "";
}

function renderRewardForm() {
  const d = rewardDraft;
  const races = rewardCatalog.races;
  const mode = d.chest_guid_by_race ? "race" : d.chest_guid ? "same" : "none";
  const perRace = d.chest_guid_by_race || {};
  const cards = d.cards || [];
  const byRace = d.card_guid_by_race || {};
  document.getElementById("rewards-form").innerHTML = `
    <div class="reward-row"><label>Gold <input type="number" min="0" id="rw-gold" value="${d.gold || 0}"></label>
      <label>XP <input type="number" min="0" id="rw-xp" value="${d.xp || 0}"></label></div>
    <h4>Chest</h4>
    <div class="reward-row">
      <label><input type="radio" name="rw-chest-mode" value="none" ${mode === "none" ? "checked" : ""}> none</label>
      <label><input type="radio" name="rw-chest-mode" value="same" ${mode === "same" ? "checked" : ""}> same for every race</label>
      <label><input type="radio" name="rw-chest-mode" value="race" ${mode === "race" ? "checked" : ""}> per race</label></div>
    <div id="rw-chest-same" ${mode === "same" ? "" : "hidden"}>${chestSelect("rw-chest", d.chest_guid || "")}</div>
    <div id="rw-chest-race" ${mode === "race" ? "" : "hidden"}>${races.map((r) =>
      `<div class="reward-row"><span class="reward-race">${esc(r)}</span>${chestSelect(`rw-chest-${races.indexOf(r)}`, perRace[r] || "")}</div>`).join("")}
      <button type="button" class="small" data-rw="fill-crayburn">Fill Crayburn chests</button></div>
    <h4>Cards (everyone)</h4>
    <div id="rw-cards">${cards.map((c, i) => `<div class="reward-row">
      <input list="rw-card-list" class="rw-card" data-icon="card" data-i="${i}" value="${esc(cardName(c.guid))}" placeholder="card name">${cardImg(c.guid)}
      <input type="number" min="1" class="rw-qty" data-i="${i}" value="${c.quantity || 1}">
      <button type="button" class="small" data-rw="del-card" data-i="${i}">✕</button></div>`).join("")}</div>
    <button type="button" class="small" data-rw="add-card">+ Card</button>
    <h4>Card by race</h4>
    ${races.map((r, i) => `<div class="reward-row"><span class="reward-race">${esc(r)}</span>
      <input list="rw-card-list" class="rw-race-card" data-icon="card" data-race="${esc(r)}" value="${esc(cardName(byRace[r]))}" placeholder="(none)">${cardImg(byRace[r])}</div>`).join("")}
    <h4>Stardust</h4>
    <div id="rw-stardust">${(d.stardust || []).map((e, i) => stardustRowHtml(e, "rw-sd", `data-rw="del-sd" data-i="${i}"`)).join("")}</div>
    <button type="button" class="small" data-rw="add-sd">+ Stardust</button>
    <h4>Equipment (everyone)</h4>
    <div id="rw-items">${(d.items || []).map((c, i) => `<div class="reward-row">
      <input list="rw-equip-list" class="rw-item" data-icon="equip" data-i="${i}" value="${esc(equipmentName(c.guid))}" placeholder="equipment name">${iconImg(c.guid)}
      <input type="number" min="1" class="rw-item-qty" data-i="${i}" value="${c.quantity || 1}">
      <button type="button" class="small" data-rw="del-item" data-i="${i}">✕</button></div>`).join("")}</div>
    <button type="button" class="small" data-rw="add-item">+ Equipment</button>
    <h4>Equipment by race</h4>
    ${races.map((r) => `<div class="reward-row"><span class="reward-race">${esc(r)}</span>
      <input list="rw-equip-list" class="rw-race-item" data-icon="equip" data-race="${esc(r)}" value="${esc(equipmentName((d.item_guid_by_race || {})[r]))}" placeholder="(none)">${iconImg((d.item_guid_by_race || {})[r])}</div>`).join("")}
    <datalist id="rw-equip-list">${rewardCatalog.equipment.map((e) => `<option value="${esc(e.name)}"></option>`).join("")}</datalist>
    <datalist id="rw-card-list">${rewardCatalog.cards.map((c) => `<option value="${esc(c.label)}">${esc(c.rarity)}${c.pve ? " · PvE" : ""}</option>`).join("")}</datalist>`;
}

// Form -> reward object (keys the form does not edit, e.g. items, are kept).
function collectRewards() {
  const extra = Object.fromEntries(Object.entries(rewardDraft).filter(([k]) => !REWARD_FORM_KEYS.includes(k)));
  const out = { ...extra };
  const number = (id) => Math.max(0, parseInt(document.getElementById(id).value, 10) || 0);
  if (number("rw-gold")) out.gold = number("rw-gold");
  if (number("rw-xp")) out.xp = number("rw-xp");
  const mode = (document.querySelector('input[name="rw-chest-mode"]:checked') || {}).value;
  if (mode === "same" && document.getElementById("rw-chest").value) out.chest_guid = document.getElementById("rw-chest").value;
  if (mode === "race") {
    const map = {};
    rewardCatalog.races.forEach((r, i) => { const v = document.getElementById(`rw-chest-${i}`).value; if (v) map[r] = v; });
    if (Object.keys(map).length) out.chest_guid_by_race = map;
  }
  const cards = [];
  document.querySelectorAll(".rw-card").forEach((input) => {
    const guid = cardGuid(input.value);
    if (input.value.trim() && !guid) throw new Error(`Unknown card “${input.value}”`);
    const qty = parseInt(document.querySelector(`.rw-qty[data-i="${input.dataset.i}"]`).value, 10) || 1;
    if (guid) cards.push({ guid, quantity: qty });
  });
  if (cards.length) out.cards = cards;
  const byRace = {};
  document.querySelectorAll(".rw-race-card").forEach((input) => {
    const guid = cardGuid(input.value);
    if (input.value.trim() && !guid) throw new Error(`Unknown card “${input.value}”`);
    if (guid) byRace[input.dataset.race] = guid;
  });
  if (Object.keys(byRace).length) out.card_guid_by_race = byRace;
  const items = [];
  document.querySelectorAll(".rw-item").forEach((input) => {
    const guid = equipmentGuid(input.value);
    if (input.value.trim() && !guid) throw new Error(`Unknown equipment “${input.value}”`);
    const qty = parseInt(document.querySelector(`.rw-item-qty[data-i="${input.dataset.i}"]`).value, 10) || 1;
    if (guid) items.push({ guid, quantity: qty });
  });
  if (items.length) out.items = items;
  const itemsByRace = {};
  document.querySelectorAll(".rw-race-item").forEach((input) => {
    const guid = equipmentGuid(input.value);
    if (input.value.trim() && !guid) throw new Error(`Unknown equipment “${input.value}”`);
    if (guid) itemsByRace[input.dataset.race] = guid;
  });
  if (Object.keys(itemsByRace).length) out.item_guid_by_race = itemsByRace;
  const stardust = collectStardust(document.getElementById("rewards-form"), "rw-sd");
  if (stardust.length) out.stardust = stardust;
  return out;
}

async function openRewardsDialog(script) {
  const quest = area.quests.find((q) => q.script === script);
  if (!quest) return;
  await loadRewardCatalog();
  rewardDraft = JSON.parse(JSON.stringify(quest.rewards || {}));
  document.getElementById("rewards-body").innerHTML = `<h2>${esc(quest.title || script)} — completion reward</h2>
    <p class="muted small-text">Granted once per champion when the quest completes. Saved to
    <code>campaign_data/quests.json</code> and applied at server start.</p>
    <div id="rewards-form"></div>
    <details><summary>Raw JSON (advanced: items, extra keys)</summary>
      <textarea id="rewards-json" rows="8" spellcheck="false"></textarea>
      <div class="actions"><button type="button" class="small" data-rw="to-json">Form → JSON</button>
      <button type="button" class="small" data-rw="from-json">JSON → form</button></div></details>
    <div id="rewards-error" class="issue" hidden></div>
    <div class="actions"><button type="button" class="primary" data-save-rewards="${esc(script)}">Save</button>
    <button type="button" data-close-rewards>Cancel</button></div>`;
  renderRewardForm();
  document.getElementById("rewards").showModal();
}

function rewardError(message) {
  const error = document.getElementById("rewards-error");
  error.textContent = message || "";
  error.hidden = !message;
}

document.getElementById("rewards").addEventListener("change", (event) => {
  if (event.target.name === "rw-chest-mode") {
    const mode = event.target.value;
    document.getElementById("rw-chest-same").hidden = mode !== "same";
    document.getElementById("rw-chest-race").hidden = mode !== "race";
  }
});

document.getElementById("rewards").addEventListener("click", async (event) => {
  const dialog = document.getElementById("rewards");
  if (event.target.closest("[data-close-rewards]")) { dialog.close(); return; }
  const action = event.target.closest("[data-rw]");
  try {
    if (action) {
      const kind = action.dataset.rw;
      if (kind === "add-card") { rewardDraft = collectRewards(); rewardDraft.cards = [...(rewardDraft.cards || []), { guid: "", quantity: 1 }]; renderRewardForm(); }
      else if (kind === "add-sd") { rewardDraft = collectRewards(); rewardDraft.stardust = [...(rewardDraft.stardust || []), { rarity: "common", amount: 1, probability: 100 }]; renderRewardForm(); }
      else if (kind === "del-sd") { rewardDraft = collectRewards(); rewardDraft.stardust.splice(Number(action.dataset.i), 1); renderRewardForm(); }
      else if (kind === "add-item") { rewardDraft = collectRewards(); rewardDraft.items = [...(rewardDraft.items || []), { guid: "", quantity: 1 }]; renderRewardForm(); }
      else if (kind === "del-item") { rewardDraft = collectRewards(); rewardDraft.items.splice(Number(action.dataset.i), 1); renderRewardForm(); }
      else if (kind === "del-card") { rewardDraft = collectRewards(); rewardDraft.cards.splice(Number(action.dataset.i), 1); renderRewardForm(); }
      else if (kind === "fill-crayburn") {
        const map = {};
        rewardCatalog.races.forEach((r, i) => { const g = defaultChest(r); document.getElementById(`rw-chest-${i}`).value = g; });
      } else if (kind === "to-json") document.getElementById("rewards-json").value = JSON.stringify(collectRewards(), null, 2);
      else if (kind === "from-json") { rewardDraft = JSON.parse(document.getElementById("rewards-json").value || "{}"); renderRewardForm(); }
      rewardError("");
      return;
    }
    const save = event.target.closest("[data-save-rewards]");
    if (!save) return;
    const result = await postJSON("/api/edit/quest-rewards", { quest: save.dataset.saveRewards, rewards: collectRewards() });
    const quest = area.quests.find((q) => q.script === save.dataset.saveRewards);
    if (quest) quest.rewards = result.rewards;
    dialog.close();
    renderQuests();
    els.status.textContent = "Quest reward saved to campaign_data/quests.json (applied at server start)";
  } catch (exc) {
    rewardError(String(exc.message || exc));
  }
});

// ---- Encounter scene rewards (encounter_scenes.rewards_json) ----
// A scene pays one flat entry, or end_of_game_rewards: a list of entries
// (each optionally gated by end_of_game_condition, e.g. a tamed troop).
const SCENE_ENTRY_KEYS = ["gold", "xp", "one_time", "chest_guid", "card_guid", "quantity", "items", "item_guid", "item_guid_by_race", "when", "stardust"];

function sceneEntries(rewards) {
  const r = rewards || {};
  if (Array.isArray(r.end_of_game_rewards)) return r.end_of_game_rewards;
  return SCENE_ENTRY_KEYS.some((k) => r[k] !== undefined) ? [Object.fromEntries(SCENE_ENTRY_KEYS.filter((k) => r[k] !== undefined).map((k) => [k, r[k]]))] : [];
}

function sceneEntryLines(entry) {
  const lines = [];
  if (entry.gold) lines.push(`${entry.gold} gold`);
  if (entry.xp) lines.push(`${entry.xp} xp`);
  if (entry.chest_guid) lines.push(`chest ${(rewardCatalog && (rewardCatalog.chests.find((c) => c.guid === entry.chest_guid) || {}).name) || entry.chest_guid.slice(0, 8)}`);
  if (entry.card_guid) lines.push(`card ${rewardCatalog ? cardName(entry.card_guid) : String(entry.card_guid).slice(0, 8)}${entry.quantity > 1 ? ` ×${entry.quantity}` : ""}`);
  for (const sd of entry.stardust || []) lines.push(`${sd.amount} ${sd.rarity} stardust${sd.probability < 100 ? ` (${sd.probability}%)` : ""}`);
  if ((entry.items || []).length || entry.item_guid || entry.item_guid_by_race) lines.push("equipment");
  const when = (entry.when || []).join(" and ");
  return `${lines.join(" · ") || "(nothing)"}${when ? ` — only if ${when}` : ""}${entry.one_time === false ? " (repeatable)" : " (once)"}`;
}

function sceneRewardSummary(rewards) {
  const entries = sceneEntries(rewards);
  const extra = rewards && rewards.card_choice ? `<div class="muted small-text">${rewards.card_choice.length} card choice(s)</div>` : "";
  if (!entries.length) return `<div class="muted small-text">No end-of-battle reward.</div>${extra}`;
  return `<div class="small-text"><b>Rewards:</b><ul class="reward-list">${entries.map((e) => `<li>${esc(sceneEntryLines(e))}${rewardIconStrip(e)}</li>`).join("")}</ul></div>${extra}`;
}

let sceneDraft = null;    // {guid, entries: [...], extras: {...}, listShape: bool}

function sceneEntryForm(entry, i) {
  const items = entry.items || [];
  const byRace = entry.item_guid_by_race || {};
  const cardIsGuid = !entry.card_guid || GUID_RE.test(entry.card_guid);
  return `<div class="card scene-entry" data-i="${i}">
    <div class="card-head"><b>Reward ${i + 1}</b><button type="button" class="small" data-sr="del-entry" data-i="${i}">Remove</button></div>
    <div class="reward-row"><label>Gold <input type="number" min="0" class="sr-gold" value="${entry.gold || 0}"></label>
      <label>XP <input type="number" min="0" class="sr-xp" value="${entry.xp || 0}"></label>
      <label><input type="checkbox" class="sr-once" ${entry.one_time === false ? "" : "checked"}> once only</label></div>
    <div class="reward-row"><span class="reward-race">Chest</span>${chestSelect(`sr-chest-${i}`, entry.chest_guid || "")}</div>
    <div class="reward-row"><span class="reward-race">Card</span>
      <input list="rw-card-list" class="sr-card" data-icon="card" value="${esc(cardIsGuid ? cardName(entry.card_guid) : entry.card_guid)}" placeholder="(none)" ${cardIsGuid ? "" : "title=\"condition token, kept as is\""}>${cardImg(cardIsGuid ? entry.card_guid : "")}
      <input type="number" min="1" class="sr-qty" value="${entry.quantity || 1}"></div>
    <div class="muted small-text">Stardust</div>
    <div class="sr-stardust">${(entry.stardust || []).map((e, j) => stardustRowHtml(e, "sr-sd", `data-sr="del-sd" data-i="${i}" data-j="${j}"`)).join("")}</div>
    <button type="button" class="small" data-sr="add-sd" data-i="${i}">+ Stardust</button>
    <div class="muted small-text">Equipment</div>
    ${items.map((it, j) => `<div class="reward-row"><input list="rw-equip-list" class="sr-item" data-icon="equip" value="${esc(equipmentName(it.guid))}">${iconImg(it.guid)}
      <input type="number" min="1" class="sr-item-qty" value="${it.quantity || 1}">
      <button type="button" class="small" data-sr="del-item" data-i="${i}" data-j="${j}">✕</button></div>`).join("")}
    <button type="button" class="small" data-sr="add-item" data-i="${i}">+ Equipment</button>
    <div class="muted small-text">Equipment by race</div>
    ${rewardCatalog.races.map((r) => `<div class="reward-row"><span class="reward-race">${esc(r)}</span>
      <input list="rw-equip-list" class="sr-race-item" data-icon="equip" data-race="${esc(r)}" value="${esc(equipmentName(byRace[r]))}" placeholder="(none)">${iconImg(byRace[r])}</div>`).join("")}
    <div class="reward-row"><span class="reward-race">Only if</span>
      <input list="sr-flag-list" class="sr-when" size="34" value="${esc((entry.when || []).join(", "))}" placeholder="flag(tamed:150b0ed9) — blank: always">
    </div>
  </div>`;
}

function renderSceneForm() {
  renderSceneChoices();
  document.getElementById("sr-entries").innerHTML = sceneDraft.entries.map(sceneEntryForm).join("") +
    `<datalist id="rw-card-list">${rewardCatalog.cards.map((c) => `<option value="${esc(c.label)}"></option>`).join("")}</datalist>
     <datalist id="rw-equip-list">${rewardCatalog.equipment.map((e) => `<option value="${esc(e.name)}"></option>`).join("")}</datalist>
     <datalist id="sr-flag-list">${ruleFlags().map((f) => `<option value="flag(${esc(f)})"></option>`).join("")}</datalist>`;
}

function renderSceneChoices() {
  document.getElementById("sr-choice").innerHTML = sceneDraft.choices.map((guid, j) => `<div class="reward-row">
    <input list="rw-card-list" class="sr-choice-card" data-icon="card" value="${esc(cardName(guid))}" placeholder="card name or GUID">${cardImg(guid)}
    <button type="button" class="small" data-sr="del-choice" data-j="${j}">✕</button></div>`).join("");
}

function collectSceneChoices() {
  return [...document.querySelectorAll("#sr-choice .sr-choice-card")].map((input) => {
    const text = input.value.trim();
    if (!text) return "";
    const guid = cardGuid(text);
    if (!guid) throw new Error(`Unknown card “${text}” — type a card name or paste its GUID`);
    return guid;
  }).filter(Boolean);
}

function collectSceneEntries() {
  return [...document.querySelectorAll("#sr-entries .scene-entry")].map((box, i) => {
    const old = sceneDraft.entries[i] || {};
    const out = {};
    const num = (sel) => Math.max(0, parseInt(box.querySelector(sel).value, 10) || 0);
    if (num(".sr-gold")) out.gold = num(".sr-gold");
    if (num(".sr-xp")) out.xp = num(".sr-xp");
    out.one_time = box.querySelector(".sr-once").checked;
    const chest = box.querySelector(`#sr-chest-${i}`).value;
    if (chest) out.chest_guid = chest;
    const cardText = box.querySelector(".sr-card").value.trim();
    if (cardText) {
      const guid = cardGuid(cardText) || (GUID_RE.test(old.card_guid || "") ? "" : cardText);   // keep condition tokens
      if (!guid) throw new Error(`Unknown card “${cardText}”`);
      out.card_guid = guid;
      if (num(".sr-qty") > 1) out.quantity = num(".sr-qty");
    }
    const items = [];
    box.querySelectorAll(".sr-item").forEach((input, j) => {
      const guid = equipmentGuid(input.value);
      if (input.value.trim() && !guid) throw new Error(`Unknown equipment “${input.value}”`);
      if (guid) items.push({ guid, quantity: parseInt(box.querySelectorAll(".sr-item-qty")[j].value, 10) || 1 });
    });
    if (items.length) out.items = items;
    const byRace = {};
    box.querySelectorAll(".sr-race-item").forEach((input) => {
      const guid = equipmentGuid(input.value);
      if (input.value.trim() && !guid) throw new Error(`Unknown equipment “${input.value}”`);
      if (guid) byRace[input.dataset.race] = guid;
    });
    if (Object.keys(byRace).length) out.item_guid_by_race = byRace;
    const stardust = collectStardust(box, "sr-sd");
    if (stardust.length) out.stardust = stardust;
    if (old.item_guid) out.item_guid = old.item_guid;
    const when = box.querySelector(".sr-when").value.split(",").map((t) => t.trim()).filter(Boolean);
    if (when.length) out.when = when;
    return out;
  });
}

// Entries -> rewards_json, keeping the scene's original shape where possible.
function buildSceneRewards(entries) {
  const out = { ...sceneDraft.extras };
  if (sceneDraft.listShape || entries.length > 1) out.end_of_game_rewards = entries;
  else if (entries.length === 1) Object.assign(out, entries[0]);
  const choices = collectSceneChoices();
  if (choices.length) out.card_choice = choices.map((guid) => ({ guid, name: cardName(guid) }));
  return out;
}

async function openSceneRewards(guid) {
  const scene = area.nodes.flatMap((n) => n.encounters).find((e) => e.guid === guid);
  if (!scene) return;
  await loadRewardCatalog();
  const rewards = scene.rewards || {};
  const listShape = Array.isArray(rewards.end_of_game_rewards);
  sceneDraft = { guid, listShape, choices: (rewards.card_choice || []).map((c) => c.guid), entries: JSON.parse(JSON.stringify(sceneEntries(rewards))),
    extras: Object.fromEntries(Object.entries(rewards).filter(([k]) => !SCENE_ENTRY_KEYS.includes(k) && k !== "end_of_game_rewards" && k !== "card_choice")) };
  document.getElementById("scene-rewards-body").innerHTML = `<h2>${esc(scene.title || scene.name)} — battle rewards</h2>
    <p class="muted small-text"><code>${esc(scene.name)}</code> · saved to <code>campaign_data/encounter_scenes.json</code>, applied at server start.
    ${Object.keys(sceneDraft.extras).length ? `Also kept unchanged: ${Object.keys(sceneDraft.extras).map((k) => `<code>${esc(k)}</code>`).join(", ")}.` : ""}</p>
    <div id="sr-entries"></div>
    <button type="button" class="small" data-sr="add-entry">+ Reward</button>
    <h3>Card choice (Shroom Haus)</h3>
    <div class="muted small-text">The player picks one of these cards (<code>card_choice</code>); leave empty for none.</div>
    <div id="sr-choice"></div>
    <button type="button" class="small" data-sr="add-choice">+ Card choice</button>
    <details><summary>Raw JSON (advanced)</summary><textarea id="sr-json" rows="8" spellcheck="false"></textarea>
      <div class="actions"><button type="button" class="small" data-sr="to-json">Form → JSON</button>
      <button type="button" class="small" data-sr="from-json">JSON → form</button></div></details>
    <div id="sr-error" class="issue" hidden></div>
    <div class="actions"><button type="button" class="primary" data-sr="save">Save</button>
    <button type="button" data-sr="close">Cancel</button></div>`;
  renderSceneForm();
  document.getElementById("scene-rewards").showModal();
}

document.getElementById("scene-rewards").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-sr]");
  if (!button) return;
  const dialog = document.getElementById("scene-rewards");
  const error = document.getElementById("sr-error");
  const show = (message) => { error.textContent = message || ""; error.hidden = !message; };
  try {
    const kind = button.dataset.sr;
    const i = Number(button.dataset.i), j = Number(button.dataset.j);
    if (kind === "close") { dialog.close(); return; }
    if (!["close", "from-json", "save", "to-json"].includes(kind)) sceneDraft.choices = collectSceneChoices();
    if (kind === "add-choice") { sceneDraft.choices.push(""); renderSceneForm(); }
    else if (kind === "del-choice") { sceneDraft.choices.splice(j, 1); renderSceneForm(); }
    else if (kind === "add-entry") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries.push({ gold: 0, xp: 0, one_time: true }); renderSceneForm(); }
    else if (kind === "del-entry") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries.splice(i, 1); renderSceneForm(); }
    else if (kind === "add-sd") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries[i].stardust = [...(sceneDraft.entries[i].stardust || []), { rarity: "common", amount: 1, probability: 100 }]; renderSceneForm(); }
    else if (kind === "del-sd") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries[i].stardust.splice(j, 1); renderSceneForm(); }
    else if (kind === "add-item") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries[i].items = [...(sceneDraft.entries[i].items || []), { guid: "", quantity: 1 }]; renderSceneForm(); }
    else if (kind === "del-item") { sceneDraft.entries = collectSceneEntries(); sceneDraft.entries[i].items.splice(j, 1); renderSceneForm(); }
    else if (kind === "to-json") document.getElementById("sr-json").value = JSON.stringify(buildSceneRewards(collectSceneEntries()), null, 2);
    else if (kind === "from-json") {
      const rewards = JSON.parse(document.getElementById("sr-json").value || "{}");
      sceneDraft.listShape = Array.isArray(rewards.end_of_game_rewards);
      sceneDraft.entries = sceneEntries(rewards);
      sceneDraft.choices = (rewards.card_choice || []).map((c) => c.guid);
      sceneDraft.extras = Object.fromEntries(Object.entries(rewards).filter(([k]) => !SCENE_ENTRY_KEYS.includes(k) && k !== "end_of_game_rewards" && k !== "card_choice"));
      renderSceneForm();
    } else if (kind === "save") {
      const result = await postJSON("/api/edit/scene-rewards", { scene: sceneDraft.guid, rewards: buildSceneRewards(collectSceneEntries()) });
      for (const node of area.nodes) for (const scene of node.encounters) if (scene.guid === sceneDraft.guid) scene.rewards = result.rewards;
      dialog.close();
      if (selected) select(selected);
      els.status.textContent = "Scene reward saved to campaign_data/encounter_scenes.json (applied at server start)";
      return;
    }
    show("");
  } catch (exc) {
    show(String(exc.message || exc));
  }
});

document.getElementById("tab-node").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-scene-rewards]");
  if (edit) {
    openSceneRewards(edit.dataset.editSceneRewards).catch((exc) => {
      els.status.textContent = `Could not open the rewards editor: ${exc.message || exc}`;
    });
  }
});

// ---- Encounter scene actions: flags set before the battle / on victory / on defeat ----
const SCENE_TRIGGERS = [["pre", "Before the battle"], ["victory", "On victory"], ["defeat", "On defeat"]];

function sceneActionSummary(actions) {
  const lines = SCENE_TRIGGERS.filter(([key]) => (actions || {})[key] && actions[key].length)
    .map(([key, label]) => `<li>${esc(label)}: ${actions[key].map((a) => `<code>${esc(a)}</code>`).join(" ")}</li>`);
  return lines.length ? `<ul class="reward-list small-text">${lines.join("")}</ul>` : "";
}

let sceneActionsDraft = null;     // {guid, scene8, actions: {pre: [], victory: [], defeat: []}}

// A scene whose victory runs check_tamed(): the simulator's "Win (tamed)"
// stands in for the server finding a tamed troop in the opponent's void.
function sceneChecksTamed(guid) {
  const key = String(guid || "").toLowerCase();
  const scene = area.nodes.flatMap((n) => n.encounters || []).find((e) => String(e.guid).toLowerCase() === key);
  return Boolean(scene && ((scene.actions || {}).victory || []).some(isCheckTamed));
}

const isCheckTamed = (action) => /^check_?tamed\(\s*\)$/i.test(String(action || "").trim());

function sceneActionRow(trigger, action) {
  const tamed = isCheckTamed(action);
  const match = /^(set|clear)_flag\((.*)\)$/.exec(action || "") || [];
  const kind = tamed ? "tamed" : match[1] === "clear" ? "clear" : "set";
  return `<div class="reward-row sa-row" data-trigger="${trigger}">
    <select class="sa-kind"><option value="set" ${kind === "set" ? "selected" : ""}>set flag</option>
      <option value="clear" ${kind === "clear" ? "selected" : ""}>clear flag</option>
      ${trigger === "victory" ? `<option value="tamed" ${tamed ? "selected" : ""}>check tamed (server: tamed troop in the opponent's void → flag tamed:${esc(sceneActionsDraft.scene8)})</option>` : ""}</select>
    <input list="sa-flag-list" class="sa-flag" size="34" value="${esc(match[2] || "")}" ${tamed ? "hidden" : ""} placeholder="flag name, e.g. battle:${esc(sceneActionsDraft.scene8)}:Diff_0">
    <button type="button" class="small" data-sa="del">✕</button></div>`;
}

function renderSceneActionRows() {
  document.getElementById("sa-sections").innerHTML = SCENE_TRIGGERS.map(([key, label]) => `<h4>${esc(label)}</h4>
    <div class="sa-list" data-trigger="${key}">${(sceneActionsDraft.actions[key] || []).map((a) => sceneActionRow(key, a)).join("")}</div>
    <button type="button" class="small" data-sa="add" data-trigger="${key}">+ Action</button>`).join("") +
    `<datalist id="sa-flag-list">${ruleFlags().map((f) => `<option value="${esc(f)}"></option>`).join("")}</datalist>`;
}

function collectSceneActions(keepBlank) {
  const out = {};
  for (const [key] of SCENE_TRIGGERS) {
    out[key] = [...document.querySelectorAll(`#sa-sections .sa-row[data-trigger="${key}"]`)].map((row) => {
      const kind = row.querySelector(".sa-kind").value;
      if (kind === "tamed") return "check_tamed()";
      const flag = row.querySelector(".sa-flag").value.trim();
      return flag || keepBlank ? `${kind}_flag(${flag})` : "";
    }).filter(Boolean);
  }
  return out;
}

function openSceneActions(guid) {
  const scene = area.nodes.flatMap((n) => n.encounters).find((e) => e.guid === guid);
  if (!scene) return;
  sceneActionsDraft = { guid, scene8: guid.slice(0, 8), actions: JSON.parse(JSON.stringify(scene.actions || {})) };
  document.getElementById("scene-actions-body").innerHTML = `<h2>${esc(scene.title || scene.name)} — actions</h2>
    <p class="muted small-text"><code>${esc(scene.name)}</code> · flags live in the campaign's public data; <code>flag(X)</code> tags
    read them. Saved to <code>campaign_data/encounter_scenes.json</code>, applied at server start; the AZ1 rules importer turns them into hooks.</p>
    <div id="sa-sections"></div>
    <div id="sa-error" class="issue" hidden></div>
    <div class="actions"><button type="button" class="primary" data-sa="save">Save</button>
    <button type="button" data-sa="close">Cancel</button></div>`;
  renderSceneActionRows();
  document.getElementById("scene-actions").showModal();
}

document.getElementById("scene-actions").addEventListener("change", (event) => {
  if (!event.target.classList.contains("sa-kind")) return;
  event.target.closest(".sa-row").querySelector(".sa-flag").hidden = event.target.value === "tamed";
});

document.getElementById("scene-actions").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-sa]");
  if (!button) return;
  const dialog = document.getElementById("scene-actions");
  const error = document.getElementById("sa-error");
  try {
    const kind = button.dataset.sa;
    if (kind === "close") { dialog.close(); return; }
    sceneActionsDraft.actions = collectSceneActions(kind !== "save");
    if (kind === "add") { (sceneActionsDraft.actions[button.dataset.trigger] ||= []).push("set_flag()"); renderSceneActionRows(); }
    else if (kind === "del") {
      const row = button.closest(".sa-row");
      const list = sceneActionsDraft.actions[row.dataset.trigger];
      list.splice([...row.parentElement.children].indexOf(row), 1);
      renderSceneActionRows();
    } else if (kind === "save") {
      const result = await postJSON("/api/edit/scene-actions", { scene: sceneActionsDraft.guid, actions: sceneActionsDraft.actions });
      for (const node of area.nodes) for (const scene of node.encounters) if (scene.guid === sceneActionsDraft.guid) scene.actions = result.actions;
      dialog.close();
      if (selected) select(selected);
      els.status.textContent = "Scene actions saved to campaign_data/encounter_scenes.json (applied at server start)";
      return;
    }
    error.hidden = true;
  } catch (exc) {
    error.textContent = String(exc.message || exc);
    error.hidden = false;
  }
});

document.getElementById("tab-node").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-scene-actions]");
  if (edit) openSceneActions(edit.dataset.editSceneActions);
});

// ---- Encounter AI personality (deck strategy): authored value or the deck evaluator's pick ----
const DECK_PERSONALITIES = ["Default", "Aggressive", "BigThreats", "BuildArmy", "Burn", "HandAdvantage", "Reanimation"];

function scenePersonalityRow(e) {
  if (!e.battle || !e.ai_deck) return "";
  const auto = e.ai_auto || {};
  const scores = Object.entries(auto.scores || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
  const current = e.ai_overridden ? e.ai_personality : "";
  const options = [`<option value="" ${current ? "" : "selected"}>Auto — ${esc(auto.personality || "Default")}</option>`]
    .concat(DECK_PERSONALITIES.map((p) => `<option value="${p}" ${current === p ? "selected" : ""}>${p}</option>`));
  return `<div class="reward-row small-text"><span class="muted">AI personality</span>
    <select data-scene-personality="${esc(e.guid)}" title="Deck evaluator scores: ${esc(scores || "n/a")}">${options.join("")}</select>
    ${e.ai_overridden ? "" : `<span class="muted">(evaluated: ${esc(e.ai_personality || auto.personality || "Default")})</span>`}</div>`;
}

document.getElementById("tab-node").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-scene-personality]");
  if (!select) return;
  try {
    const result = await postJSON("/api/edit/scene-personality",
      { scene: select.dataset.scenePersonality, personality: select.value || null });
    for (const node of area.nodes) for (const scene of node.encounters) {
      if (scene.guid === select.dataset.scenePersonality) {
        scene.ai_overridden = Boolean(result.personality);
        scene.ai_personality = result.personality || (scene.ai_auto || {}).personality || "";
      }
    }
    els.status.textContent = result.personality
      ? `AI personality ${result.personality} saved (applied at server start)`
      : "AI personality back to the deck evaluator's pick (applied at server start)";
  } catch (exc) {
    els.status.textContent = String(exc.message || exc);
  }
});

// ---- Encounter scene mods: scene-wide modifiers (title/description + mod cards) and
// ---- cards applied to a player (target + round), saved to encounter_scenes.mods_json ----
function sceneModsSummary(mods) {
  const list = (mods || []).filter((m) => m && (m.title || m.description || (m.mods || []).length));
  if (!list.length) return "";
  return `<div class="small-text"><b>Mods:</b><ul class="reward-list">${list.map((m) => {
    const where = m.target ? ` <span class="muted">(${esc(m.target)}${m.round ? `, round ${m.round}` : ""})</span>` : "";
    if (m.choice) {
      const options = (m.mods || []).map((c) => `<li>${esc(modCardLabel(c.guid))}${c.when ? ` <span class="muted">when ${esc(modTagsText(c.when))}</span>` : ""}${(c.victory || []).length ? ` <span class="muted">· on victory ${esc(c.victory.join("; "))}</span>` : ""}</li>`).join("");
      return `<li>${esc(m.title || "(untitled)")} <span class="muted">(player picks one)</span>${where}<ul>${options}</ul>${m.description ? `<div class="muted">${esc(m.description)}</div>` : ""}</li>`;
    }
    const cards = (m.mods || []).map((c) => modCardLabel(c.guid)).join(", ");
    return `<li>${esc(m.title || "(untitled)")}${where}${cards ? ` — ${esc(cards)}` : ""}${m.description ? `<div class="muted">${esc(m.description)}</div>` : ""}</li>`;
  }).join("")}</ul></div>`;
}

let sceneModsDraft = null;   // {guid, mods: [...]}

const modCardLabel = (guid) => (rewardCatalog ? cardName(guid) : String(guid || "").slice(0, 8));
// A choice option's ``when``: a tag list shown as "a; b" ("or" groups only in JSON).
const modTagsGrouped = (when) => Array.isArray(when) && when.some(Array.isArray);
const modTagsText = (when) => (modTagsGrouped(when) ? JSON.stringify(when) : (when || []).join("; "));
const splitSemi = (text) => String(text || "").split(";").map((s) => s.trim()).filter(Boolean);

function sceneModCardRow(mod, c, i, j) {
  const choice = mod.choice ? `
      <input class="sm-when" size="26" value="${esc(modTagsText(c.when))}" placeholder="offered when, e.g. flag(x)" ${modTagsGrouped(c.when) ? "disabled title=\"Or-groups: edit as JSON\"" : ""}>
      <input class="sm-win" size="26" value="${esc((c.victory || []).join("; "))}" placeholder="on victory, e.g. clear_flag(x)">` : "";
  return `<div class="reward-row sm-card-row" data-j="${j}">
      <input list="rw-card-list" class="sm-card" data-icon="card" value="${esc(cardName(c.guid))}" placeholder="card name or GUID">${cardImg(c.guid)}${choice}
      <button type="button" class="small" data-sm="del-card" data-i="${i}" data-j="${j}">✕</button></div>`;
}

function sceneModForm(mod, i) {
  const target = mod.target || "";
  return `<div class="card scene-mod" data-i="${i}">
    <div class="card-head"><b>Mod ${i + 1}</b><button type="button" class="small" data-sm="del-mod" data-i="${i}">Remove</button></div>
    <div class="reward-row"><span class="reward-race">Title</span><input class="sm-title" size="32" value="${esc(mod.title || "")}"></div>
    <div class="reward-row"><span class="reward-race">Text</span><input class="sm-desc" size="46" value="${esc(mod.description || "")}"></div>
    <div class="reward-row"><span class="reward-race">Applies to</span>
      <select class="sm-target"><option value="" ${target ? "" : "selected"}>both (scene-wide)</option>
        <option value="AIPlayer" ${target === "AIPlayer" ? "selected" : ""}>the AI</option>
        <option value="Player" ${target === "Player" ? "selected" : ""}>the player</option></select>
      <label>round <input type="number" min="0" class="sm-round" value="${mod.round || 0}"></label></div>
    <div class="reward-row"><label><input type="checkbox" class="sm-choice" ${mod.choice ? "checked" : ""}>
      Player picks one before the battle (card selector)</label></div>
    ${mod.choice ? `<div class="muted small-text">Each card is offered while its tags hold (separate tags with <code>;</code>);
      only the chosen card is applied, and its victory actions run when that battle is won. With nothing on offer the battle starts normally.
      <code>pre</code> / <code>defeat</code> actions: edit as JSON.</div>` : `<div class="muted small-text">Mod cards</div>`}
    ${(mod.mods || []).map((c, j) => sceneModCardRow(mod, c, i, j)).join("")}
    <button type="button" class="small" data-sm="add-card" data-i="${i}">+ ${mod.choice ? "Option" : "Mod card"}</button>
  </div>`;
}

const smJsonMode = () => !document.getElementById("sm-json").hidden;

function setSceneModsMode(json) {
  document.getElementById("sm-json").hidden = !json;
  document.getElementById("sm-list").hidden = json;
  document.getElementById("sm-add-mod").hidden = json;
  document.getElementById("sm-to-json").hidden = json;
  document.getElementById("sm-to-form").hidden = !json;
}

// JSON text -> draft mods (same shape as mods_json); throws on bad input.
function parseSceneModsJson(text) {
  let list;
  try { list = JSON.parse(text); } catch (exc) { throw new Error(`Invalid JSON: ${exc.message}`); }
  if (!Array.isArray(list)) throw new Error("Mods JSON must be a list [ {...}, ... ]");
  list.forEach((entry, i) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error(`Mod ${i + 1} must be an object`);
    if (entry.mods !== undefined && !Array.isArray(entry.mods)) throw new Error(`Mod ${i + 1}: "mods" must be a list`);
    for (const card of entry.mods || []) {
      if (!card || typeof card.guid !== "string") throw new Error(`Mod ${i + 1}: each mod card needs a "guid"`);
    }
  });
  return list.map((entry) => ({ ...entry, mods: (entry.mods || []).map((c) => ({ ...c, guid: c.guid.trim() })) }));
}

function renderSceneMods() {
  document.getElementById("sm-list").innerHTML = sceneModsDraft.mods.map(sceneModForm).join("") +
    `<datalist id="rw-card-list">${rewardCatalog.cards.map((c) => `<option value="${esc(c.label)}"></option>`).join("")}</datalist>`;
}

function collectSceneMods() {
  return [...document.querySelectorAll("#sm-list .scene-mod")].map((box, i) => {
    const old = sceneModsDraft.mods[i] || {};
    const out = { ...old };
    out.title = box.querySelector(".sm-title").value.trim();
    out.description = box.querySelector(".sm-desc").value.trim();
    const target = box.querySelector(".sm-target").value;
    if (target) { out.target = target; out.round = Math.max(0, parseInt(box.querySelector(".sm-round").value, 10) || 0); }
    else { delete out.target; delete out.round; }
    const choice = box.querySelector(".sm-choice").checked;
    if (choice) out.choice = true; else delete out.choice;
    out.mods = [...box.querySelectorAll(".sm-card-row")].map((row) => {
      const text = row.querySelector(".sm-card").value.trim();
      if (!text) return null;
      const guid = cardGuid(text);
      if (!guid) throw new Error(`Unknown card “${text}” — type a card name or paste its GUID`);
      // Keep fields the form does not show (pre / defeat, or-group tags).
      const card = { ...((old.mods || [])[Number(row.dataset.j)] || {}), guid };
      const when = row.querySelector(".sm-when");
      const win = row.querySelector(".sm-win");
      if (!choice) return card;      // the server drops option fields on save
      if (when && !when.disabled) { const tags = splitSemi(when.value); if (tags.length) card.when = tags; else delete card.when; }
      if (win) { const actions = splitSemi(win.value); if (actions.length) card.victory = actions; else delete card.victory; }
      return card;
    }).filter(Boolean);
    return out;
  });
}

async function openSceneMods(guid) {
  const scene = area.nodes.flatMap((n) => n.encounters).find((e) => e.guid === guid);
  if (!scene) return;
  await loadRewardCatalog();
  sceneModsDraft = { guid, mods: JSON.parse(JSON.stringify(scene.mods || [])) };
  document.getElementById("scene-mods-body").innerHTML = `<h2>${esc(scene.title || scene.name)} — mods</h2>
    <p class="muted small-text"><code>${esc(scene.name)}</code> · a mod with no target is shown to the player as a scene-wide modifier;
    one that applies to the AI or the player puts its cards into that side's setup (at the given round).
    Saved to <code>campaign_data/encounter_scenes.json</code>, applied at server start.</p>
    <div class="row"><button type="button" class="small" data-sm="to-json" id="sm-to-json">View as JSON</button>
      <button type="button" class="small" data-sm="to-form" id="sm-to-form" hidden>Apply JSON to form</button></div>
    <div id="sm-list"></div>
    <textarea id="sm-json" rows="18" cols="64" spellcheck="false" hidden></textarea>
    <button type="button" class="small" data-sm="add-mod" id="sm-add-mod">+ Mod</button>
    <div id="sm-error" class="issue" hidden></div>
    <div class="actions"><button type="button" class="primary" data-sm="save">Save</button>
    <button type="button" data-sm="close">Cancel</button></div>`;
  renderSceneMods();
  setSceneModsMode(false);
  document.getElementById("scene-mods").showModal();
}

document.getElementById("scene-mods").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-sm]");
  if (!button) return;
  const dialog = document.getElementById("scene-mods");
  const error = document.getElementById("sm-error");
  try {
    const kind = button.dataset.sm;
    const i = Number(button.dataset.i), j = Number(button.dataset.j);
    if (kind === "close") { dialog.close(); return; }
    if (kind === "to-form") {
      sceneModsDraft.mods = parseSceneModsJson(document.getElementById("sm-json").value);
      renderSceneMods(); setSceneModsMode(false); error.hidden = true; return;
    }
    sceneModsDraft.mods = smJsonMode()
      ? parseSceneModsJson(document.getElementById("sm-json").value) : collectSceneMods();
    if (kind === "to-json") {
      document.getElementById("sm-json").value = JSON.stringify(sceneModsDraft.mods, null, 2);
      setSceneModsMode(true); error.hidden = true; return;
    }
    if (kind === "add-mod") { sceneModsDraft.mods.push({ title: "", description: "", mods: [] }); renderSceneMods(); }
    else if (kind === "del-mod") { sceneModsDraft.mods.splice(i, 1); renderSceneMods(); }
    else if (kind === "add-card") { sceneModsDraft.mods[i].mods.push({ guid: "" }); renderSceneMods(); }
    else if (kind === "del-card") { sceneModsDraft.mods[i].mods.splice(j, 1); renderSceneMods(); }
    else if (kind === "save") {
      const result = await postJSON("/api/edit/scene-mods", { scene: sceneModsDraft.guid, mods: sceneModsDraft.mods });
      for (const node of area.nodes) for (const scene of node.encounters) if (scene.guid === sceneModsDraft.guid) scene.mods = result.mods;
      dialog.close();
      if (selected) select(selected);
      els.status.textContent = "Scene mods saved to campaign_data/encounter_scenes.json (applied at server start)";
      return;
    }
    error.hidden = true;
  } catch (exc) {
    error.textContent = String(exc.message || exc);
    error.hidden = false;
  }
});

for (const type of ["change", "input"]) {
  document.getElementById("scene-mods").addEventListener(type, (event) => refreshIcon(event.target));
}

// Ticking "player picks one" swaps the card rows to option rows.
document.getElementById("scene-mods").addEventListener("change", (event) => {
  if (!event.target.classList.contains("sm-choice")) return;
  const error = document.getElementById("sm-error");
  try {
    sceneModsDraft.mods = collectSceneMods();
    renderSceneMods();
    error.hidden = true;
  } catch (exc) {
    error.textContent = String(exc.message || exc);
    error.hidden = false;
  }
});

document.getElementById("tab-node").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-scene-mods]");
  if (edit) {
    openSceneMods(edit.dataset.editSceneMods).catch((exc) => {
      els.status.textContent = `Could not open the mods editor: ${exc.message || exc}`;
    });
  }
});

document.getElementById("tab-quests").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-rewards]");
  if (edit) {
    openRewardsDialog(edit.dataset.editRewards).catch((exc) => {
      els.status.textContent = `Could not open the rewards editor: ${exc.message || exc}`;
    });
  }
});

function renderIssues() {
  const order = { error: 0, warn: 1, info: 2 };
  const issues = [...area.issues].sort((a, b) => order[a.level] - order[b.level]);
  document.getElementById("tab-issues").innerHTML =
    `<p class="muted">Cross-checks between the client prefab, Records and the server catalogue.</p>` +
    (issues.length ? issues.map((i) => `<div class="issue"><span class="lvl ${i.level}">${i.level}</span>
      <button class="link" data-node="${esc(i.node)}">${esc(i.node)}</button> ${esc(i.text)}</div>`).join("")
      : `<p>No issues.</p>`);
}

els.campaign.addEventListener("change", async () => {
  const id = els.campaign.value;
  state = null;
  if (id) sim = null;
  if (id) {
    try { state = await getJSON(`/api/state?id=${encodeURIComponent(id)}`); }
    catch (error) { els.status.textContent = error.message; }
  }
  els.playerView.disabled = !hasState();
  renderLegend();
  renderQuests();
  renderSim();
  updateHash();
  if (area.zone.kind === "panorama") {
    placeNpcs();
    const npc = area.panorama.npcs.find((n) => n.name === pano.npc);
    if (npc) selectNpc(npc);
  } else if (selected) select(selected); else draw();
});

// ---------------------------------------------------------------- startup
async function loadZone(id, { from = null, node = null } = {}) {
  zoneId = id;
  els.zone.value = id;
  state = null; sim = null; selected = null; hovered = null;
  els.playerView.disabled = true;
  els.campaign.length = 1;
  els.campaign.value = "";
  try {
    area = await getJSON(`/api/area?zone=${encodeURIComponent(id)}`);
  } catch (error) {
    els.status.textContent = error.message;
    return;
  }
  nodesById = new Map(area.nodes.map((n) => [n.id, n]));
  loadMapEffects();
  const entries = area.tiles.map((tile) => {
    const img = new Image();
    const entry = { tile, img, t: null };
    img.onload = () => { entry.t = tileTransform(tile, img.naturalWidth, img.naturalHeight); draw(); };
    img.src = `/generated/${tile.image}`;
    return entry;
  });
  // drawImage needs the transform; skip tiles until their image has loaded.
  tiles = { [Symbol.iterator]: function* () { for (const e of entries) if (e.t) yield e; } };
  const counts = area.nodes.reduce((acc, n) => { acc[contentKind(n)] += 1; return acc; },
    { encounter: 0, conversation: 0, empty: 0 });
  const z = area.zone;
  els.status.textContent = z.kind === "panorama" ? "panorama"
    : `${area.nodes.length} nodes · ${area.paths.length} paths · ${counts.encounter} with battles · ${area.issues.length} issues`;
  // Back goes to the overworld node that leads here (client POI marker).
  const raceParent = z.race_parents && viewRace() ? z.race_parents[viewRace()] : null;
  const parent = from || (raceParent ? { zone: raceParent } : null) ||
    (z.parent ? { zone: z.parent, node: z.parent_node } : null);
  if (z.race_parents && !parent) els.status.textContent += " · pick a Race to link back to its starter panorama";
  els.back.hidden = !parent;
  els.back.textContent = parent ? `← ${parent.zone}` : "";
  els.back.onclick = parent ? () => loadZone(parent.zone, { node: parent.node }) : null;
  try { rules = await getJSON(`/api/rules?zone=${encodeURIComponent(id)}`); } catch (error) { rules = null; }
  renderLegend();
  renderQuests();
  renderIssues();
  // Show the right canvas before fitting so its size is known (coming from
  // a panorama the map canvas was hidden), then frame the new zone.
  setPanoramaMode(z.kind === "panorama");
  if (z.kind === "panorama") pano.home = null;   // refit the camera on its next frame
  else { resize(); fit(); }
  try {
    for (const c of await getJSON(`/api/campaigns?zone=${encodeURIComponent(id)}`)) {
      const option = document.createElement("option");
      option.value = c.id;
      option.textContent = `${c.champion_name} (campaign ${c.id})`;
      els.campaign.append(option);
    }
  } catch (error) { /* database optional */ }
  renderSim();
  if (z.kind === "panorama") renderPanorama();
  else document.getElementById("tab-node").innerHTML = `<p class="muted">Select a node on the map.${area.nodes.some((n) => n.link) ? " Double-click a 🏰/👥 node to enter it." : ""}</p>`;
  const target = node && nodesById.get(node);
  if (target) { view.scale = Math.max(view.scale, 3); centerOn(target); select(target); }
  updateHash();
}

// ---------------------------------------------------------------- panorama
const pano = { canvas: document.getElementById("pano"), layer: document.getElementById("npc-layer"),
               renderer: null, scene: null, camera: null, zone: null, npc: null };

function setPanoramaMode(on) {
  pano.canvas.hidden = !on;
  pano.layer.hidden = !on;
  canvas.hidden = on;
  els.legend.hidden = on;
}

// Unity is left-handed (z forward); three.js is right-handed. Mirror z.
const unityPoint = ([x, y, z]) => new THREE.Vector3(x, y, -z);

async function buildPanorama(sceneFile) {
  const data = await getJSON(`/generated/${sceneFile}`);
  if (!pano.renderer) {
    pano.renderer = new THREE.WebGLRenderer({ canvas: pano.canvas, antialias: true });
  }
  const scene = new THREE.Scene();
  pano.anims = [];
  pano.emitters = [];
  // Client ParallaxController: meshes sit in three panels that slide and wrap.
  pano.parallax = data.parallax || null;
  pano.groups = {};
  for (const panel of ["", "left", "center", "right"]) {
    pano.groups[panel] = new THREE.Group();
    scene.add(pano.groups[panel]);
  }
  for (const mesh of data.meshes) {
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(mesh.positions.length);
    for (let i = 0; i < mesh.positions.length; i += 3) {
      positions[i] = mesh.positions[i];
      positions[i + 1] = mesh.positions[i + 1];
      positions[i + 2] = -mesh.positions[i + 2];
    }
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("uv", new THREE.BufferAttribute(new Float32Array(mesh.uvs), 2));
    geometry.setIndex(mesh.indices);
    // Tint, blend, scrolling and animated-gradient/cloth shaders (effects.js).
    const object = new THREE.Mesh(geometry, HexEffects.makeMaterial(mesh, () => renderPanoramaFrame()));
    object.userData.uvBase = mesh.uv || [1, 1, 0, 0];
    object.userData.order = mesh.order || 0;
    if (mesh.anim) pano.anims.push({ mesh: object, player: new HexEffects.GoAnimPlayer(mesh.anim), pivot: mesh.pivot });
    pano.groups[pano.parallax && mesh.panel ? mesh.panel : ""].add(object);
  }
  for (const def of data.emitters || []) {
    const emitter = new HexEffects.ThreeEmitter(def, () => renderPanoramaFrame());
    emitter.mesh.userData.order = (def.render && def.render.order) || 0;
    emitter.mesh.userData.center = new THREE.Vector3(def.position[0], def.position[1], -def.position[2]);
    pano.emitters.push(emitter);
    pano.groups[pano.parallax && def.panel ? def.panel : ""].add(emitter.mesh);
  }
  pano.lightning = data.lightning || null;
  const cam = data.camera;
  const camera = new THREE.PerspectiveCamera(cam.fov, 16 / 9, cam.near, cam.far);
  camera.position.copy(unityPoint(cam.position));
  const [qx, qy, qz, qw] = cam.rotation;
  camera.quaternion.set(-qx, -qy, qz, qw);
  pano.scene = scene;
  pano.camera = camera;
  pano.authored = { position: camera.position.clone(), fov: cam.fov };
  pano.anchors = data.npcs.map((n) => unityPoint(n.position));
  pano.rotate = pano.parallax ? pano.parallax.rotate_y : 0;
  sortPanorama(scene, camera);
  startEffectsLoop();
  pano.home = null;  // fitted on the first frame, when the aspect is known
}

// Unity draws transparent renderers by sorting order, then far to near.
function sortPanorama(scene, camera) {
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
  const items = [];
  scene.traverse((object) => {
    if (!object.isMesh) return;
    let center = object.userData.center;
    if (!center) {
      object.geometry.computeBoundingSphere();
      center = object.geometry.boundingSphere.center;
    }
    items.push({ object, order: object.userData.order || 0,
      depth: center.clone().sub(camera.position).dot(forward) });
  });
  items.sort((a, b) => a.order - b.order || b.depth - a.depth);
  items.forEach((item, i) => { item.object.renderOrder = i; });
}

// ---------------------------------------------------------------- effects loop
// Particles, GoAnim, animated shaders and lightning advance while a zone
// with effects is on screen (toolbar "Effects").
let fxFrame = null, fxLast = 0, fxMapLast = 0;

function startEffectsLoop() {
  if (fxFrame === null) fxFrame = requestAnimationFrame(effectsTick);
}

function effectsTick(now) {
  fxFrame = null;
  const dt = Math.min(0.1, fxLast ? (now - fxLast) / 1000 : 0);
  fxLast = now;
  if (!effectsOn() || !area) { fxLast = 0; return; }
  if (area.zone.kind === "panorama" && pano.scene && !pano.canvas.hidden) {
    stepPanoramaEffects(dt, now);
    renderPanoramaScene();
  } else if (mapEffects && mapEffects.zone === zoneId && !canvas.hidden) {
    for (const sim of mapEffects.sims) sim.update(dt);
    // The 2D map redraws everything: ~30 frames a second is plenty.
    if (now - fxMapLast > 33) { fxMapLast = now; draw(); }
  } else {
    return;     // nothing animating: stop until the next zone starts it
  }
  fxFrame = requestAnimationFrame(effectsTick);
}

function stepPanoramaEffects(dt, now) {
  const h = pano.canvas.clientHeight || 1;
  for (const emitter of pano.emitters) emitter.update(dt, pano.camera, h);
  for (const { mesh, player, pivot } of pano.anims) {
    player.update(dt);
    HexEffects.applyGoAnim(player, mesh, pivot);
  }
  pano.scene.traverse((object) => {
    if (object.isMesh && object.material.uniforms && !pano.emitters.some((e) => e.mesh === object)) {
      object.material.uniforms.time.value += dt * (object.material.userData.timeScale || 1);
    }
  });
  stepLightning(dt, now);
}

// LightningController: a flash every ~FidgetRate seconds (the clip curves
// are Mecanim-compressed; this is the flash, not the authored curve).
const flashEl = document.getElementById("flash");
function stepLightning(dt) {
  const l = pano.lightning;
  if (!l || !flashEl) return;
  l.next = (l.next ?? l.rate * (0.5 + Math.random())) - dt;
  if (l.next > 0) return;
  l.next = l.rate * (0.5 + Math.random());
  flashEl.classList.remove("on");
  void flashEl.offsetWidth;          // restart the CSS animation
  flashEl.classList.add("on");
}

// Dungeon maps: particle systems drawn top-down over the art.
let mapEffects = null;
function loadMapEffects() {
  mapEffects = null;
  const defs = (area && area.emitters) || [];
  if (!defs.length) return;
  const sims = defs.map((def) => {
    const sim = new HexEffects.Emitter(def);
    sim.image = new Image();
    sim.image.src = def.material && def.material.texture ? `/generated/${def.material.texture}` : "";
    const [qx, qy, qz, qw] = def.orientation;
    sim.matrix = new THREE.Matrix4().compose(new THREE.Vector3(...def.position),
      new THREE.Quaternion(qx, qy, qz, qw),
      def.scaling === 2 ? new THREE.Vector3(1, 1, 1) : new THREE.Vector3(...def.scale));
    sim.sizeScale = def.scaling === 2 ? 1 : Math.max(...def.scale.map(Math.abs));
    return sim;
  });
  mapEffects = { zone: zoneId, sims };
  startEffectsLoop();
}

function drawMapEffects() {
  if (!mapEffects || mapEffects.zone !== zoneId || !effectsOn()) return;
  const p = new THREE.Vector3();
  ctx.save();
  for (const sim of mapEffects.sims) {
    if (!sim.image.complete || !sim.image.naturalWidth) continue;
    const material = sim.def.material || {};
    ctx.globalCompositeOperation = material.blend === "add" ? "lighter" : "source-over";
    const tint = material.tint || [1, 1, 1, 1];
    for (const particle of sim.particles) {
      const look = sim.look(particle);
      const at = sim.placed(particle);
      p.set(at[0], at[1], at[2]).applyMatrix4(sim.matrix);
      const [sx, sy] = toScreen(p.x, p.z);
      const size = Math.max(1, look.size * sim.sizeScale * view.scale);
      ctx.globalAlpha = Math.max(0, Math.min(1, look.color[3] * tint[3]));
      const [u0, v0, u1, v1] = look.uv;
      const w = sim.image.naturalWidth, h = sim.image.naturalHeight;
      ctx.drawImage(sim.image, u0 * w, (1 - v1) * h, Math.max(1, (u1 - u0) * w), Math.max(1, (v1 - v0) * h),
        sx - size / 2, sy - size / 2, size, size);
    }
  }
  ctx.restore();
}

// ParallaxController.UpdatePanorama/MovePanel: a rotation of R degrees moves
// every panel R/360 * 2B along the root's x (B = PanelBoundsX); a panel past
// +-B wraps by 2B. Near layers slide faster on screen (perspective), and
// the left/right panels are copies one wrap apart, so the scene loops.
const PANEL_START = { left: 1, center: 0, right: -1 };

function panelShift(panel) {
  const shift = new THREE.Vector3();
  const px = pano.parallax;
  if (!px || !(panel in PANEL_START)) return shift;
  const bound = px.bounds_x, reset = 2 * bound;
  const start = PANEL_START[panel] * bound;
  let local = start + (pano.rotate / 360) * reset;
  local = ((local + bound) % reset + reset) % reset - bound;
  const [ax, ay, az] = px.axis;
  return shift.set(ax, ay, -az).multiplyScalar(local - start);
}

function applyParallax() {
  if (!pano.groups) return;
  for (const panel of Object.keys(PANEL_START)) pano.groups[panel].position.copy(panelShift(panel));
}

// An NPC's anchor where its panel currently is.
function npcWorld(npc) {
  return unityPoint(npc.position).add(panelShift(pano.parallax ? npc.panel : ""));
}

// Rotate (or, without parallax data, slide the camera) by a screen drag.
function rotateByPixels(dx, from) {
  const px = pano.parallax;
  const h = pano.canvas.clientHeight || 1;
  // World size of a pixel at the scene's middle depth.
  const depth = 0.6;
  const perPixel = 2 * depth * Math.tan((pano.camera.fov * Math.PI / 180) / 2) / h;
  // Content follows the cursor: a rotation dR moves it ax * dR/360 * 2B.
  const ax = px.axis[0] || -1;
  pano.rotate = from + (dx * perPixel / ax) / (2 * px.bounds_x) * 360;
}

// Panoramas scroll sideways in the client; centre the authored camera on the
// NPCs and widen the view until they all fit the current canvas.
function fitPanorama(aspect) {
  const camera = pano.camera;
  camera.position.copy(pano.authored.position);
  camera.fov = pano.authored.fov;
  if (pano.parallax) {
    // The client's own view: authored camera at the level's start rotation.
    pano.rotate = pano.parallax.rotate_y;
    pano.home = { position: camera.position.clone(), fov: camera.fov, rotate: pano.rotate };
    return;
  }
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
  if (pano.anchors.length) {
    const offsets = pano.anchors.map((a) => a.clone().sub(camera.position).dot(right));
    const mid = (Math.min(...offsets) + Math.max(...offsets)) / 2;
    camera.position.addScaledVector(right, mid);
    const depth = Math.max(1, Math.min(...pano.anchors.map((a) => a.clone().sub(camera.position).dot(forward))));
    const half = (Math.max(...offsets) - Math.min(...offsets)) / 2 * 1.2;
    const vertical = 2 * Math.atan((half / depth) / aspect) * 180 / Math.PI;
    camera.fov = Math.min(110, Math.max(pano.authored.fov, vertical));
  }
  pano.home = { position: camera.position.clone(), fov: camera.fov };
}

// Drag pans along the camera's own axes; the wheel zooms (field of view).
let panoDrag = null;
pano.canvas.addEventListener("pointerdown", (event) => {
  panoDrag = { x: event.clientX, y: event.clientY, position: pano.camera && pano.camera.position.clone(),
    rotate: pano.rotate };
  pano.canvas.setPointerCapture(event.pointerId);
});
pano.canvas.addEventListener("pointermove", (event) => {
  if (!panoDrag || !pano.camera) return;
  if (pano.parallax) {        // the client scrolls sideways only
    rotateByPixels(event.clientX - panoDrag.x, panoDrag.rotate);
    renderPanoramaFrame();
    return;
  }
  const h = pano.canvas.clientHeight;
  const depth = 30;
  const perPixel = 2 * depth * Math.tan((pano.camera.fov * Math.PI / 180) / 2) / h;
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(pano.camera.quaternion);
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(pano.camera.quaternion);
  pano.camera.position.copy(panoDrag.position)
    .addScaledVector(right, -(event.clientX - panoDrag.x) * perPixel)
    .addScaledVector(up, (event.clientY - panoDrag.y) * perPixel);
  renderPanoramaFrame();
});
pano.canvas.addEventListener("pointerup", () => { panoDrag = null; });
pano.canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  if (!pano.camera) return;
  pano.camera.fov = Math.min(120, Math.max(10, pano.camera.fov * Math.exp(event.deltaY * 0.001)));
  renderPanoramaFrame();
}, { passive: false });

function renderPanoramaScene() {
  if (!pano.renderer || !pano.scene || pano.canvas.hidden) return;
  applyParallax();
  pano.renderer.render(pano.scene, pano.camera);
}

function renderPanoramaFrame() {
  if (!pano.renderer || !pano.scene || pano.canvas.hidden) return;
  const w = pano.canvas.clientWidth, h = pano.canvas.clientHeight;
  pano.renderer.setPixelRatio(window.devicePixelRatio || 1);
  pano.renderer.setSize(w, h, false);
  pano.camera.aspect = w / h;
  if (!pano.home) fitPanorama(w / h);
  applyParallax();
  pano.camera.updateProjectionMatrix();
  pano.renderer.render(pano.scene, pano.camera);
  placeNpcs();
}

function npcState(npc) {
  // Simulating a panorama with rules: an NPC the rules never show is hidden.
  if (sim) return sim.layout.nodes[npc.name] || { visible: false };
  if (state && state.nodes && state.nodes[npc.name]) {
    const d = state.nodes[npc.name];
    return { ...d, givequest: d.givequest, turninquest: d.turninquest, autostart: d.autostart };
  }
  return state && state.nodes ? state.nodes[npc.name] || null : null;
}

// A panorama NPC as a rules node (starter panoramas: the NPCs are the nodes).
function npcNode(npc) {
  return { id: npc.name, conversations: npc.conversations, quest_links: npc.quest_links,
    encounters: [], authored: null };
}

// Slide the camera sideways (its own right axis) until the NPC is centred.
function panToNpc(npc) {
  if (!pano.camera) return;
  const camera = pano.camera;
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
  const offset = npcWorld(npc).sub(camera.position).dot(right);
  const p = npcWorld(npc).project(camera);
  if (Math.abs(p.x) <= 0.8) return;
  if (pano.parallax) {
    // Rotate until the NPC's panel brings it to the centre.
    const [ax, ay, az] = pano.parallax.axis;
    const along = new THREE.Vector3(ax, ay, -az).dot(right);
    if (along) pano.rotate += (-offset / along) / (2 * pano.parallax.bounds_x) * 360;
  } else {
    camera.position.addScaledVector(right, offset);
  }
  renderPanoramaFrame();
}

function placeNpcs() {
  const w = pano.canvas.clientWidth, h = pano.canvas.clientHeight;
  pano.layer.innerHTML = "";
  for (const npc of area.panorama.npcs) {
    const live = npcState(npc);
    // The client draws an NPC whose location is visible and not completed.
    const drawn = Boolean(live && live.visible && !live.completed);
    if (playerView() && !drawn) continue;
    const p = npcWorld(npc).project(pano.camera);
    if (p.z > 1) continue;
    // The client pans across a wide scene: an NPC outside the view is pinned
    // to the nearest edge; clicking it pans there.
    const off = p.x < -1 ? "left" : p.x > 1 ? "right" : "";
    const el = document.createElement("button");
    el.type = "button";
    const battle = Boolean(sim && sim.state.encounter && sim.state.encounter.node === npc.name);
    el.className = "npc" + (npc.quest_links.length || (live && (live.givequest || live.turninquest)) ? " quest" : "") +
      (live && !drawn ? " hidden-npc" : "") + (live && live.completed ? " done" : "") +
      (battle ? " battle" : "") + (sim && sim.state.current === npc.name ? " current" : "") +
      (pano.npc === npc.name ? " selected" : "") + (off ? ` edge ${off}` : "");
    // Indicators only on an NPC the client draws (as UIPanoramaZoneViewModel does).
    const marks = live ? `${live.completed ? " ✓" : ""}${drawn && live.givequest ? " !" : ""}${drawn && live.turninquest ? " ?" : ""}${drawn && live.autostart ? " ▸" : ""}${battle ? " ⚔️" : ""}` : "";
    const label = npc.name + marks;
    el.textContent = off === "left" ? `◀ ${label}` : off === "right" ? `${label} ▶` : label;
    const x = Math.max(-1, Math.min(1, p.x)), y = Math.max(-0.9, Math.min(0.9, p.y));
    el.style.left = `${((x + 1) / 2) * w}px`;
    el.style.top = `${((1 - y) / 2) * h}px`;
    const status = !live ? "" : !live.visible ? "not shown" : live.completed ? "completed (the client hides it)" : "shown";
    el.title = `${npc.name}${status ? ` — ${status}` : ""}${off ? " (off-screen: click to pan there)" : ""}`;
    el.addEventListener("click", () => { panToNpc(npc); selectNpc(npc); });
    pano.layer.append(el);
  }
}

function npcCards(npc) {
  let html = "";
  for (const q of bySimMatch(npc.quest_links.filter((x) => rowAllowsRace(x)), npc.name)) html += questLinkCard(q, npc.name);
  for (const c of bySimMatch(npc.conversations.filter((x) => rowAllowsRace(x)), npc.name)) html += conversationCard(c, npc.name);
  const total = npc.quest_links.length + npc.conversations.length;
  return html ? html + hiddenNote(total, html.split('class="card').length - 1)
    : (total ? hiddenNote(total, 0) : `<p class="muted">No conversations in the tables name this NPC.</p>`);
}

function selectNpc(npc) {
  pano.npc = npc.name;
  showTab("node");
  const live = state && state.nodes ? state.nodes[npc.name] || null : null;
  const node = npcNode(npc);
  pano.npcNode = node;
  let html = `<h2>${esc(npc.name)}</h2><div class="muted">${esc(area.zone.title)} · ${esc(area.zone.parent || "")} ${esc(area.zone.parent_node || "")}</div>`;
  html += simNodeSection(node);
  if (state) {
    html += `<h3>Live state · ${esc(state.champion_name)}</h3>` + (live
      ? `<div class="chips">${["visible", "enabled", "completed", "repeatable", "givequest", "turninquest"].map((k) =>
          `<span class="chip ${live[k] ? "on" : "off"}">${k}</span>`).join("")}</div>
         ${live.conversationId ? `<div class="cond">now offers <button class="link" data-conv="${esc(live.conversationId)}">${esc(live.conversationId)}</button></div>` : ""}`
      : `<p class="muted">Not in this panorama's live state.</p>`);
  }
  html += `<h3>Conversations</h3>${npcCards(npc)}`;
  if (rules && rules.rules) html += rulesNodeSection(node);
  document.getElementById("tab-node").innerHTML = html;
  placeNpcs();
}

async function renderPanorama() {
  setPanoramaMode(true);
  showTab("node");
  const info = area.panorama;
  const list = info.npcs.map((n) => `<button class="link" data-npc="${esc(n.name)}">${esc(n.name)}</button>`).join(" · ");
  const unassigned = info.unassigned.filter((c) => rowAllowsRace(c));
  const children = (area.zone.children || []).map((id) => (area.zones || []).find((z) => z.id === id)).filter(Boolean);
  document.getElementById("tab-node").innerHTML =
    `<h2>${esc(area.zone.title)}</h2><div class="muted">Panorama · ${esc(area.zone.bundle)}${area.zone.race ? ` · ${esc(area.zone.race)} starter` : ""}</div>
     ${children.length ? `<div class="actions">${children.map((z) => `<button type="button" class="primary" data-enter="${esc(z.id)}">Enter ${esc(z.title)} (${esc(z.kind)}) →</button>`).join("")}</div>` : ""}
     <h3>NPCs</h3><p>${list || "none"}</p>
     ${unassigned.length ? `<h3>Other ${esc(area.zone.parent_node || "")} conversations</h3>` +
       unassigned.map((c) => `<div class="card"><button class="link" data-conv="${esc(c.guid)}">${esc(c.name)}</button></div>`).join("") : ""}
     ${hiddenNote(info.unassigned.length, unassigned.length)}`;
  if (!info.scene) { pano.layer.innerHTML = ""; return; }
  if (typeof THREE === "undefined") {
    els.status.textContent = "three.js could not be loaded (offline?)";
    return;
  }
  if (pano.zone !== area.zone.id) {
    pano.npc = null;
    await buildPanorama(info.scene);
    pano.zone = area.zone.id;
  }
  startEffectsLoop();
  renderPanoramaFrame();
}

els.zone.addEventListener("change", () => loadZone(els.zone.value));

async function start() {
  resize();
  let zoneList = [];
  try { zoneList = await getJSON("/api/zones"); } catch (error) { els.status.textContent = error.message; return; }
  // Race starter (AZ0) panoramas get their own heading.
  const groupOf = (z) => (z.kind === "panorama" && z.race ? "intro" : z.kind);
  const groups = { overworld: "Overworlds", dungeon: "Dungeons", panorama: "Panoramas",
    intro: "Intro Panoramas" };
  for (const [kind, label] of Object.entries(groups)) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const z of zoneList.filter((x) => groupOf(x) === kind)) {
      const option = document.createElement("option");
      option.value = z.id;
      option.textContent = kind === "overworld" ? `${z.id}: ${z.title}` : `${z.title}${z.parent ? ` (${z.parent})` : ""}`;
      if (z.race) option.dataset.race = z.race;
      group.append(option);
    }
    els.zone.append(group);
  }
  els.race.innerHTML = `<option value="">All races</option>` + RACES.map((r) => `<option>${esc(r)}</option>`).join("");
  const params = new URLSearchParams(location.hash.slice(1));
  if (RACES.includes(params.get("race"))) els.race.value = params.get("race");
  applyRaceToZoneList();
  const initial = zoneList.some((z) => z.id === params.get("zone")) ? params.get("zone") : "AZ1";
  await loadZone(initial);
  await applyHash(params);
}

// Deep links: #campaign=<id>&node=<NodeId>
async function applyHash(params = new URLSearchParams(location.hash.slice(1))) {
  const campaign = params.get("campaign");
  els.playerView.checked = params.get("view") === "player";
  if (campaign && [...els.campaign.options].some((o) => o.value === campaign)) {
    els.campaign.value = campaign;
    els.campaign.dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  const simParam = params.get("sim");
  if (simParam && rules && rules.rules) {
    if (simParam.startsWith("live:")) {
      await simStart({ campaign: Number(simParam.slice(5)) }, `From campaign ${simParam.slice(5)}`);
    } else if (RACES.includes(simParam)) {
      await simStart({ race: simParam }, `New game · ${simParam}`);
    }
  }
  const node = nodesById.get(params.get("node") || "");
  if (node) { view.scale = Math.max(view.scale, 3); centerOn(node); select(node); }
}

function applyRaceToZoneList() {
  for (const option of els.zone.querySelectorAll("option[data-race]")) {
    option.hidden = Boolean(viewRace()) && option.dataset.race !== viewRace();
  }
}

els.race.addEventListener("change", async () => {
  applyRaceToZoneList();
  updateHash();
  // A starter panorama belongs to one race: follow the selection.
  const z = area && area.zone;
  if (z && z.race && viewRace() && z.race !== viewRace()) {
    const other = (area.zones || []).find((x) => x.race === viewRace());
    if (other) { await loadZone(other.id); return; }
  }
  if (z && z.race_parents) { await loadZone(zoneId, { node: selected && selected.id }); return; }
  renderSim();
  if (z && z.kind === "panorama") {
    const npc = area.panorama.npcs.find((n) => n.name === pano.npc);
    if (npc) selectNpc(npc); else renderPanorama();
  } else if (selected) select(selected);
});

function updateHash() {
  const params = new URLSearchParams();
  if (zoneId !== "AZ1") params.set("zone", zoneId);
  if (viewRace()) params.set("race", viewRace());
  if (els.campaign.value) params.set("campaign", els.campaign.value);
  if (els.campaign.value && els.playerView.checked) params.set("view", "player");
  if (selected) params.set("node", selected.id);
  history.replaceState(null, "", params.toString() ? `#${params}` : location.pathname);
}
start();

// Item / card pictures in the summaries need the reward catalogue (names + art maps).
loadRewardCatalog().then(() => {
  if (area) { renderQuests(); if (selected) select(selected); }
}).catch(() => {});
