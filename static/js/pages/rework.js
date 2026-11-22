import { api } from '../api.js';

function jobsTable(items) {
  return `
    <table>
      <thead><tr><th>任务</th><th>条目</th><th>舱</th><th>状态</th><th>次数</th><th>去重徽标</th><th>撞件次数</th><th>去重落点 / 指纹</th><th>操作</th></tr></thead>
      <tbody>
        ${(items || []).map((j) => {
          const land = j.dedupe_landing;
          const row = j.landing_row;
          const fpOk = j.fingerprint_match;
          return `
          <tr>
            <td>${j.job_code}</td>
            <td>${j.item_key}</td>
            <td>${j.bin_code}</td>
            <td><span class="chip">${j.status}</span></td>
            <td>${j.attempt}</td>
            <td>${j.dedupe_badge ? `<span class="chip ok">${j.dedupe_badge}</span>` : '<span class="chip bad">无徽标</span>'}</td>
            <td>${j.collision_count ? `<span class="chip bad">${j.collision_count}</span>` : '<span class="chip">0</span>'}</td>
            <td>${land && row
              ? `<span class="fp ${fpOk ? '' : 'bad'}" title="${land.fingerprint}">落点 ${row.bin_code} #${row.id} · ${land.fingerprint_short}${fpOk ? '' : ' · 指纹对不上'}</span>`
              : '<span class="chip bad">无落点行</span>'}</td>
            <td class="row">
              ${['排队','失败'].includes(j.status) ? `
                <button data-ok="${j.id}">执行成功</button>
                <button data-bad="${j.id}">执行失败</button>
              ` : (j.message || '')}
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

export async function renderRework(el) {
  const data = await api('/api/rework/jobs');
  el.innerHTML = `
    <div class="card">
      <h2>重试列表 · 回炉队列</h2>
      <p class="muted">回炉联验：去重徽标、撞件次数、去重落点与指纹须互相咬合，拧花即拒绝执行。</p>
      <div id="jobs">${jobsTable(data.items)}</div>
    </div>`;
  const bind = () => {
    el.querySelectorAll('[data-ok]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rework/jobs/${btn.dataset.ok}/run`, {
            method: 'POST', body: JSON.stringify({ succeed: true }),
          });
        } catch (e) {
          window.alert(e.message);
        }
        await renderRework(el);
      };
    });
    el.querySelectorAll('[data-bad]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rework/jobs/${btn.dataset.bad}/run`, {
            method: 'POST', body: JSON.stringify({ succeed: false }),
          });
        } catch (e) {
          window.alert(e.message);
        }
        await renderRework(el);
      };
    });
  };
  bind();
}
