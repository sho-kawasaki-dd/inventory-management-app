window.StocktakePage = (() => {
  const API_BASE = window.API_CONFIG.api_base;

  function shelfKey(s) {
    // natural-ish order by splitting into alpha+numeric chunks
    const str = (s || '').trim();
    if (!str) return ['~', 0, ''];
    const m = str.match(/^([A-Za-z]+)?\s*([0-9]+)?\s*(.*)$/);
    if (!m) return [str.toLowerCase(), 0, ''];
    return [(m[1] || '').toLowerCase(), parseInt(m[2] || '0', 10), (m[3] || '').toLowerCase()];
  }

  async function load(stocktakeId) {
    const res = await window.InventoryApp.api(`${API_BASE}/stocktakes/${stocktakeId}`);
    const data = await res.json();

    document.getElementById('meta').textContent = `Lines: ${data.lines_count}, Diffs: ${data.diff_count}`;

    const tbody = document.querySelector('#stocktake-lines-table tbody');
    tbody.innerHTML = '';

    // shelf-order sorting
    data.lines.sort((a, b) => {
      const ka = shelfKey(a.shelf_location);
      const kb = shelfKey(b.shelf_location);
      if (ka[0] !== kb[0]) return ka[0] < kb[0] ? -1 : 1;
      if (ka[1] !== kb[1]) return ka[1] - kb[1];
      if (ka[2] !== kb[2]) return ka[2] < kb[2] ? -1 : 1;
      return (a.name || '').localeCompare(b.name || '');
    });

    for (const ln of data.lines) {
      const tr = document.createElement('tr');
      if (ln.is_diff) tr.classList.add('diff');

      const countedVal = (ln.counted_quantity === null || ln.counted_quantity === undefined) ? '' : ln.counted_quantity;
      tr.innerHTML = `
        <td>${ln.shelf_location ?? ''}</td>
        <td>${ln.name}</td>
        <td>${ln.sku ?? ''}</td>
        <td>${ln.expected_quantity}</td>
        <td><input type="number" step="0.001" value="${countedVal}" data-line-id="${ln.id}" class="counted" style="width: 110px"/></td>
        <td>${ln.unit}</td>
        <td class="muted">${ln.shelf_location_note ?? ''}</td>
        <td><input type="text" value="${ln.note ?? ''}" data-line-id="${ln.id}" class="note" style="width: 100%"/></td>
      `;

      tbody.appendChild(tr);
    }

    tbody.querySelectorAll('input.counted').forEach((el) => {
      el.addEventListener('change', async (e) => {
        const lineId = e.target.getAttribute('data-line-id');
        const v = e.target.value;
        const payload = { counted_quantity: v === '' ? null : Number(v) };
        await window.InventoryApp.api(`${API_BASE}/stocktakes/lines/${lineId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        // refresh meta/diff highlighting
        await load(stocktakeId);
      });
    });

    tbody.querySelectorAll('input.note').forEach((el) => {
      el.addEventListener('change', async (e) => {
        const lineId = e.target.getAttribute('data-line-id');
        const payload = { note: e.target.value };
        await window.InventoryApp.api(`${API_BASE}/stocktakes/lines/${lineId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      });
    });

    document.getElementById('confirm-btn').onclick = async () => {
      if (!confirm(`Confirm stocktake #${stocktakeId}? This applies counted quantities. Diffs: ${data.diff_count}`)) return;
      await window.InventoryApp.api(`${API_BASE}/stocktakes/${stocktakeId}/confirm`, { method: 'POST' });
      await load(stocktakeId);
    };
  }

  function init(stocktakeId) {
    load(stocktakeId);
  }

  return { init };
})();
