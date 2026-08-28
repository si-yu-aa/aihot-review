const PAGE_LIMIT = 80;
const ROW_HEIGHT = 118;
const BUFFER_ROWS = 10;
const PRELOAD_ROWS = 28;
const CACHE_ROWS = 160;

const state = {
  run: null,
  nodes: [],
  items: new Map(),
  totalCount: 0,
  uniqueCount: 0,
  runsCount: 0,
  selectedIndex: 0,
  stats: {
    all: 0,
    viewed_keep: 0,
    unviewed: 0,
    unlabeled: 0,
    keep: 0,
    review: 0,
    drop: 0,
  },
  tagCounts: {},
  loadingOffsets: new Set(),
  filters: {
    q: "",
    decision: "all",
    tag: "all",
    sort: "score",
  },
  revision: 0,
};

const $ = (id) => document.getElementById(id);
const autosaveTimers = new Map();
const pendingSaves = new Map();
const viewTimers = new Map();
let searchTimer = null;
let scrollFrame = null;

const tagLabels = {
  all: "All",
  chips_compute: "芯片算力",
  robotics: "机器人",
  business: "商业量产",
  agent_infra: "Agent",
  policy: "政策认证",
  research: "论文开源",
};

function isEffectivelyViewed(signal) {
  return Boolean(signal.viewed || signal.reviewed);
}

function decisionOptions() {
  const stats = state.stats || {};
  return [
    ["all", `All ${stats.all || 0}`],
    ["viewed_keep", `Viewed Keep ${stats.viewed_keep || 0}`],
    ["unviewed", `Unviewed ${stats.unviewed || 0}`],
    ["unlabeled", `Unlabeled ${stats.unlabeled || 0}`],
    ["keep", `Keep ${stats.keep || 0}`],
    ["review", `Review ${stats.review || 0}`],
    ["drop", `Drop ${stats.drop || 0}`],
  ];
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function apiParams(offset) {
  const params = new URLSearchParams();
  params.set("offset", String(offset));
  params.set("limit", String(PAGE_LIMIT));
  params.set("decision", state.filters.decision);
  params.set("tag", state.filters.tag);
  params.set("sort", state.filters.sort);
  if (state.filters.q.trim()) params.set("q", state.filters.q.trim());
  return params;
}

function applyPayloadMeta(payload) {
  state.run = payload;
  state.totalCount = payload.total_count ?? payload.signal_count ?? 0;
  state.uniqueCount = payload.unique_count ?? state.totalCount;
  state.runsCount = payload.runs_count ?? 0;
  state.stats = payload.stats || state.stats;
  state.tagCounts = payload.tag_counts || {};
}

function renderRunMeta() {
  const loaded = state.items.size;
  const latest = state.run?.generated_at_bj || "";
  const mode = state.run?.pull_mode ? ` · last pull ${state.run.pull_mode}` : "";
  $("runMeta").textContent =
    `${state.uniqueCount} in inbox · ${loaded}/${state.totalCount} cached · ${state.runsCount} pulls${mode}` +
    (latest ? ` · latest ${latest}` : "");
}

async function loadWindow(offset) {
  const boundedOffset = Math.max(0, Math.floor(offset / PAGE_LIMIT) * PAGE_LIMIT);
  if (state.loadingOffsets.has(boundedOffset)) return;
  const revision = state.revision;
  state.loadingOffsets.add(boundedOffset);
  renderRunMeta();
  try {
    const payload = await requestJson(`/api/signals?${apiParams(boundedOffset).toString()}`);
    if (revision !== state.revision) return;
    applyPayloadMeta(payload);
    const start = payload.offset || boundedOffset;
    for (const [index, signal] of (payload.signals || []).entries()) {
      state.items.set(start + index, signal);
    }
    if (state.selectedIndex >= state.totalCount) {
      state.selectedIndex = Math.max(0, state.totalCount - 1);
    }
    renderFilters();
    renderList({ preserveScroll: Boolean($("virtualTop")) });
    renderDetail();
  } catch (err) {
    $("runMeta").textContent = err.message;
  } finally {
    state.loadingOffsets.delete(boundedOffset);
    renderRunMeta();
  }
}

function resetSignalCache() {
  state.revision += 1;
  state.items.clear();
  state.loadingOffsets.clear();
  state.totalCount = 0;
  state.selectedIndex = 0;
  const list = $("signalList");
  if (list) list.scrollTop = 0;
}

async function reloadInbox(options = {}) {
  const selectedId = options.preserveSelectedId ? selectedSignal()?.id : null;
  const scrollTop = options.preserveScroll ? $("signalList").scrollTop : 0;
  resetSignalCache();
  if (options.preserveScroll) $("signalList").scrollTop = scrollTop;
  $("runMeta").textContent = "Loading inbox...";
  await loadWindow(Math.max(0, Math.floor(scrollTop / ROW_HEIGHT)));
  if (selectedId) restoreSelectionById(selectedId);
  renderList();
  renderDetail();
}

function restoreSelectionById(selectedId) {
  for (const [index, signal] of state.items.entries()) {
    if (signal.id === selectedId) {
      state.selectedIndex = index;
      return;
    }
  }
}

function renderFilters() {
  const decisionFilters = $("decisionFilters");
  decisionFilters.innerHTML = "";
  for (const [value, label] of decisionOptions()) {
    const button = document.createElement("button");
    button.textContent = label;
    button.className = state.filters.decision === value ? "active" : "";
    button.addEventListener("click", () => {
      state.filters.decision = value;
      reloadInbox().catch((err) => alert(err.message));
    });
    decisionFilters.appendChild(button);
  }

  const tagFilters = $("tagFilters");
  tagFilters.innerHTML = "";
  for (const tag of ["all", ...Object.keys(state.tagCounts || {}).sort()]) {
    const count = tag === "all" ? state.stats.all || 0 : state.tagCounts[tag] || 0;
    const button = document.createElement("button");
    button.className = `chip ${state.filters.tag === tag ? "active" : ""}`;
    button.textContent = `${tagLabels[tag] || tag} ${count}`;
    button.addEventListener("click", () => {
      state.filters.tag = tag;
      reloadInbox().catch((err) => alert(err.message));
    });
    tagFilters.appendChild(button);
  }
}

function ensureVirtualList() {
  const list = $("signalList");
  if ($("virtualTop")) return;
  list.innerHTML = `
    <div id="virtualTop" class="virtual-spacer"></div>
    <div id="signalRows"></div>
    <div id="virtualBottom" class="virtual-spacer"></div>
    <div id="listStatus" class="list-status"></div>
  `;
}

function currentRange() {
  const list = $("signalList");
  const scrollTop = list.scrollTop || 0;
  const viewport = list.clientHeight || 600;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - BUFFER_ROWS);
  const visibleEnd = Math.min(
    state.totalCount,
    Math.ceil((scrollTop + viewport) / ROW_HEIGHT) + BUFFER_ROWS,
  );
  const renderEnd = Math.min(state.totalCount, visibleEnd + PRELOAD_ROWS);
  return { start, renderEnd, preloadEnd: Math.min(state.totalCount, renderEnd + PRELOAD_ROWS) };
}

