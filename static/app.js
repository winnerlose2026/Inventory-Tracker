// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------
let currentPage = 'inventory';
let editingName = null;   // null = add mode, string = edit mode
let txnType = 'use';      // 'use' | 'restock'
let txnItemName = '';
let warehouseCatalog = {}; // { "Cheney Brothers": [...], "US Foods": [...] }
let inventoryCache = [];   // last-loaded inventory list, used for Edit lookups

function findCachedItem(name) {
  return inventoryCache.find(i => i.name === name);
}

async function ensureWarehouses() {
  if (Object.keys(warehouseCatalog).length) return warehouseCatalog;
  warehouseCatalog = await api('/api/warehouses');
  return warehouseCatalog;
}

function populateWarehouseSelect(selectEl, distributor, selected = '', includeAll = false) {
  const options = [];
  if (includeAll) options.push('<option value="">All warehouses</option>');
  else options.push('<option value="">—</option>');
  const list = distributor ? (warehouseCatalog[distributor] || [])
                           : [].concat(...Object.values(warehouseCatalog));
  for (const wh of list) {
    options.push(`<option value="${escAttr(wh)}" ${wh === selected ? 'selected' : ''}>${escHtml(wh)}</option>`);
  }
  selectEl.innerHTML = options.join('');
}

async function onDistributorFilterChange() {
  await ensureWarehouses();
  populateWarehouseSelect(
    document.getElementById('inv-filter-warehouse'),
    document.getElementById('inv-filter-distributor').value,
    '',
    true
  );
  loadInventory();
}

async function onFormDistributorChange() {
  await ensureWarehouses();
  populateWarehouseSelect(
    document.getElementById('f-warehouse'),
    document.getElementById('f-distributor').value,
    '',
    false
  );
}

// -------------------------------------------------------------------------
// Navigation
// -------------------------------------------------------------------------
function showPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  event.currentTarget.classList.add('active');
  currentPage = page;
  refresh(page);
}

function refresh(page) {
  if (page === 'inventory') loadInventory();
  else if (page === 'usage') loadUsage();
  else if (page === 'distributors') showPage('inventory');
  else if (page === 'report') loadReport();
  else if (page === 'pending-pos') loadPendingPOs();
  else if (page === 'freight') { loadFreight(); loadFreightLeadTimes(); }
  else if (page === 'production') loadProduction();
  else if (page === 'planner') loadProductionGuide();
  else if (page === 'traceability') initTraceability();
}

function distributorBadge(name) {
  if (!name) return '<span style="color:var(--muted)">—</span>';
  const cls = name === 'Cheney Brothers' ? 'badge-cheney'
            : name === 'US Foods' ? 'badge-yellow'
            : name === 'Chefs Warehouse' ? 'badge-violet'
            : 'badge-purple';
  return `<span class="badge ${cls}">${escHtml(name)}</span>`;
}

// -------------------------------------------------------------------------
// Toast
// -------------------------------------------------------------------------
function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast-msg ${type}`;
  el.textContent = msg;
  document.getElementById('toast').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// -------------------------------------------------------------------------
// API helpers — auth is handled by the Flask session cookie (set when the
// user signs in at /login). Browsers attach it automatically, so there's no
// X-Inventory-Token header to manage from the page anymore.
// -------------------------------------------------------------------------
function authHeaders() {
  return { 'Content-Type': 'application/json' };
}

async function api(url, method = 'GET', body = null) {
  const opts = {
    method,
    headers: authHeaders(),
    credentials: 'same-origin', // ensure session cookie is sent
  };
  if (body) opts.body = JSON.stringify(body);
  let r = await fetch(url, opts);
  if (r.status === 401) {
    // Session expired or never existed — bounce to the login page,
    // preserving the current page so we land back here after signing in.
    toast('Session expired. Redirecting to sign-in…', 'error');
    setTimeout(() => {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = '/login?next=' + next;
    }, 800);
    return {};
  }
  return r.json();
}

// -------------------------------------------------------------------------
// Dashboard
// -------------------------------------------------------------------------
function invStatusInfo(item) {
  // Single source of truth for threshold classification, shared by the
  // inventory table and the Low Stock Alerts card.
  //   qty <= 0                    -> OUT   (flashing red)
  //   weeks < 2                   -> Low   (red)
  //   2 <= weeks < 4              -> Short (yellow)
  //   weeks >= 4 / no usage rate  -> OK    (green / standard)
  const qty = +(item.quantity || 0);
  const weekly = +(item.weekly_usage || 0);
  const weeks = weekly > 0 ? (qty / weekly) : null;
  let key, badge;
  if (qty <= 0) {
    key = 'OUT';
    badge = '<span class="badge badge-out" title="Out of stock">OUT</span>';
  } else if (weeks !== null && weeks < 2) {
    key = 'Low';
    badge = '<span class="badge badge-red" title="Under 2 weeks of supply">Low</span>';
  } else if (weeks !== null && weeks < 4) {
    key = 'Short';
    badge = '<span class="badge badge-yellow" title="Between 2 and 4 weeks of supply">Short</span>';
  } else {
    key = 'OK';
    badge = '<span class="badge badge-green">OK</span>';
  }
  const weeksColor = weeks === null ? 'color:var(--muted)'
    : weeks < 2 ? 'color:var(--red);font-weight:600'
    : weeks < 4 ? 'color:var(--yellow);font-weight:600'
    : '';
  return { key, badge, weeks, weeksColor, qty, weekly };
}

function _invDisplayName(name) {
  return (name || '')
    .replace(/\s*\[[^\]]*\]\s*$/, '')
    .replace(/\s+\d+\s*oz\b/i, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

// One alert row, matching the Inventory table's columns exactly.
function alertRowHtml(i) {
  const stat = invStatusInfo(i);
  const weekly = stat.weekly, weeks = stat.weeks;
  const qtyCritical = (stat.key === 'OUT' || stat.key === 'Low');
  return `<tr class="sku-row">
    <td data-label="Variety" style="font-weight:600">${escHtml(_invDisplayName(i.name))}</td>
    <td data-label="Qty" style="font-weight:600;${qtyCritical ? 'color:var(--red)' : ''}">${(+(i.quantity || 0)).toFixed(0)}</td>
    <td data-label="On Order">${renderOnOrder(i)}</td>
    <td data-label="Unit">${escHtml(i.unit)}</td>
    <td data-label="Price">${i.price > 0 ? '$' + i.price.toFixed(2) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
    <td data-label="Weekly Usage">${weekly > 0 ? weekly.toFixed(1) + '/wk' : '<span style="color:var(--muted)">&mdash;</span>'}</td>
    <td data-label="Weeks Remaining" style="${stat.weeksColor}">${weeks !== null ? weeks.toFixed(1) + ' wk' : '<span style="color:var(--muted)">&mdash;</span>'}</td>
    <td data-label="Status">${stat.badge}</td>
  </tr>`;
}

// Low Stock Alerts: every SKU in Short / Low / OUT, grouped by warehouse,
// using the same columns as the Inventory table. Pass the grouped data
// (distributor -> warehouse -> items) when you already have it; otherwise
// it fetches /api/distributors itself.
// Collapse/expand one warehouse block inside the Low Stock Alerts card.
function toggleAlertWh(id, head) {
  const box = document.getElementById(id);
  if (!box) return;
  const open = box.style.display !== 'none';
  box.style.display = open ? 'none' : '';
  const chev = head.querySelector('.chev');
  if (chev) chev.textContent = open ? '▸' : '▾';
}

async function refreshLowStock(groups) {
  const lowCard = document.getElementById('low-stock-card');
  const body = document.getElementById('low-stock-body');
  const countEl = document.getElementById('low-stock-count');
  if (!lowCard || !body) return;
  if (!groups) {
    try { groups = await api('/api/distributors'); }
    catch (e) { return; }
  }
  const byWh = {};
  (groups || []).forEach(g => (g.warehouses || []).forEach(w => (w.items || []).forEach(it => {
    const key = invStatusInfo(it).key;
    if (key === 'Short' || key === 'Low' || key === 'OUT') {
      (byWh[w.warehouse] = byWh[w.warehouse] || []).push(it);
    }
  })));
  const warehouses = Object.keys(byWh).sort();
  const total = warehouses.reduce((s, w) => s + byWh[w].length, 0);
  if (total === 0) {
    lowCard.style.display = 'none';
    body.innerHTML = '';
    if (countEl) countEl.textContent = '';
    return;
  }
  lowCard.style.display = '';
  if (countEl) countEl.textContent = `· ${total} SKU${total === 1 ? '' : 's'} need attention`;
  const sev = { OUT: 0, Low: 1, Short: 2 };
  body.innerHTML = warehouses.map((w, idx) => {
    const items = byWh[w].slice().sort((a, b) => {
      const sa = invStatusInfo(a), sb = invStatusInfo(b);
      if (sev[sa.key] !== sev[sb.key]) return sev[sa.key] - sev[sb.key];
      const wa = sa.weeks === null ? Infinity : sa.weeks;
      const wb = sb.weeks === null ? Infinity : sb.weeks;
      return wa - wb;
    });
    // Each warehouse is a collapsible dropdown (default collapsed) so the
    // section stays compact. Count badge is red when the warehouse has any
    // Low/OUT SKU, yellow when it's only Short.
    const worst = items.reduce((m, it) => Math.min(m, sev[invStatusInfo(it).key]), 3);
    const countClass = worst <= 1 ? 'badge-red' : 'badge-yellow';
    const boxId = 'alert-wh-' + idx;
    return `
      <div style="border-top:1px solid var(--border)">
        <div onclick="toggleAlertWh('${boxId}', this)" style="cursor:pointer;display:flex;align-items:center;gap:10px;padding:12px 16px;user-select:none">
          <span class="chev" style="display:inline-block;width:12px;color:var(--muted)">&#9656;</span>
          <span style="font-family:var(--font-display);font-weight:700;font-size:18px;color:var(--accent)">${escHtml(w)}</span>
          <span class="badge ${countClass}" title="${items.length} SKU${items.length === 1 ? '' : 's'} need attention">${items.length}</span>
        </div>
        <div id="${boxId}" style="display:none;padding:0 16px 12px">
          <table class="inv-table">
            <colgroup>
              <col style="width:28%"><col style="width:8%"><col style="width:13%"><col style="width:7%">
              <col style="width:9%"><col style="width:13%"><col style="width:13%"><col style="width:9%">
            </colgroup>
            <thead>
              <tr>
                <th>Variety</th><th>Qty</th><th>On Order</th><th>Unit</th><th>Price</th>
                <th>Weekly Usage</th><th><span class="wks-full">Weeks Remaining</span><span class="wks-abbr">Wks</span></th><th>Status</th>
              </tr>
            </thead>
            <tbody>${items.map(alertRowHtml).join('')}</tbody>
          </table>
        </div>
      </div>`;
  }).join('');
}

// Back-compat: sync, reversal, and PO-cancel flows call loadDashboard() to
// refresh at-a-glance state. The Dashboard tab is gone, so this now just
// refreshes the Low Stock Alerts card on the Inventory page.
async function loadDashboard() {
  return refreshLowStock();
}

// Renders a usage/restock row with a Reverse button. Also handles reversal
// records (source: 'reversal') and already-reversed entries differently so
// the activity log stays auditable.
function renderActivityRow(e, _colspan) {
  const ts = (e.timestamp || '').replace('T',' ').slice(0,19);
  const isRestock = e.amount < 0;
  const isReversal = e.source === 'reversal';
  const alreadyReversed = !!e.reversed;

  let typeBadge;
  if (isReversal) {
    typeBadge = `<span class="badge badge-purple">↶ Reversal</span>`;
  } else if (isRestock) {
    typeBadge = `<span class="badge badge-green">▲ Restock</span>`;
  } else {
    typeBadge = `<span class="badge badge-red">▼ Use</span>`;
  }

  const noteHtml = escHtml(e.note || '')
    + (alreadyReversed ? ' <span class="badge badge-yellow" style="margin-left:6px">reversed</span>' : '');

  let actionHtml = '';
  if (isReversal) {
    actionHtml = '<span style="color:var(--muted);font-size:11px">—</span>';
  } else if (alreadyReversed) {
    actionHtml = '<span style="color:var(--muted);font-size:11px">—</span>';
  } else {
    actionHtml = `<button class="btn btn-ghost btn-sm" onclick="reverseActivity('${escAttr(e.timestamp)}')" title="Undo this entry">↶ Reverse</button>`;
  }

  const rowStyle = (alreadyReversed || isReversal)
    ? ' style="opacity:.7"' : '';

  return `<tr${rowStyle}>
    <td style="color:var(--muted);font-size:12px;white-space:nowrap">${ts}</td>
    <td>${escHtml(e.item_name)}</td>
    <td>${typeBadge}</td>
    <td class="${isRestock ? 'type-restock' : 'type-use'}" style="font-weight:600">${isRestock ? '+' : '-'}${Math.abs(e.amount).toFixed(2)}${e.unit ? ' ' + escHtml(e.unit) : ''}</td>
    <td style="color:var(--muted)">${noteHtml}</td>
    <td class="actions-col">${actionHtml}</td>
  </tr>`;
}

async function reverseActivity(timestamp) {
  if (!timestamp) return;
  if (!confirm('Reverse this entry? This will undo its effect on inventory and add a reversal record to the log.')) return;
  let res;
  try {
    res = await api('/api/usage/reverse', 'POST', { timestamp });
  } catch (err) {
    toast('Reverse failed: ' + String(err), 'error');
    return;
  }
  if (!res || !res.ok) {
    toast(`Reverse failed: ${(res && res.error) || 'unknown error'}`, 'error');
    return;
  }
  toast(`Reversed ${res.reversed_amount > 0 ? 'use' : 'restock'} of "${res.item_name}".`);
  // Refresh whatever's visible.
  loadDashboard();
  if (currentPage === 'usage') loadUsage();
  if (currentPage === 'inventory') loadInventory();
  if (currentPage === 'distributors') loadInventory();
}

// -------------------------------------------------------------------------
// Inventory
// -------------------------------------------------------------------------
// Per-warehouse count-freshness chip. A "count" is a rep inventory
// worksheet (or vendor on-hand true-up). Worksheets land weekly, so a count
// within 8 days is current (green); older or never is stale (red).
function renderWarehouseFreshness(iso) {
  if (!iso) {
    return '<span class="wh-fresh wh-fresh-red" title="No inventory count received yet">\u2717 No count</span>';
  }
  const then = new Date(iso);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  const ok = days <= 8;
  const dateStr = then.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const ageStr = days <= 0 ? 'today' : (days === 1 ? '1 day ago' : days + ' days ago');
  const mark = ok ? '\u2713' : '\u2717';
  const cls = ok ? 'wh-fresh-green' : 'wh-fresh-red';
  return `<span class="wh-fresh ${cls}" title="Latest count received ${dateStr} (${ageStr})">${mark} Counted ${dateStr}</span>`;
}

// Bottom-of-page SKU editor. Per-row actions were removed to slim the table;
// pick a SKU here and edit it. Options are rebuilt from inventoryCache on
// every inventory load.
function populateSkuEditSelect() {
  const sel = document.getElementById('sku-edit-select');
  if (!sel) return;
  const prev = sel.value;
  const items = inventoryCache.slice().sort((a, b) => (a.name < b.name ? -1 : 1));
  sel.innerHTML = '<option value="">Select a SKU\u2026</option>' +
    items.map(i => `<option value="${escAttr(i.name)}">${escHtml(i.name)}</option>`).join('');
  if (prev) sel.value = prev;
}

function editSelectedSku() {
  const sel = document.getElementById('sku-edit-select');
  const name = sel && sel.value;
  if (!name) { toast('Pick a SKU to edit first.', 'error'); return; }
  openEditModal(name);
}

async function loadInventory() {
  // Inventory now uses the grouped (distributor -> warehouse -> SKU) layout
  // that used to live on the Distributors tab. It also pulls the lot map
  // so each row can expand a lot-history sub-table.
  // Inventory is the landing page now. Build the grouped data once and feed
  // it to the Low Stock Alerts card so its rows match the table exactly.
  const [groups, lotMap] = await Promise.all([
    api('/api/distributors'),
    api('/api/production/lots-by-pair').catch(() => ({})),
  ]);
  window._lotMapByPair = lotMap || {};
  refreshLowStock(groups);

  // Cache flat items for Edit/Use/Restock modal lookups.
  inventoryCache = [];
  groups.forEach(g => g.warehouses.forEach(w => w.items.forEach(it => inventoryCache.push(it))));
  populateSkuEditSelect();

  const container = document.getElementById('inventory-container');
  if (!container) return;
  if (!groups.length) {
    container.innerHTML = '<div class="empty">No inventory yet.</div>';
    return;
  }
  container.innerHTML = groups.map(g => {
    const costs = new Set(
      g.warehouses.flatMap(w => w.items.map(i => i.case_cost || 0)).filter(c => c > 0)
    );
    const caseCost = costs.size === 1 ? [...costs][0] : null;
    const caseSizes = new Set(
      g.warehouses.flatMap(w => w.items.map(i => i.case_size || 0)).filter(c => c > 0)
    );
    const caseSize = caseSizes.size === 1 ? [...caseSizes][0] : null;
    const weeklyTotal = g.warehouses.reduce(
      (s, w) => s + w.items.reduce((a, i) => a + (i.weekly_usage || 0), 0), 0);
    const shortCount = g.warehouses.reduce(
      (s, w) => s + w.items.filter(i => (i.weekly_usage || 0) > 0 && (i.quantity / i.weekly_usage) < 4).length, 0);

    return `
    <div class="card">
      <div class="card-header">
        <span class="card-title"><span class="dist-badge-lg">${distributorBadge(g.distributor === 'Unassigned' ? '' : g.distributor)}</span>${g.distributor === 'Unassigned' ? ' Unassigned' : ''}</span>
        <span style="color:var(--muted);font-size:12px">
          ${g.warehouses.length} warehouse(s) &middot; ${g.item_count} SKU(s) &middot; ${g.total_quantity.toFixed(0)} units &middot; $${g.total_value.toFixed(2)}
          ${caseCost !== null ? ` &middot; case $${caseCost.toFixed(2)}${caseSize ? '/' + caseSize : ''}` : ''}
          &middot; ${weeklyTotal.toFixed(0)}/wk
          ${(g.total_on_order || 0) > 0 ? ` &middot; <span style="color:var(--accent)">${g.total_on_order.toFixed(0)} on order</span>` : ''}
          ${shortCount > 0 ? ` &middot; <span style="color:var(--red)">${shortCount} under 4 wk</span>` : ''}
          ${g.low_stock_count > 0 ? ` &middot; <span style="color:var(--yellow)">${g.low_stock_count} low</span>` : ''}
        </span>
      </div>
      <div style="padding:0 4px 8px">
        ${g.warehouses.map(w => {
          const whWeekly = w.items.reduce((a, i) => a + (i.weekly_usage || 0), 0);
          const whShort = w.items.filter(i => (i.weekly_usage || 0) > 0 && (i.quantity / i.weekly_usage) < 4).length;
          const whFresh = renderWarehouseFreshness(w.last_count_at);
          return `
          <div style="padding:14px 16px 4px">
            <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;gap:12px;flex-wrap:wrap">
              <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
                <div style="font-family:var(--font-display);font-weight:700;font-size:20px;color:var(--accent);letter-spacing:-.005em">${escHtml(w.warehouse)}</div>
                ${whFresh}
              </div>
              <div style="color:var(--muted);font-size:12px">
                ${w.item_count} SKU(s) &middot; ${w.total_quantity.toFixed(0)} units &middot; $${w.total_value.toFixed(2)}
                &middot; ${whWeekly.toFixed(0)}/wk
                ${whShort > 0 ? ` &middot; <span style="color:var(--red)">${whShort} under 4 wk</span>` : ''}
                ${w.low_stock_count > 0 ? ` &middot; <span style="color:var(--yellow)">${w.low_stock_count} low</span>` : ''}
              </div>
            </div>
            <table class="inv-table">
              <colgroup>
                <col style="width:28%">
                <col style="width:8%">
                <col style="width:13%">
                <col style="width:7%">
                <col style="width:9%">
                <col style="width:13%">
                <col style="width:13%">
                <col style="width:9%">
              </colgroup>
              <thead>
                <tr>
                  <th>Variety</th><th>Qty</th><th>On Order</th><th>Unit</th><th>Price</th>
                  <th>Weekly Usage</th><th><span class="wks-full">Weeks Remaining</span><span class="wks-abbr">Wks</span></th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                ${w.items.map(i => {
                  const stat = invStatusInfo(i);
                  const weekly = stat.weekly;
                  const weeks = stat.weeks;
                  const weeksColor = stat.weeksColor;
                  const qtyCritical = (stat.key === 'OUT' || stat.key === 'Low');
                  const variety = (i.name || '').split(' Bagel')[0];
                  const pairKey = `${w.warehouse}|${variety}`;
                  const lots = (window._lotMapByPair && window._lotMapByPair[pairKey]) || [];
                  // Lots come back from the API oldest-first with cs_produced
                  // / cs_remaining already FIFO-applied. Fall back to L.cs if
                  // we're talking to an older server that hasn't redeployed.
                  const lotCs = lots.reduce((s, L) => s + (L.cs_produced ?? L.cs ?? 0), 0);
                  const lotRemainingCs = lots.reduce((s, L) => s + (L.is_in_transit ? 0 : (L.cs_remaining ?? L.cs ?? 0)), 0);
                  const lotInTransitCs = lots.reduce((s, L) => s + (L.is_in_transit ? (L.cs_produced ?? L.cs ?? 0) : 0), 0);
                  const activeLotCount = lots.filter(L => !L.is_in_transit && (L.cs_remaining ?? L.cs ?? 0) > 0).length;
                  const hasLots = lots.length > 0;
                  const rowId = 'lots-' + Math.random().toString(36).slice(2, 10);
                  const chevron = hasLots
                    ? `<span style="cursor:pointer;display:inline-block;width:14px;color:var(--muted);user-select:none" onclick="toggleLotRow('${rowId}', this)">&#9656;</span>`
                    : '<span style="display:inline-block;width:14px"></span>';
                  // Display name strips the "4oz" size and the trailing
                  // "[CB - Ocala]" / "[USF - Alcoa]" distributor+DC tag —
                  // both are already conveyed by the surrounding distributor
                  // card and warehouse header, so they're noise in the row.
                  const displayName = (i.name || '')
                    .replace(/\s*\[[^\]]*\]\s*$/, '')
                    .replace(/\s+\d+\s*oz\b/i, '')
                    .replace(/\s{2,}/g, ' ')
                    .trim();
                  return `<tr class="sku-row">
                    <td data-label="Variety" style="font-weight:600">${chevron} ${escHtml(displayName)}${hasLots ? ' <span class="badge badge-blue" title="' + activeLotCount + ' active lot(s), ' + fmtCs(lotRemainingCs) + ' cs counted toward on-hand (FIFO). ' + lots.length + ' lot(s) on record, ' + fmtCs(lotCs) + ' cs total.">' + activeLotCount + ' lot' + (activeLotCount === 1 ? '' : 's') + '</span>' : ''}${lotInTransitCs > 0 ? ' <span class="badge" style="background:rgba(5,23,71,.06);color:var(--muted)" title="Produced but not yet arrived; not counted toward on-hand until the arrival date.">+' + fmtCs(lotInTransitCs) + ' in transit</span>' : ''}</td>
                    <td data-label="Qty" style="font-weight:600;${qtyCritical ? 'color:var(--red)' : ''}">${i.quantity.toFixed(0)}</td>
                    <td data-label="On Order">${renderOnOrder(i)}</td>
                    <td data-label="Unit">${escHtml(i.unit)}</td>
                    <td data-label="Price">${i.price > 0 ? '$' + i.price.toFixed(2) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
                    <td data-label="Weekly Usage">${weekly > 0 ? weekly.toFixed(1) + '/wk' : '<span style="color:var(--muted)">&mdash;</span>'}</td>
                    <td data-label="Weeks Remaining" style="${weeksColor}">${weeks !== null ? weeks.toFixed(1) + ' wk' : '<span style="color:var(--muted)">&mdash;</span>'}</td>
                    <td data-label="Status">${stat.badge}</td>
                  </tr>
                  ${hasLots ? (() => {
                    // FIFO rule: each (warehouse, variety) lot list comes
                    // back from /api/production/lots-by-pair sorted
                    // oldest-first, with cs_remaining and is_next_out
                    // pre-computed on the server. The "Next out" lot is the
                    // oldest one with cs_remaining > 0 — that's the lot the
                    // next usage event will draw down.
                    const lotRemaining = lots.reduce((s, L) => s + (L.is_in_transit ? 0 : (L.cs_remaining ?? L.cs ?? 0)), 0);
                    const inTransitCs = lots.reduce((s, L) => s + (L.is_in_transit ? (L.cs_produced ?? L.cs ?? 0) : 0), 0);
                    const inTransitCount = lots.filter(L => L.is_in_transit).length;
                    const activeCount = lots.filter(L => !L.is_in_transit && (L.cs_remaining ?? L.cs ?? 0) > 0).length;
                    const depletedCount = lots.filter(L => !L.is_in_transit && (L.cs_remaining ?? L.cs ?? 0) <= 0 && (L.cs_produced ?? 0) > 0).length;
                    return `<tr id="${rowId}" class="lot-detail-row" style="display:none;background:var(--surface-alt,#f7f7f9)">
                    <td colspan="8" style="padding:8px 16px 12px 30px">
                      <div style="font-size:12px;color:var(--muted);margin-bottom:6px">
                        <strong style="color:var(--text)">FIFO &middot; oldest production date first.</strong>
                        ${lots.length} lot${lots.length === 1 ? '' : 's'} &middot;
                        ${fmtCs(lotCs)} cs produced &middot;
                        <span style="color:var(--green)">${fmtCs(lotRemaining)} cs on-hand</span> &middot;
                        ${activeCount} active${depletedCount ? ` &middot; ${depletedCount} depleted` : ''}${inTransitCount ? ` &middot; <span style="color:var(--accent)">${inTransitCount} in transit (${fmtCs(inTransitCs)} cs incoming)</span>` : ''}
                      </div>
                      <table style="margin:0;font-size:12px;width:auto;min-width:60%">
                        <thead><tr>
                          <th style="text-align:left">Lot #</th>
                          <th style="text-align:right">cs remaining</th>
                          <th style="text-align:right">cs produced</th>
                          <th>Production date</th>
                          <th>PO #</th>
                          <th>Status</th>
                        </tr></thead>
                        <tbody>
                          ${lots.map(L => {
                            const produced = L.cs_produced ?? L.cs ?? 0;
                            const inTransit = !!L.is_in_transit;
                            const remaining = L.cs_remaining ?? produced;
                            const consumed = L.cs_consumed ?? (produced - remaining);
                            const nextOut = !inTransit && !!L.is_next_out;
                            const depleted = !inTransit && remaining <= 0 && produced > 0;
                            const rowBg = nextOut ? 'background:rgba(240,179,35,.18)' : inTransit ? 'background:rgba(5,23,71,.04)' : '';
                            const statusHtml = inTransit
                              ? '<span class="badge badge-blue" title="Produced but not yet arrived; not counted toward on-hand until the arrival date">In Transit</span>'
                              : nextOut
                              ? '<span class="badge badge-yellow" title="FIFO: next usage draws from this lot first">Next out</span>'
                              : depleted
                                ? '<span class="badge badge-red" title="Lot fully consumed under FIFO">Depleted</span>'
                                : (remaining < produced && remaining > 0)
                                  ? '<span class="badge badge-blue" title="Partially consumed under FIFO">Partial</span>'
                                  : '<span class="badge badge-green">Active</span>';
                            const remainingStyle = inTransit
                              ? 'color:var(--muted);font-style:italic'
                              : depleted
                              ? 'color:var(--muted);text-decoration:line-through'
                              : nextOut
                                ? 'color:var(--accent);font-weight:700'
                                : 'font-weight:600';
                            return `<tr style="${rowBg}">
                              <td style="font-family:monospace">${escHtml(L.lot)}</td>
                              <td style="text-align:right;${remainingStyle}">${fmtCs(remaining)}${consumed > 0 && consumed < produced ? ` <span style="color:var(--muted);font-weight:400">(−${fmtCs(consumed)})</span>` : ''}</td>
                              <td style="text-align:right;color:var(--muted)">${fmtCs(produced)}</td>
                              <td>${fmtProdDate(L.production_date)}</td>
                              <td>${escHtml(L.po_number || '')}</td>
                              <td>${statusHtml}</td>
                            </tr>`;
                          }).join('')}
                        </tbody>
                      </table>
                    </td>
                  </tr>`;
                  })() : ''}`;
                }).join('')}
              </tbody>
            </table>
          </div>
        `;}).join('')}
      </div>
    </div>
  `;}).join('');
}


