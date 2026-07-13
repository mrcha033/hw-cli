import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { VRMLLoader } from "three/examples/jsm/loaders/VRMLLoader.js";

function decodePayload(value) {
  const bytes = Uint8Array.from(atob(value.trim()), (character) => character.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function fitCamera(camera, controls, object) {
  const box = new THREE.Box3().setFromObject(object);
  const center = box.getCenter(new THREE.Vector3());
  const size = Math.max(box.getSize(new THREE.Vector3()).length(), 1);
  camera.near = Math.max(size / 1000, 0.01);
  camera.far = size * 100;
  camera.position.copy(center).add(new THREE.Vector3(size * 0.9, -size * 0.9, size * 0.72));
  controls.target.copy(center);
  controls.update();
  return () => {
    camera.position.copy(center).add(new THREE.Vector3(size * 0.9, -size * 0.9, size * 0.72));
    controls.target.copy(center);
    controls.update();
  };
}

export function mount(containerId, payloadId) {
  const container = document.getElementById(containerId);
  const payload = document.getElementById(payloadId);
  if (!container || !payload || !window.WebGLRenderingContext) return false;

  let model;
  try {
    model = new VRMLLoader().parse(decodePayload(payload.textContent || ""));
  } catch (error) {
    container.dataset.viewerStatus = "parse-failed";
    return false;
  }

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111827);
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 10000);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);
  scene.add(model);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x172554, 2.4));
  const key = new THREE.DirectionalLight(0xffffff, 3.0);
  key.position.set(120, -140, 180);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x93c5fd, 1.0);
  fill.position.set(-100, 70, 60);
  scene.add(fill);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 1;
  controls.maxDistance = 10000;
  const reset = fitCamera(camera, controls, model);

  const render = () => {
    const width = Math.max(container.clientWidth, 320);
    const height = Math.max(container.clientHeight, 360);
    if (renderer.domElement.width !== width || renderer.domElement.height !== height) {
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  };
  renderer.domElement.addEventListener("dblclick", reset);
  const fallback = container.querySelector("img");
  if (fallback) fallback.hidden = true;
  container.dataset.viewerStatus = "ready";
  render();
  return true;
}
