import { api } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  const policy = data.dedupe_policy || {};
  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <table>
        <thead><tr><th>项</th><th>说明</th><th>值</th><th></th></tr></thead>
        <tbody>
          ${(data.items || []).map((s) => `
            <tr>
              <td>${s.key}</td>
              <td>${s.label}</td>
              <td><input data-key="${s.key}" value="${s.value}" /></td>
              <td><button data-save="${s.key}">保存</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>去重口径（只读）</h2>
      <dl class="kv" data-dedupe-policy>
        <dt>去重形态</dt><dd>${policy.form_label || '独立账册'}</dd>
        <dt>指纹引用</dt><dd class="fp">${policy.fingerprint_ref || 'dedupe_ledger'}</dd>
        <dt>写方</dt><dd>${policy.owner || '隔离舱入舱事务'}</dd>
        <dt>读方</dt><dd>${policy.reader || '抓取侧只读'}</dd>
      </dl>
      <p class="hint-box">${policy.note || ''}</p>
      <p class="muted">去重形态已择定，不可在此切换；重试列表与抓取侧均按同一账册口径引用。</p>
    </div>`;
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const input = el.querySelector(`input[data-key="${btn.dataset.save}"]`);
      await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify({ key: btn.dataset.save, value: input.value }),
      });
      await renderSettings(el);
    };
  });
}