// -------------------------------------------------------------------------
// Distributor sync
// -------------------------------------------------------------------------
async function runSync(dryRun) {
  const box = document.getElementById('sync-report');
  box.style.display = '';
  box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--muted)">${dryRun ? 'Previewing sync…' : 'Syncing from distributors…'}</div></div>`;
  let result;
  try {
    result = await api('/api/sync', 'POST', { dry_run: dryRun });
  } catch (e) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Sync failed: ${escHtml(String(e))}</div></div>`;
    return;
  }
  box.innerHTML = renderSyncReport(result);
  toast(dryRun ? 'Preview complete.' : 'Sync complete.');
  if (!dryRun) loadInventory();
}

async function runSeed() {
  const box = document.getElementById('sync-report');
  box.style.display = '';
  box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--muted)">Seeding bagel catalog…</div></div>`;
  let result;
  try {
    result = await api('/api/seed', 'POST', { reset: false });
  } catch (e) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Seed failed: ${escHtml(String(e))}</div></div>`;
    return;
  }
  if (!result.ok) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Seed failed: ${escHtml(result.error || 'unknown error')}</div></div>`;
    return;
  }
  box.innerHTML = `<div class="card">
    <div class="card-header"><span class="card-title">Seed Complete</span></div>
    <div style="padding:14px 20px;font-size:13px">
      Added <strong>${result.added}</strong> SKU(s), skipped <strong>${result.skipped}</strong> existing.
      Catalog total: ${result.total} (Cheney ${result.cheney} · US Foods ${result.us_foods}).
    </div>
  </div>`;
  toast('Bagel catalog seeded.');
  loadInventory();
  loadDashboard();
}

async function runMigrateUnits() {
  if (!confirm('This converts every item from "each" to "cs": quantity, threshold, and weekly usage are divided by case_size, and price becomes the case cost. Run only once on a fresh deploy. Proceed?')) return;
  const box = document.getElementById('sync-report');
  box.style.display = '';
  box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--muted)">Converting units to cases…</div></div>`;
  let result;
  try {
    result = await api('/api/migrate-units', 'POST', {});
  } catch (e) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Convert failed: ${escHtml(String(e))}</div></div>`;
    return;
  }
  if (!result.ok) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Convert failed: ${escHtml(result.error || 'unknown error')}</div></div>`;
    return;
  }
  box.innerHTML = `<div class="card">
    <div class="card-header"><span class="card-title">Unit Conversion Complete</span></div>
    <div style="padding:14px 20px;font-size:13px">
      Converted <strong>${result.converted}</strong> SKU(s) to cases.
      ${result.already_in_cases ? `Already in cases: ${result.already_in_cases}. ` : ''}
      ${result.skipped_no_case_size ? `<span style="color:var(--yellow)">Skipped (no case_size set): ${result.skipped_no_case_size}.</span> ` : ''}
      Total catalog: ${result.total}.
    </div>
  </div>`;
  toast(`Converted ${result.converted} SKU(s) to case units.`);
  loadInventory();
  loadInventory();
  loadDashboard();
}

async function runEmailScan(dryRun) {
  const box = document.getElementById('sync-report');
  box.style.display = '';
  box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--muted)">${dryRun ? 'Previewing email scan…' : 'Scanning mailbox…'}</div></div>`;
  let result;
  try {
    result = await api('/api/email/scan', 'POST', { dry_run: dryRun });
  } catch (e) {
    box.innerHTML = `<div class="card"><div style="padding:16px 20px;color:var(--red)">Email scan failed: ${escHtml(String(e))}</div></div>`;
    return;
  }
  box.innerHTML = renderSyncReport(result);
  toast(dryRun ? 'Email preview complete.' : 'Email scan complete.');
  if (!dryRun) { loadInventory(); loadDashboard(); }
}

function renderSyncReport(result) {
  const isEmail = result.reports.some(r => r.distributor === 'Email Inbox');
  const heading = isEmail
    ? (result.dry_run ? 'Email Scan Preview' : 'Email Scan Report')
    : (result.dry_run ? 'Sync Preview' : 'Sync Report');
  const cards = result.reports.map(r => {
    if (r.status === 'not_configured') {
      return `<div class="card">
        <div class="card-header">
          <span class="card-title">${distributorBadge(r.distributor)} ${r.distributor}</span>
          <span class="badge badge-yellow">Not configured</span>
        </div>
        <div style="padding:14px 20px;color:var(--muted);font-size:13px;white-space:pre-wrap">${escHtml(r.error || '')}</div>
      </div>`;
    }
    const preview = r.changes.slice(0, 8).map(c => {
      // on_order events don't change on-hand yet — show the pending delta instead.
      const isPending = (c.event_type === 'on_order' || c.event_type === 'on_order_reversed')
                        && (c.on_order_delta !== undefined);
      const displayDelta = isPending ? c.on_order_delta : c.delta;
      const sign = displayDelta >= 0 ? '+' : '';
      const color = isPending ? 'var(--accent)'
                              : (displayDelta >= 0 ? 'var(--green)' : 'var(--red)');
      const typeLabel = c.event_type === 'on_order' ? 'on order'
                      : c.event_type === 'on_order_reversed' ? 'on order ↻'
                      : c.event_type;
      const typePill = typeLabel
        ? `<span class="badge badge-purple" style="margin-right:6px">${escHtml(typeLabel)}</span>` : '';
      const etaSuffix = isPending && c.eta
        ? ` <span style="color:var(--muted);font-size:11px">ETA ${formatDate(c.eta)}</span>`
        : '';
      return `<tr>
        <td>${typePill}${escHtml(c.name)}${etaSuffix}</td>
        <td style="color:var(--muted)">${escHtml(c.warehouse || '')}</td>
        <td style="text-align:right">${c.old_quantity.toFixed(0)}</td>
        <td style="text-align:right;font-weight:600">${c.new_quantity.toFixed(0)}</td>
        <td style="text-align:right;color:${color}">${sign}${displayDelta.toFixed(0)}</td>
      </tr>`;
    }).join('');
    const moreNote = r.changes.length > 8
      ? `<div style="padding:8px 20px;color:var(--muted);font-size:12px">…and ${r.changes.length - 8} more change(s).</div>` : '';
    const unmatchedNote = r.unmatched.length
      ? `<div style="padding:8px 20px;color:var(--yellow);font-size:12px">${r.unmatched.length} row(s) unmatched: ${escHtml(r.unmatched.slice(0, 5).join(', '))}${r.unmatched.length > 5 ? '…' : ''}</div>` : '';
    const emailMeta = (r.distributor === 'Email Inbox')
      ? ` · messages ${r.messages_parsed}/${r.messages_seen}`
        + (r.by_event_type
            ? ` · on_hand ${r.by_event_type.on_hand || 0} · restock ${r.by_event_type.restock || 0} · usage ${r.by_event_type.usage || 0}`
              + ((r.by_event_type.on_order || 0) ? ` · on_order ${r.by_event_type.on_order}` : '')
            : '')
      : '';
    const errorNote = r.error
      ? `<div style="padding:8px 20px;color:var(--red);font-size:12px">${escHtml(r.error)}</div>` : '';
    return `<div class="card">
      <div class="card-header">
        <span class="card-title">${distributorBadge(r.distributor === 'Email Inbox' ? '' : r.distributor)} ${r.distributor}</span>
        <span style="color:var(--muted);font-size:12px">
          source: ${escHtml(r.source)} · fetched ${r.fetched} · updated ${r.updated} · unchanged ${r.unchanged}${emailMeta}
        </span>
      </div>
      ${r.changes.length ? `<table>
        <thead><tr><th>SKU</th><th>Warehouse</th><th style="text-align:right">Was</th><th style="text-align:right">Now</th><th style="text-align:right">Δ</th></tr></thead>
        <tbody>${preview}</tbody>
      </table>` : '<div style="padding:16px 20px;color:var(--muted)">No changes.</div>'}
      ${moreNote}${unmatchedNote}${errorNote}
    </div>`;
  }).join('');
  return `<h2 style="font-size:16px;margin:0 0 12px 4px">${heading}${result.dry_run ? ' (dry run — nothing saved)' : ''}</h2>${cards}`;
}

