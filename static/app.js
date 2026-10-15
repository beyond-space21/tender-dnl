const live = document.getElementById("live");
const pageSize = 40;
const cardPageSize = 20;
let offset = 0;
let total = 0;
let selected = null;
let busy = false;
let latestCounts = {};
let currentTab = "overview";
let syncingHash = false;
let tenderSearchTimer = null;

const STAGE = {
  open: "Open",
  closing_soon: "Closing soon",
  expired: "Expired",
  undated: "No date",
};
const STATS = [
  ["open", "Open", "tenders", "open"],
  ["closing_soon", "Closing soon", "tenders", "closing_soon"],
  ["expired", "Expired", "tenders", "expired"],
  ["downloaded", "Downloaded", "files", ""],
  ["extracted", "Fields extracted", "files", ""],
  ["failed", "Failed", "tenders", ""],
  ["total", "Total", "tenders", ""],
];

function setHash(path) {
  const next = `#${path}`;
  if (location.hash === next) return;
  syncingHash = true;
  location.hash = next;
  setTimeout(() => {
    syncingHash = false;
  }, 0);
}

function showTab(name, options = {}) {
  currentTab = name;
  document.querySelectorAll("nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    const active = panel.id === name;
    panel.classList.toggle("active", active);
  });
  if (name === "tenders") loadPage();
  if (name === "departments") {
    if (activeDepartment) showDepartmentDetail(activeDepartment);
    else loadDepartments();
  }
  if (name === "files") {
    const active = document.activeElement;
    const typing = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT");
    if (!typing) {
      const query = document.getElementById("query");
      requestAnimationFrame(() => query.focus());
    }
  }
  if (name === "usage") loadUsage();
  if (!options.skipHash) {
    if (name === "departments" && activeDepartment) {
      setHash(`departments/${encodeURIComponent(activeDepartment)}`);
    } else {
      setHash(name);
    }
  }
}

document.querySelectorAll("nav button").forEach((button) => {
  button.onclick = () => {
    if (button.dataset.tab === "departments") {
      activeDepartment = null;
      activeDepartmentMeta = null;
    }
    showTab(button.dataset.tab);
  };
});

document.getElementById("job-chip").onclick = () => showTab("overview");

function pill(className, label) {
  const span = document.createElement("span");
  span.className = `pill ${className || ""}`;
  span.textContent = label;
  return span;
}

function showDate(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function jumpFromStat(key, target, lifecycle) {
  const packet = document.getElementById("packet-status");
  const stage = document.getElementById("lifecycle");
  if (key === "downloaded" || key === "extracted" || key === "failed") {
    if (stage) stage.value = "";
    if (packet) packet.value = key;
    offset = 0;
    showTab("tenders");
    return;
  }
  if (target === "files") {
    showTab("files");
    return;
  }
  if (stage) stage.value = lifecycle || "";
  if (packet) packet.value = key === "total" ? "" : "downloaded";
  offset = 0;
  showTab("tenders");
}

function renderStats(counts) {
  latestCounts = counts || {};
  const box = document.getElementById("stats");
  box.replaceChildren();
  for (const [key, label, target, lifecycle] of STATS) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "stat";
    card.onclick = () => jumpFromStat(key, target, lifecycle);
    const name = document.createElement("span");
    name.textContent = label;
    const value = document.createElement("strong");
    value.textContent = String(latestCounts[key] || 0);
    card.append(name, value);
    box.appendChild(card);
  }
}

function renderJobChip(job) {
  const chip = document.getElementById("job-chip");
  const phase = job.phase || "idle";
  document.getElementById("job-chip-phase").textContent = phase;
  document.getElementById("job-chip-progress").textContent = job.pages
    ? `${job.page || 0} / ${job.pages}`
    : "";
  chip.classList.toggle("busy", ["listing", "downloading", "indexing", "extracting", "corr_refresh", "stopping"].includes(phase));
}

function renderJob(job, counts) {
  renderStats(counts);
  renderJobChip(job);
  document.getElementById("job-phase").textContent = job.phase || "idle";
  const progress = job.pages ? ` ${job.page} / ${job.pages}` : "";
  document.getElementById("job-detail").textContent = `${job.detail || ""}${progress}`;
  const bar = document.getElementById("job-progress");
  const ratio = job.pages ? Math.min(100, Math.round(((job.page || 0) / job.pages) * 100)) : 0;
  bar.style.width = `${ratio}%`;
  busy = ["listing", "downloading", "indexing", "extracting", "corr_refresh", "stopping"].includes(job.phase);
  bar.classList.toggle("busy", busy && job.pages > 0);
  for (const id of ["list", "download", "extract", "corr-refresh", "index"]) {
    document.getElementById(id).disabled = busy;
  }
}

function formatMetric(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "number") return value.toLocaleString("en-IN");
  return String(value);
}

function renderDependencies(deps) {
  const grid = document.getElementById("dep-grid");
  if (!grid) return;
  grid.replaceChildren();
  for (const dep of deps || []) {
    const card = document.createElement("article");
    card.className = `dep-card ${dep.kind || ""}`;
    const kindLabel = {
      ext: "external api",
      ai: "ai api",
      data: "data store",
      portal: "browser / portal",
    };
    const tag = document.createElement("span");
    tag.className = `tag ${dep.kind || ""}`;
    tag.textContent = dep.configured === false ? "not configured" : (kindLabel[dep.kind] || dep.kind || "dep");
    const title = document.createElement("h3");
    title.textContent = dep.name || dep.id || "Dependency";
    const role = document.createElement("p");
    role.className = "role";
    role.textContent = dep.role || "";
    const metrics = document.createElement("div");
    metrics.className = "dep-metrics";
    for (const metric of dep.metrics || []) {
      const cell = document.createElement("div");
      cell.className = "dep-metric";
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = metric.label || "";
      const value = document.createElement("span");
      value.className = "value";
      value.textContent = formatMetric(metric.value);
      cell.append(label, value);
      metrics.appendChild(cell);
    }
    card.append(tag, title, role, metrics);
    if (dep.note) {
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = dep.note;
      card.appendChild(note);
    }
    grid.appendChild(card);
  }
}

