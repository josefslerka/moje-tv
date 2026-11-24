(function(root){
'use strict';
const byId = id => document.getElementById(id);
const clock = seconds => new Date(Math.max(0, seconds) * 1000).toISOString().slice(11, 19);
const gigabytes = bytes => (bytes / 1024 ** 3).toLocaleString('cs-CZ', {maximumFractionDigits:1}) + ' GB';

async function request(path = '', options = {}){
  const response = await fetch('/api/recordings' + path, {cache:'no-store', ...options});
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(data?.detail || 'Se záznamy se nepodařilo spojit.');
  return data;
}

class TVRecorder {
  constructor(){
    this.channel = null; this.active = null; this.busy = false; this.polling = false;
    byId('recordBtn').onclick = () => this.active ? this.stop() : this.open();
    byId('recordCancel').onclick = () => byId('recordDialog').close();
    byId('recordForm').onsubmit = event => { event.preventDefault(); this.start(); };
    byId('recordText').onchange = () => this.choices();
    byId('recordTextMode').onchange = () => this.choices();
    this.refresh();
    this.timer = setInterval(() => { if (document.visibilityState === 'visible') this.refresh(); }, 3000);
    root.addEventListener('pagehide', () => clearInterval(this.timer));
    root.addEventListener('pageshow', event => {
      if (event.persisted){
        this.refresh();
        this.timer = setInterval(() => { if (document.visibilityState === 'visible') this.refresh(); }, 3000);
      }
    });
  }
  setChannel(channel){ this.channel = channel; this.render(); }
  render(){
    const active = this.active;
    byId('recordBtn').disabled = this.busy || (!this.channel && !active) || active?.state === 'finishing';
    byId('recordBtn').setAttribute('aria-pressed', Boolean(active));
    byId('recordLabel').textContent = active?.state === 'finishing' ? 'Ukládám…' : active ? 'Stop · ' + clock(active.duration) : 'Record';
    if (active){
      byId('recordStatus').textContent = (active.state === 'finishing' ? 'Dokončuji' : '● Nahrávám') + ' · ' + active.channel_name
        + (active.text_mode === 'translate-cs-api' ? ' · Gemini, placené API' : '') + (active.text_error ? ' · ' + active.text_error : '');
    }
  }
  async refresh(){
    if (this.polling) return;
    this.polling = true;
    try {
      const data = await request();
      const previous = this.active;
      this.storage = data.storage;
      this.active = data.recordings.find(item => ['recording','finishing'].includes(item.state)) || null;
      if (previous && !this.active){
        const saved = data.recordings.find(item => item.id === previous.id);
        byId('recordStatus').textContent = saved?.error || saved?.text_error || 'Záznam je uložený.';
        byId('recordStatus').classList.toggle('err', Boolean(saved?.error || saved?.text_error));
      }
      this.render();
    } catch(error){
      if (this.active){
        byId('recordStatus').textContent = 'Stav nahrávání se nepodařilo ověřit. Zkontroluj Záznamy.';
        byId('recordStatus').classList.add('err');
      }
    } finally { this.polling = false; }
  }
  choices(){
    const text = byId('recordText').checked;
    byId('recordTextChoice').hidden = !text;
    byId('recordPaid').hidden = !text || byId('recordTextMode').value !== 'translate-cs-api';
  }
  async open(){
    if (!this.channel) return;
    this.target = this.channel;
    byId('recordChannel').textContent = this.target.name;
    byId('recordError').textContent = '';
    byId('recordTextMode').value = 'asr';
    byId('recordVideo').checked = true; byId('recordText').checked = true;
    byId('recordStart').disabled = true;
    byId('recordSpace').textContent = this.storage ? 'Na disku je volných ' + gigabytes(this.storage.free_bytes) + '. Při nedostatku místa se nahrávání ukončí.' : '';
    this.choices(); byId('recordDialog').showModal();
    try {
      const response = await fetch('/api/captions/status', {cache:'no-store'});
      if (!response.ok) throw new Error();
      const status = await response.json();
      byId('recordText').disabled = !status.available;
      byId('recordText').checked = Boolean(status.available);
      byId('recordTextMode').querySelector('[value="translate-cs"]').disabled = !status.translation;
      byId('recordTextMode').querySelector('[value="translate-cs-api"]').disabled = !status.cloud_translation;
      if (!status.available) byId('recordError').textContent = 'Textový přepis není dostupný. Můžeš nahrát video.';
      this.choices();
    } catch(error){
      byId('recordText').checked = false;
      byId('recordText').disabled = true;
      byId('recordError').textContent = 'Dostupnost textového přepisu se nepodařilo ověřit.';
      this.choices();
    } finally { byId('recordStart').disabled = false; }
  }
  async start(){
    if (this.busy) return;
    this.busy = true; this.render();
    byId('recordStart').disabled = true; byId('recordError').textContent = '';
    try {
      const item = await request('', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({channel_id:this.target.id, video:byId('recordVideo').checked,
          text_mode:byId('recordText').checked ? byId('recordTextMode').value : null,
          max_seconds:Number(byId('recordLimit').value)})});
      this.active = item;
      byId('recordStatus').classList.remove('err');
      byId('recordDialog').close();
    } catch(error){ byId('recordError').textContent = error.message; }
    finally { this.busy = false; byId('recordStart').disabled = false; this.render(); }
  }
  async stop(){
    if (!this.active || this.busy) return;
    this.busy = true; this.render();
    try { this.active = await request('/' + this.active.id + '/stop', {method:'POST'}); }
    catch(error){ byId('recordStatus').textContent = error.message; }
    finally { this.busy = false; this.render(); }
  }
}
root.TVRecorder = TVRecorder;
root.TVRecordings = {request, clock, gigabytes};
})(window);