// -------------------------------------------------------------------------
// Lot-row helpers used by the Distributors expandable rows.
function toggleLotRow(rowId, chev) {
  const row = document.getElementById(rowId);
  if (!row) return;
  if (row.style.display === 'none') {
    row.style.display = '';
    chev.textContent = '▾';
  } else {
    row.style.display = 'none';
    chev.textContent = '▸';
  }
}
// fmtCs — display case counts: integers stay clean (no '.00'); fractional
// values round to 2 decimals so the dashboard never shows long tails like
// '4.8171'. Used everywhere cs_remaining / cs_consumed / cs_produced are
// rendered in the lot detail table and FIFO summary line.
function fmtCs(n) {
  const v = Number(n);
  if (!isFinite(v)) return String(n);
  if (Math.abs(v - Math.round(v)) < 0.005) return String(Math.round(v));
  return v.toFixed(2);
}
function fmtProdDate(iso) {
  if (!iso) return '<span style="color:var(--muted)">—</span>';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[2]}/${m[3]}/${m[1]}` : iso;
}

// -------------------------------------------------------------------------
// Distributors (unified view)
// -------------------------------------------------------------------------
// loadDistributors() now aliases to loadInventory() (tabs merged 2026-05-14)
async function loadDistributors() { return loadInventory(); }


// -------------------------------------------------------------------------
// Usage Log
// -------------------------------------------------------------------------
// Usage Log — PO-grouped view with cs / lot # detail and pagination.
let usagePageSize = 10;
let usagePage = 0;
let usageView = 'chrono';      // 'chrono' (flat, newest-first) | 'grouped' (by PO)
let _usageCache = [];          // last fetched flat list
let _usageLotMap = {};         // {po_number: {variety: lot_number}}

// Toggle between the flat chronological feed (default) and the PO-grouped
// roll-up. Resets to page 1 and re-labels the pager so it reads correctly.
function setUsageView(view) {
  usageView = (view === 'grouped') ? 'grouped' : 'chrono';
  usagePage = 0;
  const cb = document.getElementById('usage-view-chrono');
  const gb = document.getElementById('usage-view-grouped');
  if (cb) cb.classList.toggle('active', usageView === 'chrono');
  if (gb) gb.classList.toggle('active', usageView === 'grouped');
  const psLabel = document.getElementById('usage-pagesize-label');
  if (psLabel) psLabel.textContent = usageView === 'grouped' ? 'POs / page' : 'rows / page';
  renderUsage();
}

// Dispatch to whichever view is active.
function renderUsage() {
  if (usageView === 'grouped') renderUsageGroups();
  else renderUsageChrono();
}

// Shared type filter for both views. Mirrors the Type dropdown:
//   restock  -> PO-arrival rollovers (amount < 0, source on_order_rollover)
//   use      -> consumption (amount > 0, non-reversal)
//   reversal -> reversal records
function _applyUsageTypeFilter(rows) {
  const typeF = document.getElementById('filter-usage-type')?.value || '';
  if (typeF === 'restock') return rows.filter(e => e.source !== 'reversal' && e.amount < 0 && e.source === 'on_order_rollover');
  if (typeF === 'use')      return rows.filter(e => e.source !== 'reversal' && e.amount > 0);
  if (typeF === 'reversal') return rows.filter(e => e.source === 'reversal');
  return rows;
}

function onUsageFilterChange() {
  usagePage = 0;
  loadUsage();
}
function onUsagePageSizeChange() {
  const v = parseInt(document.getElementById('usage-page-size').value, 10);
  usagePageSize = (v > 0 ? v : 10);
  usagePage = 0;
  renderUsage();
}
function onUsagePageNav(delta) {
  usagePage = Math.max(0, usagePage + delta);
  renderUsage();
}

function _updateUsagePager(first, last, total) {
  const info = document.getElementById('usage-pager-info');
  const ind  = document.getElementById('usage-page-indicator');
  const prev = document.getElementById('usage-page-prev');
  const next = document.getElementById('usage-page-next');
  if (!info || !ind || !prev || !next) return;
  if (total === 0) {
    info.textContent = 'No records';
    ind.textContent = '';
    prev.disabled = true;
    next.disabled = true;
    return;
  }
  const noun = usageView === 'grouped' ? 'PO groups' : 'entries';
  info.textContent = `Showing ${noun} ${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}`;
  const totalPages = Math.max(1, Math.ceil(total / usagePageSize));
  ind.textContent = `Page ${usagePage + 1} of ${totalPages}`;
  prev.disabled = usagePage <= 0;
  next.disabled = usagePage >= totalPages - 1;
}

function _varietyFromItemName(name) {
  return (name || '').split(' Bagel')[0];
}

async function loadUsage() {
  // Populate item filter dropdown from inventory.
  const inv = await api('/api/inventory');
  const sel = document.getElementById('filter-item');
  const current = sel.value;
  sel.innerHTML = '<option value="">All items</option>' +
    inv.map(i => `<option value="${escAttr(i.name)}" ${i.name === current ? 'selected' : ''}>${escHtml(i.name)}</option>`).join('');

  const name = document.getElementById('filter-item').value;
  const limit = document.getElementById('filter-limit').value || 500;
  const usageUrl = `/api/usage?limit=${limit}${name ? '&name=' + encodeURIComponent(name) : ''}`;

  // Fetch usage + production in parallel. Production gives us the
  // {po_number, variety -> lot_number} lookup we need to show lot
  // codes alongside the cs amounts.
  const [usage, production] = await Promise.all([
    api(usageUrl),
    api('/api/production').catch(() => []),
  ]);
  _usageCache = usage || [];

  // Build {po -> {variety -> lot}} from production records.
  const m = {};
  (production || []).forEach(r => {
    const po = (r.po_number || '').trim();
    if (!po) return;
    m[po] = m[po] || {};
    (r.lines || []).forEach(L => {
      const v = L.variety || '';
      if (!v) return;
      // Prefer the first non-empty lot we see; ingest order is
      // newest-first so this picks the most recent lot for that
      // PO/variety pair (which is what arrived).
      if (!m[po][v] && L.lot_number) m[po][v] = L.lot_number;
    });
  });
  _usageLotMap = m;

  renderUsage();
}

function renderUsageGroups() {
  const container = document.getElementById('usage-container');
  if (!container) return;

  // Apply type filter (shared with the chronological view). NB: on_order_rollover
  // entries deduct off "on order" while ADDING to qty, so amount < 0 reads as a
  // restock in the usage feed -- _applyUsageTypeFilter handles that mapping.
  let rows = _applyUsageTypeFilter(_usageCache.slice());

  // Group by po_number (fallback: 'Manual' for entries without a PO).
  const groups = {};
  rows.forEach(e => {
    const key = (e.po_number || '').trim() || '__manual__';
    (groups[key] = groups[key] || []).push(e);
  });

  // Convert to array sorted by latest timestamp in group, desc.
  const list = Object.keys(groups).map(k => {
    const entries = groups[k];
    entries.sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));
    const tsLatest = entries[0]?.timestamp || '';
    const tsEarliest = entries[entries.length - 1]?.timestamp || '';
    const totalCs = entries.reduce((s, e) => s + Math.abs(e.amount || 0), 0);
    return { key: k, entries, tsLatest, tsEarliest, totalCs };
  }).sort((a, b) => (a.tsLatest < b.tsLatest ? 1 : -1));

  if (list.length === 0) {
    container.innerHTML = '<div class="empty" style="padding:24px">No records match this filter.</div>';
    _updateUsagePager(0, 0, 0);
    return;
  }

  // Pagination
  const totalGroups = list.length;
  const totalPages = Math.max(1, Math.ceil(totalGroups / usagePageSize));
  if (usagePage >= totalPages) usagePage = totalPages - 1;
  if (usagePage < 0) usagePage = 0;
  const sliceStart = usagePage * usagePageSize;
  const sliceEnd = Math.min(sliceStart + usagePageSize, totalGroups);
  const paged = list.slice(sliceStart, sliceEnd);
  _updateUsagePager(sliceStart + 1, sliceEnd, totalGroups);

  container.innerHTML = paged.map(grp => {
    const isManual = grp.key === '__manual__';
    const label = isManual ? 'Manual usage / adjustments' : grp.key;
    const rowId = 'use-grp-' + Math.random().toString(36).slice(2, 10);
    const sample = grp.entries[0];
    const distGuess = (() => {
      // Best-effort distributor + location inference from any item_name
      // in the group. Item names look like "Plain Bagel 4oz [CB - Ocala]".
      const map = {
        CB: 'Cheney Brothers', USF: 'US Foods', CW: 'Chefs Warehouse',
        DB: 'DeliBag', CF: 'Carmela Foods', HH: 'H&H Bagels',
      };
      for (const e of grp.entries) {
        const m = /\[([A-Z]+)\s*-\s*([^\]]+)\]/.exec(e.item_name || '');
        if (m) {
          const full = map[m[1]] || m[1];
          const loc  = (m[2] || '').trim();
          return loc ? `${full} - ${loc}` : full;
        }
        // Fall-back: no bracket present (older entries)
        const n = e.item_name || '';
        if (n.includes('[CB'))  return 'Cheney Brothers';
        if (n.includes('[USF')) return 'US Foods';
        if (n.includes('[CW'))  return 'Chefs Warehouse';
        if (n.includes('[DB'))  return 'DeliBag';
        if (n.includes('[CF'))  return 'Carmela Foods';
      }
      return '';
    })();
    const tsRange = grp.tsLatest === grp.tsEarliest
      ? grp.tsLatest.replace('T', ' ').slice(0, 16)
      : `${grp.tsEarliest.replace('T',' ').slice(0,16)} → ${grp.tsLatest.replace('T',' ').slice(0,16)}`;
    // Counts
    const restockN = grp.entries.filter(e => e.source !== 'reversal' && e.amount < 0).length;
    const useN     = grp.entries.filter(e => e.source !== 'reversal' && e.amount > 0).length;
    const revN     = grp.entries.filter(e => e.source === 'reversal').length;
    const typeBits = [];
    if (restockN) typeBits.push(`<span class="badge badge-green">▲ ${restockN} restock${restockN===1?'':'s'}</span>`);
    if (useN)     typeBits.push(`<span class="badge badge-red">▼ ${useN} use${useN===1?'':'s'}</span>`);
    if (revN)     typeBits.push(`<span class="badge badge-purple">↶ ${revN} reversal${revN===1?'':'s'}</span>`);

    // Per-line detail rows
    const lotsForPo = !isManual ? (_usageLotMap[grp.key] || {}) : {};
    const detailRows = grp.entries.map(e => {
      const ts = e.timestamp.replace('T',' ').slice(0,19);
      const variety = _varietyFromItemName(e.item_name);
      const lot = lotsForPo[variety] || '';
      const isUse = e.amount > 0 && e.source !== 'reversal';
      const isReversal = e.source === 'reversal';
      const reversed = !!e.reversed;
      let typeBadge;
      if (isReversal) typeBadge = `<span class="badge badge-purple">↶ Reversal</span>`;
      else if (isUse) typeBadge = `<span class="badge badge-red">▼ Use</span>`;
      else            typeBadge = `<span class="badge badge-green">▲ Restock</span>`;
      const amt = `${e.amount < 0 ? '+' : '-'}${Math.abs(e.amount).toFixed(0)} ${escHtml(e.unit || 'cs')}`;
      let act = '<span style="color:var(--muted);font-size:11px">—</span>';
      if (!isReversal && !reversed) {
        act = `<button class="btn btn-ghost btn-sm" onclick="reverseActivity('${escAttr(e.timestamp)}')" title="Undo this entry">↶ Reverse</button>`;
      }
      const rowStyle = (reversed || isReversal) ? ' style="opacity:.7"' : '';
      return `<tr${rowStyle}>
        <td style="color:var(--muted);font-size:11px;white-space:nowrap">${ts}</td>
        <td>${escHtml(e.item_name)}</td>
        <td>${typeBadge}</td>
        <td style="text-align:right;font-weight:600;${e.amount<0?'color:var(--success,#0a0)':'color:var(--red)'}">${amt}</td>
        <td style="font-family:ui-monospace,monospace;font-size:12px">${lot ? escHtml(lot) : '<span style="color:var(--muted)">—</span>'}</td>
        <td style="color:var(--muted);font-size:11px">${escHtml(e.note || '')}${reversed ? ' <span class="badge badge-yellow" style="margin-left:6px">reversed</span>' : ''}</td>
        <td class="actions-col">${act}</td>
      </tr>`;
    }).join('');

    return `<div style="border-top:1px solid var(--border)">
      <div style="padding:10px 16px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none" onclick="(function(el){const r=el.nextElementSibling; if(r.style.display==='none'){r.style.display='';el.querySelector('.chev').textContent='▾';} else {r.style.display='none';el.querySelector('.chev').textContent='▸';}})(this)">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="chev" style="color:var(--muted);width:14px">▸</span>
          <span style="font-weight:600;font-family:ui-monospace,monospace">${isManual ? '—' : escHtml(grp.key)}</span>
          ${distGuess ? `<span style="color:var(--muted);font-size:12px">${escHtml(distGuess)}</span>` : ''}
          ${typeBits.join(' ')}
        </div>
        <div style="display:flex;align-items:center;gap:14px;font-size:12px">
          <span style="color:var(--muted)">${tsRange}</span>
          <span style="font-weight:600">${grp.totalCs.toFixed(0)} cs</span>
          <span style="color:var(--muted)">· ${grp.entries.length} line${grp.entries.length===1?'':'s'}</span>
        </div>
      </div>
      <div id="${rowId}" style="display:none;padding:0 16px 12px 32px">
        <table style="margin:0;font-size:12px">
          <thead><tr>
            <th>Timestamp</th><th>Item</th><th>Type</th>
            <th style="text-align:right">Amount</th>
            <th>Lot #</th><th>Note</th><th>Actions</th>
          </tr></thead>
          <tbody>${detailRows}</tbody>
        </table>
      </div>
    </div>`;
  }).join('');
}


// Flat, newest-first activity feed -- the old Dashboard "Recent Activity"
// view, merged in as the default Usage Log view. Reuses renderActivityRow so
// the row markup + Reverse button match, and paginates by entry (vs. the
// grouped view, which paginates by PO).
function renderUsageChrono() {
  const container = document.getElementById('usage-container');
  if (!container) return;

  let rows = _applyUsageTypeFilter(_usageCache.slice());
  rows.sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));

  const total = rows.length;
  if (total === 0) {
    container.innerHTML = '<div class="empty" style="padding:24px">No movements match this filter.</div>';
    _updateUsagePager(0, 0, 0);
    return;
  }

  const totalPages = Math.max(1, Math.ceil(total / usagePageSize));
  if (usagePage >= totalPages) usagePage = totalPages - 1;
  if (usagePage < 0) usagePage = 0;
  const start = usagePage * usagePageSize;
  const end = Math.min(start + usagePageSize, total);
  const paged = rows.slice(start, end);
  _updateUsagePager(start + 1, end, total);

  container.innerHTML = `
    <table>
      <thead><tr>
        <th>Time</th><th>Item</th><th>Type</th>
        <th>Amount</th><th>Note</th><th>Actions</th>
      </tr></thead>
      <tbody>${paged.map(e => renderActivityRow(e, 6)).join('')}</tbody>
    </table>`;
}


// -------------------------------------------------------------------------
// Report
// -------------------------------------------------------------------------
// Production cache shared by the Top Variety by Location chart. Records
// come from /api/production (= what was BAKED per warehouse).
let vrankProdCache = [];

async function loadReport() {
  // Bakery Sales vs. Labor sits at the top of the page now -- kick it off
  // first so it has data on screen by the time the lower cards finish.
  loadPlhChart();
  // Toast sales feed (Top Consumed card)
  loadToastSales();
  const [report, prod] = await Promise.all([
    api('/api/report'),
    api('/api/production'),
  ]);
  vrankProdCache = prod || [];

  // Populate the Top Variety warehouse dropdown immediately after
  // production data loads. Run it BEFORE any other render so a later
  // error can't block it.
  try { _populateRestockWarehouseDropdown(); } catch (e) { console.warn('warehouse dropdown:', e); }

  // Sync date input + render Top Variety chart up front, again with a
  // try/catch so renderChart issues below don't prevent it.
  try { _restockSyncDateInput(); renderRestockedByVarietyLocation(); }
  catch (e) { console.warn('renderRestockedByVarietyLocation:', e); }

  function renderChart(containerId, data, color) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!data || !data.length) { el.innerHTML = '<div class="empty">No data yet.</div>'; return; }
    const max = data[0].total || 1;
    el.innerHTML = data.map(d => `
      <div class="chart-bar-row">
        <div class="chart-bar-label" title="${escHtml(d.name)}">${escHtml(d.name)}</div>
        <div class="chart-bar-track">
          <div class="chart-bar-fill" style="width:${Math.round(d.total/max*100)}%;background:${color}"></div>
        </div>
        <div class="chart-bar-val">${d.total.toFixed(1)} ${escHtml(d.unit)}</div>
      </div>`).join('');
  }

  try { renderChart('chart-consumed', report.top_consumed || [], 'var(--red)'); }
  catch (e) { console.warn('chart-consumed:', e); }

}

// ---- Top Variety by Location: variety × location, weekly/monthly ----------
let restockPeriod = 'week';   // 'week' | 'month'

// Anchor date drives which window is shown. Default = a date inside
// the LAST COMPLETED week (= 7 days ago), so the first render lands on
// the most recent Mon-Sun window with full data, not the current
// partial week.
function _restockDefaultAnchor() {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return d.toISOString().slice(0, 10);
}
let restockRefDate = _restockDefaultAnchor();

// Optional warehouse filter. Empty string = show all warehouses.
let restockWarehouse = '';

function setRestockPeriod(p) {
  restockPeriod = p;
  ['week','month','year'].forEach(m => {
    const btn = document.getElementById('restock-period-' + m);
    if (btn) btn.classList.toggle('active', m === p);
  });
  // Snap the date picker to the current window's anchor so the user
  // sees the active period reflected on the input itself.
  _restockSyncDateInput();
  renderRestockedByVarietyLocation();
}
function _restockRef() {
  if (restockRefDate) return new Date(restockRefDate + 'T00:00:00');
  return new Date();
}
function _restockSyncDateInput() {
  const inp = document.getElementById('restock-ref-date');
  if (!inp) return;
  const [since] = _restockWindow();
  inp.value = since;
}
function shiftRestockOffset(delta) {
  const ref = _restockRef();
  if (restockPeriod === 'week') {
    ref.setDate(ref.getDate() + 7 * delta);
  } else if (restockPeriod === 'year') {
    ref.setFullYear(ref.getFullYear() + delta);
  } else {
    ref.setMonth(ref.getMonth() + delta);
  }
  restockRefDate = ref.toISOString().slice(0, 10);
  _restockSyncDateInput();
  renderRestockedByVarietyLocation();
}
function resetRestockOffset() {
  // Today = the period containing today (current week or month).
  restockRefDate = new Date().toISOString().slice(0, 10);
  _restockSyncDateInput();
  renderRestockedByVarietyLocation();
}

function onRestockWarehouseChange() {
  const sel = document.getElementById('restock-warehouse');
  restockWarehouse = sel ? sel.value : '';
  renderRestockedByVarietyLocation();
}
function onRestockRefDateChange() {
  const v = document.getElementById('restock-ref-date').value;
  if (v) {
    restockRefDate = v;
    renderRestockedByVarietyLocation();
  }
}

function _restockWindow() {
  const ref = _restockRef();
  if (restockPeriod === 'week') {
    const day = ref.getDay() || 7;
    const monday = new Date(ref);
    monday.setDate(ref.getDate() - (day - 1));
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    return [monday.toISOString().slice(0, 10), sunday.toISOString().slice(0, 10)];
  }
  if (restockPeriod === 'year') {
    const start = new Date(ref.getFullYear(), 0,  1);
    const end   = new Date(ref.getFullYear(), 11, 31);
    return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
  }
  const start = new Date(ref.getFullYear(), ref.getMonth(), 1);
  const end   = new Date(ref.getFullYear(), ref.getMonth() + 1, 0);
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}

let plhGrain = 'week';        // 'week' | 'month' | 'quarter'
let plhOffset = 0;            // 0 = current window; step is 1 week / 1 month / 1 year
let plhSelectedIndex = null;  // null = window totals; int = drill into that bucket
let _lastPlhData = null;      // cached response so we can re-render KPIs on click
let plhChart = null;

function setPlhGrain(g) {
  plhGrain = g;
  plhOffset = 0;
  plhSelectedIndex = null;
  ['week','month','quarter'].forEach(m => {
    const btn = document.getElementById('plh-grain-' + m);
    if (btn) btn.classList.toggle('active', m === g);
  });
  loadPlhChart();
}

async function loadPlhChart() {
  const data = await api('/api/report/plh?grain=' + encodeURIComponent(plhGrain) +
                       '&offset=' + encodeURIComponent(plhOffset));
  // Defer paint to the next frame so the canvas has real dimensions if the
  // page just transitioned from display:none. Without this, the first
  // chart paint after opening the Report page can render to a 0x0 canvas
  // and show nothing until the user clicks a button.
  const paint = () => {
    const canvas = document.getElementById('plh-chart');
    if (!canvas || typeof Chart === 'undefined') {
      // Try again shortly if Chart.js or the canvas isn't ready yet.
      setTimeout(paint, 60);
      return;
    }
    const w = canvas.clientWidth || canvas.offsetWidth || 0;
    if (w < 10) {
      // Canvas not laid out yet -- come back next frame.
      requestAnimationFrame(paint);
      return;
    }
    _lastPlhData = data;
    // If the trimmed/changed window shrank past the selected index, drop it.
    if (plhSelectedIndex != null) {
      const n = (data.buckets || []).length;
      if (plhSelectedIndex < 0 || plhSelectedIndex >= n) plhSelectedIndex = null;
    }
    renderPlhChart(data);
    renderPlhSummary(data);
    renderPlhWindowNav(data);
  };
  requestAnimationFrame(paint);
}

function renderPlhWindowNav(data) {
  const lbl = document.getElementById('plh-window-label');
  if (lbl) lbl.textContent = data && data.window_label ? data.window_label : '';
  const nextBtn = document.getElementById('plh-next');
  if (nextBtn) {
    if (plhOffset <= 0) {
      nextBtn.disabled = true;
      nextBtn.classList.add('disabled');
    } else {
      nextBtn.disabled = false;
      nextBtn.classList.remove('disabled');
    }
  }
  const todayBtn = document.getElementById('plh-today');
  if (todayBtn) {
    if (plhOffset === 0) {
      todayBtn.disabled = true;
      todayBtn.classList.add('disabled');
    } else {
      todayBtn.disabled = false;
      todayBtn.classList.remove('disabled');
    }
  }
}

function shiftPlh(delta) {
  plhOffset = Math.max(0, plhOffset + delta);
  plhSelectedIndex = null;
  loadPlhChart();
}

function resetPlh() {
  if (plhOffset === 0 && plhSelectedIndex == null) return;
  plhOffset = 0;
  plhSelectedIndex = null;
  loadPlhChart();
}

// Toggle bucket selection from a chart click or a programmatic source.
function selectPlhBucket(idx) {
  plhSelectedIndex = (plhSelectedIndex === idx) ? null : idx;
  if (_lastPlhData) {
    // Re-render the bars (so the highlight updates) and the summary
    // (so it switches between window totals and bucket detail).
    renderPlhChart(_lastPlhData);
    renderPlhSummary(_lastPlhData);
  }
}
function clearPlhSelection() {
  if (plhSelectedIndex == null) return;
  plhSelectedIndex = null;
  if (_lastPlhData) {
    renderPlhChart(_lastPlhData);
    renderPlhSummary(_lastPlhData);
  }
}

function _fmtUSD(n) {
  return new Intl.NumberFormat('en-US',
    { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n || 0);
}
function _fmtUSDFine(n) {
  return new Intl.NumberFormat('en-US',
    { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n || 0);
}

function renderPlhChart(data) {
  const canvas = document.getElementById('plh-chart');
  if (!canvas || typeof Chart === 'undefined') return;
  const buckets = data.buckets || [];
  const labels       = buckets.map(b => b.label || b.key);
  const salesSeries  = buckets.map(b => b.bakery_sales_dollars || 0);
  const splhSeries   = buckets.map(b =>
    b.splh == null ? null : Math.round(b.splh * 100) / 100);
  // Labor % of sales -- plotted as a percent (0..100) on its own axis.
  
  const laborPctSeries = buckets.map(b =>
    b.labor_pct_of_sales == null
      ? null
      : Math.round(b.labor_pct_of_sales * 1000) / 10);  // 0.437 -> 43.7

  if (plhChart) { plhChart.destroy(); plhChart = null; }
  plhChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          type: 'bar',
          label: 'Bakery sales',
          data: salesSeries,
          backgroundColor: buckets.map((_, i) =>
            i === plhSelectedIndex ? 'rgba(236, 80, 57, 0.85)'   // accent red
                                   : 'rgba(5, 23, 71, 0.78)'),   // brand navy
          borderColor: buckets.map((_, i) =>
            i === plhSelectedIndex ? 'rgba(236, 80, 57, 1)'
                                   : 'rgba(5, 23, 71, 0.95)'),
          borderWidth: buckets.map((_, i) => i === plhSelectedIndex ? 2.5 : 1),
          yAxisID: 'ySales',
          order: 3,
        },
        {
          type: 'line',
          label: '$ sales per labor hour',
          data: splhSeries,
          borderColor: 'rgba(220, 38, 38, 0.95)',       // red
          backgroundColor: 'rgba(220, 38, 38, 0.15)',
          tension: 0.25,
          pointRadius: 4,
          yAxisID: 'ySplh',
          spanGaps: true,
          order: 1,
        },
        {
          type: 'line',
          label: 'Labor % of sales',
          data: laborPctSeries,
          borderColor: 'rgba(217, 154, 24, 0.95)',      // amber
          backgroundColor: 'rgba(217, 154, 24, 0.15)',
          tension: 0.25,
          pointRadius: 4,
          yAxisID: 'yPct',
          spanGaps: true,
          order: 2,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      onHover: (event, elements, chart) => {
        // Show a pointer on bars so the user knows they're clickable.
        chart.canvas.style.cursor = elements.length ? 'pointer' : '';
      },
      onClick: (event, elements) => {
        // Use Chart.js's hit-test rather than just the first element so a
        // click anywhere along the index (lines + bars stack on top of
        // each other) selects the correct bucket.
        const hit = (elements && elements.length) ? elements[0].index : null;
        if (hit != null) selectPlhBucket(hit);
      },
      scales: {
        ySales: {
          beginAtZero: true,
          position: 'left',
          title: { display: true, text: 'Bakery sales ($)' },
          ticks: {
            callback: v => '$' + Number(v).toLocaleString(),
          },
        },
        ySplh: {
          beginAtZero: true,
          position: 'right',
          grid: { drawOnChartArea: false },
          title: { display: true, text: '$ sales / labor hour' },
          ticks: {
            callback: v => '$' + Number(v).toFixed(0),
          },
        },
        yPct: {
          beginAtZero: true,
          position: 'right',
          // Cap y-axis around the worst observed week + a little headroom.
          // Computed dynamically so a 50% labor-cost week is still on chart.
          suggestedMax: Math.max(
            10,
            Math.ceil(Math.max(...laborPctSeries.filter(v => v != null), 0) + 10)
          ),
          offset: true,
          grid: { drawOnChartArea: false },
          title: { display: true, text: 'Labor % of sales' },
          ticks: {
            callback: v => Number(v).toFixed(0) + '%',
          },
        },
      },
      plugins: {
        tooltip: {
          callbacks: {
            label(ctx) {
              // Default label formatting per series.
              const lbl = ctx.dataset.label || '';
              const v   = ctx.parsed.y;
              if (v == null) return `${lbl}: --`;
              if (lbl.startsWith('Bakery sales'))
                return `${lbl}: ${_fmtUSD(v)}`;
              if (lbl.startsWith('$ sales per labor hour'))
                return `${lbl}: ${_fmtUSDFine(v)}`;
              if (lbl.startsWith('Labor %'))
                return `${lbl}: ${v.toFixed(1)}%`;
              return `${lbl}: ${v}`;
            },
            afterBody(ctx) {
              const i = ctx[0].dataIndex;
              const b = buckets[i] || {};
              const out = [];
              const bxH = b.bakery_xlsx_labor_hours || 0;
              const bxD = b.bakery_xlsx_labor_dollars || 0;
              if (bxH || bxD) {
                out.push(`Labor (bakery model): ${bxH.toFixed(1)} hrs · ${_fmtUSD(bxD)}`);
              }
              return out;
            },
          },
        },
        legend: { position: 'top', labels: { boxWidth: 16 } },
      },
    },
  });
}

function renderPlhSummary(data) {
  const wrap = document.getElementById('plh-summary');
  if (!wrap) return;
  const buckets = data.buckets || [];

  // Build a "totals" object scoped either to the whole window (default)
  // or to the single bucket the user clicked on. SPLH + labor % use
  // bakery-model labor only, matching the workbook (I11 hours, O10 SPLH).
  const selected = (plhSelectedIndex != null && buckets[plhSelectedIndex])
    ? buckets[plhSelectedIndex]
    : null;

  let total, scopeLabel, headerHtml;
  if (selected) {
    total = {
      cs:    selected.total_cs || 0,
      hrs:   selected.bakery_xlsx_labor_hours   || 0,
      cost:  selected.bakery_xlsx_labor_dollars || 0,
      sales: selected.bakery_sales_dollars || 0,
      byCh:  Object.assign({}, selected.bakery_sales_by_channel || {}),
    };
    scopeLabel = selected.label || selected.key || 'Selected period';
    headerHtml = `
      <div style="grid-column:1 / -1;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px">
        <div style="font-size:13px;color:var(--muted)">
          Showing detail for <strong style="color:var(--text)">${escHtml(scopeLabel)}</strong>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="clearPlhSelection()">
          Show window total
        </button>
      </div>`;
  } else {
    total = buckets.reduce((acc, b) => {
      acc.cs    += (b.total_cs || 0);
      acc.hrs   += (b.bakery_xlsx_labor_hours   || 0);
      acc.cost  += (b.bakery_xlsx_labor_dollars || 0);
      acc.sales += (b.bakery_sales_dollars || 0);
      for (const [ch, v] of Object.entries(b.bakery_sales_by_channel || {})) {
        acc.byCh[ch] = (acc.byCh[ch] || 0) + (Number(v) || 0);
      }
      return acc;
    }, { cs: 0, hrs: 0, cost: 0, sales: 0, byCh: {} });
    scopeLabel = data.window_label || 'Window';
    headerHtml = `
      <div style="grid-column:1 / -1;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px">
        <div style="font-size:13px;color:var(--muted)">
          Window total &middot; <strong style="color:var(--text)">${escHtml(scopeLabel)}</strong>
          ${buckets.length > 1 ? '&nbsp;&nbsp;<span style="font-size:12px">Click a bar to drill into a single period.</span>' : ''}
        </div>
        <div></div>
      </div>`;
  }
  const splh    = total.hrs   ? total.sales / total.hrs : null;
  const laborPc = total.sales ? total.cost  / total.sales : null;

  // Headline cards (the three the user wants front-and-center) get the
  // 'kpi-hero' tag; everything else uses the default style.
  const cards = [
    { hero: true,  label: 'Bakery sales (window)', value: total.sales ? _fmtUSD(total.sales) : '—' },
    { hero: true,  label: '$SPLH (sales/hr)',      value: splh == null ? '—' : _fmtUSDFine(splh) },
    { hero: true,  label: 'Labor % of sales',
      value: laborPc == null ? '—' : (laborPc * 100).toFixed(1) + '%' },
    { hero: false, label: 'Labor cost',            value: _fmtUSD(total.cost) },
    { hero: false, label: 'Labor hours',           value: total.hrs ? total.hrs.toFixed(1) : '—' },
    { hero: false, label: 'Cases produced',        value: total.cs.toLocaleString() },
  ];

  // Channel breakdown card -- spans full row, lists each channel + $.
  const chEntries = Object.entries(total.byCh)
    .sort((a, b) => b[1] - a[1]);
  const channelHtml = chEntries.length
    ? `
      <div class="card" style="padding:12px 14px;grid-column:1 / -1">
        <div style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">
          Bakery sales by channel (window)
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:18px;font-size:14px">
          ${chEntries.map(([ch, v]) => `
            <div>
              <div style="color:var(--muted);font-size:11px">${escHtml(ch)}</div>
              <div style="font-weight:600">${escHtml(_fmtUSD(v))}
                <span style="color:var(--muted);font-weight:400;font-size:12px">
                  (${total.sales ? ((v / total.sales) * 100).toFixed(1) : '0.0'}%)
                </span>
              </div>
            </div>`).join('')}
        </div>
      </div>`
    : '';

  wrap.innerHTML = headerHtml + cards.map(c => {
    const heroStyle = c.hero
      ? 'padding:14px 16px;background:var(--surface);border:1px solid var(--border);box-shadow:0 1px 2px rgba(5,23,71,0.04)'
      : 'padding:12px 14px';
    const valueStyle = c.hero
      ? 'font-size:26px;font-weight:800;letter-spacing:-0.01em'
      : 'font-size:20px;font-weight:700';
    let toneColor = '';
    if (c.tone === 'green') toneColor = 'color:var(--green)';
    else if (c.tone === 'amber') toneColor = 'color:#B07F0E';
    else if (c.tone === 'red') toneColor = 'color:var(--red)';
    return `
      <div class="card" style="${heroStyle}">
        <div style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.5px">${escHtml(c.label)}</div>
        <div style="${valueStyle};${toneColor}">${escHtml(c.value)}</div>
      </div>`;
  }).join('') + channelHtml;
}

// Collapse warehouse aliases so the dropdown and chart don't show
// duplicate buckets for the same physical location. Add to this map
// whenever a new shorthand appears in production data.
const _RESTOCK_WAREHOUSE_ALIASES = {
  'CWF':   'Chefs Warehouse, FL',
  'CWFL':  'Chefs Warehouse, FL',
  'CWNY':  'Chefs Warehouse, NY',
  'CWC':   'Chefs Warehouse, Chicago',
  'CWMID': 'Chefs Warehouse, Mid-Atlantic',
};
function _normalizeWarehouseName(raw) {
  if (!raw) return '';
  const t = String(raw).trim();
  return _RESTOCK_WAREHOUSE_ALIASES[t.toUpperCase()] || t;
}

// Map raw_variety patterns (PARB-EVERYTHING, PLAIN SLICED, CINN RAISIN,
// JALAPENO-CHEDDAR, etc.) back to a canonical flavor name. Used when
// the structured variety field is missing or set to a non-flavor
// sentinel like "In-House Inventory" — the parser sometimes leaves
// that placeholder when it can't classify a line.
const _VARIETY_PATTERNS = [
  // order matters: more specific first
  { rx: /JALAPENO[\s\-]*CHEDDAR/i, name: 'Jalapeno Cheddar' },
  { rx: /WHOLE[\s\-]*WHEAT[\s\-]*EVERYTHING/i, name: 'Whole Wheat Everything' },
  { rx: /WHOLE[\s\-]*WHEAT/i, name: 'Whole Wheat' },
  { rx: /CINN[\.\s\-]*RAISIN/i, name: 'Cinnamon Raisin' },
  { rx: /CINNAMON[\s\-]*RAISIN/i, name: 'Cinnamon Raisin' },
  // Bare "CINNAMON" (e.g. PARB-CINNAMON) collapses into Cinnamon
  // Raisin - that's the only cinnamon-bearing bagel variety on the
  // H&H menu, and the parser sometimes drops the "RAISIN" suffix.
  { rx: /CINNAMON/i, name: 'Cinnamon Raisin' },
  { rx: /BLUEBERRY/i, name: 'Blueberry' },
  { rx: /POPPY/i, name: 'Poppy Seed' },
  { rx: /SESAME/i, name: 'Sesame' },
  { rx: /ASIAGO/i, name: 'Asiago' },
  { rx: /PUMPERNICKEL/i, name: 'Pumpernickel' },
  { rx: /ONION/i, name: 'Onion' },
  { rx: /EVERYTHING/i, name: 'Everything' },
  { rx: /\bEGG\b/i, name: 'Egg' },
  { rx: /\bPLAIN\b/i, name: 'Plain' },
];

// Variety strings that aren't real flavors. Anything matching these
// gets routed through raw_variety pattern matching; if that fails we
// bucket as "Other" so the chart never shows them as a top variety.
const _VARIETY_BLOCKLIST = new Set([
  'in-house inventory',
  'in house inventory',
  'unknown',
  'total',
  'assorted',
  'asst',
  'mini asst',
  '',
]);

// Canonical aliases applied to the final variety name after pattern
// matching - lets us consolidate real-flavor strings that should map
// onto a parent variety. (e.g. "Cinnamon" on its own means Cinnamon
// Raisin in the H&H menu - there is no plain Cinnamon bagel.)
const _VARIETY_ALIASES = {
  'cinnamon': 'Cinnamon Raisin',
};

function _normalizeVarietyName(variety, raw_variety) {
  const cooked = String(variety || '').trim();
  const cookedLower = cooked.toLowerCase();
  const isBadCooked = _VARIETY_BLOCKLIST.has(cookedLower);
  // If the structured variety is a real flavor (not blocklisted), use
  // it - but still pass through the alias map in case it's a synonym
  // of another variety.
  if (cooked && !isBadCooked) {
    return _VARIETY_ALIASES[cookedLower] || cooked;
  }
  // Try to extract a known flavor from the raw_variety string.
  const raw = String(raw_variety || '').trim();
  if (raw) {
    for (const p of _VARIETY_PATTERNS) {
      if (p.rx.test(raw)) {
        return _VARIETY_ALIASES[p.name.toLowerCase()] || p.name;
      }
    }
  }
  return 'Other';
}

function _populateRestockWarehouseDropdown() {
  const sel = document.getElementById('restock-warehouse');
  if (!sel) return;
  // Collect unique normalized warehouses across the full production
  // cache so the user can pick any one even if it had no rows in the
  // current window.
  const names = new Set();
  (vrankProdCache || []).forEach(r => {
    const wh = _normalizeWarehouseName(r.warehouse || r.warehouse_raw || '');
    if (wh) names.add(wh);
  });
  const sorted = [...names].sort((a, b) => a.localeCompare(b));
  const current = restockWarehouse;
  sel.innerHTML = '<option value="">All warehouses</option>' +
    sorted.map(n => `<option value="${escAttr(n)}"${n === current ? ' selected' : ''}>${escHtml(n)}</option>`).join('');
}

function renderRestockedByVarietyLocation() {
  const el = document.getElementById('chart-restocked');
  if (!el) return;

  _populateRestockWarehouseDropdown();

  const [since, until] = _restockWindow();
  const lbl = document.getElementById('restock-range-label');
  if (lbl) {
    const whNote = restockWarehouse ? ` · ${escHtml(restockWarehouse)}` : ' · all warehouses';
    const periodWord = restockPeriod === 'year'  ? 'one year, broken into quarters'
                     : restockPeriod === 'month' ? 'one month'
                     : 'one week';
    lbl.innerHTML = `Showing ${formatDate(since)} \u2013 ${formatDate(until)} (${periodWord})${whNote}`;
  }
  // Filter production records to the window (and warehouse if set)
  const records = (vrankProdCache || []).filter(r => {
    const d = (r.production_date || '').slice(0, 10);
    if (!d) return false;
    if (since && d < since) return false;
    if (until && d > until) return false;
    if (restockWarehouse) {
      const wh = _normalizeWarehouseName(r.warehouse || r.warehouse_raw || '');
      if (wh !== restockWarehouse) return false;
    }
    return true;
  });

  if (records.length === 0) {
    const where = restockWarehouse ? ` at ${escHtml(restockWarehouse)}` : '';
    el.innerHTML = `<div class="empty">No production logged in this window${where}.</div>`;
    return;
  }

  // Variety -> { total, byWarehouse, byQuarter }. variety is the
  // canonical flavor name; "In-House Inventory" and other placeholder
  // values get re-mapped from raw_variety so they don't appear as
  // their own row. byQuarter is only populated in 'year' mode so we
  // can render the Q1/Q2/Q3/Q4 mini-breakdown inline.
  const byVariety = {};
  let grandTotal = 0;
  records.forEach(r => {
    const wh = _normalizeWarehouseName(r.warehouse || r.warehouse_raw || '') || 'Unassigned';
    const dateStr = (r.production_date || '').slice(0, 10);
    const m = parseInt((dateStr.split('-')[1] || '0'), 10);  // 1..12
    const qIdx = m ? Math.floor((m - 1) / 3) : -1;            // 0..3
    (r.lines || []).forEach(L => {
      const v = _normalizeVarietyName(L.variety, L.raw_variety);
      const cs = Number(L.cs_count) || 0;
      if (cs <= 0) return;
      const slot = byVariety[v] = byVariety[v] || {
        variety: v, total: 0, byWh: {}, byQuarter: [0, 0, 0, 0],
      };
      slot.total += cs;
      slot.byWh[wh] = (slot.byWh[wh] || 0) + cs;
      if (qIdx >= 0) slot.byQuarter[qIdx] += cs;
      grandTotal += cs;
    });
  });

  // Sort varieties by total cs descending, but always pin the
  // "Other" bucket to the very bottom - it's an aggregation of rows
  // we couldn't classify, not a true bagel variety. The user wanted
  // it visible (for transparency) but not competing with real
  // flavors in the ranking.
  const sorted = Object.values(byVariety).sort((a, b) => {
    if (a.variety === 'Other' && b.variety !== 'Other') return  1;
    if (b.variety === 'Other' && a.variety !== 'Other') return -1;
    return b.total - a.total;
  });
  // overallMax uses the largest non-"Other" total so the bar widths
  // reflect real-flavor scale, not a possibly outsized Other bucket.
  const overallMax = (sorted.find(v => v.variety !== 'Other') || sorted[0] || { total: 1 }).total || 1;

  el.innerHTML = sorted.map((v, i) => {
    const barPct = Math.round((v.total / overallMax) * 100);
    // Stack rank position. The "Other" bucket is intentionally pinned
    // below the ranking, so it gets a dash instead of a number to keep
    // the rank sequence honest (real flavors number 1..N).
    const rankLabel = (v.variety === 'Other') ? '—' : `${i + 1}.`;

    // Breakdown line: quarter view in 'year' mode, warehouse list otherwise.
    let breakdown, subtitle;
    if (restockPeriod === 'year') {
      // Q1-Q4 mini-bars sized against the largest quarter for THIS variety
      // so users can eyeball seasonality (which quarters skew high vs low).
      const qMax = Math.max(...v.byQuarter) || 1;
      const parts = ['Q1','Q2','Q3','Q4'].map((lbl, i) => {
        const n     = v.byQuarter[i];
        const pct   = Math.round((n / qMax) * 100);
        const share = v.total ? Math.round((n / v.total) * 100) : 0;
        return `<span title="${lbl}: ${n} cs (${share}% of variety total)" style="display:inline-flex;align-items:center;gap:4px;white-space:nowrap">
          <span style="color:var(--muted);font-weight:600">${lbl}</span>
          <span style="display:inline-block;width:36px;height:6px;background:var(--border);border-radius:3px;overflow:hidden;vertical-align:middle">
            <span style="display:block;width:${pct}%;height:100%;background:var(--green)"></span>
          </span>
          <span style="color:var(--muted)">${n}</span>
        </span>`;
      }).join('&nbsp;&nbsp;');
      breakdown = parts;
      subtitle = '4 quarters';
    } else {
      const sortedWh = Object.entries(v.byWh).sort((a, b) => b[1] - a[1]);
      const head = sortedWh.slice(0, 4).map(([w, n]) =>
        `<span style="white-space:nowrap">${escHtml(w)}&nbsp;<span style="color:var(--muted)">${n}</span></span>`
      ).join(' · ');
      const more = sortedWh.length > 4
        ? ` · <span style="color:var(--muted)">+${sortedWh.length - 4} more</span>` : '';
      breakdown = head + more;
      subtitle = `${sortedWh.length} location${sortedWh.length === 1 ? '' : 's'}`;
    }

    const isOther = v.variety === 'Other';
    const labelTitle = isOther
      ? 'Production rows whose variety couldn\'t be classified (ASSORTED, MINI ASST, TOTAL, sliced totals, or blank lines). Working to resolve the underlying parser ambiguity so these get attributed to a real bagel variety.'
      : v.variety;
    const labelSubtitle = isOther
      ? '<span style="font-style:italic">unclassified rows</span>'
      : subtitle;
    return `<div class="chart-bar-row" style="align-items:flex-start;gap:12px;padding:6px 0${isOther ? ';opacity:0.75' : ''}">
      <div class="chart-bar-label" title="${escHtml(labelTitle)}" style="min-width:140px">
        <div style="font-weight:600;display:flex;align-items:baseline;gap:6px">
          <span style="color:var(--muted);font-weight:500;min-width:22px;text-align:right;font-variant-numeric:tabular-nums">${rankLabel}</span>
          <span>${escHtml(v.variety)}</span>
        </div>
        <div style="color:var(--muted);font-size:11px;margin-top:2px;padding-left:28px">${labelSubtitle}</div>
      </div>
      <div style="flex:1;min-width:120px">
        <div class="chart-bar-track">
          <div class="chart-bar-fill" style="width:${barPct}%;background:var(--green)"></div>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${breakdown}</div>
      </div>
      <div class="chart-bar-val">${v.total.toFixed(0)} <span style="color:var(--muted);font-size:11px">cs &middot; ${grandTotal ? ((v.total / grandTotal) * 100).toFixed(1) : '0.0'}%</span></div>
    </div>`;
  }).join('');
}

// -------------------------------------------------------------------------
// Modals – Add / Edit item
// -------------------------------------------------------------------------
async function openAddModal() {
  await ensureWarehouses();
  editingName = null;
  document.getElementById('modal-title').textContent = 'Add Item';
  ['f-name','f-qty','f-unit','f-cat','f-price','f-threshold',
   'f-case-cost','f-case-size','f-weekly'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('f-distributor').value = '';
  populateWarehouseSelect(document.getElementById('f-warehouse'), '', '', false);
  document.getElementById('f-name').removeAttribute('readonly');
  document.getElementById('item-modal').classList.add('open');
  document.getElementById('f-name').focus();
}

async function openEditModal(name) {
  await ensureWarehouses();
  // Resolve the item from the cache; refetch if it isn't there yet.
  let item = findCachedItem(name);
  if (!item) {
    inventoryCache = await api('/api/inventory');
    item = findCachedItem(name);
  }
  if (!item) {
    toast(`Could not load "${name}" for editing.`, 'error');
    return;
  }
  editingName = item.name;
  document.getElementById('modal-title').textContent = 'Edit Item';
  document.getElementById('f-name').value = item.name;
  document.getElementById('f-name').setAttribute('readonly', true);
  document.getElementById('f-qty').value = item.quantity;
  document.getElementById('f-unit').value = item.unit;
  document.getElementById('f-cat').value = item.category;
  document.getElementById('f-price').value = item.price || '';
  document.getElementById('f-distributor').value = item.distributor || '';
  populateWarehouseSelect(
    document.getElementById('f-warehouse'),
    item.distributor || '',
    item.warehouse || '',
    false
  );
  document.getElementById('f-threshold').value = item.low_stock_threshold;
  document.getElementById('f-case-cost').value = item.case_cost || '';
  document.getElementById('f-case-size').value = item.case_size || '';
  document.getElementById('f-weekly').value = item.weekly_usage || '';
  document.getElementById('item-modal').classList.add('open');
}

async function submitItem() {
  const name = document.getElementById('f-name').value.trim();
  const qty  = Math.round(parseFloat(document.getElementById('f-qty').value));
  const unit = document.getElementById('f-unit').value.trim();
  const cat  = document.getElementById('f-cat').value.trim() || 'general';
  const price = parseFloat(document.getElementById('f-price').value) || 0;
  const distributor = document.getElementById('f-distributor').value;
  const warehouse = document.getElementById('f-warehouse').value;
  const threshold = Math.round(parseFloat(document.getElementById('f-threshold').value)) || 1;
  const caseCost = parseFloat(document.getElementById('f-case-cost').value) || 0;
  const caseSize = parseInt(document.getElementById('f-case-size').value) || 0;
  const weekly = parseFloat(document.getElementById('f-weekly').value) || 0;

  if (!name || isNaN(qty) || !unit) {
    toast('Name, quantity and unit are required.', 'error');
    return;
  }

  const payload = {
    quantity: qty, unit, category: cat, price, distributor, warehouse,
    low_stock_threshold: threshold,
    case_cost: caseCost, case_size: caseSize, weekly_usage: weekly,
  };

  if (editingName) {
    await api(`/api/inventory/${encodeURIComponent(editingName)}`, 'PUT', payload);
    toast(`Updated "${editingName}".`);
  } else {
    await api('/api/inventory', 'POST', { name, ...payload });
    toast(`Added "${name}".`);
  }
  closeModal('item-modal');
  loadInventory();
  loadDashboard();
}

// -------------------------------------------------------------------------
// Modals – Use / Restock
// -------------------------------------------------------------------------
function openUseModal(name) {
  txnType = 'use';
  txnItemName = name;
  document.getElementById('txn-title').textContent = `Use: ${name}`;
  document.getElementById('txn-submit').textContent = 'Record Usage';
  document.getElementById('txn-submit').className = 'btn btn-danger';
  document.getElementById('txn-name').value = name;
  document.getElementById('txn-amount').value = '';
  document.getElementById('txn-note').value = '';
  document.getElementById('txn-modal').classList.add('open');
  document.getElementById('txn-amount').focus();
}

function openRestockModal(name) {
  txnType = 'restock';
  txnItemName = name;
  document.getElementById('txn-title').textContent = `Restock: ${name}`;
  document.getElementById('txn-submit').textContent = 'Add Stock';
  document.getElementById('txn-submit').className = 'btn btn-success';
  document.getElementById('txn-name').value = name;
  document.getElementById('txn-amount').value = '';
  document.getElementById('txn-note').value = '';
  document.getElementById('txn-modal').classList.add('open');
  document.getElementById('txn-amount').focus();
}

async function submitTxn() {
  const amount = Math.round(parseFloat(document.getElementById('txn-amount').value));
  const note = document.getElementById('txn-note').value.trim();
  if (isNaN(amount) || amount <= 0) {
    toast('Enter a valid whole-case amount.', 'error');
    return;
  }
  const url = txnType === 'use' ? '/api/use' : '/api/restock';
  await api(url, 'POST', { name: txnItemName, amount, note });
  toast(`${txnType === 'use' ? 'Used' : 'Restocked'} ${amount} of "${txnItemName}".`);
  closeModal('txn-modal');
  if (currentPage === 'inventory') loadInventory();
  else if (currentPage === 'usage') loadUsage();
  loadDashboard();
}

// -------------------------------------------------------------------------
// Delete
// -------------------------------------------------------------------------
async function deleteItem(name) {
  if (!confirm(`Delete "${name}" from inventory?`)) return;
  await api(`/api/inventory/${encodeURIComponent(name)}`, 'DELETE');
  toast(`Removed "${name}".`);
  loadInventory();
  loadDashboard();
}

// -------------------------------------------------------------------------
// Modal helpers
// -------------------------------------------------------------------------
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}
// Close on backdrop click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
});
// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
});
// Submit on Enter in modal inputs
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.tagName === 'INPUT') {
    const itemModal = document.getElementById('item-modal');
    const txnModal = document.getElementById('txn-modal');
    if (itemModal.classList.contains('open')) submitItem();
    else if (txnModal.classList.contains('open')) submitTxn();
  }
});

// -------------------------------------------------------------------------
// On-order column rendering
// -------------------------------------------------------------------------
function renderOnOrder(item) {
  const qty = +(item.on_order_qty || 0);
  if (!qty) return '<span style="color:var(--muted)">—</span>';
  // Prefer the operator-confirmed arrival date; fall back to the 30-day ETA
  // placeholder. Confirmed arrivals render solid green with a check mark; ETA
  // estimates render muted + italic with a ~ so the two read differently.
  const dateStr = item.on_order_next_arrival || item.on_order_next_eta || '';
  const isActual = !!item.on_order_next_is_actual;
  const dateFormatted = formatDate(dateStr);
  const badge = `<span class="badge badge-blue" title="${escAttr(qty.toFixed(0) + ' cs on order')}">+${qty.toFixed(0)}</span>`;
  if (!dateFormatted) {
    return badge + ` <span style="color:var(--muted);font-size:11px" title="pending lead time">pending</span>`;
  }
  if (isActual) {
    const tip = `Confirmed arrival ${dateFormatted}`;
    return badge + ` <span style="color:var(--green);font-weight:600;font-size:11px" title="${escAttr(tip)}">&#10003; ${dateFormatted}</span>`;
  }
  const tip = `Estimated 30-day ETA ${dateFormatted} - no confirmed arrival yet`;
  return badge + ` <span style="color:var(--muted);font-style:italic;font-size:11px" title="${escAttr(tip)}">~ ${dateFormatted}</span>`;
}

