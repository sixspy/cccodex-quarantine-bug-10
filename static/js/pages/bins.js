import { api } from '../api.js';

function fingerprintStrip(bin) {
  const fps = bin.fingerprints || [];
  if (!fps.length) return '<span class="muted">暂无去重落点</span>';
  return `<div class="fp-strip" data-bin-fp="${bin.id}">
    ${fps.map((f) => `
      <span class="fp" title="${f.source_key} · ${f.fingerprint}">
        ${f.fingerprint_short} → 行 #${f.quarantine_row_id}
      </span>`).join('')}
  </div>`;
}

export async function renderBins(el) {
  const bins = await api('/api/bins');
  const rows = await api('/api/rows');
  el.innerHTML = `
    <div class="card">
      <h2>隔离舱</h2>
      <p class="muted">舱卡片旁挂指纹条：来源去重账册只读引用</p>
      <table>
        <thead><tr><th>舱号</th><th>标题</th><th>状态</th><th>占用/容量</th><th>待复核</th><th>去重落点</th></tr></thead>
        <tbody>
          ${(bins.items || []).map((b) => `
            <tr>
              <td>${b.bin_code}</td>
              <td>${b.title}</td>
              <td><span class="chip">${b.status}</span></td>
              <td>${b.row_count}/${b.capacity}</td>
              <td>${b.pending_count}</td>
              <td>${fingerprintStrip(b)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>舱内行</h2>
      <table>
        <thead><tr><th>舱</th><th>条目键</th><th>质量</th><th>原因</th><th>状态</th></tr></thead>
        <tbody>
          ${(rows.items || []).map((r) => `
            <tr>
              <td>${r.bin_code}</td>
              <td>${r.item_key}</td>
              <td>${r.quality}</td>
              <td>${r.reason}</td>
              <td><span class="chip">${r.status}</span></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}
