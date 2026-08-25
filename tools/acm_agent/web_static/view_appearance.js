import { $, $$, setBusy, toast } from "./core.js";

const APPEARANCE_DB_NAME = "acm-agent-ui";
const APPEARANCE_DB_VERSION = 1;
const APPEARANCE_STORE_NAME = "appearance";
const SETTINGS_KEY = "settings";
const BACKGROUND_KEY = "background";
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_OUTPUT_EDGE = 3840;
const MAX_SOURCE_PIXELS = 60_000_000;
const DEFAULT_SETTINGS = Object.freeze({ cropRatio: "16:9", panelOpacity: 72 });
const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const ALLOWED_OPACITIES = new Set([60, 64, 68, 72, 76, 80, 84, 88, 92]);
const RATIOS = Object.freeze({ "16:9": 16 / 9, "16:10": 16 / 10, "4:3": 4 / 3 });
const RATIO_PARTS = Object.freeze({ "16:9": [16, 9], "16:10": [8, 5], "4:3": [4, 3] });

const appearanceState = {
  settings: { ...DEFAULT_SETTINGS },
  backgroundUrl: "",
  crop: null,
  databasePromise: null,
  selectionEpoch: 0,
  settingsWrite: Promise.resolve(),
};

function openAppearanceDatabase() {
  if (appearanceState.databasePromise) return appearanceState.databasePromise;
  appearanceState.databasePromise = new Promise((resolve, reject) => {
    const request = window.indexedDB.open(APPEARANCE_DB_NAME, APPEARANCE_DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(APPEARANCE_STORE_NAME)) {
        database.createObjectStore(APPEARANCE_STORE_NAME, { keyPath: "key" });
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      database.onversionchange = () => database.close();
      resolve(database);
    };
    request.onerror = () => reject(request.error || new Error("无法打开浏览器外观存储"));
    request.onblocked = () => reject(new Error("外观存储正在被其他页面占用"));
  });
  appearanceState.databasePromise.catch(() => { appearanceState.databasePromise = null; });
  return appearanceState.databasePromise;
}

async function readAppearanceRecords() {
  const database = await openAppearanceDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(APPEARANCE_STORE_NAME, "readonly");
    const store = transaction.objectStore(APPEARANCE_STORE_NAME);
    const settingsRequest = store.get(SETTINGS_KEY);
    const backgroundRequest = store.get(BACKGROUND_KEY);
    transaction.oncomplete = () => resolve({
      settings: settingsRequest.result || null,
      background: backgroundRequest.result || null,
    });
    transaction.onerror = () => reject(transaction.error || new Error("无法读取外观设置"));
    transaction.onabort = () => reject(transaction.error || new Error("读取外观设置已中止"));
  });
}

async function commitAppearance(settings, { background = null, removeBackground = false } = {}) {
  const database = await openAppearanceDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(APPEARANCE_STORE_NAME, "readwrite");
    const store = transaction.objectStore(APPEARANCE_STORE_NAME);
    store.put({ key: SETTINGS_KEY, ...settings });
    if (background) store.put(background);
    if (removeBackground) store.delete(BACKGROUND_KEY);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("无法保存外观设置"));
    transaction.onabort = () => reject(transaction.error || new Error("保存外观设置已中止"));
  });
}

function normalizeSettings(value) {
  const cropRatio = Object.hasOwn(RATIOS, value?.cropRatio) ? value.cropRatio : DEFAULT_SETTINGS.cropRatio;
  const opacity = Number(value?.panelOpacity);
  const panelOpacity = ALLOWED_OPACITIES.has(opacity) ? opacity : DEFAULT_SETTINGS.panelOpacity;
  return { cropRatio, panelOpacity };
}

function syncRadioGroup(name, value) {
  $$(`input[name="${name}"]`).forEach(input => { input.checked = input.value === value; });
}

