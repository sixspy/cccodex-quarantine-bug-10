import { api } from '../api.js';

export async function renderDedupe(el) {
  const dash = await api('/api/dedupe/dash');
  const rows = dash.rows || [];
  el.innerHTML = `
    <div class="card">
      <h2>抓取去重账册</h2>
      <div class="stat-strip">
        <div class="stat"><span class="muted">形态</span><b>${dash.form_label}</b></div>
        <div class="stat"><span class="muted">去重落点</span><b data-landing-count>${dash.landing_count}</b></div>
        <div class="stat"><span class="muted">撞件次数</span><b data-collision-count>${dash.collision_count}</b></div>
        <div class="stat"><span class="muted">读写口径</span><b>${dash.read_only ? '只读引用' : '可写'}</b></div>
      </div>
      <p class="hint-box">${dash.policy ? dash.policy.note : ''}</p>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>#</th><th>源件键</th><th>徽标</th><th>命运</th><th>指纹</th><th>落点行</th><th>撞件于</th><th>时间</th></tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${r.source_key}</td>
              <td><span class="chip ${r.fate === '已落舱' ? 'ok' : 'bad'}">${r.badge}</span></td>
              <td>${r.fate}</td>
              <td class="fp" title="${r.fingerprint}">${r.fingerprint_short}</td>
              <td>${r.quarantine_row_id ? `#${r.quarantine_row_id}${r.bin_code ? ` · ${r.bin_code}` : ''}` : '<span class="muted">—</span>'}</td>
              <td>${r.collision_of || '<span class="muted">—</span>'}</td>
              <td class="muted">${r.created_at}</td>
            </tr>`).join('') || '<tr><td colspan="8" class="empty">暂无去重记录</td></tr>'}
        </tbody>
      </table>
    </div>`;
}
