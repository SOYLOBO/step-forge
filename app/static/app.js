import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const codeEl = document.getElementById("code");
const renderBtn = document.getElementById("renderBtn");
const exportBtn = document.getElementById("exportBtn");
const fileInput = document.getElementById("fileInput");
const exampleBtn = document.getElementById("exampleBtn");
const statusEl = document.getElementById("status");
const fileChip = document.getElementById("filechip");
const leftPane = document.getElementById("leftPane");
const canvas = document.getElementById("viewer");

const trussifyBtn = document.getElementById("trussifyBtn");
const trussPanel = document.getElementById("trussPanel");
const trussCloseX = document.getElementById("trussCloseX");
const trussPreviewBtn = document.getElementById("trussPreviewBtn");
const trussApplyBtn = document.getElementById("trussApplyBtn");
const trussStats = document.getElementById("trussStats");
const cellSizeInput = document.getElementById("cellSize");
const wallThickInput = document.getElementById("wallThick");
const safetyMarginInput = document.getElementById("safetyMargin");
const densityInput = document.getElementById("density");
const cutAxisInput = document.getElementById("cutAxis");

const EXAMPLE = `from build123d import *

# A simple parametric bracket
with BuildPart() as bracket:
    Box(60, 40, 8)
    with Locations((-20, 0, 4)):
        Cylinder(8, 30)
    with Locations((-20, 0, 19)):
        Hole(3, 30)
    fillet(bracket.edges().filter_by(Axis.Z), 2)

result = bracket.part
`;

function setStatus(msg, kind = "") {
  statusEl.className = "status" + (kind ? " " + kind : "");
  statusEl.textContent = msg;
}

// ---- Three.js scene ----
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setClearColor(0x0a0b0e);

const scene = new THREE.Scene();
scene.add(new THREE.HemisphereLight(0xffffff, 0x202028, 0.85));
const dir = new THREE.DirectionalLight(0xffffff, 0.7);
dir.position.set(1, 1.5, 1);
scene.add(dir);

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
camera.position.set(120, 90, 120);
camera.up.set(0, 0, 1); // build123d / CAD convention: Z up

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.1;

const grid = new THREE.GridHelper(200, 20, 0x303644, 0x1f232b);
grid.rotation.x = Math.PI / 2; // grid on XY plane (Z up)
scene.add(grid);
const axes = new THREE.AxesHelper(30);
scene.add(axes);

let currentMesh = null;
const material = new THREE.MeshStandardMaterial({
  color: 0x6ea8ff,
  metalness: 0.15,
  roughness: 0.55,
  flatShading: false,
});
const edgeMat = new THREE.LineBasicMaterial({ color: 0x0a0b0e, transparent: true, opacity: 0.4 });

function fitCameraToObject(object) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z);
  const fov = camera.fov * (Math.PI / 180);
  const dist = (maxDim / (2 * Math.tan(fov / 2))) * 1.6;
  const dirVec = new THREE.Vector3(1, 1, 0.8).normalize();
  camera.position.copy(center.clone().add(dirVec.multiplyScalar(dist)));
  controls.target.copy(center);
  camera.near = Math.max(0.1, dist / 1000);
  camera.far = dist * 100;
  camera.updateProjectionMatrix();
  controls.update();
}

function replaceMesh(geometry) {
  if (currentMesh) {
    scene.remove(currentMesh);
    currentMesh.traverse((c) => c.geometry && c.geometry.dispose());
  }
  geometry.computeVertexNormals();
  const mesh = new THREE.Mesh(geometry, material);
  // Edge overlay for a CAD-ish look
  const edges = new THREE.EdgesGeometry(geometry, 25);
  const wire = new THREE.LineSegments(edges, edgeMat);
  mesh.add(wire);
  scene.add(mesh);
  currentMesh = mesh;
  fitCameraToObject(mesh);
}

function resize() {
  const rect = canvas.getBoundingClientRect();
  renderer.setSize(rect.width, rect.height, false);
  camera.aspect = rect.width / Math.max(1, rect.height);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas);
resize();

function animate() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();

// ---- API calls ----
async function postCode(path) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: codeEl.value }),
  });
  if (!res.ok) {
    let detail;
    try {
      const j = await res.json();
      detail = j.detail || JSON.stringify(j);
    } catch {
      detail = await res.text();
    }
    throw new Error(detail);
  }
  return res;
}

async function doRender() {
  setStatus("Rendering...");
  renderBtn.disabled = true;
  try {
    const t0 = performance.now();
    const res = await postCode("/api/render");
    const buf = await res.arrayBuffer();
    const geom = new STLLoader().parse(buf);
    replaceMesh(geom);
    const ms = (performance.now() - t0).toFixed(0);
    const tris = geom.attributes.position.count / 3;
    setStatus(`Rendered ${tris.toLocaleString()} triangles in ${ms} ms.`, "ok");
  } catch (e) {
    setStatus(e.message, "error");
  } finally {
    renderBtn.disabled = false;
  }
}

async function doExport() {
  setStatus("Exporting STEP...");
  exportBtn.disabled = true;
  try {
    const t0 = performance.now();
    const res = await postCode("/api/export-step");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "model.step";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    const ms = (performance.now() - t0).toFixed(0);
    setStatus(`Exported model.step (${(blob.size / 1024).toFixed(1)} KB) in ${ms} ms.`, "ok");
  } catch (e) {
    setStatus(e.message, "error");
  } finally {
    exportBtn.disabled = false;
  }
}