function pruneCache(start, end) {
  const keepStart = Math.max(0, start - CACHE_ROWS);
  const keepEnd = Math.min(state.totalCount, end + CACHE_ROWS);
  for (const index of Array.from(state.items.keys())) {
    if (index === state.selectedIndex) continue;
    if (index < keepStart || index > keepEnd) state.items.delete(index);
  }
}

function ensureRangeLoaded(start, end) {
  for (let index = start; index < end; index += 1) {
    if (!state.items.has(index)) {
      loadWindow(index).catch((err) => {
        $("runMeta").textContent = err.message;
      });
      return;
    }
  }
}

function restoreListScroll(list, previousScrollTop) {
  const maxScrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
  const nextScrollTop = Math.min(previousScrollTop, maxScrollTop);
  if (Math.abs(list.scrollTop - nextScrollTop) > 1) {
    list.scrollTop = nextScrollTop;
  }
}

function renderList(options = {}) {
  ensureVirtualList();
  const list = $("signalList");
  const previousScrollTop = list.scrollTop || 0;
  $("visibleCount").textContent = state.totalCount;
  const top = $("virtualTop");
  const rows = $("signalRows");
  const bottom = $("virtualBottom");
  const status = $("listStatus");
  rows.innerHTML = "";

  if (!state.totalCount) {
    top.style.height = "0px";
    bottom.style.height = "0px";
    status.textContent = state.loadingOffsets.size ? "Loading..." : "No signals match the current filters.";
    if (options.preserveScroll) restoreListScroll(list, previousScrollTop);
    renderRunMeta();
    return;
  }

  const { start, renderEnd, preloadEnd } = currentRange();
  top.style.height = `${start * ROW_HEIGHT}px`;
  bottom.style.height = `${Math.max(0, (state.totalCount - renderEnd) * ROW_HEIGHT)}px`;
  status.textContent = state.loadingOffsets.size ? "Loading more..." : "";

  for (let index = start; index < renderEnd; index += 1) {
    const signal = state.items.get(index);
    rows.appendChild(signal ? createSignalRow(signal, index) : createPlaceholderRow(index));
  }

  pruneCache(start, renderEnd);
  if (options.preserveScroll) restoreListScroll(list, previousScrollTop);
  ensureRangeLoaded(start, preloadEnd);
  renderRunMeta();
}