function renderCaptcha(days) {
  const box = document.getElementById("captcha");
  if (!box) return;
  box.replaceChildren();
  if (!days || !days.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.style.padding = "0.85rem 1rem";
    empty.textContent = "No 2Captcha images recorded yet.";
    box.appendChild(empty);
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const label of ["Day", "Images"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    head.appendChild(cell);
  }
  const thead = document.createElement("thead");
  thead.appendChild(head);
  const body = document.createElement("tbody");
  for (const item of days) {
    const row = document.createElement("tr");
    const day = document.createElement("td");
    day.textContent = item.used_on;
    const images = document.createElement("td");
    images.textContent = formatMetric(item.images);
    row.append(day, images);
    body.appendChild(row);
  }
  table.append(thead, body);
  box.appendChild(table);
}

function renderUsage(payload) {
  if (!payload) return;
  if (payload.dependencies) renderDependencies(payload.dependencies);
  if (payload.captcha) renderCaptcha(payload.captcha);
}

async function loadUsage() {
  try {
    const response = await fetch("/api/usage");
    if (!response.ok) throw new Error(`usage ${response.status}`);
    renderUsage(await response.json());
  } catch (err) {
    const grid = document.getElementById("dep-grid");
    if (grid && !grid.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Could not load dependency usage.";
      grid.appendChild(empty);
    }
  }
}

function fillTenderRows(rowsEl, tenders, onSelect, selectedId) {
  rowsEl.replaceChildren();
  for (const item of tenders) {
    const tr = document.createElement("tr");
    if (item.tender_id === selectedId) tr.className = "active";
    const identity = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = item.tender_id;
    identity.appendChild(strong);
    identity.appendChild(document.createElement("br"));
    identity.append(item.title || "");
    const published = document.createElement("td");
    published.textContent = showDate(item.published_at);
    const closing = document.createElement("td");
    closing.textContent = showDate(item.closing_at);
    const opening = document.createElement("td");
    opening.textContent = showDate(item.opening_at);
    const stage = document.createElement("td");
    stage.appendChild(pill(item.lifecycle, STAGE[item.lifecycle] || item.lifecycle || ""));
    const packet = document.createElement("td");
    packet.appendChild(pill(item.status, item.status));
    if (item.corrigendum_count > 0) {
      packet.appendChild(document.createElement("br"));
      packet.appendChild(pill("corrigendum", `${item.corrigendum_count} corrigendum`));
    }
    if (item.error) {
      packet.appendChild(document.createElement("br"));
      packet.append(item.error);
    }
    tr.append(identity, published, closing, opening, stage, packet);
    tr.onclick = () => onSelect(item);
    rowsEl.appendChild(tr);
  }
}

const FIELD_ROWS = [
  ["tender_reference", "Ref No"],
  ["organisation", "Organization"],
  ["pre_bid_meeting", "Pre-bid meeting"],
  ["name_of_work", "Name of work"],
  ["work_description", "Work description"],
  ["tender_fee_inr", "Tender fee (₹)"],
  ["emd_inr", "EMD (₹)"],
  ["work_value_inr", "Work value (₹)"],
  ["eligibility_percent", "Eligibility (%)"],
  ["eligibility", "Eligibility"],
  ["scope_of_work", "Scope of work"],
  ["covers", "Covers"],
  ["location", "Location"],
  ["inviting_authority", "Inviting authority"],
];

function renderDetails(container, details) {
  const fields = (details || {}).fields || {};
  const rows = FIELD_ROWS.filter(([key]) => fields[key] != null && fields[key] !== "");
  if (!rows.length) return;
  const box = document.createElement("div");
  box.className = "muted";
  const heading = document.createElement("strong");
  heading.textContent = "Extracted fields";
  box.appendChild(heading);
  const table = document.createElement("table");
  for (const [key, label] of rows) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = label;
    const value = document.createElement("td");
    let shown = fields[key];
    if (typeof shown === "number") shown = shown.toLocaleString("en-IN");
    value.textContent = String(shown);
    tr.append(name, value);
    table.appendChild(tr);
  }
  box.appendChild(table);
  container.appendChild(box);
}

async function fillPacket(paneId, item, reload) {
  const response = await fetch(`/api/tenders/${encodeURIComponent(item.tender_id)}/files`);
  const payload = await response.json();
  const files = document.getElementById(paneId);
  files.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = item.tender_id;
  const about = document.createElement("p");
  about.textContent = payload.title || "";
  files.append(title, about);
  renderDetails(files, payload.details);
  const packetItem = { ...item, details: payload.details || item.details, corrigenda: payload.corrigenda };
  renderCorrigendumChanges(files, packetItem);
  if (!payload.files.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No extracted files yet.";
    files.appendChild(empty);
    return;
  }
  for (const file of payload.files) {
    const link = document.createElement("a");
    link.className = "file";
    link.href = file.url;
    link.textContent = file.name;
    files.appendChild(link);
  }
  if (reload) reload();
}

function dash(value) {
  if (value == null || value === "") return null;
  return value;
}

function formatMoney(value) {
  if (value == null || value === "") return null;
  const n = Number(value);
  if (Number.isFinite(n)) return n.toLocaleString("en-IN");
  return String(value);
}

