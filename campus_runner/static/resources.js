'use strict';
(() => {
  let timer = null, busy = false, pendingReads = 0;
  const select = $('resource-interval');
  const saved = localStorage.getItem('campus-resource-interval');
  if (['0','5','10','30'].includes(saved)) select.value = saved;
  const visible = () => !document.hidden && location.hash === '#instances' && !!token;
  function schedule(milliseconds, collect) {
    clearTimeout(timer);
    if (visible()) timer = setTimeout(() => update(collect), milliseconds);
  }
  async function update(collect = true) {
    clearTimeout(timer);
    if (!visible() || busy) return;
    busy = true;
    let pending = false;
    try {
      const result = await api('panel/resources', {refresh:collect});
      for (const row of result.instances) {
        pending ||= row.pending;
        const instance = (state.instances || []).find(i => i.id === row.id);
        if (instance) instance.resources = row.resources;
        const block = $('resource-' + row.id);
        if (block) paintResources(block, row.resources);
      }
    } catch(e) { notice('资源读取：' + e.message, true); }
    finally { busy = false; }
    if (pending && pendingReads++ < 30) schedule(1000, false);
    else {
      pendingReads = 0;
      if (Number(select.value)) schedule(Number(select.value) * 1000, true);
    }
  }
  function resume() {
    clearTimeout(timer); pendingReads = 0;
    if (visible() && Number(select.value)) update();
  }
  select.onchange = () => { localStorage.setItem('campus-resource-interval', select.value); resume(); };
  $('resource-refresh').onclick = () => { pendingReads = 0; update(); };
  window.addEventListener('hashchange', resume);
  document.addEventListener('visibilitychange', resume);
  resume();
})();
