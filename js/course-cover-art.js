(function (root) {
  'use strict';
  const path = (d, style = 'line') => `<path class="${style}" d="${d}"/>`;
  const circle = (x, y, r = 3, style = 'point') => `<circle class="${style}" cx="${x}" cy="${y}" r="${r}"/>`;
  const ellipse = (x, y, rx, ry, style = 'line') => `<ellipse class="${style}" cx="${x}" cy="${y}" rx="${rx}" ry="${ry}"/>`;
  const label = (x, y, value) => `<text class="symbol" x="${x}" y="${y}">${value}</text>`;
  const sampled = (fn, count = 48) => Array.from({ length: count + 1 }, (_, i) => {
    const [x, y] = fn(i / count);
    return `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(' ');
  const curve = (fn, style = 'line') => path(sampled(fn), style);
  const axes = () => path('M181 124H322M194 137V20', 'soft') + path('m317 120 5 4-5 4M190 25l4-5 4 5', 'soft');
  const matrix = () => path('M203 36h-10v90h10M302 36h10v90h-10', 'strong') +
    [0, 1, 2].flatMap(row => [0, 1, 2].map(col => circle(215 + col * 36, 53 + row * 28, row === col ? 5 : 2.4, row === col ? 'accent-dot' : 'point'))).join('') +
    path('M213 116l73-58m-8-1 8 1-1 8', 'accent');
  const derivative = () => axes() + curve(t => [186 + 127 * t, 119 - 88 * t * t], 'strong') +
    path('M205 112 304 46', 'accent') + circle(253, 79, 4.6, 'accent-dot');
  const integral = () => axes() + path('M202 122V84Q237 37 270 61T312 75V122Z', 'area') +
    path('M202 84Q237 37 270 61T312 75', 'strong') + [213, 235, 257, 279, 301].map(x => path(`M${x} 122V${69 + Math.abs(x - 255) * .18}`, 'soft')).join('');
  const series = () => axes() + [0, 1, 2, 3, 4, 5].map(i => {
    const height = 75 / (i + 1);
    return path(`M${205 + i * 19} 122v-${height.toFixed(1)}`, i < 2 ? 'strong' : 'line') + circle(205 + i * 19, 122 - height, 3, 'accent-dot');
  }).join('') + path('M203 44h102', 'soft');
  const group = () => path('M250 26 304 55v56l-54 29-54-29V55Z', 'line') +
    [[250, 26], [304, 55], [304, 111], [250, 140], [196, 111], [196, 55]].map(([x, y], i) => circle(x, y, i ? 4 : 6, i ? 'point' : 'accent-dot')).join('') +
    path('M213 67q35-34 72 0m-7-3 7 3-2-8M287 95q-34 34-73 1m7 1-7-1 3 8', 'accent') + label(244, 87, 'G');
  const analytic = () => axes() + ellipse(255, 79, 57, 36, 'strong') +
    circle(226, 79, 4, 'accent-dot') + circle(284, 79, 4, 'accent-dot') + path('M226 79 267 52 284 79', 'soft');
  const topology = () => ellipse(225, 81, 28, 32, 'strong') + path('M264 81h19m-5-5 5 5-5 5', 'accent') +
    path('M296 52c-30 6-38 59-9 64 23 4 23-27 11-29-12-2-14 12-3 15', 'strong') +
    circle(296, 52, 3, 'accent-dot');
  const spiral = () => axes() + curve(t => {
    const a = t * Math.PI * 4.6, r = 5 + t * 59;
    return [255 + r * Math.cos(a), 81 + r * Math.sin(a) * .68];
  }, 'strong') + circle(255, 81, 4, 'accent-dot') + path('m293 98 8-2-2 8', 'accent');
  const complex = () => axes() + ellipse(254, 80, 43, 43, 'line') +
    path('M254 80 286 51m-6 2 6-2-1 7', 'strong') + circle(286, 51, 4, 'accent-dot') + label(303, 79, '1') + label(247, 31, 'i');
  const bell = () => axes() + curve(t => {
    const z = (t - .5) * 5;
    return [194 + t * 118, 121 - 81 * Math.exp(-z * z / 2)];
  }, 'strong') + [0, 1, 2, 3, 4].map(i => path(`M${218 + i * 18} 120v-${[27, 58, 79, 58, 27][i]}`, 'soft')).join('');
  const cantor = () => {
    const segments = [0, 1, 2, 3].map(level => {
      const width = 109 / 3 ** level, y = 47 + level * 25;
      return Array.from({ length: 2 ** level }, (_, i) => {
        const digits = i.toString(2).padStart(level, '0');
        const start = [...digits].reduce((sum, digit, pos) => sum + Number(digit) * 2 * 109 / 3 ** (pos + 1), 0);
        return path(`M${198 + start} ${y}h${width}`, level === 3 ? 'accent' : 'strong');
      }).join('');
    });
    return segments.join('') + label(195, 145, '[0,1]');
  };
  const projection = () => axes() + path('M203 117 270 43M203 117 298 117M270 43v74m-6-7h6v7', 'strong') +
    circle(270, 43, 5, 'accent-dot') + circle(270, 117, 4, 'point') + label(283, 61, 'v');
  const waves = () => [0, 1, 2, 3].map(i => curve(t => [186 + 133 * t, 46 + i * 26 + 13 * Math.sin(2 * Math.PI * (t + i * .12))], i === 1 ? 'strong' : 'line')).join('');
  const fourier = () => axes() + [1, 2, 3].map((n, i) => curve(t => [195 + 117 * t, 54 + i * 24 + 10 / n * Math.sin(t * Math.PI * 2 * n)], i === 0 ? 'strong' : 'line')).join('') + label(200, 34, 'Σ');
  const symplectic = () => axes() + [28, 45, 61].map((r, i) => ellipse(253, 81, r, r * .59, i === 1 ? 'strong' : 'line')).join('') +
    path('M276 45q16 7 20 17m-6-2 6 2 1-7', 'accent') + circle(298, 81, 4, 'accent-dot');
  const homology = () => ellipse(252, 82, 62, 40, 'strong') + ellipse(252, 82, 23, 16, 'strong') +
    path('M190 82h124M252 42v80', 'soft') + path('M226 62q26-23 54 0m-6-3 6 3-3 6', 'accent') + label(206, 79, 'a') + label(262, 118, 'b');
  const surface = () => [0, 1, 2, 3, 4].map(i => curve(t => {
    const u = t * 2 - 1, v = i / 2 - 1;
    return [252 + 51 * u + 17 * v, 83 + 23 * v - 19 * (u * u - v * v)];
  }, i === 2 ? 'strong' : 'line')).join('') + [-1, -.5, 0, .5, 1].map(u => curve(t => {
    const v = t * 2 - 1;
    return [252 + 51 * u + 17 * v, 83 + 23 * v - 19 * (u * u - v * v)];
  }, 'soft')).join('');
  const sphere = () => ellipse(252, 82, 59, 59, 'strong') + ellipse(252, 82, 59, 20, 'soft') +
    path('M252 23c-24 22-24 96 0 118m0-118c24 22 24 96 0 118', 'soft') +
    path('M209 123Q246 39 300 51', 'accent') + circle(209, 123, 4, 'accent-dot') + circle(300, 51, 4, 'accent-dot');
  const charts = () => ellipse(232, 81, 43, 36, 'line') + ellipse(279, 81, 43, 36, 'line') +
    path('M224 82h62m-6-5 6 5-6 5', 'accent') + label(206, 82, 'U') + label(293, 82, 'V');
  const ideals = () => path('M253 36 208 82 253 128 298 82Z', 'strong') +
    [[253,36],[208,82],[298,82],[253,128]].map(([x,y],i)=>circle(x,y,i===3?5:4,i===3?'accent-dot':'point')).join('') +
    label(247, 27, 'R') + label(191, 87, 'I') + label(307, 87, 'J') + label(236, 151, 'I∩J');
  const modular = () => ellipse(253, 82, 57, 57, 'line') + Array.from({ length: 8 }, (_, i) => {
    const angle = i * Math.PI / 4 - Math.PI / 2;
    return circle((253 + 57 * Math.cos(angle)).toFixed(1), (82 + 57 * Math.sin(angle)).toFixed(1), i % 2 ? 3 : 4.5, i % 2 ? 'point' : 'accent-dot');
  }).join('') + path('M253 25 310 82 253 139 196 82', 'accent') + label(245, 86, 'ℤ');
  const cubic = () => axes() + curve(t => {
    const x = (t - .5) * 2.7;
    return [253 + x * 44, 78 - (x * x * x - x) * 26];
  }, 'strong') + circle(216, 73, 4, 'accent-dot') + circle(289, 84, 4, 'accent-dot');
  const chain = () => [202, 251, 300].map(x => circle(x, 80, 11, 'outline')).join('') +
    path('M214 80h25m-5-5 5 5-5 5M263 80h25m-5-5 5 5-5 5', 'accent') + label(196, 49, 'A') + label(245, 49, 'B') + label(294, 49, 'C') + label(243, 123, 'im = ker');
  const representation = () => path('M252 28 308 118H196Z', 'line') +
    [[252, 28], [308, 118], [196, 118]].map(([x, y]) => circle(x, y, 5, 'accent-dot')).join('') +
    path('M250 34q-29 27-49 76m7-4-7 4 7 4M207 119h91m-6-5 6 5-6 5', 'accent') + label(244, 91, 'ρ');
  const model = () => axes() + curve(t => [194 + 119 * t, 120 - 66 * t + 8 * Math.sin(t * Math.PI * 2)], 'strong') +
    [[209, 109], [231, 98], [251, 89], [273, 69], [296, 62]].map(([x, y]) => circle(x, y, 4, 'accent-dot')).join('');
  const finance = () => axes() + path('M197 108 215 99 230 109 247 75 260 84 280 52 299 63 315 34', 'strong') +
    path('M197 111 215 102 230 112 247 78 260 87 280 55 299 66 315 37', 'soft') + path('M196 90h120', 'accent');
  const optimization = () => [55, 39, 24, 10].map((r, i) => ellipse(251, 82, r, r * .62, i === 3 ? 'strong' : 'line')).join('') +
    path('M305 41 277 62 262 76m6-1-6 1 2-7', 'accent') + circle(251, 82, 4, 'accent-dot');
  const randomWalk = () => axes() + path('M195 101 211 83 226 94 242 62 256 77 272 48 289 69 306 38', 'strong') +
    [[195,101],[211,83],[226,94],[242,62],[256,77],[272,48],[289,69],[306,38]].map(([x,y],i)=>circle(x,y, i===7?4:2.6,i===7?'accent-dot':'point')).join('');
  const statistics = () => axes() + [25, 42, 69, 78, 60, 36, 18].map((h, i) => path(`M${201 + i * 16} 122v-${h}`, i === 3 ? 'strong' : 'line')).join('') +
    path('M234 36h39m-39-5v10m39-10v10', 'accent');
  const risk = () => axes() + path('M261 80q22 17 51 42h-51Z', 'area') + bell() + path('M261 81v41', 'accent') + label(269, 61, 'VaR');
  const stochasticIntegral = () => axes() + path('M195 110 213 92 226 103 243 69 258 76 275 47 292 70 309 44', 'strong') +
    path('M195 122h15v-26h16v14h16V77h16v4h17V55h17v17h17', 'accent');
  const hypercube = () => path('M212 47h74v74h-74zM229 30h74v74h-74zM212 47l17-17m57 17 17-17m-91 91 17-17m57 17 17-17', 'line') +
    [[212,47],[286,47],[286,121],[212,121],[229,30],[303,30],[303,104],[229,104]].map(([x,y])=>circle(x,y,3,'accent-dot')).join('');
  const sparse = () => path('M198 30h-9v100h9M307 30h9v100h-9', 'strong') +
    [0,1,2,3].flatMap(i => [0,1,2,3].map(j => circle(207+j*30,45+i*24,(i===j || i===j+1)?4:1.5,(i===j || i===j+1)?'accent-dot':'point'))).join('');
  const interpolation = () => axes() + curve(t => [194 + 118 * t, 119 - 62 * Math.sin(t * Math.PI) + 15 * t], 'strong') +
    [0,.24,.5,.75,1].map(t => circle((194+118*t).toFixed(1),(119-62*Math.sin(t*Math.PI)+15*t).toFixed(1),4,'accent-dot')).join('');
  const classifier = () => path('M201 122 300 34m-6 2 6-2-1 7', 'accent') +
    [[206,49],[226,55],[214,77],[241,42],[233,73],[272,102],[292,108],[273,123],[301,81],[282,88]].map(([x,y],i)=>circle(x,y,4,i<5?'point':'accent-dot')).join('');
  const neural = () => {
    const layers = [[194,[52,82,112]],[250,[42,66,98,124]],[309,[54,82,110]]];
    return layers.slice(0,-1).flatMap((layer,i)=>layer[1].flatMap(y=>layers[i+1][1].map(next=>path(`M${layer[0]} ${y} ${layers[i+1][0]} ${next}`,'soft')))).join('') +
      layers.flatMap((layer,i)=>layer[1].map(y=>circle(layer[0],y,5,i===1?'accent-dot':'outline'))).join('');
  };
  const motifs = { matrix, derivative, integral, series, group, analytic, topology, spiral, complex, bell, cantor, projection, waves, fourier, symplectic, homology, surface, sphere, charts, ideals, modular, cubic, chain, representation, model, finance, optimization, randomWalk, statistics, risk, stochasticIntegral, hypercube, sparse, interpolation, classifier, neural };
  const designs = {
    algebra: ['matrix', 'algebra', 'LINEAR TRANSFORMATIONS'],
    analysis: ['derivative', 'analysis', 'LIMITS & DERIVATIVES'],
    'analysis-ii': ['integral', 'analysis', 'INTEGRALS & SERIES'],
    'analysis-iii': ['series', 'analysis', 'INFINITE SERIES'],
    'abstract-algebra': ['group', 'algebra', 'GROUP STRUCTURE'],
    'analytic-geometry': ['analytic', 'geometry', 'COORDINATES & CONICS'],
    'point-set-topology': ['topology', 'geometry', 'CONTINUITY & SHAPE'],
    'ordinary-differential-equations': ['spiral', 'analysis', 'DYNAMICAL SYSTEMS'],
    'complex-analysis': ['complex', 'analysis', 'COMPLEX PLANE'],
    probability: ['bell', 'probability', 'RANDOM VARIABLES'],
    'real-analysis': ['cantor', 'analysis', 'SETS & MEASURE'],
    'functional-analysis': ['projection', 'analysis', 'HILBERT SPACE'],
    pde: ['waves', 'analysis', 'WAVE EQUATIONS'],
    'harmonic-analysis': ['fourier', 'analysis', 'FOURIER ANALYSIS'],
    'course-symplectic-math-physics': ['symplectic', 'geometry', 'PHASE SPACE'],
    homology: ['homology', 'geometry', 'CYCLES & HOLES'],
    geometry: ['surface', 'geometry', 'CURVATURE & SURFACES'],
    'riemannian-geometry': ['sphere', 'geometry', 'GEODESICS'],
    'differentiable-manifolds': ['charts', 'geometry', 'LOCAL COORDINATES'],
    'commutative-algebra': ['ideals', 'algebra', 'IDEALS & RINGS'],
    'number-theory': ['modular', 'algebra', 'MODULAR ARITHMETIC'],
    'algebraic-geometry': ['cubic', 'geometry', 'ALGEBRAIC CURVES'],
    'homological-algebra': ['chain', 'algebra', 'EXACT SEQUENCES'],
    'groups-representations': ['representation', 'algebra', 'SYMMETRY & ACTION'],
    'mathematical-modeling': ['model', 'applied', 'DATA & MODELS'],
    'financial-mathematics': ['finance', 'applied', 'MARKETS & PAYOFFS'],
    'operations-research': ['optimization', 'applied', 'OPTIMIZATION'],
    'stochastic-processes': ['randomWalk', 'probability', 'RANDOM WALKS'],
    'mathematical-statistics': ['statistics', 'probability', 'ESTIMATION'],
    'risk-management': ['risk', 'probability', 'TAIL RISK'],
    'stochastic-analysis': ['stochasticIntegral', 'probability', 'STOCHASTIC PATHS'],
    'high-dimensional-probability': ['hypercube', 'probability', 'HIGH DIMENSIONS'],
    'numerical-linear-algebra': ['sparse', 'applied', 'SPARSE MATRICES'],
    'numerical-analysis': ['interpolation', 'applied', 'NUMERICAL METHODS'],
    'machine-learning': ['classifier', 'applied', 'DECISION BOUNDARIES'],
    'neural-networks': ['neural', 'applied', 'NEURAL NETWORKS']
  };
  const fallback = { algebra: designs.algebra, analysis: designs.analysis, geometry: designs.geometry, probability: designs.probability, pde: designs.pde, symplectic: designs['course-symplectic-math-physics'] };
  function designFor(course) {
    return designs[course.catalogId || course.id] || fallback[course.theme] || designs.algebra;
  }
  function render(course) {
    const [motif] = designFor(course);
    return `<svg class="whu-course-figure" viewBox="0 0 340 164" aria-hidden="true"><circle class="halo" cx="254" cy="82" r="74"/>${motifs[motif]()}</svg>`;
  }
  const api = { designs, designFor, render };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CourseCoverArt = api;
})(typeof window === 'undefined' ? globalThis : window);