function applySettings(settings) {
  appearanceState.settings = normalizeSettings(settings);
  document.documentElement.dataset.panelOpacity = String(appearanceState.settings.panelOpacity);
  $("#appearance-opacity").value = String(appearanceState.settings.panelOpacity);
  $("#appearance-opacity-value").textContent = `${appearanceState.settings.panelOpacity}%`;
  syncRadioGroup("appearance_ratio", appearanceState.settings.cropRatio);
}

function clearBackgroundVisual() {
  const previousUrl = appearanceState.backgroundUrl;
  appearanceState.backgroundUrl = "";
  $("#app-background-stage").classList.remove("is-active");
  $("#app-background-fill").removeAttribute("src");
  $("#app-background").removeAttribute("src");
  $("#appearance-preview-image").removeAttribute("src");
  $("#appearance-preview-empty").classList.remove("hidden");
  $("#background-remove-button").disabled = true;
  document.documentElement.removeAttribute("data-has-background");
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

function applyBackgroundRecord(record) {
  if (
    !(record?.blob instanceof Blob)
    || record.blob.size <= 0
    || !ALLOWED_TYPES.has(record.blob.type)
    || Number(record.width) <= 0
    || Number(record.height) <= 0
  ) {
    clearBackgroundVisual();
    return;
  }
  const nextUrl = URL.createObjectURL(record.blob);
  const previousUrl = appearanceState.backgroundUrl;
  appearanceState.backgroundUrl = nextUrl;
  $("#app-background-fill").src = nextUrl;
  $("#app-background").src = nextUrl;
  $("#app-background-stage").classList.add("is-active");
  $("#appearance-preview-image").src = nextUrl;
  $("#appearance-preview-empty").classList.add("hidden");
  $("#background-remove-button").disabled = false;
  document.documentElement.dataset.hasBackground = "true";
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

async function loadAppearance() {
  applySettings(DEFAULT_SETTINGS);
  try {
    const records = await readAppearanceRecords();
    applySettings(records.settings || DEFAULT_SETTINGS);
    applyBackgroundRecord(records.background);
  } catch (error) {
    clearBackgroundVisual();
    toast("外观设置未恢复", error.message, "error");
  }
}

function decodeImage(blob) {
  return new Promise((resolve, reject) => {
    const sourceUrl = URL.createObjectURL(blob);
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve({ image, sourceUrl });
    image.onerror = () => {
      URL.revokeObjectURL(sourceUrl);
      reject(new Error("图片无法解码，请选择有效的 JPG、PNG 或 WebP 文件"));
    };
    image.src = sourceUrl;
  });
}

function releaseCropSource() {
  if (appearanceState.crop?.sourceUrl) URL.revokeObjectURL(appearanceState.crop.sourceUrl);
  appearanceState.crop = null;
  $("#background-crop-canvas").classList.remove("is-dragging");
}

function cropFrame(canvasWidth, canvasHeight, ratio) {
  const padding = 26;
  const availableWidth = Math.max(1, canvasWidth - padding * 2);
  const availableHeight = Math.max(1, canvasHeight - padding * 2);
  let width = availableWidth;
  let height = width / ratio;
  if (height > availableHeight) {
    height = availableHeight;
    width = height * ratio;
  }
  return {
    x: (canvasWidth - width) / 2,
    y: (canvasHeight - height) / 2,
    width,
    height,
  };
}

function sourceCropRect(crop) {
  const sourceWidth = crop.image.naturalWidth;
  const sourceHeight = crop.image.naturalHeight;
  const ratio = RATIOS[crop.ratio];
  let baseWidth = sourceWidth;
  let baseHeight = baseWidth / ratio;
  if (baseHeight > sourceHeight) {
    baseHeight = sourceHeight;
    baseWidth = baseHeight * ratio;
  }
  const width = baseWidth / crop.zoom;
  const height = baseHeight / crop.zoom;
  crop.centerX = Math.max(width / 2, Math.min(sourceWidth - width / 2, crop.centerX));
  crop.centerY = Math.max(height / 2, Math.min(sourceHeight - height / 2, crop.centerY));
  return {
    x: crop.centerX - width / 2,
    y: crop.centerY - height / 2,
    width,
    height,
  };
}

function cropGeometry() {
  const canvas = $("#background-crop-canvas");
  const crop = appearanceState.crop;
  if (!crop) return null;
  const bounds = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(bounds.width));
  const height = Math.max(1, Math.round(bounds.height));
  const ratio = RATIOS[crop.ratio];
  const frame = cropFrame(width, height, ratio);
  const source = sourceCropRect(crop);
  return { bounds, width, height, frame, source };
}

function drawCropCanvas() {
  const crop = appearanceState.crop;
  if (!crop) return;
  const canvas = $("#background-crop-canvas");
  const geometry = cropGeometry();
  if (!geometry) return;
  const pixelRatio = Math.max(1, window.devicePixelRatio || 1);
  const targetWidth = Math.max(1, Math.round(geometry.width * pixelRatio));
  const targetHeight = Math.max(1, Math.round(geometry.height * pixelRatio));
  if (canvas.width !== targetWidth) canvas.width = targetWidth;
  if (canvas.height !== targetHeight) canvas.height = targetHeight;
  const context = canvas.getContext("2d");
  if (!context) return;
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, geometry.width, geometry.height);
  context.fillStyle = "#101512";
  context.fillRect(0, 0, geometry.width, geometry.height);
  context.drawImage(
    crop.image,
    geometry.source.x,
    geometry.source.y,
    geometry.source.width,
    geometry.source.height,
    geometry.frame.x,
    geometry.frame.y,
    geometry.frame.width,
    geometry.frame.height,
  );
  context.strokeStyle = "rgba(255, 255, 255, .94)";
  context.lineWidth = 2;
  context.setLineDash([7, 6]);
  context.strokeRect(geometry.frame.x, geometry.frame.y, geometry.frame.width, geometry.frame.height);
  context.setLineDash([]);
}

