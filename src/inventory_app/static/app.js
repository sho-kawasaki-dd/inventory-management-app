window.InventoryApp = (() => {
  const API_BASE = window.API_CONFIG.api_base;

  async function api(url, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(url, Object.assign({}, opts, { headers }));
  }

  async function loadItems() {
    const res = await api(`${API_BASE}/items`);
    const items = await res.json();
    const tbody = document.querySelector('#items-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    for (const it of items) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${it.id}</td><td>${it.sku ?? ''}</td><td>${it.name}</td><td>${it.unit}</td>`;
      tbody.appendChild(tr);
    }
  }

  async function loadStocks() {
    const res = await api(`${API_BASE}/stocks`);
    const rows = await res.json();
    const tbody = document.querySelector('#stocks-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    for (const r of rows) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${r.name}</td>
        <td>${r.sku ?? ''}</td>
        <td>${r.quantity}</td>
        <td>${r.unit}</td>
        <td>${r.shelf_location ?? ''}</td>
        <td>${r.shelf_location_note ?? ''}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  // Stocktake list includes confirm button + diff_count
  async function loadStocktakes() {
    const res = await api(`${API_BASE}/stocktakes`);
    const rows = await res.json();
    const tbody = document.querySelector('#stocktakes-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    for (const st of rows) {
      const tr = document.createElement('tr');
      const status = st.completed_at ? 'Completed' : 'Open';
      tr.innerHTML = `
        <td>${st.id}</td>
        <td><a href="/stocktakes/${st.id}">${st.title}</a></td>
        <td>${st.lines_count}</td>
        <td>${st.diff_count}</td>
        <td>${status}</td>
        <td></td>
      `;

      const td = tr.querySelector('td:last-child');
      const btn = document.createElement('button');
      btn.className = 'btn danger';
      btn.textContent = 'Confirm';
      btn.disabled = !!st.completed_at;
      btn.addEventListener('click', async () => {
        if (!confirm(`Confirm stocktake #${st.id}? This applies counted quantities. Diffs: ${st.diff_count}`)) return;
        await api(`${API_BASE}/stocktakes/${st.id}/confirm`, { method: 'POST' });
        await loadStocktakes();
      });
      td.appendChild(btn);

      tbody.appendChild(tr);
    }
  }

  return { api, loadItems, loadStocks, loadStocktakes };
})();