renderBtn.addEventListener("click", doRender);
exportBtn.addEventListener("click", doExport);
exampleBtn.addEventListener("click", () => {
  codeEl.value = EXAMPLE;
  fileChip.textContent = "example";
});

// ---- Trussify ----
function trussParams() {
  return {
    cell_size: parseFloat(cellSizeInput.value),
    wall_thickness: parseFloat(wallThickInput.value),
    safety_margin: parseFloat(safetyMarginInput.value),
    density_g_per_cm3: parseFloat(densityInput.value),
    cut_axis: cutAxisInput.value,
  };
}

function fmt(n, d = 1) { return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }); }

function renderTrussStats(h) {
  const beforeVol = parseFloat(h.get("X-Before-volume_cm3"));
  const afterVol = parseFloat(h.get("X-After-volume_cm3"));
  const beforeMass = parseFloat(h.get("X-Before-mass_g"));
  const afterMass = parseFloat(h.get("X-After-mass_g"));
  const savings = parseFloat(h.get("X-Savings-Pct"));
  const bx = parseFloat(h.get("X-Before-bbox_mm-x"));
  const by = parseFloat(h.get("X-Before-bbox_mm-y"));
  const bz = parseFloat(h.get("X-Before-bbox_mm-z"));
  trussStats.innerHTML = `
    <div><span class="muted">bbox  </span>${fmt(bx)} × ${fmt(by)} × ${fmt(bz)} mm</div>
    <div><span class="muted">before</span> ${fmt(beforeVol)} cm³ · ${fmt(beforeMass)} g</div>
    <div><span class="muted">after </span> ${fmt(afterVol)} cm³ · ${fmt(afterMass)} g</div>
    <div class="savings">saved ${fmt(savings, 1)}% · ${fmt(beforeMass - afterMass)} g</div>
  `;
}

async function doTrussPreview() {
  setStatus("Trussifying… (this may take a few seconds)");
  trussPreviewBtn.disabled = true;
  trussStats.innerHTML = '<span class="muted">Working…</span>';
  try {
    const t0 = performance.now();
    const params = trussParams();
    const res = await fetch("/api/trussify-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: codeEl.value, ...params }),
    });
    if (!res.ok) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = await res.text(); }
      throw new Error(detail);
    }
    const buf = await res.arrayBuffer();
    const geom = new STLLoader().parse(buf);
    replaceMesh(geom);
    renderTrussStats(res.headers);
    const ms = (performance.now() - t0).toFixed(0);
    setStatus(`Trussified in ${ms} ms — preview only (script unchanged).`, "ok");
  } catch (e) {
    setStatus(e.message, "error");
    trussStats.innerHTML = '<span class="muted">Error — see status below.</span>';
  } finally {
    trussPreviewBtn.disabled = false;
  }
}

function doTrussApply() {
  const p = trussParams();
  const axisArg = p.cut_axis === "Z" ? "" : `, cut_axis="${p.cut_axis}"`;
  const line = `\nresult = trussify(result, cell_size=${p.cell_size}, wall_thickness=${p.wall_thickness}, safety_margin=${p.safety_margin}${axisArg})\n`;
  const code = codeEl.value;
  if (code.includes("trussify(result")) {
    setStatus("Script already contains a trussify(result, …) call — edit it manually to change params.", "error");
    return;
  }
  codeEl.value = code.replace(/\s*$/, "") + line;
  setStatus("Appended trussify(...) call to script. Re-rendering...", "ok");
  doRender();
}

trussifyBtn.addEventListener("click", () => {
  trussPanel.classList.toggle("open");
});
trussCloseX.addEventListener("click", () => trussPanel.classList.remove("open"));
trussPreviewBtn.addEventListener("click", doTrussPreview);
trussApplyBtn.addEventListener("click", doTrussApply);

// ---- File input + drag/drop ----
async function loadFile(file) {
  if (!file) return;
  if (!/\.py$/i.test(file.name)) {
    setStatus("Only .py files are supported.", "error");
    return;
  }
  const text = await file.text();
  codeEl.value = text;
  fileChip.textContent = file.name;
  setStatus(`Loaded ${file.name} (${text.length} chars).`);
}

fileInput.addEventListener("change", (e) => loadFile(e.target.files[0]));

["dragenter", "dragover"].forEach((ev) =>
  leftPane.addEventListener(ev, (e) => {
    e.preventDefault();
    leftPane.classList.add("dragging");
  })
);
["dragleave", "drop"].forEach((ev) =>
  leftPane.addEventListener(ev, (e) => {
    if (ev === "dragleave" && e.target !== leftPane) return;
    leftPane.classList.remove("dragging");
  })
);
leftPane.addEventListener("drop", (e) => {
  e.preventDefault();
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) loadFile(f);
});

// ---- Keyboard ----
codeEl.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    doRender();
  } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "e") {
    e.preventDefault();
    doExport();
  } else if (e.key === "Tab") {
    e.preventDefault();
    const s = codeEl.selectionStart, ev = codeEl.selectionEnd;
    codeEl.value = codeEl.value.slice(0, s) + "    " + codeEl.value.slice(ev);
    codeEl.selectionStart = codeEl.selectionEnd = s + 4;
  }
});

// Auto-load example on first visit
if (!codeEl.value.trim()) {
  codeEl.value = EXAMPLE;
  fileChip.textContent = "example";
}
