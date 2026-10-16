// Slide viewer for the CSCI 2113 lab page.
// Slides are drawn with PDF.js (vendored in ./pdfjs), so they look the same in
// every browser: no built-in PDF toolbars or side panels, and it works on phones.

const root = document.documentElement;
root.setAttribute("data-viewer", "ready");

const $ = (id) => document.getElementById(id);
const player = $("player");
const stage = $("stage");
const sheet = $("sheet");
const progressBar = $("progress");
const counter = $("counter");
const prevBtn = $("prev");
const nextBtn = $("next");
const fullscreenBtn = $("fullscreen");
const kickerEl = $("viewer-kicker");
const titleEl = $("viewer-title");
const openBtn = $("viewer-open");
const downloadBtn = $("viewer-download");
const fallbackLink = $("stage-fallback");
const viewer = $("viewer");
const deckNav = $("decks");

const stacked = window.matchMedia("(max-width: 959px)");
const canHover = window.matchMedia("(hover: hover) and (pointer: fine)");
const coarse = window.matchMedia("(pointer: coarse)");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const decks = Array.from(document.querySelectorAll(".deck"), (el) => ({
  el,
  id: el.dataset.id,
  href: el.getAttribute("href"),
  kicker: el.dataset.kicker || "",
  title: el.querySelector(".deck-title").textContent.trim(),
}));

const state = { deck: null, doc: null, page: 1, pages: 0 };
const documents = new Map(); // href -> Promise<PDFDocumentProxy>
const lastPage = new Map(); // deck id -> page number
let pdfjs = null;
let libraryFailed = false;
let loadSeq = 0;
let renderSeq = 0;
let renderTask = null;
let renderedSize = "";
let overlay = false;

/* ---------- PDF.js ---------- */

function loadLibrary() {
  if (!pdfjs) {
    pdfjs = import("./pdfjs/pdf.min.js").then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = new URL("./pdfjs/pdf.worker.min.js", import.meta.url).href;
      return lib;
    });
  }
  return pdfjs;
}

async function openDocument(href) {
  let promise = documents.get(href);
  if (!promise) {
    let lib;
    try {
      lib = await loadLibrary();
    } catch (error) {
      libraryFailed = true;
      throw error;
    }
    promise = lib.getDocument({ url: href, isEvalSupported: false }).promise;
    documents.set(href, promise);
    promise.catch(() => documents.delete(href));
  }
  return promise;
}

/* ---------- Deck selection ---------- */

async function select(deck, { page, updateHash = false } = {}) {
  if (!deck) return;
  const seq = ++loadSeq;
  const target = page || lastPage.get(deck.id) || 1;

  if (state.deck !== deck) {
    state.deck = deck;
    state.doc = null;
    state.pages = 0;
    for (const d of decks) {
      if (d === deck) d.el.setAttribute("aria-current", "true");
      else d.el.removeAttribute("aria-current");
    }
    const file = decodeURIComponent(deck.href.split("/").pop());
    kickerEl.textContent = deck.kicker;
    titleEl.textContent = deck.title;
    openBtn.href = deck.href;
    downloadBtn.href = deck.href;
    downloadBtn.setAttribute("download", file);
    fallbackLink.href = deck.href;
    revealInList(deck.el);
    updateControls();
    setStatus("loading");
  }

  try {
    const doc = await openDocument(deck.href);
    if (seq !== loadSeq) return;
    state.doc = doc;
    state.pages = doc.numPages;
    await goTo(target, { updateHash });
  } catch (error) {
    if (seq !== loadSeq) return;
    console.error(error);
    if (libraryFailed) {
      // Very old browser: fall back to plain links that open each PDF.
      root.classList.replace("js", "no-js");
      return;
    }
    setStatus("error");
  }
}

async function goTo(number, { updateHash = true } = {}) {
  if (!state.doc) return;
  state.page = Math.min(Math.max(1, Math.round(number) || 1), state.pages);
  lastPage.set(state.deck.id, state.page);
  updateControls();
  if (updateHash) writeHash();
  await render();
}

function step(delta) {
  if (!state.doc) return;
  const target = state.page + delta;
  if (target < 1 || target > state.pages) return;
  goTo(target).catch(fail);
  wake();
}

function fail(error) {
  console.error(error);
  setStatus("error");
}

function setStatus(status) {
  player.classList.toggle("is-loading", status === "loading");
  player.classList.toggle("is-error", status === "error");
}

function updateControls() {
  const { page, pages } = state;
  counter.replaceChildren();
  if (pages) {
    const current = document.createElement("b");
    current.textContent = page;
    counter.append(current, ` / ${pages}`);
  }
  prevBtn.disabled = !pages || page <= 1;
  nextBtn.disabled = !pages || page >= pages;
  progressBar.style.width = pages ? `${(page / pages) * 100}%` : "0";
}