// -------------------------------------------------------------------------
// Column sort state for the inventory table.
// Single source of truth: clicking a header toggles asc -> desc -> none.
// -------------------------------------------------------------------------
let invSortKey = null;
let invSortDir = 0;   // 1 = asc, -1 = desc, 0 = none

function _sortValue(item, key) {
  if (key === 'status') {
    if (item.quantity <= item.low_stock_threshold) return 0;
    if (item.quantity <= item.low_stock_threshold * 1.5) return 1;
    return 2;
  }
  const v = item[key];
  if (v == null) return '';
  if (typeof v === 'number') return v;
  return String(v).toLowerCase();
}

function applyInvSort(items) {
  if (!invSortKey || !invSortDir) return items.slice();
  const key = invSortKey;
  const dir = invSortDir;
  return items.slice().sort((a, b) => {
    const va = _sortValue(a, key);
    const vb = _sortValue(b, key);
    if (va < vb) return -1 * dir;
    if (va > vb) return  1 * dir;
    return 0;
  });
}

function updateSortIndicators(tbodyId) {
  // Walk the matching thead and set sort-asc / sort-desc classes.
  const map = { 'inv-tbody': 'inv-thead-row', 'pending-tbody': 'pending-thead-row' };
  const headRow = document.getElementById(map[tbodyId]);
  if (!headRow) return;
  const state = tbodyId === 'inv-tbody'
    ? { key: invSortKey, dir: invSortDir }
    : { key: pendingSortKey, dir: pendingSortDir };
  headRow.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === state.key && state.dir === 1) th.classList.add('sort-asc');
    else if (th.dataset.sort === state.key && state.dir === -1) th.classList.add('sort-desc');
  });
}

function onInvSortClick(key) {
  if (invSortKey !== key) { invSortKey = key; invSortDir = 1; }
  else if (invSortDir === 1) invSortDir = -1;
  else if (invSortDir === -1) { invSortKey = null; invSortDir = 0; }
  else invSortDir = 1;
  loadInventory();
}

// -------------------------------------------------------------------------
// Pending POs page
// -------------------------------------------------------------------------
let pendingSortKey = 'ordered_at';
let pendingSortDir = 1;        // earliest first by default
let pendingFilterMode = 'all'; // 'all' | 'overdue' | 'noship' | 'pallet'
let pendingCache = [];          // flat list of {entry, item} pairs
let editingPos = new Set();     // PO#s ticked into inline-edit via the row checkbox
let pendingSearchQuery = '';    // PO# substring filter (full or partial)

function onPendingSearchInput() {
  const el = document.getElementById('pending-filter-search');
  pendingSearchQuery = ((el && el.value) || '').trim().toLowerCase();
  renderPendingPOs();
}

function setPendingFilter(mode) {
  pendingFilterMode = mode;
  ['all', 'overdue', 'noship', 'pallet'].forEach(m => {
    const btn = document.getElementById('pending-filter-' + m);
    if (btn) btn.classList.toggle('active', m === mode);
  });
  renderPendingPOs();
}

function onPendingSortClick(key) {
  if (pendingSortKey !== key) { pendingSortKey = key; pendingSortDir = 1; }
  else if (pendingSortDir === 1) pendingSortDir = -1;
  else if (pendingSortDir === -1) { pendingSortKey = null; pendingSortDir = 0; }
  else pendingSortDir = 1;
  renderPendingPOs();
}

// Stash of Chefs Warehouse PO groups, kept separate from `pendingCache`
// (which is built from inventory `on_order` pairs). CW POs don't live
// in inventory.json by design — they're merged into the Pending POs
// page render only, never the Inventory tab. We fetch the FULL CW set
// (status=all) once and filter client-side so the Status dropdown and
// the In Production section can both work off one dataset.
let cwPendingGroups = [];
let ledgerGroups = [];        // Phase 2b: records from /api/pos/ledger (single source)
let usingLedger = false;      // true when the ledger read succeeded (else legacy fallback)
// Arrived inventory-side POs (USF/Cheney) reconstructed from the usage
// log by /api/arrived-pos. These no longer live in `on_order` (they
// rolled over into quantity), so without this the Arrived view could
// only ever show Chefs Warehouse POs.
let arrivedInvGroups = [];
let freightShipIndex = {};      // normalized PO key -> verified ship date (Lineage freight)
let statusOverrides = {};       // normalized PO key -> manual status override
let modifyPoMap = {};           // po_number -> modify-modal info
let modifyPoOrder = [];         // PO numbers in display order (for the filter list)
let modifySelectedPo = '';      // currently selected PO in the Modify modal

// Status dropdown changed — re-render. All status slices are computed
// client-side from the full dataset, so no reload is required.
function onPendingStatusChange() {
  renderPendingPOs();
}

function _currentPendingStatus() {
  const el = document.getElementById('pending-filter-status');
  return (el && el.value) || 'pending';
}

async function loadPendingPOs() {
  await ensureWarehouses();
  // Populate warehouse filter dropdown
  const wSel = document.getElementById('pending-filter-warehouse');
  if (wSel.options.length <= 1) {
    populateWarehouseSelect(wSel, '', '', true);
  }
  // Phase 2b: read the canonical PO ledger as the single source. Fall
  // back to the legacy multi-source stitch only if the ledger read fails.
  usingLedger = false; ledgerGroups = [];
  try {
    const lg = await api('/api/pos/ledger');
    if (lg && lg.ok && Array.isArray(lg.pos)) { ledgerGroups = lg.pos; usingLedger = true; }
  } catch (e) { console.warn('PO ledger fetch failed; using legacy sources:', e); }
  if (!usingLedger) {
  const items = await api('/api/inventory');
  const pairs = [];
  items.forEach(item => {
    (item.on_order || []).forEach(entry => {
      pairs.push({ item, entry });
    });
  });
  pendingCache = pairs;

  // Pull the full Chefs Warehouse PO set (active + arrived + canceled)
  // from the parallel data store. They never appear in /api/inventory.
  // Each record carries a server-computed `status`. A network failure
  // here must not break the rest of the page — log it and render
  // without CW rows.
  try {
    const cwResp = await api('/api/chefs-warehouse/pos?status=all');
    cwPendingGroups = (cwResp && cwResp.pos) ? cwResp.pos : [];
  } catch (e) {
    console.warn('Failed to load Chefs Warehouse POs:', e);
    cwPendingGroups = [];
  }

  // Pull arrived inventory-side POs (reconstructed from rollover usage
  // rows). Non-fatal on failure.
  try {
    const arrResp = await api('/api/arrived-pos');
    arrivedInvGroups = (arrResp && arrResp.pos) ? arrResp.pos : [];
  } catch (e) {
    console.warn('Failed to load arrived inventory POs:', e);
    arrivedInvGroups = [];
  }

  // Freight ship-date index: normalized PO key -> verified ship date from a
  // Lineage freight invoice. Non-fatal on failure.
  try {
    const fr = await api('/api/freight/ship-date-index');
    freightShipIndex = (fr && fr.index) || {};
  } catch (e) {
    console.warn('Failed to load freight ship-date index:', e);
    freightShipIndex = {};
  }

  try {
    const so = await api('/api/pending/status-overrides');
    statusOverrides = (so && so.overrides) || {};
  } catch (e) {
    console.warn('Failed to load status overrides:', e);
    statusOverrides = {};
  }
  }  // end legacy fallback

  renderPendingPOs();
}

