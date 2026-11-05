const $ = id => document.getElementById(id);
let current = null;
async function update() {
  const s = await chrome.runtime.sendMessage({type: 'state'});
  current = s.pending;
  const active = s.grant;
  $('reason').textContent = current?.reason || active?.reason || 'No current request';
  $('url').textContent = current?.url || (active ? 'Your approved task tab' : 'A blank tab');
  const deadline = current?.deadline || active?.expires;
  $('time').textContent = deadline ? `${Math.max(0, Math.ceil((deadline - Date.now()) / 1000))} seconds left` : '—';
  $('status').textContent = active ? 'Control is active. Stop at any time.' : current ? `Allow up to ${current.minutes} minute(s). This request expires in 60 seconds.` : s.lastEnd || 'No current request';
  $('allow').disabled = !current || !s.connected;
  $('deny').disabled = !current;
  $('stop').hidden = !active;
}
for (const type of ['allow', 'deny', 'stop']) $(type).onclick = async () => {
  const result = await chrome.runtime.sendMessage({type, id: current?.id});
  await update();
  if (result.error) $('status').textContent = result.message;
};
await update();
setInterval(() => void update(), 500);