function formatDay(value) {
  if (!value) return null;
  return new Date(value).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fieldOf(item, key) {
  const fields = ((item.details || {}).fields) || {};
  if (fields[key] != null && fields[key] !== "") return fields[key];
  if (item[key] != null && item[key] !== "") return item[key];
  return null;
}

function encodeFilePath(tenderId, relative) {
  return `/files/${encodeURIComponent(tenderId)}/${relative.split("/").map(encodeURIComponent).join("/")}`;
}

function firstMappedFile(item, kind) {
  const files = ((item.details || {}).files) || {};
  const list = files[kind] || [];
  for (const entry of list) {
    if (entry && entry.file) return entry;
  }
  return list[0] || null;
}

const SOURCE_LABELS = {
  page: "Page",
  boq: "BoQ",
  llm: "LLM",
  corrigendum_history: "Corr history",
  corrigendum_pdf: "Corr PDF",
  "page+corrigendum": "Page + Corr",
  list: "Corr list",
  history: "Corr history",
  pdf: "Corr PDF",
};

function fieldSourceOf(item, key) {
  if (!key || !item) return null;
  const map = ((item.details || {}).field_sources) || {};
  return map[key] || null;
}

function sourceChip(sourceEntry) {
  if (!sourceEntry || !sourceEntry.source) return null;
  const span = document.createElement("span");
  const raw = String(sourceEntry.source);
  span.className = `src-chip src-${raw.replace(/\+/g, "-").replace(/_/g, "-")}`;
  span.textContent = SOURCE_LABELS[raw] || raw;
  if (sourceEntry.note) span.title = sourceEntry.note;
  return span;
}

function appendSourceLegend(parent, sourceEntry) {
  const chip = sourceChip(sourceEntry);
  if (!chip) return;
  const wrap = document.createElement("div");
  wrap.className = "src-legend";
  wrap.appendChild(chip);
  parent.appendChild(wrap);
}

function cellValue(parent, label, value, options = {}) {
  const cell = document.createElement("div");
  cell.className = "band-cell";
  const name = document.createElement("span");
  name.className = "band-label";
  name.textContent = label;
  const body = document.createElement("span");
  body.className = "band-value";
  const shown = dash(value);
  if (shown == null) {
    body.classList.add("empty");
    body.textContent = "—";
  } else if (options.onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "corr-open";
    btn.textContent = String(shown);
    btn.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      options.onClick();
    };
    body.appendChild(btn);
  } else if (options.link) {
    const a = document.createElement("a");
    a.className = "doc-link";
    a.href = options.link;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = String(shown);
    body.appendChild(a);
  } else if (options.clamp) {
    body.classList.add("clamp");
    body.textContent = String(shown);
    cell.append(name, body);
    if (String(shown).length > 160) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "show-more";
      more.textContent = "Show more";
      more.onclick = () => {
        const open = body.classList.toggle("open");
        more.textContent = open ? "Show less" : "Show more";
      };
      cell.append(more);
    }
    appendSourceLegend(cell, options.source);
    parent.appendChild(cell);
    return cell;
  } else {
    body.textContent = String(shown);
  }
  cell.append(name, body);
  appendSourceLegend(cell, options.source);
  parent.appendChild(cell);
  return cell;
}

function greenRow(parent, label, value, clamp, sourceEntry) {
  const row = document.createElement("div");
  row.className = "green-row";
  const name = document.createElement("div");
  name.className = "green-label";
  name.textContent = label;
  const body = document.createElement("div");
  body.className = "green-value";
  const shown = dash(value);
  if (shown == null) {
    body.className += " band-value empty";
    body.textContent = "—";
  } else if (clamp) {
    const text = document.createElement("div");
    text.className = "band-value clamp";
    text.textContent = String(shown);
    body.appendChild(text);
    if (String(shown).length > 160) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "show-more";
      more.textContent = "Show more";
      more.onclick = () => {
        const open = text.classList.toggle("open");
        more.textContent = open ? "Show less" : "Show more";
      };
      body.appendChild(more);
    }
  } else {
    body.className += " band-value";
    body.textContent = String(shown);
  }
  appendSourceLegend(body, sourceEntry);
  row.append(name, body);
  parent.appendChild(row);
}

const HISTORY_DATE_LABELS = {
  "Publish Date": "e-Published Date",
  "Bid Opening Date": "Opening Date",
  "Bid Submission End Date": "Closing Date",
  "Bid Submission Start Date": "Bid Submission Start",
  "Document Download/Sale Start Date": "Document Download Start",
  "Document Download/Sale End Date": "Document Download End",
  "Clarification Start Date": "Clarification Start",
  "Clarification End Date": "Clarification End",
};

function corrigendumView(item) {
  const stored = ((item.details || {}).corrigendum) || {};
  if (stored.present || (stored.changes || []).length) return stored;
  const fromFiles = ((item.details || {}).files || {}).corrigendum || [];
  const fromList = item.corrigenda || [];
  const phases = (fromFiles.length ? fromFiles : fromList).map((phase) => {
    const extra = fromList.find((row) => String(row.serial) === String(phase.serial)) || {};
    const original = Object.keys(phase.original_dates || {}).length
      ? phase.original_dates
      : extra.original_dates || {};
    const revised = Object.keys(phase.revised_dates || {}).length
      ? phase.revised_dates
      : extra.revised_dates || {};
    return { ...extra, ...phase, original_dates: original, revised_dates: revised };
  });
  const changes = [];
  for (const phase of phases) {
    const original = phase.original_dates || {};
    const revised = phase.revised_dates || {};
    let dated = false;
    for (const [key, label] of Object.entries(HISTORY_DATE_LABELS)) {
      const before = original[key];
      const after = revised[key];
      if (!after || after === before || String(after).toUpperCase() === "NA") continue;
      dated = true;
      changes.push({
        label,
        original: before && String(before).toUpperCase() !== "NA" ? before : "—",
        revised: after,
        title: phase.title,
        corr_type: phase.corr_type,
        serial: phase.serial,
        file: phase.file,
        source: "history",
        change_kind: "date",
        detail: `${label} revised in corrigendum history`,
      });
    }
    if (!dated && (phase.title || phase.corr_type)) {
      changes.push({
        label: phase.title || "Corrigendum",
        original: "Original requirement",
        revised: phase.corr_type || "Issued",
        title: phase.title,
        corr_type: phase.corr_type,
        serial: phase.serial,
        file: phase.file,
        source: "list",
        change_kind: "other",
        detail: "Corrigendum issued; no structured field diffs extracted yet.",
      });
    }
  }
  const present = phases.length > 0 || (item.corrigendum_count || 0) > 0;
  return {
    present,
    count: phases.length || item.corrigendum_count || 0,
    summary: stored.summary || null,
    changes,
    phases,
  };
}

function corrSourceLabel(source) {
  return SOURCE_LABELS[source] || source || "—";
}

function fillValueCell(cell, value, raw) {
  cell.textContent = value || "—";
  // Bilingual corrigenda: show the as-written Tamil under the English rendering.
  if (raw && String(raw) !== String(value || "")) {
    const rawLine = document.createElement("div");
    rawLine.className = "corr-raw";
    rawLine.textContent = raw;
    cell.appendChild(rawLine);
  }
}