// Build a "PO group" from one or more pendingCache pairs sharing a
// po_number. Aggregates qty across line items and preserves a list of
// {variety, qty, unit} for the Items column. Each PO has one warehouse,
// one ordered_at, one ship_date, and one arrival_date in normal use —
// take those from the first entry but fall back across entries in case
// of partial data.
function _buildPoGroups(pairs) {
  const byPo = new Map();
  pairs.forEach(p => {
    const po = p.entry.po_number || '__no_po__';
    if (!byPo.has(po)) {
      byPo.set(po, {
        po_number:   p.entry.po_number || '',
        distributor: p.item.distributor || '',
        warehouse:   p.item.warehouse || '',
        ordered_at:  p.entry.ordered_at || '',
        eta:         p.entry.eta || '',
        ship_date:   p.entry.ship_date || '',
        arrival_date: p.entry.arrival_date || '',
        total_cs:    0,
        lines:       [],   // [{variety, qty, unit, name}]
      });
    }
    const g = byPo.get(po);
    g.total_cs += Number(p.entry.qty) || 0;
    // Pull variety from the SKU name if not on the entry itself
    const variety = (p.item.name || '').split('Bagel')[0].trim() || p.item.name;
    g.lines.push({
      variety, name: p.item.name,
      qty: Number(p.entry.qty) || 0,
      unit: p.entry.unit || 'cs',
    });
    // Prefer a non-empty ship_date / arrival from any line (they should
    // be uniform across the PO since ship_date is set per-PO).
    if (!g.ship_date    && p.entry.ship_date)    g.ship_date    = p.entry.ship_date;
    if (!g.arrival_date && p.entry.arrival_date) g.arrival_date = p.entry.arrival_date;
  });
  return Array.from(byPo.values());
}

function _pendingSortValue(group, key) {
  if (key === 'po_number')   return (group.po_number || '').toLowerCase();
  if (key === 'distributor') return (group.distributor || '').toLowerCase();
  if (key === 'warehouse')   return (group.warehouse || '').toLowerCase();
  if (key === 'ordered_at')  return (group.ordered_at || '');
  if (key === 'eta')         return (group.eta || '');
  if (key === 'total_cs')    return Number(group.total_cs) || 0;
  return '';
}

function renderPendingPOs() {
  const tbody = document.getElementById('pending-tbody');
  if (!tbody) return;

  // Assemble the full PO set (inventory pending + Chefs Warehouse +
  // reconstructed arrived inventory POs), each tagged with a computed
  // _state, then slice it for the main table and the In Production
  // section.
  let groups = _assemblePendingGroups();

  // Shared filters apply to BOTH the In Production section and the main
  // table: distributor, warehouse, and the PO# search box.
  const distF = document.getElementById('pending-filter-distributor').value;
  const whF   = document.getElementById('pending-filter-warehouse').value;
  if (distF) groups = groups.filter(g => (g.distributor || '') === distF);
  if (whF)   groups = groups.filter(g => (g.warehouse  || '') === whF);
  if (pendingSearchQuery) {
    groups = groups.filter(g =>
      (g.po_number || '').toLowerCase().includes(pendingSearchQuery));
  }

  // --- In Production section ------------------------------------------
  // "Anything with a ship date within the next 7 days" — independent of
  // the Status dropdown and quick-filter buttons. Excludes arrived /
  // cancelled POs. A PO can carry an Overdue tag here (if it's also >30
  // days old) and still belong in this section; that's intentional.
  const now = new Date();
  renderInProductionSection(groups.filter(g =>
    g._state !== 'arrived' && g._state !== 'cancelled' &&
    _shipInProductionWindow(g.ship_date, now)));

  // --- In Transit section ---------------------------------------------
  // POs already shipped (ship date passed) and not yet arrived -- en route
  // between ship and arrival. Like In Production, this is window-based
  // (independent of the Overdue tag) so a shipped-but-overdue PO still shows
  // here. Honors the same shared distributor/warehouse/search filters.
  renderInTransitSection(groups.filter(g => {
    if (g._state === 'arrived' || g._state === 'cancelled') return false;
    const ship = _poDate(g.ship_date);
    if (!ship) return false;
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return ship < today;
  }));

  // --- Cases On Order breakdown (production planning) -----------------
  // Rollup of every active (not-arrived/cancelled) PO by variety and
  // warehouse. Respects the shared distributor/warehouse/search filters.
  renderOnOrderBreakdown(groups);

  // --- Main table -----------------------------------------------------
  // Status dropdown maps onto the computed tag state.
  const statusF = _currentPendingStatus();
  if (statusF === 'pending') {
    groups = groups.filter(g => g._state !== 'arrived' && g._state !== 'cancelled');
  } else if (statusF === 'arrived') {
    groups = groups.filter(g => g._state === 'arrived');
  } else if (statusF === 'canceled') {
    groups = groups.filter(g => g._state === 'cancelled');
  } // 'all' -> no status filter

  // Quick-filter buttons.
  if (pendingFilterMode === 'overdue') {
    // Unified on the order-date + 30-day rule (same as the Overdue tag).
    groups = groups.filter(g => g._state === 'overdue');
  } else if (pendingFilterMode === 'noship') {
    groups = groups.filter(g => !g.ship_date);
  } else if (pendingFilterMode === 'pallet') {
    groups = groups.filter(g => (Number(g.total_cs) % 56) !== 0);
  }

  // Sort
  if (pendingSortKey && pendingSortDir) {
    const k = pendingSortKey;
    const d = pendingSortDir;
    groups.sort((a, b) => {
      const va = _pendingSortValue(a, k);
      const vb = _pendingSortValue(b, k);
      if (va < vb) return -1 * d;
      if (va > vb) return  1 * d;
      return 0;
    });
  }

  if (groups.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">No POs match this filter.</td></tr>';
    updateSortIndicators('pending-tbody');
    return;
  }

  tbody.innerHTML = groups.map(g => _pendingRowHtml(g, now)).join('');
  updateSortIndicators('pending-tbody');
}

// ---------------------------------------------------------------------------
// PO status tags
// ---------------------------------------------------------------------------
// Exactly one tag per PO, chosen by precedence:
//   Cancelled > Arrived > Overdue > In Production > In Transit > Open
//   - Cancelled / Arrived are terminal states.
//   - Overdue = ordered_at + 30 days in the past (ops rule: ANY PO older
//     than 30 days from its order date is overdue). Outranks the ship-
//     date states so a stale PO keeps flagging even after a ship date is
//     entered.
//   - In Production = ship date set within the next 7 days (today -> +7);
//     the order is being made and ships imminently.
//   - In Transit = ship date set but outside that window (already shipped
//     or scheduled further out), not yet arrived.
//   - Open = pending, under 30 days old, no ship date yet.
const PENDING_TAGS = {
  arrived:       { label: 'Arrived',       cls: 'badge-green'  },
  in_transit:    { label: 'In Transit',    cls: 'badge-blue'   },
  in_production: { label: 'In Production', cls: 'badge-violet' },
  overdue:       { label: 'Overdue',       cls: 'badge-red'    },
  cancelled:     { label: 'Cancelled',     cls: 'badge-gray'   },
  open:          { label: 'Open',          cls: 'badge-yellow' },
};

function _poTagHtml(state) {
  const t = PENDING_TAGS[state] || PENDING_TAGS.open;
  return `<span class="badge ${t.cls}" style="font-size:10px">${t.label}</span>`;
}

