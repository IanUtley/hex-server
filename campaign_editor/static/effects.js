// Zone effects for the campaign editor: Unity particle systems, the client's
// GoAnim component and the animated panorama shaders, from the data
// AssetExtraction/campaign_effects.py writes. Simulation is renderer-neutral
// (Unity local space); app.js draws it with three.js (panoramas) or the 2D
// canvas (dungeon maps).
"use strict";

const HexEffects = (() => {
  // ------------------------------------------------------------ curves
  // Unity AnimationCurve: cubic Hermite between keys [time, value, in, out].
  function evalKeys(keys, t) {
    if (!keys || !keys.length) return 1;
    if (t <= keys[0][0]) return keys[0][1];
    const last = keys[keys.length - 1];
    if (t >= last[0]) return last[1];
    for (let i = 0; i < keys.length - 1; i++) {
      const [t0, v0, , o0] = keys[i];
      const [t1, v1, i1] = keys[i + 1];
      if (t > t1) continue;
      const d = t1 - t0 || 1e-6;
      const s = (t - t0) / d, s2 = s * s, s3 = s2 * s;
      return (2 * s3 - 3 * s2 + 1) * v0 + (s3 - 2 * s2 + s) * o0 * d +
        (-2 * s3 + 3 * s2) * v1 + (s3 - s2) * i1 * d;
    }
    return last[1];
  }

  // MinMaxCurve: 0 constant, 1 curve, 2 random between curves, 3 random constants.
  function sample(c, t, r) {
    if (!c) return 0;
    switch (c.mode) {
      case 1: return c.max * evalKeys(c.kmax, t);
      case 2: return c.max * (evalKeys(c.kmin, t) + (evalKeys(c.kmax, t) - evalKeys(c.kmin, t)) * r);
      case 3: return c.min + (c.max - c.min) * r;
      default: return c.max;
    }
  }

  function gradAt(g, t) {
    if (!g) return [1, 1, 1, 1];
    const lerpKeys = (keys, n) => {
      if (!keys.length) return n === 3 ? [1, 1, 1] : [1];
      if (t <= keys[0][0]) return keys[0].slice(1);
      for (let i = 0; i < keys.length - 1; i++) {
        const a = keys[i], b = keys[i + 1];
        if (t <= b[0]) {
          const s = (t - a[0]) / ((b[0] - a[0]) || 1e-6);
          return a.slice(1).map((v, k) => v + (b[k + 1] - v) * s);
        }
      }
      return keys[keys.length - 1].slice(1);
    };
    return [...lerpKeys(g.colors, 3), ...lerpKeys(g.alphas, 1)];
  }

  // MinMaxGradient: 0 colour, 1 gradient, 2 random colours, 3 random gradients.
  function sampleColor(c, t, r) {
    if (!c) return [1, 1, 1, 1];
    const mix = (a, b) => a.map((v, i) => v + (b[i] - v) * r);
    switch (c.mode) {
      case 1: return gradAt(c.gmax, t);
      case 2: return mix(c.min, c.max);
      case 3: return mix(gradAt(c.gmin, t), gradAt(c.gmax, t));
      default: return c.max;
    }
  }

  // ------------------------------------------------------------ emitter
  const TAU = Math.PI * 2;

  function unitRandom() {
    const z = Math.random() * 2 - 1, a = Math.random() * TAU, s = Math.sqrt(1 - z * z);
    return [s * Math.cos(a), s * Math.sin(a), z];
  }

  function normalize(v) {
    const l = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / l, v[1] / l, v[2] / l];
  }

  // Spawn position and direction in the emitter's local space (Unity shape
  // types: 0/1 sphere, 2/3 hemisphere, 4/7/8/9 cone, 5/15/16 box,
  // 10/11 circle (XY plane), 12 edge (along X, emitting +Y)).
  function shapeSample(shape) {
    if (!shape || !shape.enabled) return { pos: [0, 0, 0], dir: [0, 0, 1] };
    const r = shape.radius, arc = (shape.arc || 360) * Math.PI / 180;
    let pos, dir;
    switch (shape.type) {
      case 0: case 1: case 2: case 3: {
        dir = unitRandom();
        if (shape.type >= 2) dir[2] = Math.abs(dir[2]);
        const d = shape.type === 1 || shape.type === 3 ? r : r * Math.cbrt(Math.random());
        pos = dir.map((v) => v * d);
        break;
      }
      case 4: case 7: case 8: case 9: {
        const a = Math.random() * arc;
        const k = shape.type === 7 || shape.type === 9 ? 1 : Math.sqrt(Math.random());
        const x = Math.cos(a) * k, y = Math.sin(a) * k;
        pos = [x * r, y * r, 0];
        const spread = Math.tan((shape.angle || 0) * Math.PI / 180);
        dir = normalize([x * spread, y * spread, 1]);
        if (shape.type >= 8) {
          const along = Math.random() * (shape.length || 0);
          pos = pos.map((v, i) => v + dir[i] * along);
        }
        break;
      }
      case 5: case 15: case 16: {
        const [bx, by, bz] = shape.box;
        pos = [(Math.random() - 0.5) * bx, (Math.random() - 0.5) * by, (Math.random() - 0.5) * bz];
        dir = [0, 0, 1];
        break;
      }
      case 10: case 11: {
        const a = Math.random() * arc;
        const k = shape.type === 11 ? 1 : Math.sqrt(Math.random());
        pos = [Math.cos(a) * r * k, Math.sin(a) * r * k, 0];
        dir = [Math.cos(a), Math.sin(a), 0];
        break;
      }
      case 12:
        pos = [(Math.random() * 2 - 1) * r, 0, 0];
        dir = [0, 1, 0];
        break;
      default:
        pos = [0, 0, 0];
        dir = [0, 0, 1];
    }
    if (shape.random_direction > 0) {
      const rnd = unitRandom(), k = Math.min(1, shape.random_direction);
      dir = normalize(dir.map((v, i) => v + (rnd[i] - v) * k));
    }
    return { pos, dir };
  }

  const MAX_PARTICLES = 1500;

  class Emitter {
    constructor(def) {
      this.def = def;
      this.particles = [];
      this.time = 0;
      this.carry = 0;
      this.delay = sample(def.delay, 0, Math.random());
      this.cap = Math.min(def.max_particles || 1000, MAX_PARTICLES);
      this.lastCycle = 0;
      if (def.prewarm && def.looping) {
        const steps = Math.min(150, Math.ceil(Math.min(def.duration || 5, 10) * 15));
        for (let i = 0; i < steps; i++) this.update(Math.min(def.duration || 5, 10) / steps);
      }
    }

    spawn(cycle) {
      const def = this.def;
      if (this.particles.length >= this.cap) return;
      const r = () => Math.random();
      const life = Math.max(0.01, sample(def.lifetime, cycle, r()));
      const { pos, dir } = shapeSample(def.shape);
      const speed = sample(def.speed, cycle, r());
      const sheet = def.sheet;
      this.particles.push({
        pos, shape: pos.slice(), vel: dir.map((v) => v * speed), age: 0, life,
        size: sample(def.size, cycle, r()),
        color: sampleColor(def.color, cycle, r()),
        rot: sample(def.rotation, cycle, r()),
        spinSign: def.random_rotation_direction > r() ? -1 : 1,
        rv: [r(), r(), r(), r(), r(), r()],
        row: sheet && sheet.random_row ? Math.floor(r() * sheet.tiles[1]) : (sheet ? sheet.row : 0),
      });
    }

    update(dt) {
      const def = this.def;
      dt *= def.speed_scale || 1;
      this.time += dt;
      const duration = def.duration || 5;
      const live = this.time - this.delay;
      if (live > 0 && (def.looping || live < duration)) {
        const cycleTime = def.looping ? live % duration : live;
        const cycle = cycleTime / duration;
        // Bursts fire as the cycle passes their time.
        const wrapped = cycleTime < this.lastCycle;
        for (const burst of def.bursts || []) {
          if ((burst.time > this.lastCycle || wrapped) && burst.time <= cycleTime) {
            const n = burst.min + Math.floor(Math.random() * (burst.max - burst.min + 1));
            for (let i = 0; i < n; i++) this.spawn(cycle);
          }
        }
        this.lastCycle = cycleTime;
        this.carry += Math.max(0, sample(def.rate, cycle, Math.random())) * dt;
        while (this.carry >= 1) { this.carry -= 1; this.spawn(cycle); }
      }
      const g = -9.81 * (def.gravity || 0);
      const kept = [];
      for (const p of this.particles) {
        p.age += dt;
        if (p.age >= p.life) continue;
        const t = p.age / p.life;
        if (def.force) {
          p.vel[0] += sample(def.force.x, t, p.rv[0]) * dt;
          p.vel[1] += sample(def.force.y, t, p.rv[1]) * dt;
          p.vel[2] += sample(def.force.z, t, p.rv[2]) * dt;
        }
        if (g) p.vel[1] += g * dt;
        let vx = p.vel[0], vy = p.vel[1], vz = p.vel[2];
        if (def.velocity) {
          vx += sample(def.velocity.x, t, p.rv[3]);
          vy += sample(def.velocity.y, t, p.rv[4]);
          vz += sample(def.velocity.z, t, p.rv[5]);
        }
        p.pos[0] += vx * dt; p.pos[1] += vy * dt; p.pos[2] += vz * dt;
        p.v = [vx, vy, vz];
        if (def.spin) p.rot += sample(def.spin, t, p.rv[2]) * dt * p.spinSign;
        kept.push(p);
      }
      this.particles = kept;
    }

    // Position in the emitter's frame for drawing with ``frameMatrix``:
    // Unity's "Shape" scaling (2) scales only the spawn shape, not how far a
    // particle has travelled; other modes scale everything (full matrix).
    placed(p) {
      if (this.def.scaling !== 2) return p.pos;
      const s = this.def.scale || [1, 1, 1];
      return [0, 1, 2].map((i) => p.shape[i] * s[i] + (p.pos[i] - p.shape[i]));
    }

    // Per-particle draw values at its current age.
    look(p) {
      const def = this.def, t = p.age / p.life;
      let color = p.color;
      if (def.color_over_life) {
        const c = sampleColor(def.color_over_life, t, p.rv[1]);
        color = color.map((v, i) => v * c[i]);
      }
      let size = p.size;
      if (def.size_over_life) size *= sample(def.size_over_life, t, p.rv[0]);
      let uv = [0, 0, 1, 1];
      const sheet = def.sheet;
      if (sheet) {
        const [tx, ty] = sheet.tiles;
        const frames = sheet.row_mode === 1 ? tx : tx * ty;
        let f = sample(sheet.frame, (t * (sheet.cycles || 1)) % 1, p.rv[5]) +
          sample(sheet.start, 0, p.rv[4]) / frames;
        f = ((f % 1) + 1) % 1;
        const index = Math.min(frames - 1, Math.floor(f * frames));
        const col = index % tx;
        const row = sheet.row_mode === 1 ? p.row : Math.floor(index / tx);
        uv = [col / tx, 1 - (row + 1) / ty, (col + 1) / tx, 1 - row / ty];
      }
      return { color, size, uv };
    }
  }

  // ------------------------------------------------------------ GoAnim
  // A port of GoAnim.AnimContainer timing (HexClient GoAnim.cs).
  const rand = ([a, b]) => a + Math.random() * (b - a);

  class AnimStack {
    constructor(a) {
      this.a = a;
      this.cur = a.time || 0;
      this.plays = 0;
    }
    reset(now) {
      this.start = now + rand(this.a.interval);
      const d = rand(this.a.duration);
      this.scale = d > 0 ? 1 / d : 0;
    }
    update(dt, now) {
      const a = this.a;
      if (a.max_plays > 0 && this.plays >= a.max_plays) return;
      this.cur += dt * this.scale;
      if (this.cur > 1) {
        this.plays++;
        if (a.loop) this.cur -= 1; else { this.cur = 0; this.reset(now); }
      } else if (this.cur < 0) {
        this.plays++;
        if (a.loop) this.cur += 1; else { this.cur = 1; this.reset(now); }
      }
      if (now < this.start) this.cur = 0;
    }
    value() { return this.a.curve && this.a.curve.length ? evalKeys(this.a.curve, this.cur) : this.cur; }
  }

  class GoAnimPlayer {
    constructor(anim) {
      this.anim = anim;
      this.elapsed = 0;
      this.materials = (anim.materials || []).map((a) => new AnimStack(a));
      this.transforms = (anim.transforms || []).map((a) => new AnimStack(a));
      for (const s of [...this.materials, ...this.transforms]) {
        if (anim.time_offset) s.cur = anim.time_offset;
        s.reset(0);
      }
    }
    update(dt) {
      if (!this.anim.play) return;
      const step = dt * (this.anim.time_scale || 1);
      this.elapsed += dt;
      for (const s of [...this.materials, ...this.transforms]) {
        if (s.a.delay < this.elapsed) s.update(step, this.elapsed);
      }
    }
  }

  const lerp = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);

  // ------------------------------------------------------------ three.js
  const VERTEX = `
    attribute vec4 pcolor;
    uniform float hasVColor;
    varying vec2 vUv;
    varying vec4 vColor;
    void main() {
      vUv = uv;
      vColor = hasVColor > 0.5 ? pcolor : vec4(1.0);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`;
  const FRAGMENT = `
    uniform sampler2D map, alphaMap, maskMap, distortMap;
    uniform vec4 distort;   // tiling.xy, speed.zw
    uniform float distortAmount, hasDistort;
    uniform float hasMap, hasAlpha, hasMask, mode, time, gradAlpha, premul;
    uniform vec4 tint, uvST, alphaST, wave;
    uniform vec2 scroll, scrollAlpha;
    uniform vec3 reBlack, reWhite;
    varying vec2 vUv;
    varying vec4 vColor;
    void main() {
      vec2 uv = vUv * uvST.xy + uvST.zw + scroll * time;
      if (mode > 1.5) {          // waving cloth (Hex_DiffuseTransparentVertexAnim, approximated)
        float m = hasMask > 0.5 ? texture2D(maskMap, vUv).r : 1.0;
        float along = wave.w < 0.5 ? vUv.x : vUv.y;
        float phase = along * wave.y * 0.04 + time * wave.x * 60.0;
        vec2 axis = wave.w < 0.5 ? vec2(0.0, 1.0) : vec2(1.0, 0.0);
        uv += axis * sin(phase) * wave.z * 0.04 * m;
      }
      if (hasDistort > 0.5) {   // Hex_TransparentDiffuseDistort: wobble, picture fixed
        vec2 d = texture2D(distortMap, vUv * distort.xy + distort.zw * time).rg - 0.5;
        uv += d * 2.0 * distortAmount;
      }
      vec4 c = hasMap > 0.5 ? texture2D(map, uv) : vec4(vec3(vUv.y), 1.0);
      if (mode > 0.5 && mode < 1.5) {   // Hex/Animated Grad: remap, scrolling alpha
        c.rgb = mix(reBlack, reWhite, c.r);
        float a = hasAlpha > 0.5 ? texture2D(alphaMap, vUv * alphaST.xy + alphaST.zw + scrollAlpha * time).r : 1.0;
        c.a *= a * gradAlpha;
      }
      c *= tint * vColor;
      // Particles/Alpha Blended Premultiply: colour carries its alpha.
      if (premul > 0.5) c.rgb *= c.a;
      gl_FragColor = c;
    }`;

  const textureCache = new Map();
  function texture(url, onLoad) {
    if (!url) return null;
    if (!textureCache.has(url)) {
      const tex = new THREE.TextureLoader().load(`/generated/${url}`, onLoad);
      tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
      textureCache.set(url, tex);
    }
    return textureCache.get(url);
  }

  function blendFactors(material, blend) {
    material.blending = THREE.CustomBlending;
    material.blendEquation = THREE.AddEquation;
    material.blendSrc = blend === "premultiply" ? THREE.OneFactor : THREE.SrcAlphaFactor;
    material.blendDst = blend === "add" ? THREE.OneFactor : THREE.OneMinusSrcAlphaFactor;
  }

  // One shader for every panorama mesh and particle system.
  function makeMaterial(info, onLoad, particles = false) {
    const grad = info.grad, wave = info.wave;
    const map = texture(info.texture, onLoad);
    const alphaMap = grad ? texture(grad.alpha_texture, onLoad) : null;
    const maskMap = wave ? texture(wave.mask, onLoad) : null;
    const dist = info.distort;
    const distortMap = dist ? texture(dist.texture, onLoad) : null;
    const tint = info.tint || [1, 1, 1, 1];
    const uniforms = {
      map: { value: map }, hasMap: { value: map ? 1 : 0 },
      alphaMap: { value: alphaMap }, hasAlpha: { value: alphaMap ? 1 : 0 },
      maskMap: { value: maskMap }, hasMask: { value: maskMap ? 1 : 0 },
      mode: { value: grad ? 1 : wave ? 2 : 0 }, time: { value: 0 },
      tint: { value: new THREE.Vector4(...tint) },
      uvST: { value: new THREE.Vector4(...(info.uv || [1, 1, 0, 0])) },
      alphaST: { value: new THREE.Vector4(1, 1, 0, 0) },
      scroll: { value: new THREE.Vector2(...((grad && grad.scroll) || info.scroll || [0, 0])) },
      scrollAlpha: { value: new THREE.Vector2(...((grad && grad.scroll_alpha) || [0, 0])) },
      reBlack: { value: new THREE.Vector3(...((grad && grad.black) || [0, 0, 0]).slice(0, 3)) },
      reWhite: { value: new THREE.Vector3(...((grad && grad.white) || [1, 1, 1]).slice(0, 3)) },
      gradAlpha: { value: grad ? grad.alpha : 1 },
      wave: { value: new THREE.Vector4(wave ? wave.speed : 0, wave ? wave.frequency : 0,
        wave ? wave.intensity : 0, wave ? wave.axis : 0) },
      hasVColor: { value: particles ? 1 : 0 },
      distortMap: { value: distortMap }, hasDistort: { value: distortMap ? 1 : 0 },
      distort: { value: new THREE.Vector4(...((dist && dist.tiling) || [1, 1]), ...((dist && dist.speed) || [0, 0])) },
      distortAmount: { value: dist ? Math.abs(dist.intensity) : 0 },
      premul: { value: info.blend === "premultiply" ? 1 : 0 },
    };
    const material = new THREE.ShaderMaterial({
      uniforms, vertexShader: VERTEX, fragmentShader: FRAGMENT,
      transparent: true, depthWrite: false, side: THREE.DoubleSide,
    });
    blendFactors(material, info.blend);
    material.userData.timeScale = grad ? grad.time : 1;
    material.userData.particleShader = /particles/i.test(info.shader || "");
    return material;
  }

  // GoAnim material/transform values applied to a three.js mesh.
  function applyGoAnim(player, mesh, pivot) {
    const u = mesh.material.uniforms;
    player.materials.forEach((stack) => {
      const a = stack.a, k = stack.value();
      if (a.type === "color") {
        const c = lerp(a.values[0], a.values[1], k);
        const twice = mesh.material.userData.particleShader && a.id === "_TintColor" ? 2 : 1;
        if (a.id === "_ReWhite") u.reWhite.value.set(c[0], c[1], c[2]);
        else if (a.id === "_ReBlack") u.reBlack.value.set(c[0], c[1], c[2]);
        else u.tint.value.set(c[0] * twice, c[1] * twice, c[2] * twice, c[3] * twice);
      } else if (a.type === "float" && a.id === "_Alpha") {
        u.gradAlpha.value = a.values[0] + (a.values[1] - a.values[0]) * k;
      } else if (a.type === "texture" && a.id === "_MainTex") {
        const base = mesh.userData.uvBase;
        const scale = a.scale[0].join() !== a.scale[1].join() ? lerp(a.scale[0], a.scale[1], k) : [0, 0];
        const off = a.offset[0].join() !== a.offset[1].join() ? lerp(a.offset[0], a.offset[1], k) : [0, 0];
        u.uvST.value.set(base[0] + scale[0], base[1] + scale[1], base[2] + off[0], base[3] + off[1]);
      }
    });
    if (player.transforms.length && pivot) {
      // Scale (the only transform animation the panoramas use) about the
      // object's pivot: localScale = lerp(S0, S1, k) + start.
      let sx = 1, sy = 1, sz = 1;
      const start = pivot.local_scale || [1, 1, 1];
      player.transforms.forEach((stack) => {
        const a = stack.a, k = stack.value();
        if (a.scale[0].join() !== a.scale[1].join()) {
          const s = lerp(a.scale[0], a.scale[1], k);
          sx *= (s[0] + start[0]) / (start[0] || 1);
          sy *= (s[1] + start[1]) / (start[1] || 1);
          sz *= (s[2] + start[2]) / (start[2] || 1);
        }
      });
      const p = new THREE.Vector3(pivot.position[0], pivot.position[1], -pivot.position[2]);
      const [qx, qy, qz, qw] = pivot.rotation;
      const q = new THREE.Quaternion(-qx, -qy, qz, qw);
      const m = new THREE.Matrix4().makeTranslation(p.x, p.y, p.z)
        .multiply(new THREE.Matrix4().makeRotationFromQuaternion(q))
        .multiply(new THREE.Matrix4().makeScale(sx, sy, sz))
        .multiply(new THREE.Matrix4().makeRotationFromQuaternion(q.clone().invert()))
        .multiply(new THREE.Matrix4().makeTranslation(-p.x, -p.y, -p.z));
      mesh.matrixAutoUpdate = false;
      mesh.matrix.copy(m);
    }
  }

  // A particle system drawn as camera-facing quads rebuilt each frame.
  class ThreeEmitter {
    constructor(def, onLoad) {
      this.sim = new Emitter(def);
      this.def = def;
      const n = this.sim.cap;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * 12), 3));
      geometry.setAttribute("uv", new THREE.BufferAttribute(new Float32Array(n * 8), 2));
      geometry.setAttribute("pcolor", new THREE.BufferAttribute(new Float32Array(n * 16), 4));
      const index = new Uint32Array(n * 6);
      for (let i = 0; i < n; i++) index.set([i * 4, i * 4 + 1, i * 4 + 2, i * 4, i * 4 + 2, i * 4 + 3], i * 6);
      geometry.setIndex(new THREE.BufferAttribute(index, 1));
      geometry.setDrawRange(0, 0);
      this.mesh = new THREE.Mesh(geometry, makeMaterial(def.material, onLoad, true));
      this.mesh.frustumCulled = false;
      const [x, y, z] = def.position, [qx, qy, qz, qw] = def.orientation;
      // Shape scaling: positions arrive pre-scaled from Emitter.placed().
      this.matrix = new THREE.Matrix4().compose(new THREE.Vector3(x, y, -z),
        new THREE.Quaternion(-qx, -qy, qz, qw),
        def.scaling === 2 ? new THREE.Vector3(1, 1, 1) : new THREE.Vector3(...def.scale));
      // "Shape" scaling leaves particle size unscaled.
      this.sizeScale = def.scaling === 2 ? 1 : Math.max(...def.scale.map(Math.abs));
    }

    update(dt, camera, viewHeight) {
      this.sim.update(dt);
      const geo = this.mesh.geometry;
      const pos = geo.attributes.position.array, uvs = geo.attributes.uv.array, col = geo.attributes.pcolor.array;
      const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
      const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
      const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
      const render = this.def.render || { mode: 0, max_size: 0.5 };
      const tanHalf = Math.tan((camera.fov * Math.PI / 180) / 2);
      const p = new THREE.Vector3(), v = new THREE.Vector3(), a = new THREE.Vector3(), b = new THREE.Vector3();
      const groupShift = this.mesh.parent ? this.mesh.parent.position : new THREE.Vector3();
      let n = 0;
      for (const particle of this.sim.particles) {
        const look = this.sim.look(particle);
        // Unity local -> three world (z mirrored on both sides of the matrix).
        const at = this.sim.placed(particle);
        p.set(at[0], at[1], -at[2]).applyMatrix4(this.matrix);
        const depth = Math.max(1e-4, p.clone().add(groupShift).sub(camera.position).dot(forward));
        let half = look.size * this.sizeScale / 2;
        const cap = render.max_size * 2 * depth * tanHalf / 2;
        if (render.max_size > 0 && half > cap) half = cap;
        if (render.mode === 1 && particle.v) {
          // Stretched billboard: along the velocity, length grows with speed.
          v.set(particle.v[0], particle.v[1], -particle.v[2]).transformDirection(this.matrix);
          const speed = Math.hypot(...particle.v) * this.sizeScale;
          v.sub(forward.clone().multiplyScalar(v.dot(forward))).normalize();
          if (!v.lengthSq()) v.copy(up);
          const length = half * render.length_scale + speed * render.velocity_scale / 2;
          a.copy(v).multiplyScalar(length);
          b.copy(v).cross(forward).normalize().multiplyScalar(half);
        } else if (render.mode === 2) {
          a.set(half, 0, 0); b.set(0, 0, half);
        } else {
          const c = Math.cos(particle.rot), s = Math.sin(particle.rot);
          a.copy(right).multiplyScalar(c * half).addScaledVector(up, s * half);
          b.copy(up).multiplyScalar(c * half).addScaledVector(right, -s * half);
          if (render.mode === 3) b.copy(new THREE.Vector3(0, half, 0));
        }
        const o = n * 12;
        pos[o] = p.x - a.x - b.x; pos[o + 1] = p.y - a.y - b.y; pos[o + 2] = p.z - a.z - b.z;
        pos[o + 3] = p.x + a.x - b.x; pos[o + 4] = p.y + a.y - b.y; pos[o + 5] = p.z + a.z - b.z;
        pos[o + 6] = p.x + a.x + b.x; pos[o + 7] = p.y + a.y + b.y; pos[o + 8] = p.z + a.z + b.z;
        pos[o + 9] = p.x - a.x + b.x; pos[o + 10] = p.y - a.y + b.y; pos[o + 11] = p.z - a.z + b.z;
        const [u0, v0, u1, v1] = look.uv;
        uvs.set([u0, v0, u1, v0, u1, v1, u0, v1], n * 8);
        for (let k = 0; k < 4; k++) col.set(look.color, n * 16 + k * 4);
        n++;
      }
      geo.setDrawRange(0, n * 6);
      geo.attributes.position.needsUpdate = true;
      geo.attributes.uv.needsUpdate = true;
      geo.attributes.pcolor.needsUpdate = true;
      this.mesh.material.uniforms.time.value += dt;
      return n;
    }
  }

  return { evalKeys, sample, sampleColor, Emitter, GoAnimPlayer, makeMaterial, applyGoAnim, ThreeEmitter };
})();