function createPlaceholderRow(index) {
  const row = document.createElement("article");
  row.className = `signal-row placeholder ${index === state.selectedIndex ? "selected" : ""}`;
  row.innerHTML = `
    <div class="score">...</div>
    <div>
      <p class="row-title">Loading signal...</p>
      <div class="row-meta">Fetching this window from the inbox pool</div>
      <div class="row-tags"></div>
    </div>
    <div><span class="badge">load</span></div>
  `;
  return row;
}

function createSignalRow(signal, index) {
  const row = document.createElement("article");
  row.className = `signal-row ${index === state.selectedIndex ? "selected" : ""}`;
  row.innerHTML = `
    <div class="score">${signal.score}</div>
    <div>
      <p class="row-title">${escapeHtml(signal.title)}</p>
      <div class="row-meta">${escapeHtml(signal.time_bj || "")} · ${escapeHtml(signal.source || "")} · ${escapeHtml(signal.suggested_node || "")}</div>
      <div class="row-tags">${(signal.reasons || []).map(escapeHtml).join(" / ")}</div>
    </div>
    <div>
      ${decisionBadge(signal.decision)}
      ${isEffectivelyViewed(signal) ? '<span class="badge viewed">viewed</span>' : ""}
    </div>
  `;
  row.addEventListener("click", () => {
    state.selectedIndex = index;
    renderList();
    renderDetail({ markViewed: true });
  });
  return row;
}

function selectedSignal() {
  return state.items.get(state.selectedIndex) || null;
}

function decisionBadge(decision) {
  return `<span class="badge ${decision}">${decision}</span>`;
}

function renderDetail(options = {}) {
  const signal = selectedSignal();
  $("emptyState").classList.toggle("hidden", Boolean(signal));
  $("detailContent").classList.toggle("hidden", !signal);
  if (!signal) return;
  if (options.markViewed) scheduleMarkViewed(signal);

  $("detailMeta").textContent = `${signal.time_bj || ""} · score ${signal.score} · ${signal.category || "unknown"}`;
  $("detailTitle").textContent = signal.title;
  $("detailSummary").textContent = signal.summary || "No summary available. Open the source for details.";
  $("detailSource").href = signal.url;
  $("nodeInput").value = signal.suggested_node || "";
  $("noteInput").value = signal.user_note || "";
  $("saveStatus").textContent = signal.reviewed ? `Saved ${signal.reviewed_at || ""}` : "Autosave enabled";

  document.querySelectorAll("[data-decision]").forEach((button) => {
    button.classList.toggle("active", button.dataset.decision === signal.decision);
  });
  document.querySelectorAll("[data-strength]").forEach((button) => {
    button.classList.toggle("active", button.dataset.strength === signal.strength);
  });
}

function scheduleMarkViewed(signal) {
  if (!signal?.id || signal.viewed) return;
  if (viewTimers.has(signal.id)) clearTimeout(viewTimers.get(signal.id));
  const timer = setTimeout(() => {
    viewTimers.delete(signal.id);
    markViewed(signal).catch((err) => console.error(err));
  }, 800);
  viewTimers.set(signal.id, timer);
}

function adjustStatsForView(before, after) {
  if (!before || !after) return;
  if (!isEffectivelyViewed(before) && isEffectivelyViewed(after)) {
    state.stats.unviewed = Math.max(0, (state.stats.unviewed || 0) - 1);
  }
  const beforeViewedKeep = isEffectivelyViewed(before) && before.reviewed && before.decision === "keep";
  const afterViewedKeep = isEffectivelyViewed(after) && after.reviewed && after.decision === "keep";
  if (!beforeViewedKeep && afterViewedKeep) state.stats.viewed_keep = (state.stats.viewed_keep || 0) + 1;
  if (beforeViewedKeep && !afterViewedKeep) state.stats.viewed_keep = Math.max(0, (state.stats.viewed_keep || 0) - 1);
}

