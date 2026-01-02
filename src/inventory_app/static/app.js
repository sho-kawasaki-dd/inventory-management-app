window.InventoryApp = (() => {
  const API_BASE = window.API_CONFIG.api_base;
  let selectedStock = null;
  let selectedRowEl = null;
  const TRANSACTIONS_PAGE_SIZE = 50;
  let transactionsPage = 1;

  // Set status text in the transaction panel
  function setTransactionMessage(message, isError = false) {
    const el = document.querySelector('#transaction-message');
    if (!el) return;
    el.textContent = message || '';
    el.style.color = isError ? '#c33' : '#333';
  }

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
      tr.innerHTML = `<td>${it.id}</td><td>${it.sku ?? ''}</td><td>${it.name}</td><td>${it.category ?? ''}</td><td>${it.usage ?? ''}</td><td>${it.manufacturer ?? ''}</td><td>${it.unit}</td>`;
      tbody.appendChild(tr);
    }
  }

  async function loadStocks() {
    const res = await api(`${API_BASE}/stocks`);
    const rows = await res.json();
    const tbody = document.querySelector('#stocks-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    let selectedFound = false;
    for (const r of rows) {
      const tr = document.createElement('tr');
      tr.dataset.itemId = r.item_id;
      tr.innerHTML = `
        <td>${r.name}</td>
        <td>${r.sku ?? ''}</td>
        <td>${r.quantity}</td>
        <td>${r.unit}</td>
        <td>${r.shelf_location ?? ''}</td>
        <td>${r.shelf_location_note ?? ''}</td>
      `;

      tr.addEventListener('click', () => selectStock(r, tr));

      // Keep selection after refresh
      if (selectedStock && selectedStock.item_id === r.item_id) {
        selectedFound = true;
        selectedStock = r;
        selectedRowEl = tr;
        tr.classList.add('selected');
        renderSelectedStock();
      }

      tbody.appendChild(tr);
    }

    if (selectedStock && !selectedFound) {
      clearSelection();
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
      const status = st.completed_at ? '完了' : '進行中';
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
      btn.textContent = '確定';
      btn.disabled = !!st.completed_at;
      btn.addEventListener('click', async () => {
        if (!confirm(`棚卸 #${st.id} を確定しますか？これによりカウント数量が適用されます。差異数: ${st.diff_count}`)) return;
        await api(`${API_BASE}/stocktakes/${st.id}/confirm`, { method: 'POST' });
        await loadStocktakes();
      });
      td.appendChild(btn);

      tbody.appendChild(tr);
    }
  }

  function renderSelectedStock() {
    if (!selectedStock) return;
    const nameEl = document.querySelector('#selected-item-title');
    const skuEl = document.querySelector('#selected-item-sku');
    const qtyEl = document.querySelector('#selected-item-qty');
    if (nameEl) nameEl.textContent = selectedStock.name;
    if (skuEl) skuEl.textContent = selectedStock.sku || '-';
    if (qtyEl) qtyEl.textContent = `${selectedStock.quantity} ${selectedStock.unit}`;
  }

  function showTransactionPanel() {
    const panel = document.querySelector('#transaction-panel');
    const placeholder = document.querySelector('#transaction-placeholder');
    if (panel) panel.classList.remove('hidden');
    if (placeholder) placeholder.classList.add('hidden');
  }

  function hideTransactionPanel() {
    const panel = document.querySelector('#transaction-panel');
    const placeholder = document.querySelector('#transaction-placeholder');
    if (panel) panel.classList.add('hidden');
    if (placeholder) placeholder.classList.remove('hidden');
    setTransactionMessage('');
  }

  function clearSelection() {
    selectedStock = null;
    if (selectedRowEl) {
      selectedRowEl.classList.remove('selected');
      selectedRowEl = null;
    }
    hideTransactionPanel();
  }

  async function selectStock(stock, rowEl) {
    if (selectedRowEl) selectedRowEl.classList.remove('selected');
    selectedStock = stock;
    selectedRowEl = rowEl;
    rowEl.classList.add('selected');
    renderSelectedStock();
    showTransactionPanel();
    setTransactionMessage('');
    await loadItemTransactions(stock.item_id);
  }

  async function loadItemTransactions(itemId) {
    const tbody = document.querySelector('#transaction-history tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="4">読み込み中...</td></tr>';
    const res = await api(`${API_BASE}/items/${itemId}/transactions?limit=20`);
    if (!res.ok) {
      tbody.innerHTML = '<tr><td colspan="4">取得に失敗しました</td></tr>';
      return;
    }
    const rows = await res.json();
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">まだ取引がありません</td></tr>';
      return;
    }

    for (const r of rows) {
      const tr = document.createElement('tr');
      const delta = Number(r.delta_quantity);
      const deltaStr = delta > 0 ? `+${delta}` : `${delta}`;
      const dt = new Date(r.created_at);
      tr.innerHTML = `
        <td>${dt.toLocaleString()}</td>
        <td>${r.txn_type}</td>
        <td>${deltaStr}</td>
        <td>${r.reason ?? ''}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  function updateTransactionsPagination(meta) {
    const summaryEl = document.querySelector('#transactions-summary');
    const prevBtn = document.querySelector('#transactions-prev');
    const nextBtn = document.querySelector('#transactions-next');

    const total = meta.total ?? 0;
    const limit = meta.limit || TRANSACTIONS_PAGE_SIZE;
    const offset = meta.offset || 0;

    const page = total === 0 ? 1 : Math.floor(offset / limit) + 1;
    const totalPages = total === 0 ? 1 : Math.ceil(total / limit);
    const from = total === 0 ? 0 : offset + 1;
    const to = total === 0 ? 0 : Math.min(offset + limit, total);

    if (summaryEl) {
      summaryEl.textContent = total === 0
        ? '取引はまだありません'
        : `全${total}件中 ${from}-${to}件 (ページ${page}/${totalPages})`;
    }

    if (prevBtn) {
      prevBtn.disabled = page <= 1 || total === 0;
      prevBtn.onclick = () => {
        if (page > 1) loadTransactionsList(page - 1);
      };
    }

    if (nextBtn) {
      const noMore = offset + limit >= total || total === 0;
      nextBtn.disabled = noMore;
      nextBtn.onclick = () => {
        if (!noMore) loadTransactionsList(page + 1);
      };
    }
  }

  async function loadTransactionsList(page) {
    const tbody = document.querySelector('#item-transactions-table tbody');
    if (!tbody) return;

    transactionsPage = page || transactionsPage || 1;
    const offset = (transactionsPage - 1) * TRANSACTIONS_PAGE_SIZE;

    const statusEl = document.querySelector('#transactions-status');
    if (statusEl) statusEl.textContent = '';

    tbody.innerHTML = '<tr><td colspan="6">読み込み中...</td></tr>';
    const res = await api(`${API_BASE}/transactions?limit=${TRANSACTIONS_PAGE_SIZE}&offset=${offset}`);
    if (!res.ok) {
      if (statusEl) statusEl.textContent = '取得に失敗しました';
      tbody.innerHTML = '<tr><td colspan="6">取得に失敗しました</td></tr>';
      return;
    }

    const data = await res.json();
    const rows = data.items || [];
    const meta = data.meta || { total: rows.length, limit: TRANSACTIONS_PAGE_SIZE, offset };
    if (meta.limit) {
      transactionsPage = Math.floor((meta.offset || 0) / meta.limit) + 1;
    }

    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">取引はまだありません</td></tr>';
      updateTransactionsPagination(meta);
      return;
    }

    for (const r of rows) {
      const tr = document.createElement('tr');
      const delta = Number(r.delta_quantity);
      const deltaStr = delta > 0 ? `+${delta}` : `${delta}`;
      const dt = new Date(r.created_at);
      tr.innerHTML = `
        <td>${dt.toLocaleString()}</td>
        <td>${r.item_name}</td>
        <td>${r.item_sku ?? ''}</td>
        <td>${r.txn_type}</td>
        <td>${deltaStr}</td>
        <td>${r.reason ?? ''}</td>
      `;
      tbody.appendChild(tr);
    }

    updateTransactionsPagination(meta);
  }

  function bindTransactionForm() {
    const form = document.querySelector('#transaction-form');
    if (!form) return;
    const typeEl = document.querySelector('#transaction-type');
    const qtyEl = document.querySelector('#transaction-qty');
    const reasonEl = document.querySelector('#transaction-reason');
    const cancelBtn = document.querySelector('#transaction-cancel');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!selectedStock) {
        setTransactionMessage('備品を選択してください', true);
        return;
      }

      const type = typeEl.value;
      const qtyRaw = parseFloat(qtyEl.value);
      const reason = reasonEl.value || null;

      if (Number.isNaN(qtyRaw)) {
        setTransactionMessage('数量を入力してください', true);
        return;
      }

      let url = '';
      let payload = {};

      if (type === 'receipt') {
        if (qtyRaw <= 0) {
          setTransactionMessage('入庫数量は正の数で入力してください', true);
          return;
        }
        url = `${API_BASE}/items/${selectedStock.item_id}/receipts`;
        payload = { quantity: qtyRaw, reason };
      } else if (type === 'issue') {
        if (qtyRaw <= 0) {
          setTransactionMessage('出庫数量は正の数で入力してください', true);
          return;
        }
        url = `${API_BASE}/items/${selectedStock.item_id}/issues`;
        payload = { quantity: qtyRaw, reason };
      } else if (type === 'adjust') {
        if (qtyRaw === 0) {
          setTransactionMessage('調整数量は0以外で入力してください', true);
          return;
        }
        url = `${API_BASE}/items/${selectedStock.item_id}/adjustments`;
        payload = { delta: qtyRaw, reason };
      } else {
        setTransactionMessage('不明な取引種別です', true);
        return;
      }

      const res = await api(url, { method: 'POST', body: JSON.stringify(payload) });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: '登録に失敗しました' }));
        setTransactionMessage(err.error || '登録に失敗しました', true);
        return;
      }

      qtyEl.value = '';
      reasonEl.value = '';
      setTransactionMessage('登録しました');
      await loadStocks();
      await loadItemTransactions(selectedStock.item_id);
    });

    if (cancelBtn) {
      cancelBtn.addEventListener('click', () => {
        qtyEl.value = '';
        reasonEl.value = '';
        clearSelection();
      });
    }
  }

  // Initialize form bindings on load
  bindTransactionForm();

  return { api, loadItems, loadStocks, loadStocktakes, loadTransactionsList };
})();