function updateCropRatio(value) {
  const crop = appearanceState.crop;
  if (!crop || !Object.hasOwn(RATIOS, value)) return;
  const relativeX = crop.centerX / crop.image.naturalWidth;
  const relativeY = crop.centerY / crop.image.naturalHeight;
  crop.ratio = value;
  crop.centerX = relativeX * crop.image.naturalWidth;
  crop.centerY = relativeY * crop.image.naturalHeight;
  syncRadioGroup("crop_ratio", value);
  drawCropCanvas();
}

async function openCropDialog(file, epoch) {
  if (!ALLOWED_TYPES.has(file.type)) throw new Error("仅支持 JPG、PNG 或 WebP 图片");
  if (file.size > MAX_FILE_BYTES) throw new Error("图片不能超过 20 MiB");
  if (file.size <= 0) throw new Error("图片文件为空");
  const decoded = await decodeImage(file);
  if (epoch !== appearanceState.selectionEpoch) {
    URL.revokeObjectURL(decoded.sourceUrl);
    return;
  }
  if (decoded.image.naturalWidth * decoded.image.naturalHeight > MAX_SOURCE_PIXELS) {
    URL.revokeObjectURL(decoded.sourceUrl);
    throw new Error("图片解码尺寸过大，请选择不超过 6000 万像素的图片");
  }
  releaseCropSource();
  appearanceState.crop = {
    image: decoded.image,
    sourceUrl: decoded.sourceUrl,
    ratio: appearanceState.settings.cropRatio,
    zoom: 1,
    centerX: decoded.image.naturalWidth / 2,
    centerY: decoded.image.naturalHeight / 2,
    dragging: false,
    pointerId: null,
    lastX: 0,
    lastY: 0,
  };
  $("#background-crop-zoom").value = "100";
  $("#background-crop-zoom-value").textContent = "100%";
  syncRadioGroup("crop_ratio", appearanceState.crop.ratio);
  $("#background-crop-dialog").showModal();
  window.setTimeout(drawCropCanvas, 0);
}

function canvasToBlob(canvas, type, quality) {
  return new Promise(resolve => canvas.toBlob(resolve, type, quality));
}