// Parse a stored date (ISO date or datetime) to a local midnight Date.
function _poDate(s) {
  const m = String(s || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

// True when a ship date is set AND lands in [today, today+7].
function _shipInProductionWindow(shipDate, now) {
  const ship = _poDate(shipDate);
  if (!ship) return false;
  now = now || new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const plus7 = new Date(today);
  plus7.setDate(plus7.getDate() + 7);
  return ship >= today && ship <= plus7;
}

// True when the PO's effective arrival trigger is in the past, or it has
// been explicitly reconstructed / flagged as arrived.
function _isPoArrived(g, now) {
  if (g.status === 'arrived' || g._source === 'arrived') return true;
  const trg = (g.arrival_date || g.eta || '').trim();
  if (!trg) return false;
  const d = new Date(trg);
  return !isNaN(d.getTime()) && d <= (now || new Date());
}

function _poState(g, now) {
  now = now || new Date();
  if (g.status === 'canceled' || g.canceled) return 'cancelled';
  if (_isPoArrived(g, now)) return 'arrived';
  const ordered = _poDate(g.ordered_at);
  if (ordered) {
    const cutoff = new Date(ordered);
    cutoff.setDate(cutoff.getDate() + 30);
    if (cutoff < now) return 'overdue';
  }
  if (_shipInProductionWindow(g.ship_date, now)) return 'in_production';
  const ship = _poDate(g.ship_date);
  if (ship) {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    // Ship date already passed = en route between ship and arrival; a ship
    // date still in the future = being produced to ship.
    return ship < today ? 'in_transit' : 'in_production';
  }
  return 'open';
}

// Merge inventory on_order pairs, Chefs Warehouse POs, and reconstructed
// arrived inventory POs into one normalized list, each carrying _source
// and a computed _state. Dedupes arrived records against any PO already
// represented by a live (inventory / CW) group.
// Normalize a PO/reference token for matching against the freight index.
// Mirrors _norm_po_key() on the server (uppercase, drop HHB- prefix, strip
// surrounding dots/space; leading zeros preserved on purpose).
function _normPoKey(s) {
  let t = String(s || '').trim().toUpperCase();
  if (t.startsWith('HHB-') || t.startsWith('HHB ')) t = t.slice(4);
  return t.replace(/^\.+|\.+$/g, '').trim();
}

// Add n days to a YYYY-MM-DD (or ISO) date, returning YYYY-MM-DD.
function _addDaysISO(iso, n) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso || '';
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  d.setDate(d.getDate() + n);
  const p = x => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Green check shown when a ship date is verified by a Lineage freight invoice.
function _shipVerifiedMark() {
  return ' <span title="Ship date verified by a Lineage freight invoice" style="color:var(--green);font-weight:700;cursor:help">&#10003;</span>';
}

function _assemblePendingGroups() {
  // Phase 2b: prefer the canonical ledger (one source). Rendering below is
  // unchanged -- _poState still computes the display state from the same
  // date fields -- so this only swaps the DATA SOURCE.
  if (usingLedger) return _groupsFromLedger(ledgerGroups);
  return _assemblePendingGroupsLegacy();
}

function _groupsFromLedger(records) {
  const now = new Date();
  return (records || []).map(r => {
    const src = (r.source_kind === 'chefs_warehouse') ? 'chefs_warehouse'
              : (r.source_kind === 'arrived') ? 'arrived' : 'inventory';
    const g = {
      po_number: r.po_number || '', po_revision: r.po_revision || '',
      distributor: r.distributor || '', warehouse: r.warehouse || '',
      dc_code: r.dc_code || '', ordered_at: r.ordered_at || '', eta: r.eta || '',
      ship_date: r.ship_date || '', ship_date_source: r.ship_date_source || '',
      arrival_date: r.arrival_date || '', total_cs: Number(r.total_cs) || 0,
      status: r.status || '', transfer_group: r.transfer_group || null,
      lines: (r.lines || []).map(L => ({ variety: L.variety || '', qty: Number(L.qty) || 0,
                                         unit: L.unit || 'cs', name: L.name || L.variety || '' })),
      _source: src,
    };
    // Match the legacy freight behavior: a freight-verified ship date on a
    // not-yet-arrived PO implies arrival = ship + 7.
    if (g.ship_date_source === 'freight' && src !== 'arrived' && g.status !== 'arrived')
      g.arrival_date = _addDaysISO(g.ship_date, 7);
    g._stateOverride = r.override || '';
    g._state = g._stateOverride || _poState(g, now);
    return g;
  });
}

function _assemblePendingGroupsLegacy() {
  let groups = _buildPoGroups(pendingCache).map(g => {
    g._source = 'inventory';
    return g;
  });

  const cw = (cwPendingGroups || []).map(g => ({
    po_number:    g.po_number || '',
    po_revision:  g.po_revision || '',
    distributor:  'Chefs Warehouse',
    warehouse:    g.warehouse || '',
    dc_code:      g.dc_code || '',
    ordered_at:   g.ordered_at || '',
    eta:          g.eta || '',
    ship_date:    g.ship_date || '',
    arrival_date: g.arrival_date || '',
    total_cs:     Number(g.total_cs) || 0,
    status:       g.status || '',
    ship_date_source: g.ship_date_source || '',
    lines:        (g.lines || []).map(L => ({
      variety: L.variety || '', qty: Number(L.qty) || 0,
      unit: L.unit || 'cs', name: L.name || '',
    })),
    _source: 'chefs_warehouse',
  }));

  const seen = new Set(groups.concat(cw).map(g => g.po_number));
  const arrived = (arrivedInvGroups || [])
    .filter(g => !seen.has(g.po_number || ''))
    .map(g => ({
      po_number:    g.po_number || '',
      po_revision:  g.po_revision || '',
      distributor:  g.distributor || '',
      warehouse:    g.warehouse || '',
      ordered_at:   g.ordered_at || '',
      eta:          g.eta || '',
      ship_date:    g.ship_date || '',
      arrival_date: g.arrival_date || '',
      total_cs:     Number(g.total_cs) || 0,
      status:       'arrived',
      ship_date_source: g.ship_date_source || '',
      lines:        (g.lines || []).map(L => ({
        variety: L.variety || '', qty: Number(L.qty) || 0,
        unit: L.unit || 'cs', name: L.name || '',
      })),
      _source: 'arrived',
    }));

  groups = groups.concat(cw).concat(arrived);

  // Freight verification: a matching Lineage freight invoice carries the
  // ACTUAL ship date, so it is authoritative -- it overrides any manual
  // entry, sets a derived arrival (ship + 7) for not-yet-arrived POs, and
  // tags the source so the UI shows a verified check mark. Arrived POs keep
  // their real arrival_date (the rollover timestamp).
  groups.forEach(g => {
    const fd = freightShipIndex[_normPoKey(g.po_number || '')];
    if (!fd) return;
    g.ship_date = fd;
    g.ship_date_source = 'freight';
    const isArrived = g.status === 'arrived' || g._source === 'arrived';
    if (!isArrived) g.arrival_date = _addDaysISO(fd, 7);
  });

  const now = new Date();
  groups.forEach(g => {
    const ov = statusOverrides[_normPoKey(g.po_number || '')];
    g._stateOverride = ov || '';
    g._state = ov || _poState(g, now);
  });
  return groups;
}

// Items column (shared by the main table and the In Production section).
function _poItemsCellHtml(g) {
  const sortedLines = (g.lines || []).slice()
    .sort((a, b) => (a.variety || '').localeCompare(b.variety || ''));
  const head = sortedLines.slice(0, 4).map(L =>
    `${escHtml(L.variety)}&nbsp;<span style="color:var(--muted)">${(Number(L.qty) || 0).toFixed(0)}</span>`
  ).join(' · ');
  const more = sortedLines.length > 4
    ? ` · <span style="color:var(--muted)">+${sortedLines.length - 4} more</span>` : '';
  const tipText = sortedLines.map(L =>
    `${L.variety}: ${(Number(L.qty) || 0).toFixed(0)} ${L.unit || 'cs'}`).join('\n');
  return `<td title="${escAttr(tipText)}" style="font-size:12px">
        <div>${head}${more}</div>
        <div style="color:var(--muted);font-size:11px">${sortedLines.length} item${sortedLines.length === 1 ? '' : 's'}</div>
      </td>`;
}

function _poWarehouseLabel(g) {
  return (g._source === 'chefs_warehouse' && g.dc_code)
    ? `${escHtml(g.warehouse || '')} <span style="color:var(--muted);font-size:11px">(${escHtml(g.dc_code)})</span>`
    : escHtml(g.warehouse || '');
}

function togglePoRowEdit(po) {
  if (!po) return;
  if (editingPos.has(po)) editingPos.delete(po); else editingPos.add(po);
  renderPendingPOs();
}

function _pendingRowHtml(g, now) {
  now = now || new Date();
  const state = g._state || _poState(g, now);
  const shipISO = (g.ship_date || '').slice(0, 10);
  const arrival = g.arrival_date ? formatDate(g.arrival_date) : '<span style="color:var(--muted)">—</span>';
  const eta = g.eta ? formatDate(g.eta) : '<span style="color:var(--muted)">—</span>';
  const palletMismatch = (Number(g.total_cs) % 56) !== 0;
  const rowStyle = (state === 'overdue')
    ? ' style="background:var(--surface2)"'
    : palletMismatch
      ? ' style="background:rgba(217, 119, 6, 0.08)"'
      : '';
  const palletBadge = palletMismatch
    ? ` <span class="badge badge-yellow" title="Total cs (${g.total_cs}) is not a multiple of 56" style="margin-left:4px;font-size:10px">⚠ not ×56</span>`
    : '';
  const source = g._source || 'inventory';
  const terminal = (state === 'arrived' || state === 'cancelled');
  const shipVerified = g.ship_date_source === 'freight';
  const isArrived = state === 'arrived';
  const isCancelled = state === 'cancelled';
  // Freight-verified ship dates are locked (the invoice is the final word).
  // Cancelled rows are read-only. Pending rows -- and CW Arrived rows that
  // aren't freight-verified -- get the editable date box (CW status is
  // date-driven, so editing/clearing it un-arrives safely). Inventory Arrived
  // rows show a read-only date and are edited via Reopen.
  // Editability: freight-verified + cancelled are locked; inventory-arrived is
  // changed via Reopen; CW-arrived (date-driven) stays editable. The date box
  // appears only when this PO's row checkbox is ticked (opt-in per PO).
  const editable = !shipVerified && !isCancelled
    && (!terminal || (isArrived && source === 'chefs_warehouse'));
  const editing = editingPos.has(g.po_number || '');
  let shipCell;
  if (editable && editing) {
    shipCell = `<input type="date" class="ship-date-input" value="${escAttr(shipISO)}" onchange="onShipDateChange('${escAttr(g.po_number || '')}', this.value, '${escAttr(source)}')" />`;
  } else {
    const disp = shipISO
      ? (formatDate(shipISO) + (shipVerified ? _shipVerifiedMark() : ''))
      : '<span style="color:var(--muted)">&mdash;</span>';
    const hint = (editing && !editable)
      ? ' <span title="Freight-verified or already arrived; use Reopen to change" style="cursor:help">&#128274;</span>'
      : '';
    shipCell = disp + hint;
  }
  return `<tr${rowStyle}>
      <td style="white-space:nowrap"><input type="checkbox" class="po-row-chk" title="Tick to modify this PO ship date" ${editing ? 'checked' : ''} onclick="togglePoRowEdit('${escAttr(g.po_number || '')}')" style="margin-right:7px;vertical-align:middle;cursor:pointer"><span style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(g.po_number || '—')}</span></td>
      <td>${distributorBadge(g.distributor)}</td>
      <td>${_poWarehouseLabel(g)}</td>
      ${_poItemsCellHtml(g)}
      <td style="text-align:right;font-weight:600">${(Number(g.total_cs) || 0).toFixed(0)} <span style="color:var(--muted);font-size:11px">cs</span>${palletBadge}</td>
      <td style="white-space:nowrap">${formatDate(g.ordered_at)}</td>
      <td>${shipCell}</td>
      <td style="white-space:nowrap">${arrival}</td>
      <td style="white-space:nowrap;color:var(--muted)">${eta}</td>
      <td>${_poTagHtml(state)}</td>
    </tr>`;
}

function renderInProductionSection(list) {
  const card = document.getElementById('pending-inprod-card');
  const tbody = document.getElementById('pending-inprod-tbody');
  const countEl = document.getElementById('pending-inprod-count');
  if (!card || !tbody) return;
  list = (list || []).slice()
    .sort((a, b) => (a.ship_date || '').localeCompare(b.ship_date || ''));
  if (list.length === 0) {
    card.style.display = 'none';
    tbody.innerHTML = '';
    if (countEl) countEl.textContent = '';
    return;
  }
  card.style.display = '';
  if (countEl) countEl.textContent = `· ${list.length} PO${list.length === 1 ? '' : 's'}`;
  tbody.innerHTML = list.map(g => {
    const ship = g.ship_date ? (formatDate(g.ship_date) + (g.ship_date_source === 'freight' ? _shipVerifiedMark() : '')) : '<span style="color:var(--muted)">—</span>';
    const arrival = g.arrival_date ? formatDate(g.arrival_date) : '<span style="color:var(--muted)">—</span>';
    return `<tr>
      <td style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(g.po_number || '—')}</td>
      <td>${distributorBadge(g.distributor)}</td>
      <td>${_poWarehouseLabel(g)}</td>
      ${_poItemsCellHtml(g)}
      <td style="text-align:right;font-weight:600">${(Number(g.total_cs) || 0).toFixed(0)} <span style="color:var(--muted);font-size:11px">cs</span></td>
      <td style="white-space:nowrap">${g.ordered_at ? formatDate(g.ordered_at) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
      <td style="white-space:nowrap;font-weight:600">${ship}</td>
      <td style="white-space:nowrap">${arrival}</td>
      <td style="white-space:nowrap;color:var(--muted)">${g.eta ? formatDate(g.eta) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
      <td>${_poTagHtml(g._state || 'in_production')}</td>
    </tr>`;
  }).join('');
}

// In Transit: POs already shipped (ship date passed) and not yet arrived.
// Sorted by soonest arrival so what's landing next is on top. Arrival is the
// emphasized column here (vs. ship date in the In Production section).
function renderInTransitSection(list) {
  const card = document.getElementById('pending-intransit-card');
  const tbody = document.getElementById('pending-intransit-tbody');
  const countEl = document.getElementById('pending-intransit-count');
  if (!card || !tbody) return;
  list = (list || []).slice()
    .sort((a, b) => (a.arrival_date || a.eta || '').localeCompare(b.arrival_date || b.eta || ''));
  if (list.length === 0) {
    card.style.display = 'none';
    tbody.innerHTML = '';
    if (countEl) countEl.textContent = '';
    return;
  }
  card.style.display = '';
  if (countEl) countEl.textContent = `· ${list.length} PO${list.length === 1 ? '' : 's'}`;
  tbody.innerHTML = list.map(g => {
    const ship = g.ship_date ? (formatDate(g.ship_date) + (g.ship_date_source === 'freight' ? _shipVerifiedMark() : '')) : '<span style="color:var(--muted)">&mdash;</span>';
    const arrival = g.arrival_date ? formatDate(g.arrival_date) : '<span style="color:var(--muted)">&mdash;</span>';
    return `<tr>
      <td style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(g.po_number || '—')}</td>
      <td>${distributorBadge(g.distributor)}</td>
      <td>${_poWarehouseLabel(g)}</td>
      ${_poItemsCellHtml(g)}
      <td style="text-align:right;font-weight:600">${(Number(g.total_cs) || 0).toFixed(0)} <span style="color:var(--muted);font-size:11px">cs</span></td>
      <td style="white-space:nowrap">${g.ordered_at ? formatDate(g.ordered_at) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
      <td style="white-space:nowrap">${ship}</td>
      <td style="white-space:nowrap;font-weight:600">${arrival}</td>
      <td style="white-space:nowrap;color:var(--muted)">${g.eta ? formatDate(g.eta) : '<span style="color:var(--muted)">&mdash;</span>'}</td>
      <td>${_poTagHtml(g._state || 'in_transit')}</td>
    </tr>`;
  }).join('');
}

// Cases On Order breakdown for production planning. Rolls up every active
// (not-arrived, not-cancelled) PO by variety (what to bake) and by warehouse
// (where it ships). Driven off the same filtered group set as the sections.
function renderOnOrderBreakdown(groups) {
  const card = document.getElementById('pending-onorder-card');
  const body = document.getElementById('pending-onorder-body');
  const totalEl = document.getElementById('pending-onorder-total');
  if (!card || !body) return;
  const active = (groups || []).filter(g => g._state !== 'arrived' && g._state !== 'cancelled');
  const byVar = {}, byWh = {}, varPOs = {}, whPOs = {};
  let totalCs = 0;
  active.forEach(g => {
    const wh = (g.warehouse || '').trim() || 'Unassigned';
    let gCs = 0;
    (g.lines || []).forEach(L => {
      const v = (L.variety || '').trim() || '(unspecified)';
      const cs = Number(L.qty) || 0;
      byVar[v] = (byVar[v] || 0) + cs;
      (varPOs[v] = varPOs[v] || new Set()).add(g.po_number || ('_' + Math.random()));
      gCs += cs;
    });
    byWh[wh] = (byWh[wh] || 0) + gCs;
    (whPOs[wh] = whPOs[wh] || new Set()).add(g.po_number || ('_' + Math.random()));
    totalCs += gCs;
  });
  if (totalCs <= 0) {
    card.style.display = 'none';
    body.innerHTML = '';
    if (totalEl) totalEl.textContent = '';
    return;
  }
  card.style.display = '';
  if (totalEl) totalEl.textContent =
    `${totalCs.toFixed(0)} cs (${(totalCs / 56).toFixed(1)} pallets) \u00b7 ${Object.keys(byVar).length} varieties \u00b7 ${active.length} PO${active.length === 1 ? '' : 's'}`;
  const varRows = Object.keys(byVar).sort((a, b) => byVar[b] - byVar[a]).map(v =>
    `<tr><td style="font-weight:600">${escHtml(v)}</td><td style="text-align:right;font-weight:700">${byVar[v].toFixed(0)}</td><td style="text-align:right">${(byVar[v] / 56).toFixed(1)}</td><td style="text-align:right;color:var(--muted)">${varPOs[v].size}</td></tr>`).join('');
  const whRows = Object.keys(byWh).sort((a, b) => byWh[b] - byWh[a]).map(w =>
    `<tr><td style="font-weight:600">${escHtml(w)}</td><td style="text-align:right;font-weight:700">${byWh[w].toFixed(0)}</td><td style="text-align:right">${(byWh[w] / 56).toFixed(1)}</td><td style="text-align:right;color:var(--muted)">${whPOs[w].size}</td></tr>`).join('');
  const totalRow = (lbl) => `<tr style="border-top:2px solid var(--border)"><td style="font-weight:700">Total</td><td style="text-align:right;font-weight:700">${totalCs.toFixed(0)}</td><td style="text-align:right;font-weight:700">${(totalCs / 56).toFixed(1)}</td><td></td></tr>`;
  body.innerHTML = `
    <div style="display:grid;gap:24px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">
      <div>
        <div style="font-family:var(--font-display);font-weight:700;font-size:15px;color:var(--accent);margin:4px 0 6px">By Variety <span style="color:var(--muted);font-weight:400;font-size:12px">&mdash; what to bake</span></div>
        <table style="margin:0">
          <thead><tr><th>Variety</th><th style="text-align:right">cs on order</th><th style="text-align:right">pallets</th><th style="text-align:right">POs</th></tr></thead>
          <tbody>${varRows}</tbody>
          <tfoot>${totalRow()}</tfoot>
        </table>
      </div>
      <div>
        <div style="font-family:var(--font-display);font-weight:700;font-size:15px;color:var(--accent);margin:4px 0 6px">By Warehouse <span style="color:var(--muted);font-weight:400;font-size:12px">&mdash; where it ships</span></div>
        <table style="margin:0">
          <thead><tr><th>Warehouse</th><th style="text-align:right">cs on order</th><th style="text-align:right">pallets</th><th style="text-align:right">POs</th></tr></thead>
          <tbody>${whRows}</tbody>
          <tfoot>${totalRow()}</tfoot>
        </table>
      </div>
    </div>`;
}

function openModifyModal(presetPo) {
  const groups = _assemblePendingGroups()
    .filter(g => (g.po_number || '').trim())
    .sort((a, b) => (a.po_number || '').localeCompare(b.po_number || ''));
  modifyPoMap = {};
  modifyPoOrder = [];
  groups.forEach(g => {
    modifyPoMap[g.po_number] = {
      source: g._source || 'inventory',
      total_cs: Number(g.total_cs) || 0,
      lines: (g.lines || []).length,
      ship_date: (g.ship_date || '').slice(0, 10),
      ship_date_source: g.ship_date_source || '',
      override: g._stateOverride || '',
      state: g._state || '',
      distributor: g.distributor || '',
      warehouse: g.warehouse || '',
    };
    modifyPoOrder.push(g.po_number);
  });
  modifySelectedPo = (presetPo && modifyPoMap[presetPo]) ? presetPo : (modifyPoOrder[0] || '');
  const f = document.getElementById('modify-po-filter');
  if (f) f.value = '';
  renderModifyList('');
  onModifyPoSelect();
  document.getElementById('modify-po-modal').classList.add('open');
}

// Type-to-filter the PO list by PO #, distributor, warehouse, status, or date.
function onModifyPoFilter() {
  const el = document.getElementById('modify-po-filter');
  renderModifyList(((el && el.value) || '').trim().toLowerCase());
}

function renderModifyList(q) {
  const box = document.getElementById('modify-po-list');
  const empty = document.getElementById('modify-po-empty');
  if (!box) return;
  const rows = modifyPoOrder.filter(po => {
    if (!q) return true;
    const i = modifyPoMap[po] || {};
    const tag = (PENDING_TAGS[i.state] || {}).label || '';
    return [po, i.distributor, i.warehouse, i.state, tag, i.ship_date]
      .join(' ').toLowerCase().indexOf(q) !== -1;
  });
  if (modifySelectedPo && rows.indexOf(modifySelectedPo) === -1) {
    modifySelectedPo = rows[0] || '';
    onModifyPoSelect();
  }
  if (empty) empty.style.display = rows.length ? 'none' : '';
  box.innerHTML = rows.map(po => {
    const i = modifyPoMap[po] || {};
    const tag = (PENDING_TAGS[i.state] || PENDING_TAGS.open || {}).label || '';
    const sel = po === modifySelectedPo;
    const ship = i.ship_date ? formatDate(i.ship_date) : '\u2014';
    return `<div onclick="selectModifyPo('${escAttr(po)}')" role="option" aria-selected="${sel}" style="padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border);${sel ? 'background:var(--surface2)' : ''}">
        <span style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(po)}</span>
        <span style="color:var(--muted);font-size:11px"> &middot; ${escHtml(i.distributor || '')} &middot; ${escHtml(i.warehouse || '')} &middot; ${escHtml(tag)} &middot; ship ${escHtml(ship)}</span>
      </div>`;
  }).join('');
}

function selectModifyPo(po) {
  modifySelectedPo = po;
  const el = document.getElementById('modify-po-filter');
  renderModifyList(((el && el.value) || '').trim().toLowerCase());
  onModifyPoSelect();
}

function onModifyPoSelect() {
  const po = modifySelectedPo;
  const info = modifyPoMap[po] || {};
  const statusSel = document.getElementById('modify-po-status');
  const shipInput = document.getElementById('modify-po-ship');
  const note = document.getElementById('modify-po-ship-note');
  const verified = info.ship_date_source === 'freight';
  const invArrived = (info.source !== 'chefs_warehouse') && (info.state === 'arrived');
  const locked = verified || invArrived;
  if (statusSel) statusSel.value = info.override || '';
  if (shipInput) {
    shipInput.value = info.ship_date || '';
    shipInput.disabled = !!locked;
  }
  if (note) note.textContent = verified
    ? 'Ship date is verified by a freight invoice and is locked.'
    : invArrived
      ? 'This PO has already arrived \u2014 use Reopen on its row to change it.'
      : 'Arrival auto-sets to ship + 7 days. Leave blank to use the 30-day ETA.';
}

async function saveModifyPO() {
  const po = modifySelectedPo;
  if (!po) { closeModal('modify-po-modal'); return; }
  const info = modifyPoMap[po] || {};
  const newStatus = document.getElementById('modify-po-status').value;
  const shipInput = document.getElementById('modify-po-ship');
  const newShip = shipInput ? shipInput.value : '';
  const verified = info.ship_date_source === 'freight';
  const invArrived = (info.source !== 'chefs_warehouse') && (info.state === 'arrived');
  let changed = false;
  try {
    if ((newStatus || '') !== (info.override || '')) {
      await api('/api/pending/set-status', 'POST', { po_number: po, status: newStatus });
      changed = true;
    }
    if (!verified && !invArrived && (newShip || '') !== (info.ship_date || '')) {
      const isCW = info.source === 'chefs_warehouse';
      const r = await api(isCW ? '/api/chefs-warehouse/ship-date' : '/api/on-order/ship-date', 'POST',
                isCW ? { po_number: po, ship_date: newShip || '' }
                     : { po_number: po, ship_date: newShip || null });
      if (!isCW && r && r.entries_updated === 0) {
        toast(`No pending lines for PO ${po} \u2014 ship date unchanged (it may have already arrived; use Reopen).`, 'error');
      } else {
        changed = true;
      }
    }
  } catch (e) {
    toast('Save failed: ' + String(e), 'error');
    return;
  }
  toast(changed ? `Updated PO ${po}` : `No changes for PO ${po}`);
  closeModal('modify-po-modal');
  await loadPendingPOs();
  loadDashboard();
}

async function cancelModifyPO() {
  const po = modifySelectedPo;
  if (!po) return;
  const info = modifyPoMap[po] || {};
  closeModal('modify-po-modal');
  await cancelPendingPO(po, info.lines || 0, (info.total_cs || 0).toFixed(0), info.source || 'inventory');
}

async function reopenPendingPO(poNumber, source, totalCs) {
  if (!poNumber) return;
  source = source || 'inventory';
  const isCW = source === 'chefs_warehouse';
  const msg = isCW
    ? `Reopen Chefs Warehouse PO ${poNumber}?\n\nThis clears its ship/arrival date so it returns to the active pipeline as pending. Inventory is not affected.`
    : `Reopen PO ${poNumber}?\n\nThis removes the ${totalCs} cs that were added to on-hand when this PO was marked Arrived (use only if it hasn't actually arrived) and returns it to the tab as Open, awaiting a ship date.`;
  if (!confirm(msg)) return;
  let result;
  try {
    result = await api('/api/pending/reopen', 'POST', { po_number: poNumber, source });
  } catch (e) {
    toast('Reopen failed: ' + String(e), 'error');
    return;
  }
  if (!result || !result.ok) {
    toast('Reopen failed: ' + ((result && result.error) || 'unknown'), 'error');
    return;
  }
  toast(isCW
    ? `Reopened CW PO ${poNumber}`
    : `Reopened PO ${poNumber} (${result.restored_lines} line${result.restored_lines === 1 ? '' : 's'}, ${result.removed_cs} cs back to pending)`);
  await loadPendingPOs();
  loadDashboard();
}

async function cancelPendingPO(poNumber, lineCount, totalCs, source) {
  if (!poNumber) return;
  source = source || 'inventory';
  const isCW = source === 'chefs_warehouse';
  const msg = isCW
    ? `Cancel Chefs Warehouse PO ${poNumber}?\n\nThis removes ${lineCount} line${lineCount===1?'':'s'} (${totalCs} cs) from the Pending POs tab. Inventory is not affected (Chefs Warehouse POs never enter the Inventory tab).`
    : `Cancel PO ${poNumber}?\n\nThis removes ${lineCount} on-order line${lineCount===1?'':'s'} (${totalCs} cs) and the PO will disappear from Pending POs.\n\nAlready-arrived quantity in inventory is NOT affected.`;
  if (!confirm(msg)) return;
  let result;
  try {
    const endpoint = isCW ? '/api/chefs-warehouse/cancel' : '/api/admin/remove-po';
    result = await api(endpoint, 'POST', { po_number: poNumber });
  } catch (e) {
    toast('Cancel failed: ' + String(e));
    return;
  }
  if (!result.ok) {
    toast('Cancel failed: ' + (result.error || 'unknown'));
    return;
  }
  if (isCW) {
    toast(`Canceled CW PO ${poNumber}`);
  } else {
    toast(`Canceled PO ${poNumber} (${result.removed_entries} entries removed from ${result.affected_items.length} item${result.affected_items.length===1?'':'s'})`);
  }
  // Refresh both the Pending POs view and the Inventory cache.
  await loadPendingPOs();
  loadDashboard();
}

// -------------------------------------------------------------------------
// Daily Production page
// -------------------------------------------------------------------------
// Build {po_number: aggregated PO info} from the inventory 's on_order
// entries. Each PO has one ship_date / arrival_date / eta because the
// API ship-date endpoint fans out across all line items.
function _buildProdPoLookup(items) {
  const map = {};
  items.forEach(item => {
    (item.on_order || []).forEach(e => {
      const po = (e.po_number || '').trim();
      if (!po) return;
      const existing = map[po];
      if (!existing) {
        map[po] = {
          po_number:    po,
          ship_date:    e.ship_date || '',
          arrival_date: e.arrival_date || '',
          eta:          e.eta || '',
          warehouses:   new Set([item.warehouse || '']),
          line_count:   1,
        };
      } else {
        // Prefer a non-empty ship_date / arrival across line items
        if (!existing.ship_date    && e.ship_date)    existing.ship_date    = e.ship_date;
        if (!existing.arrival_date && e.arrival_date) existing.arrival_date = e.arrival_date;
        existing.warehouses.add(item.warehouse || '');
        existing.line_count += 1;
      }
    });
  });
  return map;
}

let prodPeriod = 'week';        // 'day' | 'week' | 'month'
let prodSortKey = 'production_date';
let prodSortDir = -1;            // newest first by default
let prodCache = [];
let prodPageSize = 10;           // rows per page
let prodPage = 0;                // 0-indexed current page

function onProdSearchInput() {
  prodPage = 0;                  // any filter change snaps back to page 1
  renderProductionDetail();
}
function onProdPageSizeChange() {
  const v = parseInt(document.getElementById('prod-page-size').value, 10);
  prodPageSize = (v > 0 ? v : 10);
  prodPage = 0;
  renderProductionDetail();
}
function onProdPageNav(delta) {
  prodPage = Math.max(0, prodPage + delta);
  renderProductionDetail();
}

// Pagination state for the Production-by-period (summary) table.
let prodSummaryPageSize = 10;
let prodSummaryPage = 0;
let _prodSummaryBuckets = [];   // cache of last-rendered buckets, for pager-only re-renders

function onProdSummaryPageSizeChange() {
  const v = parseInt(document.getElementById('prod-summary-page-size').value, 10);
  prodSummaryPageSize = (v > 0 ? v : 10);
  prodSummaryPage = 0;
  if (_prodSummaryBuckets.length) renderProductionSummary({ buckets: _prodSummaryBuckets });
}
function onProdSummaryPageNav(delta) {
  prodSummaryPage = Math.max(0, prodSummaryPage + delta);
  if (_prodSummaryBuckets.length) renderProductionSummary({ buckets: _prodSummaryBuckets });
}
function _updateProdSummaryPager(first, last, total) {
  const info = document.getElementById('prod-summary-pager-info');
  const ind  = document.getElementById('prod-summary-page-indicator');
  const prev = document.getElementById('prod-summary-page-prev');
  const next = document.getElementById('prod-summary-page-next');
  if (!info || !ind || !prev || !next) return;
  if (total === 0) {
    info.textContent = '';
    ind.textContent  = '';
    prev.disabled = true;
    next.disabled = true;
    return;
  }
  info.textContent = `\u00b7 Showing ${first.toLocaleString()}\u2013${last.toLocaleString()} of ${total.toLocaleString()}`;
  const totalPages = Math.max(1, Math.ceil(total / prodSummaryPageSize));
  ind.textContent  = `Page ${prodSummaryPage + 1} of ${totalPages}`;
  prev.disabled = prodSummaryPage <= 0;
  next.disabled = prodSummaryPage >= totalPages - 1;
}

function setProdPeriod(p) {
  prodPeriod = p;
  prodSummaryPage = 0;
  ['week','month','day'].forEach(m => {
    const btn = document.getElementById('prod-period-' + m);
    if (btn) btn.classList.toggle('active', m === p);
  });
  loadProductionSummary();
}

function onProdSortClick(key) {
  if (prodSortKey !== key) { prodSortKey = key; prodSortDir = 1; }
  else if (prodSortDir === 1) prodSortDir = -1;
  else if (prodSortDir === -1) { prodSortKey = null; prodSortDir = 0; }
  else prodSortDir = 1;
  renderProductionDetail();
}

function _updateProdPager(first, last, total) {
  const info = document.getElementById('prod-pager-info');
  const ind  = document.getElementById('prod-page-indicator');
  const prev = document.getElementById('prod-page-prev');
  const next = document.getElementById('prod-page-next');
  if (!info || !ind || !prev || !next) return;
  if (total === 0) {
    info.textContent = 'No records';
    ind.textContent  = '';
    prev.disabled = true;
    next.disabled = true;
    return;
  }
  info.textContent = `Showing ${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}`;
  const totalPages = Math.max(1, Math.ceil(total / prodPageSize));
  ind.textContent  = `Page ${prodPage + 1} of ${totalPages}`;
  prev.disabled = prodPage <= 0;
  next.disabled = prodPage >= totalPages - 1;
}

let prodPoLookup = {};  // po_number -> aggregated on_order info

// -------------------------------------------------------------------------
// Freight Costs (Lineage outbound shipping)
// -------------------------------------------------------------------------
// Pulls Lineage Freight Management LLC invoices ingested via the 6h
// mailbox scan. The same scan that picks up distributor POs (US Foods,
// Cheney, Chefs Warehouse) also picks up the "Billable Invoice(s)
// from LINEAGE FREIGHT MANAGEMENT LLC" emails sent by their TMS
// (noreply@tms.blujaysolutions.net); the .zip attachment is unpacked
// to PDFs server-side and each PDF turns into one freight_invoices.json
// row.
//
// Ship dates from those invoices are matched against pending POs by
// (distributor, dest_dc, po_number/shipper_ref) so the table shows
// which inventory line a shipment actually backed.

let _freightInvoices = [];
let _freightAllInvoices = [];          // unfiltered server payload
let _freightSummary    = {};
let _freightRangeDays  = 90;           // 90 / 180 / 365 / 0 (all)
let _freightMetric     = "total";      // total | per_pallet | per_case | pallets
let _freightChart      = null;
let _leadtimeChart     = null;

function setFreightRange(days) {
  _freightRangeDays = days;
  document.querySelectorAll('[id^="freight-range-"]').forEach(b => b.classList.remove('active'));
  const id = "freight-range-" + (days === 0 ? "all" : String(days));
  const btn = document.getElementById(id);
  if (btn) btn.classList.add('active');
  loadFreight();
}

function setFreightMetric(m) {
  _freightMetric = m;
  document.querySelectorAll('[id^="freight-metric-"]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(
    "freight-metric-" + ({total: "cost", per_pallet: "pp", per_case: "pc", pallets: "pallets"})[m]
  );
  if (btn) btn.classList.add('active');
  drawFreightChart();
}

async function loadFreightLeadTimes() {
  const kpis = document.getElementById('freight-leadtime-kpis');
  const note = document.getElementById('freight-leadtime-note');
  const canvas = document.getElementById('freight-leadtime-chart');
  if (!kpis || !canvas) return;
  let data;
  try { data = await api('/api/freight/lead-times'); }
  catch (e) { kpis.innerHTML = '<div class="subtitle">Lead-time data unavailable.</div>'; return; }
  if (!data || !data.ok) { kpis.innerHTML = '<div class="subtitle">Lead-time data unavailable.</div>'; return; }
  const o = data.overall || {};
  const kpi = (label, a) => `<div><div class="subtitle" style="margin-bottom:2px">${label}</div>` +
    `<div style="font-size:22px;font-weight:700">` +
    ((a && a.n) ? `${a.avg} days <span style="color:var(--muted);font-size:12px;font-weight:400">(median ${a.median}, n=${a.n})</span>` : '<span style="color:var(--muted)">&mdash;</span>') +
    `</div></div>`;
  kpis.innerHTML = kpi('PO placed &rarr; arrival (total)', o.order_to_arrival) +
                   kpi('Departure &rarr; arrival (transit)', o.ship_to_arrival);
  const whs = (data.by_warehouse || []).filter(w => (w.order_to_arrival.n || w.ship_to_arrival.n));
  if (_leadtimeChart) { _leadtimeChart.destroy(); _leadtimeChart = null; }
  if (!whs.length || typeof Chart === 'undefined') {
    if (note) note.textContent = 'Not enough dated POs yet to chart lead times.';
    return;
  }
  if (note) note.textContent = 'Total lead counts from the day the PO hits our inbox to arrival (includes production + transit); transit is ship date to arrival. POs with gaps over 120 days are excluded.';
  _leadtimeChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels: whs.map(w => w.warehouse),
      datasets: [
        { label: 'PO placed to arrival (total days)',
          data: whs.map(w => w.order_to_arrival.avg),
          backgroundColor: 'rgba(5, 23, 71, 0.80)' },
        { label: 'Departure to arrival (transit days)',
          data: whs.map(w => w.ship_to_arrival.avg),
          backgroundColor: 'rgba(21, 101, 192, 0.75)' },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { beginAtZero: true, title: { display: true, text: 'days' } } },
      plugins: { legend: { position: 'top' } },
    },
  });
}

async function loadFreight() {
  const params = new URLSearchParams();
  if (_freightRangeDays > 0) {
    const since = new Date();
    since.setDate(since.getDate() - _freightRangeDays);
    params.set("since", since.toISOString().slice(0, 10));
  }
  let payload;
  try {
    payload = await api("/api/freight/invoices?" + params.toString());
  } catch (err) {
    toast("Failed to load freight: " + err.message, 'error');
    return;
  }
  _freightAllInvoices = payload.invoices || [];
  _freightSummary     = payload.summary || {};
  // Populate the DC filter dropdown from the unfiltered set so users
  // see every DC even when filtering returns no rows for one of them.
  const dcs = Array.from(new Set(_freightAllInvoices.map(r => r.dest_dc).filter(Boolean))).sort();
  const sel = document.getElementById("freight-filter-dc");
  if (sel) {
    const cur = sel.value;
    sel.innerHTML = '<option value="">All</option>' +
      dcs.map(d => `<option value="${escHtml(d)}">${escHtml(d)}</option>`).join("");
    sel.value = cur;
  }
  renderFreight();
}

function renderFreight() {
  const dc   = (document.getElementById("freight-filter-dc")   || {}).value || "";
  const dist = (document.getElementById("freight-filter-dist") || {}).value || "";
  _freightInvoices = _freightAllInvoices.filter(r =>
    (!dc   || r.dest_dc     === dc) &&
    (!dist || r.distributor === dist)
  );
  // KPI tiles
  let cost = 0, pallets = 0, cases = 0;
  for (const r of _freightInvoices) {
    cost    += Number(r.total_due) || 0;
    pallets += Number(r.pallets)   || 0;
    cases   += Number(r.cases)     || 0;
  }
  const $ = id => document.getElementById(id);
  $("freight-kpi-count").textContent   = _freightInvoices.length.toLocaleString();
  $("freight-kpi-cost").textContent    = "$" + cost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  $("freight-kpi-pallets").textContent = pallets.toLocaleString();
  $("freight-kpi-cases").textContent   = cases.toLocaleString();
  $("freight-kpi-pp").textContent      = pallets ? ("$" + (cost / pallets).toFixed(2)) : "—";
  $("freight-kpi-pc").textContent      = cases   ? ("$" + (cost / cases).toFixed(2))   : "—";
  // Category tiles — sum line_items by description across visible rows.
  const cats = {};
  for (const r of _freightInvoices) {
    for (const li of (r.line_items || [])) {
      const d = (li.description || "Other").trim();
      cats[d] = (cats[d] || 0) + (Number(li.total) || 0);
    }
  }
  const catSorted = Object.entries(cats).sort((a, b) => b[1] - a[1]);
  const catBox = $("freight-kpi-categories");
  if (catBox) {
    if (!catSorted.length) {
      catBox.innerHTML = `<div class="subtitle">No line-item breakdown available for this filter.</div>`;
    } else {
      catBox.innerHTML = catSorted.map(([d, v]) => {
        const pct = cost ? (v / cost * 100).toFixed(1) : "0.0";
        const color = _freightCatColor(d);
        return `<div>
          <div class="subtitle" style="margin-bottom:2px">
            <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};vertical-align:middle;margin-right:6px"></span>${escHtml(d)}
          </div>
          <div style="font-size:18px;font-weight:700">$${v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
          <div class="subtitle" style="font-size:11px">${pct}% of spend</div>
        </div>`;
      }).join("");
    }
  }
  // Table
  renderFreightTable();
  drawFreightChart();
}

function freightPoMatch(r) {
  // Lineage PDFs put the distributor PO in the "PO:" reference. Strip
  // an "HHB-" prefix if present (Lineage's correction invoices reuse
  // the shipper ref as the PO).
  const raw = (r.po_number || "").toString();
  return raw.replace(/^HHB-/i, "");
}