/* ---------- Rendering ---------- */

async function render() {
  const { doc, page: number, deck } = state;
  if (!doc) return;
  const seq = ++renderSeq;
  const page = await doc.getPage(number);
  if (seq !== renderSeq) return;

  const base = page.getViewport({ scale: 1 });
  stage.style.aspectRatio = `${base.width} / ${base.height}`;
  const box = stage.getBoundingClientRect();
  const fit = Math.min(box.width / base.width, box.height / base.height);
  if (!(fit > 0)) return;

  const ratio = Math.min(window.devicePixelRatio || 1, 3);
  const cssViewport = page.getViewport({ scale: fit });
  const pixelViewport = page.getViewport({ scale: fit * ratio });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(pixelViewport.width);
  canvas.height = Math.floor(pixelViewport.height);
  canvas.style.width = `${Math.floor(cssViewport.width)}px`;
  canvas.style.height = `${Math.floor(cssViewport.height)}px`;

  if (renderTask) renderTask.cancel();
  const task = page.render({ canvasContext: canvas.getContext("2d"), viewport: pixelViewport });
  renderTask = task;
  try {
    await task.promise;
  } catch (error) {
    if (error && error.name === "RenderingCancelledException") return;
    throw error;
  }
  if (seq !== renderSeq) return;
  renderTask = null;

  const links = await linkLayer(page, cssViewport);
  if (seq !== renderSeq) return;

  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `${deck.title}, slide ${number} of ${state.pages}`);
  sheet.style.width = canvas.style.width;
  sheet.style.height = canvas.style.height;
  sheet.replaceChildren(canvas, links);
  renderedSize = `${Math.round(box.width)}x${Math.round(box.height)}`;
  setStatus("ready");

  if (number < state.pages) doc.getPage(number + 1).catch(() => {});
}

// Clickable hyperlinks from the PDF (web links and in-deck links).
async function linkLayer(page, viewport) {
  const layer = document.createElement("div");
  layer.className = "links";
  let annotations = [];
  try {
    annotations = await page.getAnnotations({ intent: "display" });
  } catch {
    return layer;
  }

  for (const note of annotations) {
    if (note.subtype !== "Link" || !note.rect) continue;
    const link = document.createElement("a");
    const url = note.url || note.unsafeUrl;

    if (url && /^(https?:|mailto:)/i.test(url)) {
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = url;
    } else if (note.dest || note.action) {
      link.href = "#";
      link.setAttribute("aria-label", "Go to slide");
      link.addEventListener("click", async (event) => {
        event.preventDefault();
        const target = note.dest ? await resolveDestination(note.dest) : namedAction(note.action);
        if (target) goTo(target).catch(fail);
      });
    } else {
      continue;
    }

    const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(note.rect);
    Object.assign(link.style, {
      left: `${Math.min(x1, x2)}px`,
      top: `${Math.min(y1, y2)}px`,
      width: `${Math.abs(x2 - x1)}px`,
      height: `${Math.abs(y2 - y1)}px`,
    });
    link.addEventListener("click", (event) => event.stopPropagation());
    layer.append(link);
  }
  return layer;
}

async function resolveDestination(dest) {
  try {
    const explicit = typeof dest === "string" ? await state.doc.getDestination(dest) : dest;
    if (!Array.isArray(explicit)) return null;
    const ref = explicit[0];
    if (Number.isInteger(ref)) return ref + 1;
    if (ref && typeof ref === "object") return (await state.doc.getPageIndex(ref)) + 1;
  } catch (error) {
    console.warn(error);
  }
  return null;
}

function namedAction(action) {
  const { page, pages } = state;
  return { NextPage: page + 1, PrevPage: page - 1, FirstPage: 1, LastPage: pages }[action] || null;
}

let resizeTimer;
new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const box = stage.getBoundingClientRect();
    if (`${Math.round(box.width)}x${Math.round(box.height)}` !== renderedSize) render().catch(fail);
  }, 80);
}).observe(stage);

/* ---------- Full screen ---------- */

const nativeElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;
const isFullscreen = () => overlay || nativeElement() === player;

function toggleFullscreen() {
  if (overlay) return setOverlay(false);
  if (nativeElement()) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  const request = player.requestFullscreen || player.webkitRequestFullscreen;
  if (!request) return setOverlay(true);
  try {
    Promise.resolve(request.call(player)).catch(() => setOverlay(true));
  } catch {
    setOverlay(true);
  }
}

function setOverlay(on) {
  overlay = on;
  root.classList.toggle("has-overlay", on);
  syncFullscreen();
}