async function encodeCrop() {
  const crop = appearanceState.crop;
  const geometry = cropGeometry();
  if (!crop || !geometry) throw new Error("没有可应用的裁剪图片");
  const parts = RATIO_PARTS[crop.ratio];
  const unit = Math.floor(Math.min(
    geometry.source.width / parts[0],
    geometry.source.height / parts[1],
    MAX_OUTPUT_EDGE / Math.max(parts[0], parts[1]),
  ));
  if (unit < 1) throw new Error("图片尺寸过小，无法按所选比例裁剪");
  const outputWidth = parts[0] * unit;
  const outputHeight = parts[1] * unit;
  const output = document.createElement("canvas");
  output.width = outputWidth;
  output.height = outputHeight;
  const context = output.getContext("2d");
  if (!context) throw new Error("浏览器无法创建裁剪画布");
  context.drawImage(
    crop.image,
    geometry.source.x,
    geometry.source.y,
    geometry.source.width,
    geometry.source.height,
    0,
    0,
    outputWidth,
    outputHeight,
  );
  let blob = await canvasToBlob(output, "image/webp", 0.96);
  if (!blob || blob.size <= 0 || blob.type !== "image/webp") blob = await canvasToBlob(output, "image/png");
  if (!blob || blob.size <= 0) throw new Error("浏览器无法编码裁剪后的图片");
  return {
    key: BACKGROUND_KEY,
    blob,
    width: outputWidth,
    height: outputHeight,
    mimeType: blob.type,
    updatedAt: new Date().toISOString(),
  };
}

function persistSettings() {
  const snapshot = { ...appearanceState.settings };
  appearanceState.settingsWrite = appearanceState.settingsWrite
    .catch(() => undefined)
    .then(() => commitAppearance(snapshot))
    .catch(error => toast("外观设置未保存", error.message, "error"));
  return appearanceState.settingsWrite;
}

function moveCrop(deltaX, deltaY) {
  const crop = appearanceState.crop;
  const geometry = cropGeometry();
  if (!crop || !geometry) return;
  crop.centerX -= deltaX / geometry.frame.width * geometry.source.width;
  crop.centerY -= deltaY / geometry.frame.height * geometry.source.height;
  drawCropCanvas();
}