function renderFreightTable() {
  const tbody = document.getElementById("freight-tbody");
  if (!tbody) return;
  const rows = _freightInvoices.slice().sort((a, b) => {
    return (b.ship_date || "").localeCompare(a.ship_date || "");
  });
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;padding:24px;color:var(--muted)">No freight invoices in this range.</td></tr>`;
  } else {
    tbody.innerHTML = rows.map(r => {
      const inv = escHtml(r.invoice_number || "");
      const sd  = escHtml(r.ship_date      || "");
      const dest= escHtml(r.dest_dc || r.consignee_name || "");
      const dist= distributorBadge(r.distributor);
      const po  = escHtml(freightPoMatch(r));
      const pal = (r.pallets || 0).toLocaleString();
      const cs  = (r.cases   || 0).toLocaleString();
      const tot = "$" + (Number(r.total_due) || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
      const pp  = r.pallets ? ("$" + (Number(r.cost_per_pallet) || 0).toFixed(2)) : "—";
      const pc  = r.cases   ? ("$" + (Number(r.cost_per_case)   || 0).toFixed(2)) : "—";
      const bd  = renderFreightBreakdown(r);
      return `<tr>
        <td>${sd}</td>
        <td><code>${inv}</code></td>
        <td>${dest}</td>
        <td>${dist}</td>
        <td>${po ? '<code>' + po + '</code>' : '<span style="color:var(--muted)">—</span>'}</td>
        <td style="text-align:right">${pal}</td>
        <td style="text-align:right">${cs}</td>
        <td style="text-align:right">${tot}</td>
        <td style="text-align:right">${pp}</td>
        <td style="text-align:right">${pc}</td>
        <td>${bd}</td>
      </tr>`;
    }).join("");
  }
  // Footer totals
  let cost = 0, pallets = 0, cases = 0;
  for (const r of rows) {
    cost    += Number(r.total_due) || 0;
    pallets += Number(r.pallets)   || 0;
    cases   += Number(r.cases)     || 0;
  }
  const tfoot = document.getElementById("freight-tfoot");
  if (tfoot) {
    tfoot.innerHTML = rows.length ? `<tr style="font-weight:600;background:var(--surface2)">
      <td colspan="5">Total (${rows.length} shipments)</td>
      <td style="text-align:right">${pallets.toLocaleString()}</td>
      <td style="text-align:right">${cases.toLocaleString()}</td>
      <td style="text-align:right">$${cost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</td>
      <td style="text-align:right">${pallets ? ('$' + (cost / pallets).toFixed(2)) : '—'}</td>
      <td style="text-align:right">${cases ? ('$' + (cost / cases).toFixed(2)) : '—'}</td>
      <td></td>
    </tr>` : "";
  }
}

function _freightBucket(sd, grain) {
  if (!sd) return "";
  if (grain === "day")   return sd;
  if (grain === "month") return sd.slice(0, 7);  // YYYY-MM
  // week: ISO week start (Monday)
  const d = new Date(sd + "T00:00:00Z");
  if (isNaN(d.getTime())) return sd;
  const day = (d.getUTCDay() + 6) % 7;           // Mon=0..Sun=6
  d.setUTCDate(d.getUTCDate() - day);
  return d.toISOString().slice(0, 10);
}

// Color palette for cost categories — stable across reloads.
const _FREIGHT_CAT_COLORS = {
  "BASIS ITEM":            "#1F77B4",
  "FUEL SURCHARGE":        "#FF7F0E",
  "LUMPER":                "#2CA02C",
  "DETENTION (UNLOADING)": "#D62728",
  "DETENTION":             "#D62728",
  "LAYOVER":               "#9467BD",
  "TONU":                  "#8C564B",
  "ACCESSORIAL":           "#E377C2",
};
function _freightCatColor(cat) {
  const k = (cat || "").toUpperCase();
  if (_FREIGHT_CAT_COLORS[k]) return _FREIGHT_CAT_COLORS[k];
  // Hash-based fallback so unknown categories still get a stable color.
  let h = 0; for (let i = 0; i < k.length; i++) h = ((h << 5) - h + k.charCodeAt(i)) | 0;
  const hue = Math.abs(h) % 360;
  return `hsl(${hue}, 65%, 48%)`;
}

function renderFreightBreakdown(r) {
  // Compact inline breakdown for the table — e.g. "Basis $608.58 · Fuel $319.50".
  const items = (r.line_items || []);
  if (!items.length) return '<span style="color:var(--muted)">—</span>';
  // Aggregate by description in case a single invoice has two BASIS ITEM lines.
  const byDesc = new Map();
  for (const li of items) {
    const d = (li.description || "Other").trim();
    byDesc.set(d, (byDesc.get(d) || 0) + (Number(li.total) || 0));
  }
  const SHORT = {
    "BASIS ITEM": "Basis", "FUEL SURCHARGE": "Fuel", "LUMPER": "Lumper",
    "DETENTION (UNLOADING)": "Detention", "DETENTION": "Detention",
    "LAYOVER": "Layover", "TONU": "TONU",
  };
  const parts = Array.from(byDesc.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([d, v]) => {
      const label = SHORT[d.toUpperCase()] || d.toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
      const color = _freightCatColor(d);
      return `<span style="display:inline-block;margin-right:8px;white-space:nowrap">
        <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};vertical-align:middle;margin-right:4px"></span>
        ${escHtml(label)} <strong>$${v.toFixed(2)}</strong>
      </span>`;
    });
  return `<div style="font-size:12px;line-height:1.7">${parts.join("")}</div>`;
}

function drawFreightChart() {
  const canvas = document.getElementById("freight-chart");
  if (!canvas || typeof Chart === "undefined") return;
  const grain = (document.getElementById("freight-group-by") || {}).value || "week";
  const metric = _freightMetric;
  // Group: bucket -> dest_dc -> running sums
  const bucketSet = new Set();
  const dcMap = new Map();   // dc -> {bucket: {cost, pallets, cases}}
  for (const r of _freightInvoices) {
    const b = _freightBucket(r.ship_date, grain);
    if (!b) continue;
    bucketSet.add(b);
    const dc = r.dest_dc || r.consignee_name || "Unknown";
    if (!dcMap.has(dc)) dcMap.set(dc, {});
    const tmap = dcMap.get(dc);
    if (!tmap[b]) tmap[b] = {cost: 0, pallets: 0, cases: 0};
    tmap[b].cost    += Number(r.total_due) || 0;
    tmap[b].pallets += Number(r.pallets)   || 0;
    tmap[b].cases   += Number(r.cases)     || 0;
  }
  const buckets = Array.from(bucketSet).sort();
  const dcs = Array.from(dcMap.keys()).sort();

  // "By category" mode — stacked bar chart of line-item totals per bucket.
  if (metric === "categories") {
    const catSeries = {};   // category -> [bucket totals]
    const bucketIdx = Object.fromEntries(buckets.map((b, i) => [b, i]));
    for (const r of _freightInvoices) {
      const b = _freightBucket(r.ship_date, grain);
      if (!b) continue;
      for (const li of (r.line_items || [])) {
        const d = (li.description || "Other").trim();
        if (!catSeries[d]) catSeries[d] = new Array(buckets.length).fill(0);
        catSeries[d][bucketIdx[b]] += Number(li.total) || 0;
      }
    }
    const catList = Object.keys(catSeries).sort((a, b) =>
      catSeries[b].reduce((s, x) => s + x, 0) - catSeries[a].reduce((s, x) => s + x, 0)
    );
    const datasets = catList.map(cat => ({
      label: cat,
      data: catSeries[cat].map(v => Number(v.toFixed(2))),
      backgroundColor: _freightCatColor(cat),
      borderColor: _freightCatColor(cat),
      stack: "cost",
    }));
    const sub = document.getElementById("freight-chart-sub");
    if (sub) sub.textContent = "Cost categories per " + grain;
    if (_freightChart) _freightChart.destroy();
    _freightChart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: { labels: buckets, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              label: ctx => ctx.dataset.label + ": $" + Number(ctx.parsed.y).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})
            }
          }
        },
        scales: {
          x: { stacked: true },
          y: { stacked: true, beginAtZero: true,
               ticks: { callback: v => "$" + Number(v).toLocaleString() } }
        }
      }
    });
    return;
  }

  // Stable color per DC
  const palette = [
    "#1F77B4","#FF7F0E","#2CA02C","#D62728","#9467BD",
    "#8C564B","#E377C2","#7F7F7F","#BCBD22","#17BECF",
    "#3B82F6","#F59E0B","#10B981","#EF4444","#8B5CF6",
  ];
  const datasets = dcs.map((dc, i) => {
    const buckets_for_dc = dcMap.get(dc) || {};
    return {
      label: dc,
      data: buckets.map(b => {
        const v = buckets_for_dc[b];
        if (!v) return null;
        if (metric === "total")      return Number(v.cost.toFixed(2));
        if (metric === "per_pallet") return v.pallets ? Number((v.cost / v.pallets).toFixed(2)) : null;
        if (metric === "per_case")   return v.cases   ? Number((v.cost / v.cases).toFixed(4))   : null;
        if (metric === "pallets")    return v.pallets;
        return null;
      }),
      borderColor: palette[i % palette.length],
      backgroundColor: palette[i % palette.length] + "33",
      tension: 0.25,
      spanGaps: true,
      pointRadius: 3,
    };
  });
  // Subtitle
  const labels = {
    total: "Total $ per " + grain,
    per_pallet: "$ per pallet per " + grain,
    per_case: "$ per case per " + grain,
    pallets: "Pallets shipped per " + grain,
  };
  const sub = document.getElementById("freight-chart-sub");
  if (sub) sub.textContent = labels[metric] || "";
  // Chart
  if (_freightChart) _freightChart.destroy();
  const ctx = canvas.getContext("2d");
  _freightChart = new Chart(ctx, {
    type: "line",
    data: { labels: buckets, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              const v = ctx.parsed.y;
              if (v == null) return ctx.dataset.label + ": —";
              const fmt = (metric === "pallets")
                ? v.toLocaleString()
                : "$" + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
              return ctx.dataset.label + ": " + fmt;
            }
          }
        }
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: {
            callback: v => (metric === "pallets")
                ? v
                : "$" + Number(v).toLocaleString()
          }
        }
      }
    }
  });
}


async function loadProduction() {
  await ensureWarehouses();
  // Pull production records AND inventory in parallel — the inventory call
  // gives us live on_order data so we can show ship/arrival on each
  // production row by matching po_number.
  const [records, inventory] = await Promise.all([
    api('/api/production'),
    api('/api/inventory'),
  ]);
  prodCache = records;
  prodPoLookup = _buildProdPoLookup(inventory || []);

  // Populate warehouse filter from the records actually present
  const wSel = document.getElementById('prod-filter-warehouse');
  const presentWh = Array.from(new Set(records.map(r => r.warehouse).filter(Boolean))).sort();
  if (wSel.options.length <= 1 + presentWh.length - 1) {
    wSel.innerHTML = '<option value="">All</option>' +
      presentWh.map(w => `<option>${escHtml(w)}</option>`).join('');
  }

  // Wire sort click handlers once
  const head = document.getElementById('prod-thead-row');
  if (head && !head.dataset.sortWired) {
    head.querySelectorAll('th.sortable').forEach(th => {
      th.addEventListener('click', () => onProdSortClick(th.dataset.sort));
    });
    head.dataset.sortWired = '1';
  }

  renderProductionDetail();
  loadProductionSummary();
}

async function loadProductionSummary() {
  const data = await api('/api/production/summary?period=' + encodeURIComponent(prodPeriod));
  renderProductionSummary(data);
}

function renderProductionSummary(data) {
  const buckets = data.buckets || [];
  const periodLabel = { day: 'Daily', week: 'Weekly', month: 'Monthly' }[prodPeriod] || 'Weekly';
  document.getElementById('prod-period-label').textContent =
    `Rolled up by ${periodLabel.toLowerCase()}, newest first`;

  // Summary cards — current period totals
  const current = buckets[0] || { total_cs: 0, by_distributor: {}, by_variety: {} };
  const cumulative = buckets.reduce((acc, b) => {
    acc.total_cs += b.total_cs || 0;
    return acc;
  }, { total_cs: 0 });
  const cards = document.getElementById('prod-summary-cards');
  const topDist = Object.entries(current.by_distributor || {})
    .sort((a, b) => b[1] - a[1]);
  const cardHtml = [
    `<div class="card" style="padding:16px 20px">
       <div style="color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:0.5px">This ${prodPeriod}</div>
       <div style="font-size:28px;font-weight:700">${(current.total_cs || 0).toFixed(0)} <span style="color:var(--muted);font-size:14px">cs</span></div>
       <div style="color:var(--muted);font-size:12px">${current.start ? formatDate(current.start) : ''}${current.end && current.end !== current.start ? ' – ' + formatDate(current.end) : ''}</div>
     </div>`,
    `<div class="card" style="padding:16px 20px">
       <div style="color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:0.5px">Cumulative (${buckets.length} ${prodPeriod}${buckets.length === 1 ? '' : 's'})</div>
       <div style="font-size:28px;font-weight:700">${cumulative.total_cs.toFixed(0)} <span style="color:var(--muted);font-size:14px">cs</span></div>
       <div style="color:var(--muted);font-size:12px">all production logged to date</div>
     </div>`,
  ];
  topDist.slice(0, 3).forEach(([name, n]) => {
    cardHtml.push(
      `<div class="card" style="padding:16px 20px">
         <div style="color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:0.5px">This ${prodPeriod} — ${escHtml(name)}</div>
         <div style="font-size:28px;font-weight:700">${n.toFixed(0)} <span style="color:var(--muted);font-size:14px">cs</span></div>
       </div>`
    );
  });
  cards.innerHTML = cardHtml.join('');

  // Build a {date -> [records]} index from prodCache so we can compute
  // distinct production dates per bucket (the API summary only ships
  // aggregates).
  const recordsByDate = {};
  (prodCache || []).forEach(r => {
    const d = (r.production_date || '').slice(0, 10);
    if (!d) return;
    (recordsByDate[d] = recordsByDate[d] || []).push(r);
  });

  // Cache the buckets so the pager handlers can re-render without
  // re-fetching the summary endpoint.
  _prodSummaryBuckets = buckets.slice();

  // Bucket table
  const tbody = document.getElementById('prod-summary-tbody');
  if (buckets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No production records yet.</td></tr>';
    _updateProdSummaryPager(0, 0, 0);
    return;
  }

  // Pagination — slice to the current page.
  const _totalBuckets = buckets.length;
  const _totalPages = Math.max(1, Math.ceil(_totalBuckets / prodSummaryPageSize));
  if (prodSummaryPage >= _totalPages) prodSummaryPage = _totalPages - 1;
  if (prodSummaryPage < 0) prodSummaryPage = 0;
  const _sliceStart = prodSummaryPage * prodSummaryPageSize;
  const _sliceEnd = Math.min(_sliceStart + prodSummaryPageSize, _totalBuckets);
  const _pagedBuckets = buckets.slice(_sliceStart, _sliceEnd);
  _updateProdSummaryPager(_sliceStart + 1, _sliceEnd, _totalBuckets);

  const todayISO = new Date().toISOString().slice(0, 10);
  tbody.innerHTML = _pagedBuckets.map(b => {
    const dist = Object.entries(b.by_distributor || {})
      .sort((x, y) => y[1] - x[1])
      .map(([n, c]) => `${escHtml(n)}&nbsp;<span style="color:var(--muted)">${c}</span>`)
      .join(' · ');
    const variety = Object.entries(b.by_variety || {})
      .sort((x, y) => y[1] - x[1])
      .slice(0, 5)
      .map(([n, c]) => `${escHtml(n)}&nbsp;<span style="color:var(--muted)">${c}</span>`)
      .join(' · ');
    const rangeLabel = b.start === b.end ? formatDate(b.start) : `${formatDate(b.start)} – ${formatDate(b.end)}`;

    // ---- Production-day gap analysis ---------------------------------
    // Expected production days = every Mon-Sat (skip Sunday) in the
    // bucket range, BUT clipped at today so we don't flag the future
    // half of an in-progress week / month.
    const start = new Date(b.start + 'T00:00:00');
    const end   = new Date(b.end   + 'T00:00:00');
    const expected = [];
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
      const iso = d.toISOString().slice(0, 10);
      if (iso > todayISO) break;
      if (d.getDay() === 0) continue;  // 0 = Sunday
      expected.push(iso);
    }
    const actualSet = new Set(expected.filter(d => recordsByDate[d]));
    const missing   = expected.filter(d => !actualSet.has(d));
    const inProgress = b.end > todayISO;
    const daysOk     = inProgress
      ? actualSet.size >= 1   // current bucket: just need at least one day so far
      : actualSet.size >= 5;  // closed bucket: 5+ days is healthy
    const daysCellStyle = daysOk ? '' : 'color:var(--warning,#c00);font-weight:600';
    const missingLabel = missing.length === 0
      ? '<span style="color:var(--muted)">—</span>'
      : missing.slice(0, 3).map(d => formatDate(d)).join(', ')
        + (missing.length > 3 ? ` <span style="color:var(--muted)">+${missing.length - 3} more</span>` : '');
    const expectedCount = expected.length || (inProgress ? '—' : 0);
    const rowStyle = !daysOk ? ' style="background:rgba(217, 119, 6, 0.08)"' : '';

    return `<tr${rowStyle}>
      <td style="font-weight:600">${escHtml(b.key)}${inProgress ? ' <span style="color:var(--muted);font-size:11px">in progress</span>' : ''}</td>
      <td>${rangeLabel}</td>
      <td style="text-align:right;font-weight:600">${(b.total_cs || 0).toFixed(0)}</td>
      <td style="text-align:center;${daysCellStyle}">${actualSet.size}<span style="color:var(--muted);font-weight:normal"> / ${expectedCount}</span></td>
      <td style="font-size:12px">${missingLabel}</td>
      <td>${dist || '<span style="color:var(--muted)">—</span>'}</td>
      <td>${variety || '<span style="color:var(--muted)">—</span>'}</td>
    </tr>`;
  }).join('');
}

function _prodSortValue(r, key) {
  if (key === 'production_date') return r.production_date || '';
  if (key === 'warehouse')       return (r.warehouse || '').toLowerCase();
  if (key === 'distributor')     return (r.distributor || '').toLowerCase();
  if (key === 'total_cases')     return Number(r.total_cases) || 0;
  return '';
}

function renderProductionDetail() {
  const tbody = document.getElementById('prod-tbody');
  if (!tbody) return;
  let rows = prodCache.slice();
  const distF = document.getElementById('prod-filter-distributor').value;
  const whF   = document.getElementById('prod-filter-warehouse').value;
  const qRaw  = (document.getElementById('prod-filter-search')?.value || '').trim().toLowerCase();
  if (distF) rows = rows.filter(r => (r.distributor || '') === distF);
  if (whF)   rows = rows.filter(r => (r.warehouse  || '') === whF);
  if (qRaw) {
    rows = rows.filter(r => {
      const po = (r.po_number || '').toLowerCase();
      const dIso = (r.production_date || '').toLowerCase();        // 2026-05-08
      const dFmt = formatDate(r.production_date || '').toLowerCase(); // 05/08/2026
      if (po.includes(qRaw) || dIso.includes(qRaw) || dFmt.includes(qRaw)) return true;
      // Also match any lot code on a line of this record (case-insensitive)
      const lines = r.lines || [];
      for (const L of lines) {
        const lot = (L.lot_number || '').toLowerCase();
        if (lot && lot.includes(qRaw)) return true;
      }
      return false;
    });
  }

  if (prodSortKey && prodSortDir) {
    const k = prodSortKey;
    const d = prodSortDir;
    rows.sort((a, b) => {
      const va = _prodSortValue(a, k);
      const vb = _prodSortValue(b, k);
      if (va < vb) return -1 * d;
      if (va > vb) return  1 * d;
      return 0;
    });
  }

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">No production records match this filter.</td></tr>';
    updateSortIndicators('pending-tbody');
    _updateProdPager(0, 0, 0);
    return;
  }

  // Pagination — slice to the current page.
  const totalRows = rows.length;
  const totalPages = Math.max(1, Math.ceil(totalRows / prodPageSize));
  if (prodPage >= totalPages) prodPage = totalPages - 1;
  if (prodPage < 0) prodPage = 0;
  const sliceStart = prodPage * prodPageSize;
  const sliceEnd = Math.min(sliceStart + prodPageSize, totalRows);
  rows = rows.slice(sliceStart, sliceEnd);
  _updateProdPager(sliceStart + 1, sliceEnd, totalRows);

  tbody.innerHTML = rows.map(r => {
    const lines = (r.lines || []).slice().sort((a, b) => a.variety.localeCompare(b.variety));
    const head = lines.slice(0, 4).map(L =>
      `${escHtml(L.variety)}&nbsp;<span style="color:var(--muted)">${L.cs_count}</span>`
    ).join(' · ');
    const more = lines.length > 4 ? ` · <span style="color:var(--muted)">+${lines.length - 4} more</span>` : '';
    const tipText = lines.map(L => {
      const lot = L.lot_number ? `  [lot ${L.lot_number}]` : '';
      return `${L.variety}: ${L.cs_count} cs${lot}`;
    }).join('\n');
    const errBadge = r.parse_error
      ? '<div style="color:var(--warning,#c00);font-size:11px;margin-top:2px">PDF needs re-scan</div>'
      : '';
    const srcSubj = (r.source_subject || '').replace(/^Daily [Pp]roduction\s*/, '');
    const srcSender = (r.source_sender || '').match(/<([^>]+)>/)?.[1] || r.source_sender || '';
    // Cross-link to live PO data (if any line of this PO is still
    // pending). When no match is found the PO has already rolled into
    // the SKU's quantity — show as "Arrived".
    const po = (r.po_number || '').trim();
    const linked = po ? prodPoLookup[po] : null;
    let shipCell, arrivalCell, statusCell;
    if (linked) {
      shipCell = linked.ship_date
        ? formatDate(linked.ship_date)
        : '<span style="color:var(--muted)">—</span>';
      arrivalCell = linked.arrival_date
        ? formatDate(linked.arrival_date)
        : '<span style="color:var(--muted)">—</span>';
      const trigger = linked.arrival_date || linked.eta;
      const overdue = trigger && new Date(trigger) < new Date();
      if (overdue) {
        statusCell = `<span class="badge badge-red" style="font-size:10px">Overdue</span>`;
      } else if (linked.ship_date) {
        statusCell = `<span class="badge badge-blue" style="font-size:10px">In transit</span>`;
      } else {
        statusCell = `<span class="badge badge-yellow" style="font-size:10px">Open</span>`;
      }
    } else if (po) {
      // PO no longer in on_order = it rolled over into quantity
      shipCell    = '<span style="color:var(--muted)">—</span>';
      arrivalCell = '<span style="color:var(--muted)">—</span>';
      statusCell  = `<span class="badge badge-green" style="font-size:10px">Arrived</span>`;
    } else {
      shipCell    = '<span style="color:var(--muted)">—</span>';
      arrivalCell = '<span style="color:var(--muted)">—</span>';
      statusCell  = '<span style="color:var(--muted)">—</span>';
    }
    return `<tr>
      <td style="white-space:nowrap;font-weight:600">${r.production_date ? formatDate(r.production_date) : '<span style="color:var(--warning,#c00)">—</span>'}</td>
      <td>${escHtml(r.warehouse || '')}${r.warehouse_raw && r.warehouse_raw !== r.warehouse ? ` <span style="color:var(--muted);font-size:11px">(${escHtml(r.warehouse_raw)})</span>` : ''}</td>
      <td>${distributorBadge(r.distributor || '')}</td>
      <td style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(r.po_number || '—')}</td>
      <td title="${escAttr(tipText)}" style="font-size:12px">
        <div>${head}${more}${errBadge}</div>
        <div style="color:var(--muted);font-size:11px">${lines.length} item${lines.length === 1 ? '' : 's'}</div>
      </td>
      <td style="text-align:right;font-weight:600">${(r.total_cases || 0).toFixed(0)} <span style="color:var(--muted);font-size:11px">cs</span></td>
      <td style="white-space:nowrap;font-size:12px">${shipCell}</td>
      <td style="white-space:nowrap;font-size:12px">${arrivalCell}</td>
      <td>${statusCell}</td>
      <td style="font-size:11px;color:var(--muted)">
        <div>${escHtml(srcSubj || '—')}</div>
        <div>${escHtml(srcSender)}</div>
      </td>
    </tr>`;
  }).join('');
  updateSortIndicators('prod-tbody');
}

// Hook prod-tbody into the shared updateSortIndicators map
(function _wireProdSortIndicators() {
  const _old = window.updateSortIndicators;
  window.updateSortIndicators = function(tbodyId) {
    if (tbodyId === 'prod-tbody') {
      const head = document.getElementById('prod-thead-row');
      if (!head) return;
      head.querySelectorAll('th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === prodSortKey && prodSortDir === 1) th.classList.add('sort-asc');
        else if (th.dataset.sort === prodSortKey && prodSortDir === -1) th.classList.add('sort-desc');
      });
      return;
    }
    _old(tbodyId);
  };
})();