function syncFullscreen() {
  const on = isFullscreen();
  player.classList.toggle("is-fullscreen", on);
  const label = on ? "Exit full screen" : "Full screen";
  fullscreenBtn.setAttribute("aria-label", label);
  fullscreenBtn.title = `${label} (F)`;
  fullscreenBtn.querySelector(".ctrl-label").textContent = on ? "Exit" : "Full screen";
  // Drop focus left on a clicked button so Space/Enter flip slides instead of pressing it again.
  const focused = document.activeElement;
  if (on && focused && player.contains(focused) && !focused.matches(":focus-visible")) focused.blur();
  if (on && nativeElement() && coarse.matches && screen.orientation && screen.orientation.lock) {
    screen.orientation.lock("landscape").catch(() => {});
  }
  wake();
  render().catch(fail);
}

document.addEventListener("fullscreenchange", syncFullscreen);
document.addEventListener("webkitfullscreenchange", syncFullscreen);

// In full screen, fade the controls out after a moment of inactivity.
let idleTimer;
function wake() {
  player.classList.remove("is-idle");
  clearTimeout(idleTimer);
  if (isFullscreen()) {
    idleTimer = setTimeout(() => player.classList.add("is-idle"), canHover.matches ? 2500 : 3500);
  }
}
player.addEventListener("pointermove", (event) => { if (event.pointerType === "mouse") wake(); });

/* ---------- Input ---------- */

prevBtn.addEventListener("click", () => step(-1));
nextBtn.addEventListener("click", () => step(1));
fullscreenBtn.addEventListener("click", toggleFullscreen);

// In full screen: click/tap the left side to go back, the right side to go forward.
// On touch screens a tap in the middle shows or hides the controls.
stage.addEventListener("click", (event) => {
  if (!isFullscreen() || !state.doc) return;
  const box = stage.getBoundingClientRect();
  const x = (event.clientX - box.left) / box.width;
  if (!canHover.matches && x > 0.3 && x < 0.7) {
    if (player.classList.contains("is-idle")) wake();
    else { clearTimeout(idleTimer); player.classList.add("is-idle"); }
    return;
  }
  step(x < 0.3 ? -1 : 1);
});

let touch = null;
stage.addEventListener("touchstart", (event) => {
  touch = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
}, { passive: true });
stage.addEventListener("touchend", (event) => {
  if (!touch) return;
  const point = event.changedTouches[0];
  const dx = point.clientX - touch.x;
  const dy = point.clientY - touch.y;
  touch = null;
  if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
}, { passive: true });

document.addEventListener("keydown", (event) => {
  if (libraryFailed || event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return;
  if (event.target.closest && event.target.closest("input, textarea, select, [contenteditable]")) return;
  const full = isFullscreen();
  switch (event.key) {
    case "ArrowRight": step(1); break;
    case "ArrowLeft": step(-1); break;
    case "PageDown": case " ": if (!full) return; step(event.shiftKey ? -1 : 1); break;
    case "PageUp": if (!full) return; step(-1); break;
    case "Home": if (!full) return; goTo(1).catch(fail); break;
    case "End": if (!full) return; goTo(state.pages).catch(fail); break;
    case "f": case "F": toggleFullscreen(); break;
    case "Escape": if (!overlay) return; setOverlay(false); break;
    default: return;
  }
  event.preventDefault();
});

for (const deck of decks) {
  deck.el.addEventListener("click", (event) => {
    if (libraryFailed || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    select(deck, { updateHash: true });
    if (stacked.matches) {
      const top = viewer.getBoundingClientRect().top;
      if (top < 0 || top > window.innerHeight * 0.4) {
        viewer.scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
      }
    }
  });
}

// Keep the selected deck visible when the list scrolls on its own (long lists).
function revealInList(el) {
  if (stacked.matches || deckNav.scrollHeight <= deckNav.clientHeight + 1) return;
  const item = el.getBoundingClientRect();
  const box = deckNav.getBoundingClientRect();
  if (item.top < box.top) deckNav.scrollTop -= box.top - item.top + 12;
  else if (item.bottom > box.bottom) deckNav.scrollTop += item.bottom - box.bottom + 12;
}

/* ---------- Links like index.html#lab3 or #lab3/12 ---------- */

function readHash() {
  let hash = location.hash;
  try { hash = decodeURIComponent(hash); } catch { /* keep the raw hash */ }
  const match = /^#([\w-]+)(?:\/(\d+))?$/.exec(hash);
  const deck = match && decks.find((d) => d.id === match[1]);
  return deck ? { deck, page: match[2] ? Number(match[2]) : undefined } : null;
}

function writeHash() {
  const hash = `#${state.deck.id}${state.page > 1 ? `/${state.page}` : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

window.addEventListener("hashchange", () => {
  const target = readHash();
  if (target) select(target.deck, { page: target.page });
});

/* ---------- Start ---------- */

const initial = readHash();
select(
  initial ? initial.deck : decks.find((d) => d.el.hasAttribute("data-default")) || decks[decks.length - 1],
  { page: initial && initial.page },
);
