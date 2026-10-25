'use strict';
(() => {
  let session = null, timer = null, signature = '', polling = false;
  const form = $('portal-form');
  const field = name => form.elements[name];
  const busy = s => ['opening', 'busy', 'closing'].includes(s.status);
  function options(name, entries, initial) {
    const select = field(name);
    select.replaceChildren();
    for (const [value, label] of [['', initial], ...entries]) {
      const option = el('option', label); option.value = value; select.append(option);
    }
  }
  function fill(s) {
    const candidate = s.candidates.find(c => c.platform_id === s.selected);
    if (!candidate || !s.mappings?.length) { form.hidden = true; return; }
    const key = JSON.stringify([s.session_id, candidate, s.mappings]);
    if (key === signature) return;
    signature = key;
    form.reset(); form.hidden = false;
    $('portal-selected').textContent = candidate.name + ' · 平台编号 ' + candidate.platform_id;
    field('name').value = candidate.name;
    field('mapping_index').replaceChildren();
    s.mappings.forEach((m, index) => {
      const option = el('option', `${m.host}:${m.port}`); option.value = index; field('mapping_index').append(option);
    });
    options('target_instance_id', (state.instances || []).map(i => [i.id, i.name + ' · ' + i.username + ' · ' + i.id]), '自动匹配已有实例，无匹配时创建');
    options('credential_instance_id', (state.instances || []).map(i => [i.id, i.name + ' · ' + i.username]), '填写密码或保留当前实例密码');
    let existing = (state.instances || []).filter(i => i.portal?.platform_id === candidate.platform_id && i.portal.origin === 'https://ailab.snnu.edu.cn');
    if (!existing.length) {
      const host = value => value.trim().replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase();
      const mapping = s.mappings[0];
      existing = (state.instances || []).filter(i => !i.portal && host(i.host) === host(mapping.host) && Number(i.port) === Number(mapping.port));
    }
    if (existing.length === 1) { field('target_instance_id').value = existing[0].id; copyTarget(); }
    field('expires_at').value = candidate.expires_at ? new Date(new Date(candidate.expires_at).getTime() + 8 * 3600000).toISOString().slice(0,16) : '';
    field('expires_at').readOnly = !!candidate.expires_at;
    $('portal-expiry-note').textContent = candidate.expires_at ? (candidate.expiry_estimated ? '程序已按剩余时间保守估算到期时间。' : '程序已读取平台准确到期时间。') + ' 读取时间：' + date(candidate.observed_at) : '平台未提供可核对的到期时间，请核对平台后手动填写。';
  }
  function copyTarget() {
    const instance = (state.instances || []).find(i => i.id === field('target_instance_id').value);
    if (!instance) return;
    for (const key of ['username','root','conda','default_environment','python']) field(key).value = instance[key] || '';
    syncConnectionDefaults(form);
    field('credential_instance_id').value = instance.id;
  }
  function render(s) {
    session = s;
    $('portal-message').textContent = s.message || '导入窗口已关闭';
    $('portal-scan').disabled = busy(s) || s.status === 'closed';
    form.querySelector('button[type="submit"]').disabled = s.status !== 'ready';
    const target = $('portal-candidates'); target.replaceChildren();
    if (s.candidates?.length) target.append(table(['实例 / 平台编号','状态','到期时间（北京）','操作'], s.candidates.map(c => {
      const actions = el('div', undefined, 'actions');
      for (const [label, action] of [['读取连接信息','connections'], ['读取我已打开的连接弹窗','dialog']]) {
        const b = button(label, () => read(action, c.platform_id)); b.disabled = busy(s); actions.append(b);
      }
      return [c.name + ' / ' + c.platform_id, c.state, c.expires_at ? date(c.expires_at) + (c.expiry_estimated ? '（估算）' : '') : '需要填写', actions];
    })));
    if (!s.selected) { form.hidden = true; signature = ''; } else fill(s);
    if (s.status === 'closed') { clearInterval(timer); timer = null; form.hidden = true; field('password').value = ''; }
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try { render(await api('panel/portal/status')); } catch(e) { notice(e.message, true); } finally { polling = false; }
  }
  async function read(action, platform_id) {
    if (action === 'dialog' && !confirm('请确认学校窗口当前连接弹窗属于平台实例 ' + platform_id + '。程序会把该弹窗映射与此实例关联。')) return;
    render(await api('panel/portal/read', {session_id:session.session_id, action, platform_id}));
  }
  $('portal-open').onclick = async () => {
    try {
      $('portal-panel').hidden = false;
      render(await api('panel/portal/start'));
      if (!timer) timer = setInterval(poll, 1500);
    } catch(e) { notice(e.message, true); }
  };
  $('portal-scan').onclick = () => read('scan').catch(e => notice(e.message, true));
  $('portal-close').onclick = async () => { try { await api('panel/portal/close'); await poll(); } catch(e) { notice(e.message, true); } };
  field('target_instance_id').onchange = copyTarget;
  form.onsubmit = async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const {password, mapping_index, credential_instance_id, target_instance_id, ...instance} = data;
    instance.expires_at = new Date(instance.expires_at + ':00+08:00').toISOString();
    const submit = form.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
      const saved = await api('panel/portal/save', {session_id:session.session_id, platform_id:session.selected, mapping_index:Number(mapping_index), credential_instance_id, target_instance_id, password, instance});
      field('password').value = ''; await refresh();
      notice('实例 ' + saved.name + ' 已导入。请点击“检查连接与后台运行”，并核对服务器指纹。');
      await api('panel/portal/close'); await poll();
    } catch(e) { notice(e.message, true); } finally { submit.disabled = false; }
  };
})();