function adjustStatsForDecision(before, after) {
  if (!before || !after) return;
  if (before.decision !== after.decision) {
    if (["keep", "review", "drop"].includes(before.decision)) {
      state.stats[before.decision] = Math.max(0, (state.stats[before.decision] || 0) - 1);
    }
    if (["keep", "review", "drop"].includes(after.decision)) {
      state.stats[after.decision] = (state.stats[after.decision] || 0) + 1;
    }
  }
  if (!before.reviewed && after.reviewed) {
    state.stats.unlabeled = Math.max(0, (state.stats.unlabeled || 0) - 1);
  }
  adjustStatsForView(before, after);
}

async function markViewed(signal) {
  if (!signal || signal.viewed) return;
  const before = { ...signal };
  const saved = await requestJson("/api/view", {
    method: "POST",
    body: JSON.stringify({ id: signal.id }),
  });
  signal.viewed = true;
  signal.viewed_at = saved.viewed_at;
  adjustStatsForView(before, signal);
  if (state.filters.decision === "unviewed" || state.filters.decision === "viewed_keep") {
    await reloadInbox({ preserveScroll: true });
  } else {
    renderFilters();
    renderList();
    renderDetail();
  }
}

function updateSelected(fields, options = {}) {
  const signal = selectedSignal();
  if (!signal) return;
  Object.assign(signal, fields, { reviewed: false });
  renderList();
  if (options.renderDetail !== false) renderDetail();
  if (options.autosave !== false) {
    if (options.immediateSave) {
      saveSignal(signal, selectedSignal()?.id === signal.id, fields).catch((err) => {
        if (selectedSignal()?.id === signal.id) $("saveStatus").textContent = err.message;
      });
    } else {
      scheduleAutosave(signal, options.delay ?? 150);
    }
  }
}

function signalPayload(signal) {
  return {
    id: signal.id,
    decision: signal.decision,
    strength: signal.strength,
    suggested_node: signal.suggested_node || "ai",
    user_note: signal.user_note || "",
  };
}

function scheduleAutosave(signal, delay) {
  if (!signal?.id) return;
  if (autosaveTimers.has(signal.id)) clearTimeout(autosaveTimers.get(signal.id));
  const timer = setTimeout(() => {
    autosaveTimers.delete(signal.id);
    saveSignal(signal, selectedSignal()?.id === signal.id).catch((err) => {
      if (selectedSignal()?.id === signal.id) $("saveStatus").textContent = err.message;
    });
  }, delay);
  autosaveTimers.set(signal.id, timer);
  if (selectedSignal()?.id === signal.id) $("saveStatus").textContent = "Saving...";
}