function renderCorrigendumChanges(container, item, options = {}) {
  const view = corrigendumView(item);
  if (!view.present) return;
  const box = document.createElement("div");
  box.className = "corr-map";
  if (options.heading !== false) {
    const heading = document.createElement("strong");
    heading.textContent = "Corrigendum change map";
    box.appendChild(heading);
  }
  if (view.summary) {
    const summary = document.createElement("p");
    summary.className = "corr-map-summary";
    summary.textContent = view.summary;
    box.appendChild(summary);
  }
  const table = document.createElement("table");
  table.className = "corr-changes";
  const head = document.createElement("tr");
  for (const label of ["Field", "Original", "Now", "Source", "Corrigendum", "What changed"]) {
    const th = document.createElement("th");
    th.textContent = label;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const change of view.changes || []) {
    const tr = document.createElement("tr");
    const field = document.createElement("td");
    field.textContent = change.label || change.field || "Change";
    if (change.change_kind) {
      const kind = document.createElement("div");
      kind.className = "corr-kind";
      kind.textContent = change.change_kind;
      field.appendChild(kind);
    }
    const original = document.createElement("td");
    fillValueCell(original, change.original, change.original_raw);
    const revised = document.createElement("td");
    fillValueCell(revised, change.revised, change.revised_raw);
    const source = document.createElement("td");
    source.textContent = corrSourceLabel(change.source);
    const corr = document.createElement("td");
    const serial = change.serial ? `#${change.serial}` : "";
    const title = change.title || change.corr_type || "";
    corr.textContent = [serial, title].filter(Boolean).join(" · ") || "—";
    const detail = document.createElement("td");
    detail.className = "corr-detail";
    const detailParts = [];
    if (change.detail) detailParts.push(change.detail);
    if (change.quote) detailParts.push(`“${change.quote}”`);
    detail.textContent = detailParts.join(" — ") || "—";
    tr.append(field, original, revised, source, corr, detail);
    table.appendChild(tr);
  }
  if ((view.changes || []).length) box.appendChild(table);
  const phases = view.phases || [];
  for (const phase of phases) {
    if (!phase.file) continue;
    const link = document.createElement("a");
    link.className = "file";
    link.href = encodeFilePath(item.tender_id, phase.file);
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = phase.file;
    box.appendChild(link);
  }
  container.appendChild(box);
}

function openCorrigendumDialog(item) {
  const view = corrigendumView(item);
  const dialog = document.getElementById("corr-dialog");
  document.getElementById("corr-dialog-title").textContent =
    `${item.tender_id} · Corrigendum`;
  document.getElementById("corr-dialog-summary").textContent =
    view.summary || (view.count ? `${view.count} corrigendum issued` : "");
  const body = document.getElementById("corr-dialog-body");
  body.replaceChildren();
  renderCorrigendumChanges(body, item, { heading: false });
  if (typeof dialog.showModal === "function") dialog.showModal();
}

function renderDocCell(grid, label, item, kind) {
  const entry = firstMappedFile(item, kind);
  if (!entry) {
    cellValue(grid, label, null);
    return;
  }
  const labelText = entry.name || entry.file || null;
  const link = entry.file ? encodeFilePath(item.tender_id, entry.file) : null;
  cellValue(grid, label, labelText, { link });
}

function renderTenderCard(item, serial) {
  const card = document.createElement("article");
  card.className = "tender-card";
  card.dataset.tenderId = item.tender_id;

  const key = document.createElement("div");
  key.className = "card-source-key";
  key.innerHTML =
    "<span>Sources:</span> " +
    ["Page", "BoQ", "LLM", "Corr history", "Corr PDF", "Page + Corr"]
      .map((label) => `<span class="src-chip">${label}</span>`)
      .join(" ");
  card.appendChild(key);

  const yellow = document.createElement("div");
  yellow.className = "band band-yellow";
  const yGrid = document.createElement("div");
  yGrid.className = "band-grid";
  const ref = fieldOf(item, "tender_reference");
  const idLine = ref ? `${ref} / ${item.tender_id}` : item.tender_id;
  cellValue(yGrid, "S.No", serial);
  cellValue(yGrid, "e-Published Date", formatDay(fieldOf(item, "published_at")), {
    source: fieldSourceOf(item, "published_at"),
  });
  cellValue(yGrid, "Closing Date", formatDay(fieldOf(item, "closing_at")), {
    source: fieldSourceOf(item, "closing_at"),
  });
  cellValue(yGrid, "Opening Date", formatDay(fieldOf(item, "opening_at")), {
    source: fieldSourceOf(item, "opening_at"),
  });
  cellValue(yGrid, "Pre Bid Meeting", fieldOf(item, "pre_bid_meeting"), {
    source: fieldSourceOf(item, "pre_bid_meeting"),
  });
  cellValue(yGrid, "Title & Ref No. / Tender ID", idLine, {
    source: fieldSourceOf(item, "tender_reference") || { source: "page" },
  });
  cellValue(yGrid, "Organization", fieldOf(item, "organisation"), {
    source: fieldSourceOf(item, "organisation"),
  });
  yellow.appendChild(yGrid);
  card.appendChild(yellow);

  if (item.status !== "downloaded") {
    const note = document.createElement("div");
    note.className = "not-downloaded";
    note.textContent = item.status === "failed"
      ? `Packet failed${item.error ? `: ${item.error}` : ""}`
      : "Not downloaded — fee, EMD, documents fill in after download.";
    card.appendChild(note);
  }

  const green = document.createElement("div");
  green.className = "band band-green";
  greenRow(green, "Title", fieldOf(item, "title") || item.title, false, fieldSourceOf(item, "title"));
  greenRow(green, "Name of the Work", fieldOf(item, "name_of_work"), true, fieldSourceOf(item, "name_of_work"));
  greenRow(green, "Work Description", fieldOf(item, "work_description"), true, fieldSourceOf(item, "work_description"));
  card.appendChild(green);

  const blue = document.createElement("div");
  blue.className = "band band-blue";
  const bGrid = document.createElement("div");
  bGrid.className = "band-grid";
  cellValue(bGrid, "Tender Fee", formatMoney(fieldOf(item, "tender_fee_inr")), {
    source: fieldSourceOf(item, "tender_fee_inr"),
  });
  cellValue(bGrid, "EMD", formatMoney(fieldOf(item, "emd_inr")), {
    source: fieldSourceOf(item, "emd_inr"),
  });
  cellValue(bGrid, "Work Value", formatMoney(fieldOf(item, "work_value_inr")), {
    source: fieldSourceOf(item, "work_value_inr"),
  });
  cellValue(bGrid, "Eligibility (%)", fieldOf(item, "eligibility_percent"), {
    source: fieldSourceOf(item, "eligibility_percent"),
  });
  cellValue(bGrid, "Scope of the Work", fieldOf(item, "scope_of_work"), {
    clamp: true,
    source: fieldSourceOf(item, "scope_of_work"),
  });
  cellValue(bGrid, "Single / Two Cover", fieldOf(item, "covers"), {
    source: fieldSourceOf(item, "covers"),
  });
  blue.appendChild(bGrid);
  card.appendChild(blue);

  const orange = document.createElement("div");
  orange.className = "band band-orange";
  const oGrid = document.createElement("div");
  oGrid.className = "band-grid";
  renderDocCell(oGrid, "Tender Notice", item, "tender_notice");
  renderDocCell(oGrid, "Tender Document", item, "tender_document");
  renderDocCell(oGrid, "BoQ", item, "boq");
  const corr = corrigendumView(item);
  if (corr.present) {
    cellValue(oGrid, "Corrigendum", "Yes — view changes", {
      onClick: () => openCorrigendumDialog(item),
      source: { source: "corrigendum_history" },
    });
  } else {
    cellValue(oGrid, "Corrigendum", null);
  }
  orange.appendChild(oGrid);
  card.appendChild(orange);

  return card;
}