function bindCropCanvasEvents() {
  const canvas = $("#background-crop-canvas");
  canvas.addEventListener("pointerdown", event => {
    const crop = appearanceState.crop;
    if (!crop) return;
    crop.dragging = true;
    crop.pointerId = event.pointerId;
    crop.lastX = event.clientX;
    crop.lastY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("is-dragging");
  });
  canvas.addEventListener("pointermove", event => {
    const crop = appearanceState.crop;
    if (!crop?.dragging || crop.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - crop.lastX;
    const deltaY = event.clientY - crop.lastY;
    crop.lastX = event.clientX;
    crop.lastY = event.clientY;
    moveCrop(deltaX, deltaY);
  });
  const endDrag = event => {
    const crop = appearanceState.crop;
    if (!crop || crop.pointerId !== event.pointerId) return;
    crop.dragging = false;
    crop.pointerId = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    canvas.classList.remove("is-dragging");
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("lostpointercapture", endDrag);
  canvas.addEventListener("keydown", event => {
    const movement = event.shiftKey ? 24 : 8;
    const deltas = {
      ArrowLeft: [-movement, 0],
      ArrowRight: [movement, 0],
      ArrowUp: [0, -movement],
      ArrowDown: [0, movement],
    };
    const delta = deltas[event.key];
    if (!delta) return;
    event.preventDefault();
    moveCrop(delta[0], delta[1]);
  });
}

function bindAppearanceEvents() {
  $("#background-select-button").addEventListener("click", () => $("#background-file-input").click());
  $("#background-file-input").addEventListener("change", async event => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    const epoch = ++appearanceState.selectionEpoch;
    try {
      await openCropDialog(file, epoch);
    } catch (error) {
      toast("图片无法使用", error.message, "error");
    }
  });
  $$("input[name=appearance_ratio]").forEach(input => input.addEventListener("change", event => {
    appearanceState.settings.cropRatio = event.currentTarget.value;
    persistSettings();
  }));
  $("#appearance-opacity").addEventListener("input", event => {
    const opacity = Number(event.currentTarget.value);
    appearanceState.settings.panelOpacity = opacity;
    document.documentElement.dataset.panelOpacity = String(opacity);
    $("#appearance-opacity-value").textContent = `${opacity}%`;
  });
  $("#appearance-opacity").addEventListener("change", persistSettings);
  $("#background-remove-button").addEventListener("click", async () => {
    if (!window.confirm("移除当前背景图片？")) return;
    try {
      await appearanceState.settingsWrite.catch(() => undefined);
      await commitAppearance(appearanceState.settings, { removeBackground: true });
      clearBackgroundVisual();
      toast("背景已移除", "面板已恢复为原有实色外观。");
    } catch (error) {
      toast("背景移除失败", error.message, "error");
    }
  });
  $("#appearance-reset-button").addEventListener("click", async () => {
    if (!window.confirm("恢复默认外观并移除当前背景？")) return;
    try {
      await appearanceState.settingsWrite.catch(() => undefined);
      await commitAppearance(DEFAULT_SETTINGS, { removeBackground: true });
      applySettings(DEFAULT_SETTINGS);
      clearBackgroundVisual();
      toast("外观已恢复默认", "裁剪比例为 16:9，面板不透明度为 72%。");
    } catch (error) {
      toast("默认外观恢复失败", error.message, "error");
    }
  });
  $$("input[name=crop_ratio]").forEach(input => input.addEventListener("change", event => updateCropRatio(event.currentTarget.value)));
  $("#background-crop-zoom").addEventListener("input", event => {
    if (!appearanceState.crop) return;
    const percent = Number(event.currentTarget.value);
    appearanceState.crop.zoom = percent / 100;
    $("#background-crop-zoom-value").textContent = `${percent}%`;
    drawCropCanvas();
  });
  $("#background-crop-apply").addEventListener("click", async event => {
    const button = event.currentTarget;
    const dialog = $("#background-crop-dialog");
    const cropEpoch = appearanceState.selectionEpoch;
    dialog.dataset.busy = "true";
    $$('[value="cancel"]', dialog).forEach(control => { control.disabled = true; });
    setBusy(button, true, "应用中…");
    try {
      const background = await encodeCrop();
      if (cropEpoch !== appearanceState.selectionEpoch || !dialog.open || !appearanceState.crop) return;
      const nextSettings = { ...appearanceState.settings, cropRatio: appearanceState.crop.ratio };
      await appearanceState.settingsWrite.catch(() => undefined);
      await commitAppearance(nextSettings, { background });
      applySettings(nextSettings);
      applyBackgroundRecord(background);
      dialog.close("applied");
      toast("背景已应用", "图片与外观设置仅保存在当前浏览器。");
    } catch (error) {
      toast("背景应用失败", error.message, "error");
    } finally {
      setBusy(button, false);
      delete dialog.dataset.busy;
      $$('[value="cancel"]', dialog).forEach(control => { control.disabled = false; });
    }
  });
  $("#background-crop-dialog").addEventListener("cancel", event => {
    if (event.currentTarget.dataset.busy === "true") event.preventDefault();
  });
  $("#background-crop-dialog").addEventListener("close", () => {
    appearanceState.selectionEpoch += 1;
    releaseCropSource();
  });
  window.addEventListener("resize", () => {
    if ($("#background-crop-dialog").open) drawCropCanvas();
  });
  window.addEventListener("pagehide", () => {
    releaseCropSource();
    if (appearanceState.backgroundUrl) URL.revokeObjectURL(appearanceState.backgroundUrl);
  });
  bindCropCanvasEvents();
}

export { bindAppearanceEvents, loadAppearance };