async function onShipDateChange(poNumber, shipDate, source) {
  if (!poNumber) return;
  source = source || 'inventory';
  const isCW = source === 'chefs_warehouse';
  try {
    const endpoint = isCW
      ? '/api/chefs-warehouse/ship-date'
      : '/api/on-order/ship-date';
    const payload = isCW
      ? { po_number: poNumber, ship_date: shipDate || '' }
      : { po_number: poNumber, ship_date: shipDate || null };
    const r = await api(endpoint, 'POST', payload);
    if (r && r.ok) {
      if (isCW) {
        toast(`Ship date ${shipDate || 'cleared'} for CW PO ${poNumber}`);
      } else {
        toast(`Ship date ${shipDate || 'cleared'} for PO ${poNumber} (${r.entries_updated} line${r.entries_updated === 1 ? '' : 's'})`);
      }
      loadPendingPOs();
    } else {
      toast('Failed to update ship date', 'error');
    }
  } catch (exc) {
    toast('Error: ' + exc.message, 'error');
  }
}

// -------------------------------------------------------------------------
// Date helper — render an ISO datetime / "YYYY-MM-DD" string as MM/DD/YYYY.
// Returns the input unchanged if it doesn't start with a parseable date.
// -------------------------------------------------------------------------
function formatDate(s) {
  if (!s) return '';
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return s;
  return m[2] + '/' + m[3] + '/' + m[1];
}

// -------------------------------------------------------------------------
// Escape HTML helpers
// -------------------------------------------------------------------------
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) {
  return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;');
}

// -------------------------------------------------------------------------
// Boot — session cookie handles auth, so just load the dashboard. If the
// session has expired, api() bounces the user to /login.
// -------------------------------------------------------------------------
loadInventory();
// -------------------------------------------------------------------------
// Traceability tab
// -------------------------------------------------------------------------
let _traceLastResults = [];      // raw API records cached for paging
let tracePageSize = 10;
let tracePage = 0;
let _traceMfgCodeMap = {};       // {"1150": "Plain", ...}
let _traceInitialized = false;

async function initTraceability() {
  if (_traceInitialized) return;
  _traceInitialized = true;
  // Populate mfg-code dropdown.
  try {
    const r = await api('/api/traceability/search?to=1900-01-01');  // empty result, just to read mfg_code_map
    _traceMfgCodeMap = r.mfg_code_map || {};
  } catch (e) { /* fall through */ }
  const mfgSel = document.getElementById('trace-filter-mfg');
  if (mfgSel && Object.keys(_traceMfgCodeMap).length) {
    const opts = Object.keys(_traceMfgCodeMap).sort().map(code =>
      `<option value="${escAttr(code)}">${escHtml(code)} &mdash; ${escHtml(_traceMfgCodeMap[code])}</option>`
    );
    mfgSel.innerHTML = '<option value="">All varieties</option>' + opts.join('');
  }
  // Populate warehouse dropdown from production records (only DCs that
  // have ever shown up — keeps the picker tight).
  try {
    const recs = await api('/api/production');
    const whs = Array.from(new Set(recs.map(r => r.warehouse).filter(Boolean))).sort();
    const sel = document.getElementById('trace-filter-warehouse');
    if (sel) {
      sel.innerHTML = '<option value="">All</option>' +
        whs.map(w => `<option>${escHtml(w)}</option>`).join('');
    }
  } catch (e) { /* ignore */ }
}
function onTraceFilterChange() {
  tracePage = 0;
  // Debounce a search for typed input fields.
  clearTimeout(window._traceDebounce);
  window._traceDebounce = setTimeout(runTraceabilitySearch, 200);
}
function onTracePageSizeChange() {
  const v = parseInt(document.getElementById('trace-page-size').value, 10);
  tracePageSize = (v > 0 ? v : 10);
  tracePage = 0;
  renderTraceResults();
}
function onTracePageNav(delta) {
  tracePage = Math.max(0, tracePage + delta);
  renderTraceResults();
}
function _updateTracePager(first, last, total) {
  const info = document.getElementById('trace-pager-info');
  const ind  = document.getElementById('trace-page-indicator');
  const prev = document.getElementById('trace-page-prev');
  const next = document.getElementById('trace-page-next');
  if (!info || !ind || !prev || !next) return;
  if (total === 0) {
    info.textContent = 'No production records match the current filters.';
    ind.textContent = '';
    prev.disabled = true;
    next.disabled = true;
    return;
  }
  info.textContent = `Showing dates ${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}`;
  const totalPages = Math.max(1, Math.ceil(total / tracePageSize));
  ind.textContent = `Page ${tracePage + 1} of ${totalPages}`;
  prev.disabled = tracePage <= 0;
  next.disabled = tracePage >= totalPages - 1;
}
function resetTraceabilityFilters() {
  document.getElementById('trace-filter-from').value = '';
  document.getElementById('trace-filter-to').value = '';
  document.getElementById('trace-filter-lot').value = '';
  document.getElementById('trace-filter-mfg').value = '';
  document.getElementById('trace-filter-warehouse').value = '';
  document.getElementById('trace-filter-distributor').value = '';
  runTraceabilitySearch();
}
async function runTraceabilitySearch() {
  const qs = new URLSearchParams();
  const from = document.getElementById('trace-filter-from').value;
  const to   = document.getElementById('trace-filter-to').value;
  const lot  = document.getElementById('trace-filter-lot').value.trim();
  const mfg  = document.getElementById('trace-filter-mfg').value;
  const wh   = document.getElementById('trace-filter-warehouse').value;
  const dist = document.getElementById('trace-filter-distributor').value;
  if (from) qs.set('from', from);
  if (to)   qs.set('to', to);
  if (lot)  qs.set('lot', lot);
  if (mfg)  qs.set('mfg_code', mfg);
  if (wh)   qs.set('warehouse', wh);
  if (dist) qs.set('distributor', dist);
  let res;
  try {
    res = await api('/api/traceability/search?' + qs.toString());
  } catch (e) {
    document.getElementById('trace-results-container').innerHTML =
      `<div class="empty" style="padding:24px;color:var(--red)">Search failed: ${escHtml(String(e))}</div>`;
    return;
  }
  _traceLastResults = res.records || [];
  tracePage = 0;
  renderTraceResults();
}

function _poStatusBadge(status, ship, arrival, eta) {
  if (status === 'canceled') return '<span class="badge badge-red">canceled</span>';
  if (status === 'arrived')  return '<span class="badge badge-green">arrived</span>';
  if (status === 'pending') {
    const trig = arrival || eta;
    const overdue = trig && new Date(trig) < new Date();
    if (overdue) return '<span class="badge badge-red">overdue</span>';
    if (ship)    return '<span class="badge badge-blue">in transit</span>';
    return '<span class="badge badge-yellow">open</span>';
  }
  return '<span class="badge" style="color:var(--muted)">—</span>';
}

function renderTraceResults() {
  const container = document.getElementById('trace-results-container');
  if (!container) return;
  const recs = _traceLastResults || [];
  if (recs.length === 0) {
    container.innerHTML = '<div class="empty" style="padding:24px">No production records match the current filters.</div>';
    _updateTracePager(0, 0, 0);
    return;
  }
  // Group by production_date.
  const byDate = {};
  recs.forEach(r => {
    const d = (r.production_date || '').slice(0, 10) || '__unknown__';
    (byDate[d] = byDate[d] || []).push(r);
  });
  const dateKeys = Object.keys(byDate).sort((a,b) => a < b ? 1 : -1);  // newest first
  const totalDates = dateKeys.length;
  const totalPages = Math.max(1, Math.ceil(totalDates / tracePageSize));
  if (tracePage >= totalPages) tracePage = totalPages - 1;
  if (tracePage < 0) tracePage = 0;
  const sliceStart = tracePage * tracePageSize;
  const sliceEnd = Math.min(sliceStart + tracePageSize, totalDates);
  const pageDates = dateKeys.slice(sliceStart, sliceEnd);
  _updateTracePager(sliceStart + 1, sliceEnd, totalDates);

  container.innerHTML = pageDates.map(d => {
    const records = byDate[d];
    const dayCs = records.reduce((s, r) => s + (r.total_cases || 0), 0);
    const dayLabel = (d === '__unknown__') ? 'Unknown date' : formatDate(d);
    const cards = records.map(r => {
      const ship    = r.ship_date    ? formatDate(r.ship_date)    : '<span style="color:var(--muted)">—</span>';
      const arrival = r.arrival_date ? formatDate(r.arrival_date) : '<span style="color:var(--muted)">—</span>';
      const eta     = r.eta          ? formatDate(r.eta)          : '<span style="color:var(--muted)">—</span>';
      const badge   = _poStatusBadge(r.po_status, r.ship_date, r.arrival_date, r.eta);
      const lineRows = (r.lines || []).map(L => {
        const usage = L.usage_total_cs > 0
          ? `${(L.usage_total_cs).toFixed(0)} cs <span style="color:var(--muted);font-size:11px">(${L.usage_event_count})</span>`
          : '<span style="color:var(--muted)">—</span>';
        const onHand = L.on_hand_now > 0
          ? `${(L.on_hand_now).toFixed(0)} cs`
          : '<span style="color:var(--muted)">—</span>';
        return `<tr>
          <td style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(L.lot_number || '')}</td>
          <td style="font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)">${escHtml((L.lot_number || '').slice(0,4))}</td>
          <td>${escHtml(L.variety || '')}</td>
          <td style="text-align:right;font-weight:600">${(L.cs_count || 0).toFixed(0)} cs</td>
          <td style="text-align:right">${onHand}</td>
          <td style="text-align:right">${usage}</td>
        </tr>`;
      }).join('');
      return `<div style="padding:14px 16px;border-top:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px">
          <div>
            <span style="font-weight:600">${escHtml(r.warehouse || '—')}</span>
            <span style="color:var(--muted);font-size:12px;margin-left:8px">${distributorBadge(r.distributor || '')} ${escHtml(r.distributor || '')}</span>
            <span style="color:var(--muted);font-size:12px;margin-left:8px">PO <span style="font-family:ui-monospace,monospace">${escHtml(r.po_number || '—')}</span></span>
          </div>
          <div style="font-size:12px;color:var(--muted)">
            Status ${badge}
            &middot; Ship ${ship} &middot; Arrival ${arrival} &middot; ETA ${eta}
          </div>
        </div>
        <table style="margin:0;font-size:12px">
          <thead><tr>
            <th>Lot #</th>
            <th>Mfg code</th>
            <th>Variety</th>
            <th style="text-align:right">Cases produced</th>
            <th style="text-align:right">On hand now</th>
            <th style="text-align:right">Usage cs (events)</th>
          </tr></thead>
          <tbody>${lineRows}</tbody>
        </table>
      </div>`;
    }).join('');
    return `<div style="border-top:2px solid var(--border)">
      <div style="padding:10px 16px;background:var(--surface-alt,#f7f7f9);display:flex;justify-content:space-between;align-items:center">
        <div style="font-weight:600">${dayLabel}</div>
        <div style="font-size:12px;color:var(--muted)">${records.length} record${records.length===1?'':'s'} &middot; ${dayCs.toFixed(0)} cs</div>
      </div>
      ${cards}
    </div>`;
  }).join('');
}


// -------------------------------------------------------------------------
// Report -> Top Consumed (Toast sales)
// -------------------------------------------------------------------------
let toastSalesPeriod = 'week';
let _toastSalesLocationsLoaded = false;
// Always a YYYY-MM-DD - the report now shows exactly the bucket
// containing this date. Seeded to today's date on first declaration so
// the initial page load renders one bucket (the current week/month)
// rather than the most-recent-N-with-data view.
let toastSalesAnchor = new Date().toISOString().slice(0, 10);
function setToastSalesPeriod(p) {
  toastSalesPeriod = p;
  ['week','month'].forEach(m => {
    const b = document.getElementById('toast-sales-period-' + m);
    if (b) b.classList.toggle('active', m === p);
  });
  loadToastSales();
}
function _toastSalesRef() {
  return toastSalesAnchor ? new Date(toastSalesAnchor + 'T00:00:00') : new Date();
}
function shiftToastSalesOffset(delta) {
  const d = _toastSalesRef();
  if (toastSalesPeriod === 'week') d.setDate(d.getDate() + 7 * delta);
  else d.setMonth(d.getMonth() + delta);
  toastSalesAnchor = d.toISOString().slice(0, 10);
  const inp = document.getElementById('toast-sales-date');
  if (inp) inp.value = toastSalesAnchor;
  loadToastSales();
}
function resetToastSalesOffset() {
  // Today = explicitly select today's date so the report shows the
  // bucket containing today (one week or month, not the historical
  // tail). Previously this cleared the anchor and showed multiple
  // buckets.
  toastSalesAnchor = new Date().toISOString().slice(0, 10);
  const inp = document.getElementById('toast-sales-date');
  if (inp) inp.value = toastSalesAnchor;
  loadToastSales();
}
function onToastSalesDateChange() {
  const v = document.getElementById('toast-sales-date').value;
  if (v) {
    toastSalesAnchor = v;
    loadToastSales();
  }
}
async function ensureToastLocationDropdown() {
  if (_toastSalesLocationsLoaded) return;
  _toastSalesLocationsLoaded = true;
  try {
    const r = await api('/api/sales/locations');
    const sel = document.getElementById('toast-sales-location');
    if (sel && r.locations) {
      const opts = r.locations.map(L => {
        const name  = L.location || L.restaurant_guid;
        const state = L.state || '';
        const label = escHtml(state ? `${name}, ${state}` : name);
        return `<option value="${escAttr(L.restaurant_guid)}">${label}</option>`;
      });
      sel.innerHTML = '<option value="">All locations</option>' + opts.join('');
    }
  } catch (e) { /* fallthrough */ }
  // Pre-fill the date picker with the active anchor so the user can
  // see what period they're looking at.
  const inp = document.getElementById('toast-sales-date');
  if (inp && !inp.value) inp.value = toastSalesAnchor;
}
async function loadToastSales() {
  await ensureToastLocationDropdown();
  const loc = document.getElementById('toast-sales-location').value;
  const qs = new URLSearchParams();
  qs.set('period', toastSalesPeriod);
  if (loc) qs.set('location', loc);
  if (toastSalesAnchor) qs.set('end_date', toastSalesAnchor);
  const subtitle = document.getElementById('toast-sales-subtitle');
  if (subtitle) subtitle.textContent =
    `Top-selling items by retail $ from Toast POS with sales mix %. Pulled per location, aggregated by ${toastSalesPeriod}.`;

  // When the user picks a specific date the backend may fetch live
  // from Toast - that can take up to ~30s. Show an explicit loading
  // state instead of leaving the previous render on screen.
  const container = document.getElementById('toast-sales-container');
  if (toastSalesAnchor) {
    container.innerHTML = `<div class="empty" style="padding:18px;color:var(--muted)">
      <div style="display:flex;align-items:center;gap:8px">
        <div class="spinner" style="width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--text);border-radius:50%;animation:spin 0.8s linear infinite"></div>
        <span>Checking cache${loc ? ' for the selected location' : ' for all retail locations'}, fetching from Toast if needed (up to 30s)...</span>
      </div>
    </div>`;
  }

  let res;
  try {
    res = await api('/api/report/toast-sales?' + qs.toString());
  } catch (e) {
    container.innerHTML =
      `<div class="empty" style="padding:18px;color:var(--red)">Failed: ${escHtml(String(e))}</div>`;
    return;
  }
  const buckets = (res && res.buckets) || [];
  const fetchMeta = (res && res.fetch) || {};
  if (!buckets.length) {
    container.innerHTML = '<div class="empty" style="padding:18px">No Toast sales rows ingested yet. Run the Toast sync from a Cowork session to populate.</div>';
    return;
  }
  // Surface the fetch result in the subtitle so the user knows whether
  // they got cached data or a fresh Toast pull, and any auth issues.
  if (subtitle && toastSalesAnchor) {
    let tag = '';
    if (fetchMeta.fetch_error === 'toast_not_configured') {
      tag = ' · cache only (set TOAST_CLIENT_ID/SECRET/HOSTNAME on Render to enable live fetch)';
    } else if (fetchMeta.fetch_error) {
      tag = ` · live fetch failed: ${escHtml(fetchMeta.fetch_error)}`;
    } else if (fetchMeta.rows_fetched > 0) {
      tag = ` · pulled ${fetchMeta.rows_fetched} fresh rows from Toast`;
    } else if (fetchMeta.fetched_at) {
      tag = ' · fetched from Toast (no new orders found)';
    } else {
      tag = ' · served from cache';
    }
    subtitle.textContent =
      `Top-selling items by retail $ from Toast POS with sales mix %, aggregated by ${toastSalesPeriod}.${tag}`;
  }
  container.innerHTML = buckets.map(b => {
    const headerLabel = b.label || b.key;
    const items = b.items || [];
    const itemsHtml = items.map((it, idx) => `<tr>
      <td style="color:var(--muted);width:28px;text-align:right">${idx + 1}.</td>
      <td>${escHtml(it.item)}${it.menu_group ? ` <span style="color:var(--muted);font-size:11px">[${escHtml(it.menu_group)}]</span>` : ''}</td>
      <td style="text-align:right;font-weight:600">$${it.gross.toFixed(2)}</td>
      <td style="text-align:right;color:var(--muted)">${it.qty.toLocaleString()}</td>
      <td style="text-align:right;font-weight:600">${it.mix_pct.toFixed(1)}%</td>
    </tr>`).join('');
    const emptyHelp = fetchMeta.fetch_error
      ? `Live Toast pull failed (${escHtml(fetchMeta.fetch_error)}). Falling back to cached data, which has no rows for this ${toastSalesPeriod}.`
      : `No Toast sales found for this ${toastSalesPeriod}${loc ? ' at this location' : ''}. Either the restaurant had no orders that ${toastSalesPeriod === 'week' ? 'week' : 'month'}, or the data hasn't been ingested yet.`;
    const body = items.length
      ? `<table style="margin:0;font-size:12px">
        <thead><tr>
          <th></th><th>Item</th>
          <th style="text-align:right">Gross $</th>
          <th style="text-align:right">Qty</th>
          <th style="text-align:right">Mix %</th>
        </tr></thead>
        <tbody>${itemsHtml}</tbody>
      </table>`
      : `<div class="empty" style="padding:12px;color:var(--muted);font-size:12px">${emptyHelp}</div>`;
    return `<div style="border-top:1px solid var(--border);padding:10px 0">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
        <div style="font-weight:600">${escHtml(headerLabel)}</div>
        <div style="color:var(--muted);font-size:12px">$${(b.total_gross || 0).toFixed(2)} gross &middot; ${b.item_count} items</div>
      </div>
      ${body}
    </div>`;
  }).join('');
}

// -------------------------------------------------------------------------
// Production Planning tab -- renders /api/planning/guide (Phase 4 output).
// -------------------------------------------------------------------------
async function loadProductionGuide() {
  const sum = document.getElementById('planner-summary');
  const body = document.getElementById('planner-body');
  if (!sum || !body) return;
  sum.innerHTML = '<div style="color:var(--muted)">Loading the weekly guide…</div>';
  body.innerHTML = '';
  let g;
  try { g = await api('/api/planning/guide'); }
  catch (e) { sum.innerHTML = '<div style="color:var(--red)">Failed to load guide: ' + escHtml(String(e)) + '</div>'; return; }
  if (!g || !g.ok) { sum.innerHTML = '<div style="color:var(--red)">Guide unavailable.</div>'; return; }
  const s = g.summary || {}, cap = g.capacity || {}, ba = g.buildahead || {};
  const chip = (l, v, c) => `<span class="badge ${c || 'badge-gray'}" style="font-size:12px;margin-right:6px">${escHtml(l)}: ${escHtml(String(v))}</span>`;
  sum.innerHTML = `<div class="card" style="padding:14px 18px">
    <div style="margin-bottom:8px">
      ${chip('Bake now', (s.produce_now_cs || 0) + ' cs / ' + (s.produce_now_pos || 0) + ' POs', (s.produce_now_cs ? 'badge-red' : 'badge-green'))}
      ${chip('Top-4', (s.produce_now_top4_cs || 0) + ' cs', 'badge-yellow')}
      ${chip('Buffer watch', s.buffer_watch || 0, (s.buffer_watch ? 'badge-yellow' : 'badge-green'))}
    </div>
    <div style="font-size:13px">Capacity: <strong>${cap.committed_cs || 0}</strong> cs (${cap.committed_pallets || 0} pallets) / ${cap.weekly_dependable_cs || 0} dependable (${cap.utilization_pct_of_dependable || 0}%) &mdash; ${escHtml(cap.note || '')}</div>
    <div style="font-size:13px;margin-top:4px">Build-ahead spare: <strong>${ba.spare_capacity_pallets_max || 0}</strong> pallets (within ${ba.freezer_pallet_cap || '—'}-pallet freezer).</div>
    ${g.toast_note ? `<div style="font-size:13px;margin-top:6px;color:var(--accent)">&#128200; ${escHtml(g.toast_note)}</div>` : ''}
  </div>`;
  const q = g.production_queue || [];
  const qTbl = q.length ? `<div class="card" style="margin-bottom:16px">
    <div style="padding:10px 16px;border-bottom:1px solid var(--border)"><span class="badge badge-red" style="font-size:10px">PRODUCTION QUEUE</span> <strong>${q.length}</strong> open PO(s) to bake &middot; ${g.summary.produce_now_cs || 0} cs</div>
    <table class="table"><thead><tr><th>PO</th><th>Dist</th><th>Warehouse</th><th style="text-align:right">Cases</th><th>Produce by</th><th>Status</th></tr></thead><tbody>${
      q.map(r => `<tr>
        <td style="font-family:ui-monospace,monospace;font-size:12px">${escHtml(r.po_number)}${r.has_top4 ? ' <span class="badge badge-yellow" style="font-size:10px">top-4</span>' : ''}</td>
        <td>${distributorBadge(r.distributor)}</td>
        <td>${escHtml(r.warehouse)}${r.transfer_group ? ' <span class="badge badge-cheney" style="font-size:10px">pool</span>' : ''}</td>
        <td style="text-align:right;font-weight:600">${Number(r.total_cs || 0).toFixed(0)}</td>
        <td style="white-space:nowrap">${r.produce_by || '<span style="color:var(--accent)">ASAP</span>'}</td>
        <td><span class="badge badge-gray" style="font-size:10px">${escHtml(r.status)}</span></td>
      </tr>`).join('')
    }</tbody></table></div>` : '<div style="color:var(--muted);padding:8px">No open POs to bake &mdash; production queue is clear.</div>';
  const bake = g.bake_by_variety || [];
  const bakeTbl = bake.length ? `<div class="card" style="margin-bottom:16px">
    <div style="padding:10px 16px;border-bottom:1px solid var(--border)"><strong>Bake this cycle &mdash; by variety</strong></div>
    <table class="table"><thead><tr><th>Variety</th><th style="text-align:right">Cases</th><th style="text-align:right">Pallets</th></tr></thead><tbody>${
      bake.map(b => `<tr><td>${escHtml(b.variety)}${b.top4 ? ' <span class="badge badge-yellow" style="font-size:10px">top-4</span>' : ''}</td><td style="text-align:right;font-weight:600">${b.cs}</td><td style="text-align:right">${b.pallets}</td></tr>`).join('')
    }</tbody></table></div>` : '';
  const bw = g.buffer_watch || [];
  const bwTbl = bw.length ? `<div class="card" style="margin-bottom:16px">
    <div style="padding:10px 16px;border-bottom:1px solid var(--border)"><span class="badge badge-yellow" style="font-size:10px">BUFFER WATCH</span> <strong>${bw.length}</strong> depleting with no open PO</div>
    <table class="table"><thead><tr><th>Dist</th><th>Unit</th><th>Variety</th><th style="text-align:right">On-hand</th><th style="text-align:right">Cover</th></tr></thead><tbody>${
      bw.map(r => `<tr><td>${distributorBadge(r.distributor)}</td><td>${escHtml(r.unit)}${r.is_pool ? ' <span class="badge badge-cheney" style="font-size:10px">pool</span>' : ''}</td><td>${escHtml(r.variety)}${r.top4 ? ' <span class="badge badge-yellow" style="font-size:10px">top-4</span>' : ''}</td><td style="text-align:right">${r.on_hand}</td><td style="text-align:right">${r.cover_days}d</td></tr>`).join('')
    }</tbody></table></div>` : '';
  body.innerHTML = qTbl + bakeTbl + bwTbl;
}