function closingRangeFromPreset() {
  const preset = document.getElementById("closing-preset").value;
  const fromEl = document.getElementById("closing_from");
  const toEl = document.getElementById("closing_to");
  if (preset === "custom") {
    return { from: fromEl.value || "", to: toEl.value || "" };
  }
  if (preset === "7" || preset === "30") {
    const start = new Date();
    const end = new Date();
    end.setDate(end.getDate() + Number(preset));
    const iso = (d) => d.toISOString().slice(0, 10);
    return { from: iso(start), to: iso(end) };
  }
  return { from: "", to: "" };
}

function syncClosingCustomVisibility() {
  const custom = document.getElementById("closing-preset").value === "custom";
  document.getElementById("closing-from-wrap").hidden = !custom;
  document.getElementById("closing-to-wrap").hidden = !custom;
}

function filterSummary(totalCount) {
  const parts = [`${totalCount} tender${totalCount === 1 ? "" : "s"}`];
  const stage = document.getElementById("lifecycle").value;
  const packet = document.getElementById("packet-status").value;
  const preset = document.getElementById("closing-preset").value;
  const q = (document.getElementById("tender-q").value || "").trim();
  if (packet) parts.push(`Packet: ${packet}`);
  if (stage) parts.push(`Stage: ${STAGE[stage] || stage}`);
  if (preset === "7") parts.push("Closing: next 7 days");
  else if (preset === "30") parts.push("Closing: next 30 days");
  else if (preset === "custom") parts.push("Closing: custom");
  if (q) parts.push(`Search: “${q}”`);
  return parts.join(" · ");
}

function clearTenderFilters() {
  document.getElementById("tender-q").value = "";
  document.getElementById("lifecycle").value = "";
  document.getElementById("packet-status").value = "downloaded";
  document.getElementById("closing-preset").value = "";
  document.getElementById("closing_from").value = "";
  document.getElementById("closing_to").value = "";
  syncClosingCustomVisibility();
  offset = 0;
  loadPage();
}

async function loadPage() {
  syncClosingCustomVisibility();
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(cardPageSize),
  });
  const lifecycle = document.getElementById("lifecycle").value;
  if (lifecycle) params.set("lifecycle", lifecycle);
  const status = document.getElementById("packet-status").value;
  if (status) params.set("status", status);
  const q = (document.getElementById("tender-q").value || "").trim();
  if (q) params.set("q", q);
  const range = closingRangeFromPreset();
  if (range.from) params.set("closing_from", range.from);
  if (range.to) params.set("closing_to", range.to);

  const response = await fetch(`/api/tenders?${params.toString()}`);
  const payload = await response.json();
  total = payload.total;
  const box = document.getElementById("tender-cards");
  box.replaceChildren();
  document.getElementById("tender-meta").textContent = filterSummary(total);

  if (!payload.tenders.length) {
    const empty = document.createElement("div");
    empty.className = "tender-empty";
    empty.append("No tenders match these filters.");
    empty.appendChild(document.createElement("br"));
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "secondary";
    clear.textContent = "Clear filters";
    clear.onclick = clearTenderFilters;
    empty.appendChild(clear);
    box.appendChild(empty);
  } else {
    payload.tenders.forEach((item, index) => {
      box.appendChild(renderTenderCard(item, offset + index + 1));
    });
  }

  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + cardPageSize, total);
  document.getElementById("pageinfo").textContent = `${from}–${to} of ${total}`;
}

let departmentCatalog = [];
let activeDepartment = null;
let activeDepartmentMeta = null;
let deptOffset = 0;
let deptTotal = 0;
let deptSelected = null;

async function loadDepartments(options = {}) {
  document.getElementById("dept-list").hidden = false;
  document.getElementById("dept-detail").hidden = true;
  activeDepartment = null;
  activeDepartmentMeta = null;
  const response = await fetch("/api/departments");
  const payload = await response.json();
  departmentCatalog = payload.departments || [];
  renderDepartmentGrid();
  if (!options.skipHash && currentTab === "departments") setHash("departments");
}

function renderDepartmentGrid() {
  const query = (document.getElementById("dept-filter").value || "").trim().toLowerCase();
  const grid = document.getElementById("dept-grid");
  grid.replaceChildren();
  const rows = departmentCatalog.filter((item) => !query || item.department.toLowerCase().includes(query));
  document.getElementById("dept-meta").textContent = `${rows.length} departments`;
  for (const item of rows) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "dept-card";
    const title = document.createElement("h3");
    title.textContent = item.department;
    const counts = document.createElement("div");
    counts.className = "counts";
    for (const [key, label] of [
      ["total", "Total"],
      ["open", "Open"],
      ["closing_soon", "Soon"],
      ["expired", "Expired"],
      ["downloaded", "Downloaded"],
    ]) {
      counts.appendChild(pill(key === "total" ? "" : key, `${label} ${item[key] || 0}`));
    }
    card.append(title, counts);
    card.onclick = () => openDepartment(item);
    grid.appendChild(card);
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No departments match that filter.";
    grid.appendChild(empty);
  }
}