async function saveSignal(signal, showStatus = true, changedFields = {}) {
  if (!signal) return;
  const before = { ...signal };
  const payload = signalPayload(signal);
  if (showStatus) $("saveStatus").textContent = "Saving...";
  const previous = pendingSaves.get(signal.id) || Promise.resolve();
  const savePromise = previous.catch(() => {}).then(async () => {
    const saved = await requestJson("/api/decision", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    Object.assign(signal, payload, {
      reviewed: true,
      reviewed_at: saved.reviewed_at,
      viewed: true,
      viewed_at: signal.viewed_at || saved.reviewed_at,
      viewed_inferred: true,
    });
    adjustStatsForDecision(before, signal);
    if (showStatus) $("saveStatus").textContent = `Saved ${saved.reviewed_at}`;
    return saved;
  });
  pendingSaves.set(signal.id, savePromise);
  try {
    await savePromise;
  } finally {
    if (pendingSaves.get(signal.id) === savePromise) pendingSaves.delete(signal.id);
  }
  if (changedFields.decision && state.filters.decision !== "all") {
    await reloadInbox({ preserveScroll: true, preserveSelectedId: true });
  } else {
    renderFilters();
    renderList();
    renderDetail();
  }
}

async function saveSelected() {
  const signal = selectedSignal();
  if (!signal) return;
  if (autosaveTimers.has(signal.id)) {
    clearTimeout(autosaveTimers.get(signal.id));
    autosaveTimers.delete(signal.id);
  }
  signal.suggested_node = $("nodeInput").value.trim() || "ai";
  signal.user_note = $("noteInput").value.trim();
  await saveSignal(signal, true);
  renderDetail();
}

function findCachedSignal(id) {
  for (const signal of state.items.values()) {
    if (signal.id === id) return signal;
  }
  return null;
}

async function flushPendingSaves() {
  for (const [id, timer] of autosaveTimers.entries()) {
    clearTimeout(timer);
    autosaveTimers.delete(id);
    const signal = findCachedSignal(id);
    if (signal) saveSignal(signal, selectedSignal()?.id === id).catch((err) => console.error(err));
  }
  await Promise.allSettled(Array.from(pendingSaves.values()));
}

async function loadSignals() {
  await reloadInbox();
}

async function pullLatest() {
  const hours = Number($("hoursInput").value || 24);
  const mode = $("pullMode").value || "since_last";
  $("pullButton").disabled = true;
  $("pullButton").textContent = "Pulling...";
  try {
    await requestJson("/api/pull", {
      method: "POST",
      body: JSON.stringify({ hours, mode }),
    });
    await reloadInbox();
  } finally {
    $("pullButton").disabled = false;
    $("pullButton").textContent = "Pull Latest";
  }
}

function scrollSelectedIntoView() {
  const list = $("signalList");
  const top = state.selectedIndex * ROW_HEIGHT;
  const bottom = top + ROW_HEIGHT;
  if (top < list.scrollTop) {
    list.scrollTop = top;
  } else if (bottom > list.scrollTop + list.clientHeight) {
    list.scrollTop = bottom - list.clientHeight;
  }
}

function moveSelection(delta) {
  if (!state.totalCount) return;
  state.selectedIndex = Math.max(0, Math.min(state.totalCount - 1, state.selectedIndex + delta));
  scrollSelectedIntoView();
  renderList();
  const signal = selectedSignal();
  if (signal) {
    renderDetail({ markViewed: true });
  } else {
    loadWindow(state.selectedIndex).then(() => renderDetail({ markViewed: true }));
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function bindEvents() {
  $("searchInput").addEventListener("input", (event) => {
    state.filters.q = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => reloadInbox().catch((err) => alert(err.message)), 180);
  });
  $("sortSelect").addEventListener("change", (event) => {
    state.filters.sort = event.target.value;
    reloadInbox().catch((err) => alert(err.message));
  });
  $("signalList").addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      renderList({ preserveScroll: true });
    });
  });
  $("pullButton").addEventListener("click", () => pullLatest().catch((err) => alert(err.message)));
  $("saveButton").addEventListener("click", () => saveSelected().catch((err) => alert(err.message)));
  $("nodeInput").addEventListener("input", (event) =>
    updateSelected({ suggested_node: event.target.value }, { renderDetail: false, delay: 650 }),
  );
  $("noteInput").addEventListener("input", (event) =>
    updateSelected({ user_note: event.target.value }, { renderDetail: false, delay: 900 }),
  );
  document.querySelectorAll("[data-decision]").forEach((button) => {
    button.addEventListener("click", () => updateSelected({ decision: button.dataset.decision }, { immediateSave: true }));
  });
  document.querySelectorAll("[data-strength]").forEach((button) => {
    button.addEventListener("click", () => updateSelected({ strength: button.dataset.strength }, { immediateSave: true }));
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
    if (event.key === "j") moveSelection(1);
    if (event.key === "k") moveSelection(-1);
    if (event.key === "1") updateSelected({ decision: "keep" }, { immediateSave: true });
    if (event.key === "2") updateSelected({ decision: "review" }, { immediateSave: true });
    if (event.key === "3") updateSelected({ decision: "drop" }, { immediateSave: true });
    if (event.key === "h") updateSelected({ strength: "high" }, { immediateSave: true });
    if (event.key === "m") updateSelected({ strength: "medium" }, { immediateSave: true });
    if (event.key === "l") updateSelected({ strength: "low" }, { immediateSave: true });
    if (event.key === "s") saveSelected().catch((err) => alert(err.message));
    if (event.key === "o") {
      const signal = selectedSignal();
      if (signal?.url) window.open(signal.url, "_blank", "noreferrer");
    }
  });
}

async function loadNodes() {
  const payload = await requestJson("/api/nodes");
  state.nodes = payload.nodes || [];
  const datalist = $("nodeList");
  datalist.innerHTML = "";
  for (const node of state.nodes) {
    const option = document.createElement("option");
    option.value = node.id;
    option.label = node.title;
    datalist.appendChild(option);
  }
}

bindEvents();
Promise.all([loadNodes(), loadSignals()]).catch((err) => {
  $("runMeta").textContent = err.message;
});