function showDepartmentDetail(name) {
  document.getElementById("dept-list").hidden = true;
  document.getElementById("dept-detail").hidden = false;
  document.getElementById("dept-title").textContent = name;
  if (activeDepartmentMeta) {
    const meta = activeDepartmentMeta;
    document.getElementById("dept-summary").textContent =
      `${meta.total || 0} tenders · ${meta.open || 0} open · ${meta.closing_soon || 0} closing soon · ${meta.expired || 0} expired`;
  }
  loadDepartmentTenders();
  if (currentTab === "departments") setHash(`departments/${encodeURIComponent(name)}`);
}

async function openDepartment(item) {
  activeDepartment = item.department;
  activeDepartmentMeta = item;
  deptOffset = 0;
  deptSelected = null;
  document.getElementById("dept-packet").innerHTML =
    '<h2>Packet</h2><p class="muted">Select a tender to see extracted files.</p>';
  showDepartmentDetail(item.department);
}

async function loadDepartmentTenders() {
  if (!activeDepartment) return;
  const params = new URLSearchParams({
    offset: String(deptOffset),
    department: activeDepartment,
  });
  const lifecycle = document.getElementById("dept-lifecycle").value;
  if (lifecycle) params.set("lifecycle", lifecycle);
  const response = await fetch(`/api/tenders?${params.toString()}`);
  const payload = await response.json();
  deptTotal = payload.total;
  fillTenderRows(document.getElementById("dept-rows"), payload.tenders, openDepartmentFiles, deptSelected);
  const from = deptTotal ? deptOffset + 1 : 0;
  const to = Math.min(deptOffset + pageSize, deptTotal);
  document.getElementById("dept-pageinfo").textContent = `${from}–${to} of ${deptTotal}`;
  document.getElementById("dept-summary").textContent =
    `${deptTotal} tenders in this department` +
    (lifecycle ? ` · filtered to ${STAGE[lifecycle] || lifecycle}` : "");
}

async function openDepartmentFiles(item) {
  deptSelected = item.tender_id;
  await fillPacket("dept-packet", item, loadDepartmentTenders);
}

document.getElementById("dept-filter").addEventListener("input", renderDepartmentGrid);
document.getElementById("dept-back").onclick = () => loadDepartments();
document.getElementById("dept-lifecycle").onchange = () => {
  deptOffset = 0;
  loadDepartmentTenders();
};
document.getElementById("dept-prev").onclick = () => {
  deptOffset = Math.max(0, deptOffset - pageSize);
  loadDepartmentTenders();
};
document.getElementById("dept-next").onclick = () => {
  if (deptOffset + pageSize < deptTotal) {
    deptOffset += pageSize;
    loadDepartmentTenders();
  }
};

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  const payload = await response.json();
  if (!response.ok) alert(payload.detail || "Request failed");
}

document.getElementById("list").onclick = () => post("/api/job/list");
document.getElementById("download").onclick = () => post("/api/job/download");
document.getElementById("extract").onclick = () => post("/api/job/extract");
document.getElementById("corr-refresh").onclick = () => post("/api/job/corr_refresh");
document.getElementById("export").onclick = () => { window.location.href = "/api/export.xlsx"; };
document.getElementById("index").onclick = () => post("/api/job/index");
document.getElementById("stop").onclick = () => post("/api/job/stop");
document.getElementById("prev").onclick = () => {
  offset = Math.max(0, offset - cardPageSize);
  loadPage();
};
document.getElementById("next").onclick = () => {
  if (offset + cardPageSize < total) {
    offset += cardPageSize;
    loadPage();
  }
};
function reloadTendersFromStart() {
  offset = 0;
  loadPage();
}
document.getElementById("lifecycle").onchange = reloadTendersFromStart;
document.getElementById("packet-status").onchange = reloadTendersFromStart;
document.getElementById("closing-preset").onchange = () => {
  syncClosingCustomVisibility();
  reloadTendersFromStart();
};
document.getElementById("closing_from").onchange = reloadTendersFromStart;
document.getElementById("closing_to").onchange = reloadTendersFromStart;
document.getElementById("tender-clear").onclick = clearTenderFilters;
document.getElementById("tender-q").addEventListener("input", () => {
  clearTimeout(tenderSearchTimer);
  tenderSearchTimer = setTimeout(reloadTendersFromStart, 250);
});

function groupTitle(group) {
  const heading = document.createElement("h3");
  heading.textContent = group.tender_id;
  heading.appendChild(document.createTextNode(" "));
  heading.appendChild(pill(group.lifecycle, STAGE[group.lifecycle] || group.lifecycle));
  return heading;
}

const SEARCH_PAGE = 40;
let searchPage = 1;
let searchFound = 0;
let searchLifecycle = "";
let searchExtensions = new Set();
let searchTimer = null;
let selectedHit = null;
const PREVIEW_EXT = new Set(["txt", "csv", "html", "htm", "md", "xml"]);

function setChipActive(row, attribute, value) {
  row.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("active", (chip.dataset[attribute] || "") === value);
  });
}

function renderBars(targetId, rows, labelFn, onSelect) {
  const box = document.getElementById(targetId);
  box.replaceChildren();
  const max = Math.max(1, ...rows.map((row) => row.count || 0));
  for (const row of rows.slice(0, 8)) {
    const item = document.createElement(onSelect ? "button" : "div");
    if (onSelect) {
      item.type = "button";
      item.onclick = () => onSelect(row.value);
    }
    item.className = "bar-row";
    const left = document.createElement("div");
    const label = document.createElement("div");
    label.className = "bar-label";
    label.textContent = labelFn(row.value);
    label.title = row.value;
    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("div");
    fill.className = "bar-fill";
    track.appendChild(fill);
    left.append(label, track);
    const count = document.createElement("div");
    count.className = "bar-count";
    count.textContent = String(row.count || 0);
    item.append(left, count);
    box.appendChild(item);
    requestAnimationFrame(() => {
      fill.style.width = `${Math.round(((row.count || 0) / max) * 100)}%`;
    });
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No data";
    box.appendChild(empty);
  }
}

function departmentFromOrganisation(value) {
  if (!value) return "Unknown";
  return value.split("||")[0].trim() || "Unknown";
}

function openDepartmentByName(name) {
  const match = departmentCatalog.find((item) => item.department === name);
  currentTab = "departments";
  document.querySelectorAll("nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === "departments");
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === "departments");
  });
  if (match) openDepartment(match);
  else {
    openDepartment({
      department: name,
      total: 0,
      open: 0,
      closing_soon: 0,
      expired: 0,
      downloaded: 0,
    });
  }
}

function renderCharts(facets) {
  const charts = document.getElementById("search-charts");
  const hasData =
    (facets.lifecycle && facets.lifecycle.length) ||
    (facets.extension && facets.extension.length) ||
    (facets.organisation && facets.organisation.length);
  charts.hidden = !hasData;
  if (!hasData) return;
  renderBars("chart-lifecycle", facets.lifecycle || [], (value) => STAGE[value] || value);
  renderBars("chart-extension", facets.extension || [], (value) => (value || "unknown").toUpperCase());
  renderBars(
    "chart-organisation",
    facets.organisation || [],
    (value) => value || "Unknown",
    (value) => openDepartmentByName(departmentFromOrganisation(value))
  );
}

function renderExtensionChips(rows) {
  const box = document.getElementById("extension-chips");
  box.replaceChildren();
  if (!rows.length && !searchExtensions.size) return;
  const all = document.createElement("button");
  all.type = "button";
  all.className = `chip${searchExtensions.size ? "" : " active"}`;
  all.textContent = "All types";
  all.onclick = () => {
    searchExtensions.clear();
    searchPage = 1;
    runSearch();
  };
  box.appendChild(all);
  const seen = new Set(rows.map((row) => row.value));
  for (const value of searchExtensions) seen.add(value);
  for (const value of [...seen].filter(Boolean).sort()) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${searchExtensions.has(value) ? " active" : ""}`;
    chip.textContent = value.toUpperCase();
    chip.onclick = () => {
      if (searchExtensions.has(value)) searchExtensions.delete(value);
      else searchExtensions.add(value);
      searchPage = 1;
      runSearch();
    };
    box.appendChild(chip);
  }
}

function safeSnippet(html) {
  const template = document.createElement("template");
  template.innerHTML = html || "";
  const walk = (node) => {
    [...node.childNodes].forEach((child) => {
      if (child.nodeType !== Node.ELEMENT_NODE) return;
      if (child.tagName.toLowerCase() === "mark") {
        [...child.attributes].forEach((attr) => child.removeAttribute(attr.name));
        walk(child);
        return;
      }
      child.replaceWith(...child.childNodes);
    });
  };
  walk(template.content);
  return template.innerHTML;
}

function shortOrg(value) {
  if (!value) return "";
  return value.length > 80 ? `${value.slice(0, 77)}…` : value;
}

async function showDetail(group, file) {
  selectedHit = `${group.tender_id}:${file.relative_path}`;
  document.querySelectorAll(".hit-card").forEach((card) => {
    card.classList.toggle("active", card.dataset.key === selectedHit);
  });
  const pane = document.getElementById("detail-pane");
  pane.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = group.tender_id;
  const about = document.createElement("p");
  about.textContent = group.title || "";
  const list = document.createElement("dl");
  const rows = [
    ["Matched file", file.filename || file.relative_path || ""],
    ["Organisation", group.organisation || ""],
    ["Stage", STAGE[group.lifecycle] || group.lifecycle || ""],
    ["Closing", showDate(group.closing_at)],
    ["Matched in", (file.matched_fields || []).join(", ") || "—"],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value || "—";
    list.append(dt, dd);
  }
  const open = document.createElement("a");
  open.className = "hit";
  open.href = file.url;
  open.target = "_blank";
  open.rel = "noopener";
  open.textContent = "Open matched file";
  const packetHeading = document.createElement("h3");
  packetHeading.className = "packet-heading";
  packetHeading.textContent = "All tender files";
  const packetList = document.createElement("div");
  packetList.className = "packet-list";
  packetList.textContent = "Loading packet…";
  pane.append(title, about, list, open, packetHeading, packetList);

  try {
    const response = await fetch(`/api/tenders/${encodeURIComponent(group.tender_id)}/files`);
    const payload = await response.json();
    packetList.replaceChildren();
    if (!response.ok) {
      packetList.textContent = payload.detail || "Could not load packet files.";
    } else if (!payload.files || !payload.files.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No extracted files for this tender.";
      packetList.appendChild(empty);
    } else {
      const matchedPath = (file.relative_path || file.filename || "").replace(/^\/+/, "");
      for (const item of payload.files) {
        const row = document.createElement("div");
        row.className = "packet-row";
        const link = document.createElement("a");
        link.className = "file packet-file";
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = item.name;
        row.appendChild(link);
        if (item.name === matchedPath || item.name.endsWith(`/${matchedPath}`)) {
          row.classList.add("matched");
          const badge = document.createElement("span");
          badge.className = "match-badge";
          badge.textContent = "Match";
          row.appendChild(badge);
        }
        packetList.appendChild(row);
      }
      const count = document.createElement("p");
      count.className = "muted packet-count";
      count.textContent = `${payload.files.length} file${payload.files.length === 1 ? "" : "s"} in packet`;
      packetList.appendChild(count);
    }
  } catch (error) {
    packetList.textContent = "Could not load packet files.";
  }

  const extension = (file.extension || "").toLowerCase();
  if (PREVIEW_EXT.has(extension)) {
    const preview = document.createElement("pre");
    preview.className = "preview";
    preview.textContent = "Loading preview…";
    pane.appendChild(preview);
    try {
      const response = await fetch(file.url);
      const text = await response.text();
      preview.textContent = text.slice(0, 4000) || "(empty file)";
    } catch (error) {
      preview.textContent = "Preview could not be loaded.";
    }
  }
  if (window.matchMedia("(max-width: 900px)").matches) {
    pane.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function renderHit(group, file) {
  const card = document.createElement("article");
  card.className = "hit-card";
  card.dataset.key = `${group.tender_id}:${file.relative_path}`;
  if (card.dataset.key === selectedHit) card.classList.add("active");
  card.appendChild(groupTitle(group));
  const about = document.createElement("p");
  about.className = "muted";
  about.textContent = shortOrg(group.organisation);
  const meta = document.createElement("div");
  meta.className = "hit-meta";
  const name = document.createElement("strong");
  name.textContent = file.filename;
  const ext = document.createElement("span");
  ext.className = "ext-badge";
  ext.textContent = file.extension || "file";
  meta.append(name, ext);
  if (group.closing_at) {
    const closing = document.createElement("span");
    closing.textContent = `Closes ${showDate(group.closing_at)}`;
    meta.appendChild(closing);
  }
  if (file.score != null) {
    const score = document.createElement("span");
    score.textContent = `Score ${file.score}`;
    meta.appendChild(score);
  }
  const rawSnippet = file.snippet || "";
  const plainSnippet = rawSnippet.replace(/<\/?mark>/gi, "").trim();
  const fileLabel = (file.filename || file.relative_path || "").trim();
  const snippetIsFilename =
    !plainSnippet ||
    plainSnippet === fileLabel ||
    plainSnippet === file.relative_path ||
    fileLabel.startsWith(plainSnippet) ||
    plainSnippet.startsWith(fileLabel.replace(/\.[^.]+$/, ""));
  if (rawSnippet && !snippetIsFilename) {
    const snippet = document.createElement("p");
    snippet.className = "snippet";
    snippet.innerHTML = safeSnippet(rawSnippet);
    card.append(about, meta, snippet);
  } else {
    card.append(about, meta);
  }
  card.onclick = () => showDetail(group, file);
  return card;
}

async function runSearch(event) {
  if (event) event.preventDefault();
  const query = document.getElementById("query").value.trim();
  const include = document.getElementById("include-expired").checked;
  const meta = document.getElementById("search-meta");
  const results = document.getElementById("results");
  const pager = document.getElementById("search-pager");
  results.replaceChildren();
  if (!query) {
    meta.textContent = "Enter a file name or a phrase from a document.";
    document.getElementById("search-charts").hidden = true;
    document.getElementById("extension-chips").replaceChildren();
    pager.hidden = true;
    return;
  }
  meta.textContent = "Searching…";
  const params = new URLSearchParams({
    q: query,
    include_expired: include ? "true" : "false",
    page: String(searchPage),
  });
  if (searchLifecycle) params.set("lifecycle", searchLifecycle);
  if (searchExtensions.size) params.set("extension", [...searchExtensions].join(","));
  const response = await fetch(`/api/search?${params.toString()}`);
  const payload = await response.json();
  if (!response.ok) {
    meta.textContent = payload.detail || "Search failed.";
    document.getElementById("search-charts").hidden = true;
    pager.hidden = true;
    return;
  }
  searchFound = payload.found || 0;
  meta.textContent = `${searchFound} matching files`;
  renderCharts(payload.facets || {});
  renderExtensionChips((payload.facets && payload.facets.extension) || []);
  for (const group of payload.groups || []) {
    for (const file of group.files || []) {
      results.appendChild(renderHit(group, file));
    }
  }
  if (!payload.groups || !payload.groups.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No files matched these filters.";
    results.appendChild(empty);
  }
  const from = searchFound ? (searchPage - 1) * SEARCH_PAGE + 1 : 0;
  const to = Math.min(searchPage * SEARCH_PAGE, searchFound);
  document.getElementById("search-pageinfo").textContent = `${from}–${to} of ${searchFound}`;
  pager.hidden = searchFound <= SEARCH_PAGE;
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchPage = 1;
    runSearch();
  }, 300);
}

document.getElementById("search-form").onsubmit = runSearch;
document.getElementById("query").addEventListener("input", scheduleSearch);
document.getElementById("include-expired").addEventListener("change", () => {
  searchPage = 1;
  runSearch();
});
document.querySelectorAll("#stage-chips .chip").forEach((chip) => {
  chip.onclick = () => {
    searchLifecycle = chip.dataset.lifecycle || "";
    setChipActive(document.getElementById("stage-chips"), "lifecycle", searchLifecycle);
    searchPage = 1;
    runSearch();
  };
});
document.getElementById("search-prev").onclick = () => {
  if (searchPage > 1) {
    searchPage -= 1;
    runSearch();
  }
};
document.getElementById("search-next").onclick = () => {
  if (searchPage * SEARCH_PAGE < searchFound) {
    searchPage += 1;
    runSearch();
  }
};

document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
  const tag = (event.target && event.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  event.preventDefault();
  showTab("files");
});

async function applyHash() {
  if (syncingHash) return;
  const raw = decodeURIComponent((location.hash || "#overview").slice(1));
  const [tab, ...rest] = raw.split("/");
  const name = rest.join("/");
  if (!["overview", "departments", "tenders", "files", "usage"].includes(tab || "")) {
    showTab("overview", { skipHash: true });
    return;
  }
  if (tab === "departments" && name) {
    currentTab = "departments";
    document.querySelectorAll("nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.tab === "departments");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "departments");
    });
    if (!departmentCatalog.length) {
      const response = await fetch("/api/departments");
      departmentCatalog = (await response.json()).departments || [];
    }
    const match = departmentCatalog.find((item) => item.department === name);
    openDepartment(
      match || {
        department: name,
        total: 0,
        open: 0,
        closing_soon: 0,
        expired: 0,
        downloaded: 0,
      }
    );
    return;
  }
  if (tab === "departments") activeDepartment = null;
  showTab(tab, { skipHash: true });
}

window.addEventListener("hashchange", () => {
  applyHash();
});

function connect() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss://" : "ws://"}${location.host}/ws`);
  socket.onopen = () => {
    live.textContent = "Live";
  };
  socket.onclose = () => {
    live.textContent = "Reconnecting…";
    setTimeout(connect, 1000);
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.job) renderJob(message.job, message.counts || {});
    if (message.captcha) renderCaptcha(message.captcha);
    if (currentTab === "usage" && message.captcha) {
      /* Refresh full dependency metrics while watching Usage. */
      loadUsage();
    }
    if (busy && document.getElementById("tenders").classList.contains("active")) loadPage();
  };
}

const corrDialog = document.getElementById("corr-dialog");
document.getElementById("corr-dialog-close").onclick = () => corrDialog.close();
corrDialog.addEventListener("click", (event) => {
  if (event.target === corrDialog) corrDialog.close();
});

connect();
applyHash();
